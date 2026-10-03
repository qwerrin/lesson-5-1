"""1回の実行の結果を `01_Inbox/YYYY-MM-DD-scout.md` へ**追記**し、**読み戻す**段（M6）。

追記だけで済む形にする
--------------------------------------------------------------------------

frontmatter は**ファイルを作るときだけ**書き、中身は固定にする。件数を入れると
2回目の実行でファイル全体を書き戻すことになり、*読んだ後に他所が書いたぶんを消す*
（教訓 `read-modify-write-drops-concurrent-edits`）。件数は実行ごとの節に書く。

1回の実行は**1回の書き込み**で末尾に足し、読み戻しは**自分の run-id** で探す
——「最後の節」で探すと、同じ日の別の実行と取り違える。

読み戻しは入力と突き合わせる
--------------------------------------------------------------------------

書いた文字列と比べるだけだと、描画が記事を1件落としても「書いたとおり」と答える。
**件数と URL は入力から**取った期待値で数える。

読み戻しの失敗は**例外にしない**。例外だと `notify` まで届かず、失敗が無音になる。
直しも消しもしない——*手で書かれたものを機械が直すと、何を失ったかが分からなくなる*。

外から来た文字列
--------------------------------------------------------------------------

タイトル・要約は Qiita と Gemini から来る。**節の構造を壊せない形**にしてから書く:
要約は各行を `> ` で引用にし、`[` `]` `<` を逃がす。`[[` が残ると vault のリンク検査に
*偽のリンク切れ*が出る。URL は http(s) だけ通す（`javascript:` を vault に置かない）。
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

from split import Headline, Split
from summarize import Digest, Failure
from verify_source import CONFIRMED, MISMATCH, UNVERIFIABLE, Audit, Check

#: run-id に使える字。**節の見出しと HTML コメントの中に入る**ので、どちらも壊さない字だけ。
RUN_ID = re.compile(r"[0-9A-Za-z][0-9A-Za-z_.-]*")
DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
TAGS = ("scout", "inbox")

VERDICTS = {
    CONFIRMED: "照合できた",
    MISMATCH: "本文に無い主張あり",
    UNVERIFIABLE: "確認できない",
}

#: URL の中で、Markdown のリンクを閉じてしまう字。
_URL_ESCAPES = {" ": "%20", "(": "%28", ")": "%29", "<": "%3C", ">": "%3E"}


@dataclass(frozen=True)
class Emitted:
    path: Path
    created: bool
    entries: int
    problems: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return not self.problems

    @property
    def summary(self) -> str:
        """**件数と、読み戻せたかを必ず出す。**"""
        how = "新規" if self.created else "追記"
        result = "問題なし" if self.ok else f"問題 {len(self.problems)} 件: " + "／".join(self.problems)
        return f"{self.path.name} に {self.entries} 件を書いた（{how}）／読み戻し: {result}"


def emit(
    inbox: Path,
    *,
    at: datetime,
    run_id: str,
    stages: Sequence[str],
    split: Split,
    digest: Digest,
    audit: Audit,
) -> Emitted:
    """**書く前に全部確かめ、1回で書き、読み戻す。**"""
    if not inbox.is_dir():
        # **作らない。** 作ると、パスの書き間違いが「新しいフォルダに書けた」になる。
        raise ValueError(f"Inbox が無い: {inbox}")
    block, urls = render(at=at, run_id=run_id, stages=stages, split=split, digest=digest, audit=audit)

    path = inbox / f"{at:%Y-%m-%d}-scout.md"
    created = not path.exists()
    if created:
        _write(path, "x", _head(at) + block)
    else:
        old = path.read_bytes().decode("utf-8")
        if _section_start(old, run_id) >= 0:
            raise ValueError(f"run-id `{run_id}` は {path.name} で使用済み。同じ節が2つあると読み戻しで区別できない")
        # 節は `\n` で始まるので、手で書かれた最終行に改行が無くても**見出しは行頭に来る**。
        _write(path, "a", block)

    text = path.read_bytes().decode("utf-8")
    problems = readback(text, block=block, run_id=run_id, urls=urls, entries=split.total)
    return Emitted(path=path, created=created, entries=split.total, problems=problems)


def render(
    *,
    at: datetime,
    run_id: str,
    stages: Sequence[str],
    split: Split,
    digest: Digest,
    audit: Audit,
) -> tuple[str, tuple[str, ...]]:
    """1回の実行の節と、そこに載る URL を返す。**段どうしが合わないなら作らない**（E）。"""
    if at.tzinfo is not None:
        # **日付の物差しは1本。** fetch はローカルの素の日時にそろえている。
        raise ValueError(f"時差つきの日時は受け取らない: {at.isoformat()}")
    if not RUN_ID.fullmatch(run_id) or "--" in run_id:
        raise ValueError(f"run-id に使えない形: {run_id!r}")
    _same("split の要約対象", split.summarize, "digest の結果", [*digest.done, *digest.failed])
    _same("digest の要約", digest.done, "audit の照合", [c.summary for c in audit.checks])

    entries: list[str] = []
    urls: list[str] = []
    for check in audit.checks:
        scored = check.summary.scored
        url = _url(scored.kept.article.url)
        urls.append(url)
        entries.append(
            _heading(scored.kept.article.title, url)
            + f"- {_inline(scored.kept.article.source)}・点 {scored.score}・照合: {_verdict(check)}\n"
            + _quote(check.summary.text)
        )
    for failure in digest.failed:
        url = _url(failure.scored.kept.article.url)
        urls.append(url)
        entries.append(_failure(failure, url))
    for headline in split.headline:
        url = _url(headline.kept.article.url)
        urls.append(url)
        entries.append(_headline(headline, url))

    lines = [f"- {_inline(stage)}" for stage in stages]
    lines.append(
        f"- 記事 {split.total} 件（要約 {len(digest.done)}"
        f"・要約できなかった {len(digest.failed)}・見出しだけ {len(split.headline)}）"
    )
    block = (
        f"\n## {at:%H:%M} {_label(run_id)}\n\n"
        + "\n".join(lines)
        + "\n"
        + "".join(f"\n{entry}" for entry in entries)
        + f"\n{_end(run_id)}\n"
    )
    return block, tuple(urls)


def readback(text: str, *, block: str, run_id: str, urls: Iterable[str], entries: int) -> tuple[str, ...]:
    """読み戻した全文を見て、**問題を全部**返す。空なら問題なし。"""
    problems: list[str] = []
    problems.extend(_check_frontmatter(text))

    times = text.count(block)
    if times == 0:
        problems.append(f"run {run_id} の節が書いたとおりに読み戻せない")
    elif times > 1:
        problems.append(f"run {run_id} の節が {times} 回ある")

    start = _section_start(text, run_id)
    end = text.find(_end(run_id), start) if start >= 0 else -1
    if start < 0 or end < 0:
        problems.append(f"run {run_id} の節の始まりか終わりが見つからない")
        return tuple(problems)
    section = text[start:end]
    found = sum(1 for line in section.splitlines() if line.startswith("### "))
    if found != entries:
        problems.append(f"記事が {found} 件（期待は {entries} 件）")
    problems.extend(f"URL が無い: {url}" for url in urls if f"]({url})" not in section)
    return tuple(problems)


# ---------------------------------------------------------------------------
# 書き込み
# ---------------------------------------------------------------------------


def _write(path: Path, mode: str, text: str) -> None:
    # **改行は LF に固定する。** Windows の既定（CRLF）だと、読み戻しが書いたものと一致しない。
    with path.open(mode, encoding="utf-8", newline="\n") as f:
        f.write(text)


def _head(at: datetime) -> str:
    tags = "".join(f"  - {t}\n" for t in TAGS)
    return f"---\ntags:\n{tags}date: {at:%Y-%m-%d}\n---\n\n# {at:%Y-%m-%d} scout\n"


def _label(run_id: str) -> str:
    # 前後をバッククォートで閉じるので、`r1` が `r10` の頭に当たらない。
    return f"実行 `{run_id}`"


def _section_start(text: str, run_id: str) -> int:
    """**節の見出しの行**で探す。部分一致だと、前の実行の記事タイトルに当たる。"""
    match = re.search(rf"^## [0-9]{{2}}:[0-9]{{2}} {re.escape(_label(run_id))}$", text, flags=re.MULTILINE)
    return match.start() if match else -1


def _end(run_id: str) -> str:
    return f"<!-- scout:end {run_id} -->"


def _check_frontmatter(text: str) -> list[str]:
    if not text.startswith("---\n"):
        return ["frontmatter が先頭に無い"]
    close = text.find("\n---\n", 3)
    if close < 0:
        return ["frontmatter が閉じていない"]
    fields: dict[str, list[str] | str] = {}
    key = ""
    for line in text[4:close].splitlines():
        if line.startswith("  - ") and isinstance(fields.get(key), list):
            fields[key].append(line[4:].strip())  # type: ignore[union-attr]
        elif ":" in line:
            key, _, value = line.partition(":")
            fields[key] = value.strip() or []
    problems: list[str] = []
    tags = fields.get("tags")
    if not isinstance(tags, list) or "scout" not in tags:
        problems.append("frontmatter の tags に scout が無い")
    date = fields.get("date")
    if not isinstance(date, str) or not DATE.fullmatch(date):
        problems.append(f"frontmatter の date が YYYY-MM-DD でない: {date!r}")
    return problems


# ---------------------------------------------------------------------------
# 1件分
# ---------------------------------------------------------------------------


def _heading(title: str, url: str) -> str:
    return f"### [{_inline(title) or '（無題）'}]({url})\n"


def _verdict(check: Check) -> str:
    label = VERDICTS.get(check.verdict, _inline(check.verdict))
    if not check.missing:
        return label
    # **消さずに印を付ける**（6-2）。
    marked = "・".join(f"`{_line(c).replace('`', chr(39))}`" for c in check.missing)
    return f"{label}（{marked}）"


def _failure(failure: Failure, url: str) -> str:
    article = failure.scored.kept.article
    return (
        _heading(article.title, url)
        + f"- {_inline(article.source)}・点 {failure.scored.score}・要約できなかった（{_inline(failure.reason)}）\n"
    )


def _headline(headline: Headline, url: str) -> str:
    article = headline.kept.article
    # **0 と「点なし」を混ぜない。**
    score = "なし" if headline.score is None else str(headline.score)
    return (
        _heading(article.title, url)
        + f"- {_inline(article.source)}・点 {score}・見出しだけ（{_inline(headline.reason)}）\n"
    )


def _quote(text: str) -> str:
    """要約は**全行を引用**にする。中に `## ` や `---` があっても節を作らない。"""
    return "".join(f"> {_escape(line)}\n" for line in text.splitlines())


# ---------------------------------------------------------------------------
# 文字列
# ---------------------------------------------------------------------------


def _same(left_name: str, left: Iterable[object], right_name: str, right: Iterable[object]) -> None:
    """**件数だけでなく、どの記事か**まで合わせる。数が同じで中身が違うのも止める。"""
    a = Counter(_key(x) for x in left)
    b = Counter(_key(x) for x in right)
    if a != b:
        raise ValueError(f"{left_name}と{right_name}が合わない: 片方だけにある {sorted((a - b) + (b - a))}")


def _key(item: object) -> str:
    scored = getattr(item, "scored", item)
    return scored.kept.key  # type: ignore[attr-defined]


def _url(url: str) -> str:
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise ValueError(f"http(s) でない URL は書かない: {url!r}")
    if any(ch.isspace() and ch != " " for ch in url):
        raise ValueError(f"URL に改行や制御的な空白がある: {url!r}")
    return "".join(_URL_ESCAPES.get(ch, ch) for ch in url)


def _line(text: str) -> str:
    return " ".join(text.split())


def _escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("[", "\\[").replace("]", "\\]").replace("<", "&lt;")


def _inline(text: str) -> str:
    return _escape(_line(text))
