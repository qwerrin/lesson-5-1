"""週次まとめ。**毎日の1通では見えないもの**だけを、7日に1回まとめる（DESIGN 10-1）。

案3「週1の生存通知」を作ろうとしたら、目的はもう果たされていた——`notify` が毎回1通送るので、
沈黙の意味は毎日の1通で確定している。毎日の1通に言えないのは次の2つ:

- **走らなかった日。** 届かなかった日は無音のまま
- **日をまたいだ傾向。** 取得元が続けて空・照合の誤報の数・物差しが平らに戻っていないか（U22）

記録は追記だけ
--------------------------------------------------------------------------

`state/runs.jsonl` に1回1行。**前の行を書き換えない**ので、手動とスケジューラが重なっても
他の回の行を消さない（教訓 `read-modify-write-drops-concurrent-edits`）。
壊れた行は捨てずに数える——黙って捨てると、走った日が少なく見える。

区切りは日付で
--------------------------------------------------------------------------

起点は前に出したまとめ（無ければ最初の記録）。**時刻ではなく日付の差**で7日を数える。
22:00:03 の回の7日後が 22:00:01 だと、時刻の差では2秒足りずに1日遅れる。

見ていないもの
--------------------------------------------------------------------------

- scout が何週間も走らないと、まとめも出ない（送るのは scout 自身）。vault_doctor が受け持つ
- 途中で止まった回は判定だけ。取得や照合の数は「中身の記録が無い回」として別に数える
"""

from __future__ import annotations

import json
import os
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import fetch
import notify
import rank
import verify_source
from summarize import Digest

#: 何日ごとにまとめるか。**日付の差**で数える。
PERIOD_DAYS = 7
#: 走らなかった日を並べる上限。何週間も止まっていたら、残りは数だけにする。
MISSED_SHOWN = 7

_LEVEL_ORDER = (notify.NORMAL, notify.ATTENTION, notify.ABNORMAL)
_STATUS_LABELS = {fetch.OK: "ok", fetch.EMPTY: "空", fetch.FAILED: "失敗"}
_VERDICT_LABELS = {
    verify_source.CONFIRMED: "裏付け",
    verify_source.MISMATCH: "本文に無い",
    verify_source.UNVERIFIABLE: "確かめられない",
}


@dataclass(frozen=True)
class Facts:
    """1回分の中身。**`None` は「分からない」で、0 とは混ぜない。**"""

    #: (取得元, 状態) を設定の順に。
    sources: tuple[tuple[str, str], ...] | None
    summarized: int | None
    #: (判定, 件数)。**知らない判定も落とさない。**
    verdicts: tuple[tuple[str, int], ...] | None
    cut_in_tie: bool | None


#: 途中で止まった回。判定（異常）だけが分かっている。
UNKNOWN = Facts(sources=None, summarized=None, verdicts=None, cut_in_tie=None)


@dataclass(frozen=True)
class Run:
    at: datetime
    run_id: str
    level: str
    facts: Facts
    #: **この回の1通でまとめを出せた**（送れて、切られずに残った）。
    weekly: bool


@dataclass(frozen=True)
class Loaded:
    runs: tuple[Run, ...]
    #: 読めなかった行の数。**捨てずに数える。**
    bad: int
    #: ファイルごと読めなかったときの理由。
    error: str | None


# ---------------------------------------------------------------------------
# 1回分を組み立てる
# ---------------------------------------------------------------------------


def facts(*, harvest: fetch.Harvest, ranking: rank.Ranking, digest: Digest, audit: verify_source.Audit) -> Facts:
    verdicts = Counter(c.verdict for c in audit.checks)
    return Facts(
        sources=tuple((r.source, r.status) for r in harvest.results),
        summarized=len(digest.done),
        verdicts=tuple(verdicts.items()),
        cut_in_tie=cut_in_tie(ranking),
    )


def cut_in_tie(ranking: rank.Ranking) -> bool:
    """**選んだ最後の1件と、上限で外れた最初の1件が同じ点**か（U22）。

    同点なら、その回は点ではなく、いいねや日付で選ばれている。
    上限に届いていない回は切れ目が無いので `False`。ミュートは点が無いので混ぜない。
    """
    # 上限で外れた記事の点は `rank` が `Scored.score`（整数）から写す。`None` はミュートだけで、理由で弾く。
    over = [d.score for d in ranking.dropped if d.reason == rank.OVER_CAP]
    if not ranking.picked or not over:
        return False
    return min(s.score for s in ranking.picked) == max(over)  # type: ignore[type-var]


# ---------------------------------------------------------------------------
# 読み書き
# ---------------------------------------------------------------------------


def append(path: Path, run: Run) -> None:
    """**1行を1回で書く。** 前の行には触らない。

    前の書き込みが途中で切れて改行が無ければ、改行から始める——繋がると、この回が丸ごと壊れた行になる。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(_to_json(run), ensure_ascii=False) + "\n"
    # バイトで書く。改行を LF に固定し（Windows の既定は CRLF）、末尾の1バイトとそろえて見る。
    with path.open("a+b") as f:
        f.seek(0, os.SEEK_END)
        if f.tell() > 0:
            f.seek(-1, os.SEEK_END)
            if f.read(1) != b"\n":
                line = "\n" + line
        f.write(line.encode("utf-8"))


def load(path: Path) -> Loaded:
    if not path.exists():
        return Loaded(runs=(), bad=0, error=None)
    try:
        data = path.read_bytes()
    except OSError as e:
        # 理由は LINE に出る。**文言にはローカルのパス（ユーザー名を含む）が入る**ので、型名とファイル名だけ。
        return Loaded(runs=(), bad=0, error=f"{type(e).__name__}（{path.name}）")
    runs: list[Run] = []
    bad = 0
    # **改行（LF）だけで割り、1行ずつ読む。** `splitlines` は JSON の中の ` ` でも割る。
    # 1バイト化けた行のために、残りの行まで捨てない。
    for raw in data.split(b"\n"):
        if not raw.strip():
            continue
        try:
            run = _from_json(raw.decode("utf-8"))
        except UnicodeDecodeError:
            run = None
        if run is None:
            bad += 1
        else:
            runs.append(run)
    return Loaded(runs=tuple(runs), bad=bad, error=None)


def _to_json(run: Run) -> dict[str, Any]:
    f = run.facts
    return {
        "at": run.at.isoformat(),
        "run_id": run.run_id,
        "level": run.level,
        "facts": {
            "sources": None if f.sources is None else dict(f.sources),
            "summarized": f.summarized,
            "verdicts": None if f.verdicts is None else dict(f.verdicts),
            "cut_in_tie": f.cut_in_tie,
        },
        "weekly": run.weekly,
    }


def _from_json(line: str) -> Run | None:
    """形が1か所でも違えば `None`（＝読めなかった行）。

    表でない値（`[]`・数・`null`）に鍵で触ると `TypeError` になるので、それも読めなかった行に落ちる。
    """
    try:
        data = json.loads(line)
        at = datetime.fromisoformat(data["at"])
        run_id, level, sent = data["run_id"], data["level"], data["weekly"]
        if not isinstance(run_id, str) or not isinstance(level, str) or not isinstance(sent, bool):
            return None
        if at.tzinfo is not None:
            # **書く側は時差の無い時刻だけを書く。** 混ざると比較で `TypeError` になり、通知の前に落ちる。
            return None
        facts_ = _facts_from_json(data["facts"])
    except (ValueError, TypeError, KeyError):
        return None
    if facts_ is None:
        return None
    return Run(at=at, run_id=run_id, level=level, facts=facts_, weekly=sent)


def _facts_from_json(raw: Any) -> Facts | None:
    """`_from_json` の `try` の中で呼ぶ。表でなければ `TypeError` で読めなかった行になる。"""
    sources = _pairs(raw["sources"], str)
    verdicts = _pairs(raw["verdicts"], int)
    summarized = raw["summarized"]
    cut = raw["cut_in_tie"]
    if sources is False or verdicts is False:
        return None
    if summarized is not None and not _count(summarized):
        return None
    if cut is not None and not isinstance(cut, bool):
        return None
    return Facts(sources=sources, summarized=summarized, verdicts=verdicts, cut_in_tie=cut)


def _pairs(value: Any, kind: type) -> tuple[tuple[str, Any], ...] | None | bool:
    """表を (鍵, 値) の並びに。`None` はそのまま、形が違えば `False`。"""
    if value is None:
        return None
    if not isinstance(value, dict):
        return False
    for v in value.values():
        if kind is int and not _count(v):
            return False
        if kind is str and not isinstance(v, str):
            return False
    return tuple(value.items())


def _count(value: Any) -> bool:
    # bool は int の仲間なので、素朴な型検査を素通りする。
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


# ---------------------------------------------------------------------------
# 区切りと本文
# ---------------------------------------------------------------------------


def due(loaded: Loaded, run: Run) -> bool:
    """この回の1通にまとめを足すか。**読めない記録は毎回出す**——黙ると何週間も気づかない。"""
    if loaded.error is not None:
        return True
    start = _start(loaded.runs)
    if start is None:
        return False
    return (run.at.date() - start.at.date()).days >= PERIOD_DAYS


def compose(loaded: Loaded, run: Run) -> str:
    if loaded.error is not None:
        return f"【週のまとめ】記録を読めなかった（{_line(loaded.error)}）"
    start = _start(loaded.runs)
    if start is None:
        first_day, older = run.at.date(), []
    elif start.weekly:
        # 前に出したまとめの**翌日から**。その日の回は前のまとめに入っている。
        first_day = start.at.date() + timedelta(days=1)
        older = [r for r in loaded.runs if r.at > start.at]
    else:
        first_day, older = start.at.date(), list(loaded.runs)
    window = [*older, run]
    days = [first_day + timedelta(days=i) for i in range((run.at.date() - first_day).days + 1)]

    lines = [
        f"【週のまとめ】{first_day:%m-%d}〜{run.at.date():%m-%d}（{len(days)}日）",
        _ran_line(window, days),
        _level_line(window),
    ]
    known = [r.facts for r in window if r.facts != UNKNOWN]
    stopped = len(window) - len(known)
    if stopped:
        # **「途中で止まった」とだけ書かない。** 本体は通ったが数えられなかった回（`cli` の facts_failed）も入る。
        lines.append(f"・中身の記録が無い回 {stopped}（途中で止まった・数えられなかった）")
    lines.append(_source_line(known))
    lines.append(_verdict_line(known))
    flat = [f.cut_in_tie for f in known if f.cut_in_tie is not None]
    lines.append(f"・上限の切れ目が同点の中にあった回: {sum(flat)}/{len(flat)}")
    lines.append(f"・読めなかった記録 {loaded.bad} 行")
    return "\n".join(lines)


def _start(runs: Sequence[Run]) -> Run | None:
    """起点。前に出したまとめ、無ければ最初の記録。"""
    sent = [r for r in runs if r.weekly]
    if sent:
        return max(sent, key=lambda r: r.at)
    return min(runs, key=lambda r: r.at) if runs else None


def _ran_line(window: Sequence[Run], days: Sequence[date]) -> str:
    ran = {r.at.date() for r in window}
    missed = [d for d in days if d not in ran]
    text = f"・走った日 {len(days) - len(missed)}/{len(days)}"
    if missed:
        shown = "・".join(f"{d:%m-%d}" for d in missed[:MISSED_SHOWN])
        rest = len(missed) - MISSED_SHOWN
        text += f"（走らなかった日: {shown}{f' ほか {rest} 日' if rest > 0 else ''}）"
    return text


def _level_line(window: Sequence[Run]) -> str:
    counts = Counter(r.level for r in window)
    parts = [f"{notify.LABELS[level]} {counts.pop(level, 0)}" for level in _LEVEL_ORDER]
    unknown = sum(counts.values())
    if unknown:
        # **知らない判定を落とさない。** `notify` が判定を足した日に黙って消える。
        parts.append(f"分からない {unknown}")
    return "・判定: " + "・".join(parts)


def _source_line(known: Sequence[Facts]) -> str:
    per: dict[str, Counter[str]] = {}
    for f in known:
        for name, status in f.sources or ():
            per.setdefault(name, Counter())[status] += 1
    parts = []
    for name, counts in per.items():
        text = "・".join(f"{label} {counts.pop(status, 0)}" for status, label in _STATUS_LABELS.items())
        # 知らない状態も名前のまま出す（`fetch` が状態を足した日）。
        text += "".join(f"・{_line(status)} {n}" for status, n in counts.items())
        parts.append(f"{_line(name)} {text}")
    return "・取得: " + ("／".join(parts) or "記録なし")


def _verdict_line(known: Sequence[Facts]) -> str:
    summarized = sum(f.summarized or 0 for f in known)
    counts: Counter[str] = Counter()
    for f in known:
        counts.update(dict(f.verdicts or ()))
    parts = [f"{label} {counts.pop(verdict, 0)}" for verdict, label in _VERDICT_LABELS.items()]
    unknown = sum(counts.values())
    if unknown:
        parts.append(f"分からない {unknown}")
    return f"・要約 {summarized}・照合: " + "・".join(parts)


def _line(text: str) -> str:
    return " ".join(str(text).split())
