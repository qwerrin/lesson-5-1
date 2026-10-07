"""scout/weekly のテスト。**実装より先に書いた。**

`weekly` は1回ごとの結果を `state/runs.jsonl` に追記し、7日に1回、
**毎日の1通では見えないもの**をまとめる（DESIGN 10-1）。

決めたこと（2026-10-07）
--------------------------------------------------------------------------

============ ====================================================================
A            区切りは**日付の差**で決める。時刻で比べると、22:00:03 の回の7日後が
             22:00:01 のとき2秒足りず、まとめが1日遅れる
B            区切りの起点は、前に出したまとめ。無ければ最初の記録
C            「走った日」は日付で数える。同じ日の2回目は日を増やさない
D            **知らない判定・知らない取得の状態を落とさない。**「分からない」として数える
E            壊れた行は捨てずに数える。ファイルごと読めなければ、そう書く
F            上限の切れ目が同点の中にある＝選んだ最後と外れた最初が同じ点（U22）
G            追記は1行ずつ。前の行を書き換えない
============ ====================================================================
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scout"))

import dedupe  # noqa: E402
import fetch  # noqa: E402
import notify  # noqa: E402
import rank  # noqa: E402
import summarize  # noqa: E402
import verify_source  # noqa: E402
import weekly  # noqa: E402

#: **実行日と違う日**にしておく（教訓 `today-as-expected-value-hides-date-bugs`）。
START = datetime(2026, 9, 1, 22, 0, 3)


# ---------------------------------------------------------------------------
# 組み立て
# ---------------------------------------------------------------------------


def _kept(url: str) -> dedupe.Kept:
    article = fetch.Article(
        source="qiita", url=url, title="記事", body="本文" * 300,
        published_at=START, updated_at=None, author="a", tags=("Python",), metrics={},
    )
    return dedupe.Kept(article=article, key=url)


def _scored(i: int, score: int) -> rank.Scored:
    return rank.Scored(kept=_kept(f"https://qiita.com/a/items/{i}"), score=score, hits=())


def _over(i: int, score: int) -> rank.Rejected:
    return rank.Rejected(kept=_kept(f"https://qiita.com/o/items/{i}"), reason=rank.OVER_CAP, detail="", score=score, hits=())


def _ranking(picked: tuple[int, ...], over: tuple[int, ...] = (), muted: int = 0) -> rank.Ranking:
    # ミュートに**わざと点を持たせる**。`rank` は今 None を入れるが、点の有無ではなく理由で弾いていることを確かめる。
    dropped = [
        rank.Rejected(kept=_kept(f"https://qiita.com/m/items/{i}"), reason=rank.MUTED, detail="tag:x", score=1, hits=())
        for i in range(muted)
    ]
    dropped += [_over(i, s) for i, s in enumerate(over)]
    return rank.Ranking(
        picked=tuple(_scored(i, s) for i, s in enumerate(picked)), unranked=(), dropped=tuple(dropped)
    )


def _source(name: str, status: str) -> fetch.SourceResult:
    return fetch.SourceResult(source=name, status=status, articles=(), detail="", total=None, more=False)


def _audit(*verdicts: str) -> verify_source.Audit:
    return verify_source.Audit(
        checks=tuple(
            verify_source.Check(
                summary=summarize.Summary(scored=_scored(i, 1), text="要約", quotes=(), prompt_tokens=1, output_tokens=1),
                verdict=v, found=(), missing=(), quotes_missing=(),
            )
            for i, v in enumerate(verdicts)
        )
    )


def _facts(**overrides: object) -> weekly.Facts:
    values: dict[str, object] = {
        "sources": (("qiita", fetch.OK), ("zenn", fetch.OK)),
        "summarized": 10,
        "verdicts": ((verify_source.CONFIRMED, 10),),
        "cut_in_tie": False,
    }
    values.update(overrides)
    return weekly.Facts(**values)  # type: ignore[arg-type]


def _run(at: datetime, *, level: str = notify.NORMAL, facts: weekly.Facts | None = None, sent: bool = False) -> weekly.Run:
    return weekly.Run(
        at=at, run_id=f"{at:%Y%m%d-%H%M%S}", level=level,
        facts=_facts() if facts is None else facts, weekly=sent,
    )


def _daily(days: int, **kwargs: object) -> list[weekly.Run]:
    """START から1日1回。"""
    return [_run(START + timedelta(days=d), **kwargs) for d in range(days)]  # type: ignore[arg-type]


def _loaded(runs: list[weekly.Run], *, bad: int = 0) -> weekly.Loaded:
    return weekly.Loaded(runs=tuple(runs), bad=bad, error=None)


# ---------------------------------------------------------------------------
# 1回分の記録を組み立てる
# ---------------------------------------------------------------------------


def test_facts_are_read_from_each_stage() -> None:
    harvest = fetch.Harvest(results=(_source("qiita", fetch.OK), _source("zenn", fetch.EMPTY)))
    audit = _audit(verify_source.CONFIRMED, verify_source.CONFIRMED, verify_source.MISMATCH)
    digest = summarize.Digest(done=tuple(c.summary for c in audit.checks), failed=())

    # **同点の形で組む。** False の形だと、切れ目を固定の False にしても見分けがつかない。
    facts = weekly.facts(harvest=harvest, ranking=_ranking((2, 1, 1), over=(1, 0)), digest=digest, audit=audit)

    assert facts.sources == (("qiita", fetch.OK), ("zenn", fetch.EMPTY))
    assert facts.summarized == 3
    assert dict(facts.verdicts or ()) == {verify_source.CONFIRMED: 2, verify_source.MISMATCH: 1}
    assert facts.cut_in_tie is True


@pytest.mark.parametrize(
    ("picked", "over", "expected"),
    [
        ((2, 1, 1), (0, 0), False),  # 境目で点が下がる＝物差しが効いている（2026-10-05 の形）
        ((2, 1, 1), (1, 0), True),  # 選んだ最後と外れた最初が同じ点
        ((1, 1, 1), (1, 1), True),  # 全員同点＝平ら（U22 の前の形）
        ((0, 0), (0,), True),  # 全員0点も平ら
        ((2, 1), (), False),  # 上限に届いていない＝切れ目が無い
        ((), (), False),
    ],
)
def test_cut_in_tie(picked: tuple[int, ...], over: tuple[int, ...], expected: bool) -> None:
    assert weekly.cut_in_tie(_ranking(picked, over)) is expected


def test_muted_articles_are_not_the_cut() -> None:
    """**ミュートは上限の切れ目ではない。** 点が無いので、同点の判定に混ぜない。"""
    assert weekly.cut_in_tie(_ranking((1, 1), over=(), muted=3)) is False


# ---------------------------------------------------------------------------
# 読み書き
# ---------------------------------------------------------------------------


def test_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "state" / "runs.jsonl"
    runs = [
        _run(START),
        _run(START + timedelta(days=1), level=notify.ATTENTION, facts=_facts(cut_in_tie=True), sent=True),
        _run(START + timedelta(days=2), level=notify.ABNORMAL, facts=weekly.UNKNOWN),
    ]
    for run in runs:
        weekly.append(path, run)

    assert weekly.load(path) == weekly.Loaded(runs=tuple(runs), bad=0, error=None)


def test_append_adds_one_line_and_keeps_the_earlier_ones(tmp_path: Path) -> None:
    path = tmp_path / "runs.jsonl"
    weekly.append(path, _run(START))
    first = path.read_bytes()
    weekly.append(path, _run(START + timedelta(days=1)))
    after = path.read_bytes()

    assert after.startswith(first)
    assert after.count(b"\n") == 2
    assert b"\r" not in after


def test_missing_file_is_empty_not_an_error(tmp_path: Path) -> None:
    assert weekly.load(tmp_path / "runs.jsonl") == weekly.Loaded(runs=(), bad=0, error=None)


@pytest.mark.parametrize(
    "line",
    [
        "{",
        "[]",
        '{"at": "昨日"}',
        # 形は合っているが値が違う。**1行に壊れた所は1つだけ**——2つあると、片方の検査を外しても通る
        '{"at": "2026-09-01T22:00:03", "run_id": "x", "level": 1, "weekly": false, '
        '"facts": {"sources": null, "summarized": null, "verdicts": null, "cut_in_tie": null}}',
        '{"at": "2026-09-01T22:00:03", "run_id": "x", "level": "normal", "weekly": "yes", '
        '"facts": {"sources": null, "summarized": null, "verdicts": null, "cut_in_tie": null}}',
        '{"at": "2026-09-01T22:00:03", "run_id": 5, "level": "normal", "weekly": false, '
        '"facts": {"sources": null, "summarized": null, "verdicts": null, "cut_in_tie": null}}',
        # facts は書く側が必ず表で書く。null も読めなかった行
        '{"at": "2026-09-01T22:00:03", "run_id": "x", "level": "normal", "facts": null, "weekly": false}',
        '{"at": "2026-09-01T22:00:03", "run_id": "x", "level": "normal", "facts": [], "weekly": false}',
        '{"at": "2026-09-01T22:00:03", "run_id": "x", "level": "normal", "weekly": false, '
        '"facts": {"sources": ["qiita"], "summarized": null, "verdicts": null, "cut_in_tie": null}}',
        '{"at": "2026-09-01T22:00:03", "run_id": "x", "level": "normal", "weekly": false, '
        '"facts": {"sources": null, "summarized": true, "verdicts": null, "cut_in_tie": null}}',
        '{"at": "2026-09-01T22:00:03", "run_id": "x", "level": "normal", "weekly": false, '
        '"facts": {"sources": {"qiita": 1}, "summarized": null, "verdicts": null, "cut_in_tie": null}}',
        '{"at": "2026-09-01T22:00:03", "run_id": "x", "level": "normal", "weekly": false, '
        '"facts": {"sources": null, "summarized": -1, "verdicts": null, "cut_in_tie": null}}',
        '{"at": "2026-09-01T22:00:03", "run_id": "x", "level": "normal", "weekly": false, '
        '"facts": {"sources": null, "summarized": null, "verdicts": {"confirmed": "3"}, "cut_in_tie": null}}',
        '{"at": "2026-09-01T22:00:03", "run_id": "x", "level": "normal", "weekly": false, '
        '"facts": {"sources": null, "summarized": null, "verdicts": null, "cut_in_tie": 0}}',
    ],
)
def test_broken_line_is_counted_not_dropped(tmp_path: Path, line: str) -> None:
    """E：**捨てずに数える。** 黙って捨てると、走った日が少なく見える。"""
    path = tmp_path / "runs.jsonl"
    weekly.append(path, _run(START))
    with path.open("a", encoding="utf-8", newline="\n") as f:
        f.write(line + "\n")
    weekly.append(path, _run(START + timedelta(days=1)))

    loaded = weekly.load(path)

    assert loaded.bad == 1
    assert len(loaded.runs) == 2
    assert loaded.error is None


def test_blank_lines_are_not_broken_records(tmp_path: Path) -> None:
    path = tmp_path / "runs.jsonl"
    weekly.append(path, _run(START))
    with path.open("a", encoding="utf-8") as f:
        f.write("\n\n")
    assert weekly.load(path).bad == 0


def test_a_garbled_line_is_counted_and_the_rest_is_still_read(tmp_path: Path) -> None:
    """**1バイト化けただけで履歴全体を捨てない**（2026-10-07 レビュー MEDIUM）。
    ファイルごと読めなくすると、毎日「読めなかった」が出るだけで、まとめは二度と出ない。"""
    path = tmp_path / "runs.jsonl"
    weekly.append(path, _run(START))
    with path.open("ab") as f:
        f.write(b"\xff\xfe broken\n")
    weekly.append(path, _run(START + timedelta(days=1)))

    loaded = weekly.load(path)

    assert loaded.error is None
    assert loaded.bad == 1
    assert len(loaded.runs) == 2


def test_only_newline_splits_records(tmp_path: Path) -> None:
    """`splitlines` は `\\u2028` でも割る。`ensure_ascii=False` で書くので、取得元の名前に入ると1行が2行に化ける。"""
    path = tmp_path / "runs.jsonl"
    weekly.append(path, _run(START, facts=_facts(sources=(("qi ita", fetch.OK),))))
    loaded = weekly.load(path)
    assert loaded.bad == 0
    assert loaded.runs[0].facts.sources == (("qi ita", fetch.OK),)


def test_append_after_a_torn_last_line_starts_a_new_line(tmp_path: Path) -> None:
    """**前の書き込みが途中で切れて改行が無い**と、次の回が同じ行に繋がって丸ごと消える。"""
    path = tmp_path / "runs.jsonl"
    path.write_bytes(b'{"at": "2026-09-01T22:')  # 途中で切れた
    weekly.append(path, _run(START + timedelta(days=1)))

    loaded = weekly.load(path)

    assert loaded.bad == 1
    assert [r.at for r in loaded.runs] == [START + timedelta(days=1)]


def test_append_to_an_empty_file(tmp_path: Path) -> None:
    path = tmp_path / "runs.jsonl"
    path.write_bytes(b"")
    weekly.append(path, _run(START))
    assert path.read_bytes().startswith(b"{")


def test_time_with_a_zone_is_a_broken_line(tmp_path: Path) -> None:
    """**書く側は時差の無い時刻だけを書く。** 時差付きが混ざると比較で `TypeError` になり、
    通知の前に落ちる（2026-10-07 レビュー HIGH）。読む時点で「読めなかった行」に回す。"""
    path = tmp_path / "runs.jsonl"
    weekly.append(path, _run(START))
    line = path.read_text(encoding="utf-8").replace("2026-09-01T22:00:03", "2026-09-02T22:00:03+09:00")
    with path.open("a", encoding="utf-8", newline="\n") as f:
        f.write(line)

    loaded = weekly.load(path)

    assert loaded.bad == 1
    assert weekly.due(loaded, _run(START + timedelta(days=7))) is True  # 落ちない


def test_unreadable_file_is_an_error_without_the_full_path(tmp_path: Path) -> None:
    """読めなかった理由は LINE に出る。**ローカルのパス（ユーザー名を含む）は載せない。**"""
    path = tmp_path / "runs.jsonl"
    path.mkdir()
    loaded = weekly.load(path)
    assert loaded.error
    assert "runs.jsonl" in loaded.error
    # **フォルダ名で探す。** Windows の文言は repr で `\\` が2つになり、`str(tmp_path)` では一致しない（空振りした）。
    assert tmp_path.name not in loaded.error
    assert loaded.runs == ()


def test_written_line_is_json_with_dates_as_text(tmp_path: Path) -> None:
    """人が開いて読めるように。日付は ISO の文字列、日本語はそのまま。"""
    path = tmp_path / "runs.jsonl"
    weekly.append(path, _run(START))
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["at"] == "2026-09-01T22:00:03"
    assert data["level"] == notify.NORMAL
    assert data["weekly"] is False


# ---------------------------------------------------------------------------
# 区切り
# ---------------------------------------------------------------------------


def test_not_due_without_history() -> None:
    assert weekly.due(_loaded([]), _run(START)) is False


def test_first_summary_comes_seven_days_after_the_first_record() -> None:
    runs = _daily(7)  # 09-01 〜 09-07
    assert weekly.due(_loaded(runs[:-1]), runs[-1]) is False  # 09-07 は6日後
    assert weekly.due(_loaded(runs), _run(START + timedelta(days=7))) is True  # 09-08


def test_due_by_date_not_by_seconds() -> None:
    """A：22:00:03 の回の7日後が 22:00:01 でも、その日のうちに出す。"""
    later = datetime(2026, 9, 8, 22, 0, 1)
    assert later - START < timedelta(days=7)
    assert weekly.due(_loaded([_run(START)]), _run(later)) is True


def test_next_summary_counts_from_the_last_one_sent() -> None:
    runs = _daily(8)
    runs[7] = _run(runs[7].at, sent=True)  # 09-08 に出した
    after = [_run(START + timedelta(days=d)) for d in range(8, 13)]  # 09-09 〜 09-13

    assert weekly.due(_loaded(runs + after), _run(START + timedelta(days=13))) is False  # 09-14 は6日後
    assert weekly.due(_loaded(runs + after), _run(START + timedelta(days=14))) is True  # 09-15


def test_the_latest_summary_sent_is_the_start() -> None:
    runs = _daily(9)
    runs[0] = _run(runs[0].at, sent=True)  # 09-01
    runs[7] = _run(runs[7].at, sent=True)  # 09-08
    assert weekly.due(_loaded(runs), _run(START + timedelta(days=9))) is False  # 09-10 は 09-08 から2日後


def test_a_summary_that_was_not_delivered_does_not_restart_the_week() -> None:
    """送れなかった・切れた回は `weekly=False` で残る。**翌日もう一度出す。**"""
    runs = _daily(8)  # 09-08 は出す日だったが出せなかった
    assert weekly.due(_loaded(runs), _run(START + timedelta(days=8))) is True


def test_unreadable_history_is_always_shown() -> None:
    """E：読めない記録は毎回出す。黙っていると、記録が止まったまま何週間も気づかない。"""
    loaded = weekly.Loaded(runs=(), bad=0, error="読めない")
    assert weekly.due(loaded, _run(START)) is True
    text = weekly.compose(loaded, _run(START))
    assert "読めなかった" in text
    assert "読めない" in text


# ---------------------------------------------------------------------------
# 本文
# ---------------------------------------------------------------------------


def _lines(loaded: weekly.Loaded, run: weekly.Run) -> list[str]:
    return weekly.compose(loaded, run).splitlines()


def test_compose_a_quiet_week() -> None:
    runs = _daily(7)
    lines = _lines(_loaded(runs), _run(START + timedelta(days=7)))

    assert lines[0] == "【週のまとめ】09-01〜09-08（8日）"
    assert "・走った日 8/8" in lines
    assert "・判定: 正常 8・注意 0・異常 0" in lines
    assert "・取得: qiita ok 8・空 0・失敗 0／zenn ok 8・空 0・失敗 0" in lines
    assert "・要約 80・照合: 裏付け 80・本文に無い 0・確かめられない 0" in lines
    assert "・上限の切れ目が同点の中にあった回: 0/8" in lines
    assert "・読めなかった記録 0 行" in lines


def test_days_without_a_run_are_named() -> None:
    """**毎日の1通では見えないもの。** 届かなかった日は無音のままだった。"""
    runs = [r for r in _daily(7) if r.at.day not in (3, 5)]
    lines = _lines(_loaded(runs), _run(START + timedelta(days=7)))
    assert "・走った日 6/8（走らなかった日: 09-03・09-05）" in lines


def test_second_run_on_the_same_day_does_not_add_a_day() -> None:
    runs = _daily(7)
    runs.append(_run(START + timedelta(days=6, hours=1)))  # 09-07 23:00 に手で
    lines = _lines(_loaded(runs), _run(START + timedelta(days=7)))
    assert "・走った日 8/8" in lines
    assert "・判定: 正常 9・注意 0・異常 0" in lines


def test_window_after_a_sent_summary_excludes_the_older_runs() -> None:
    runs = _daily(8, level=notify.ABNORMAL)
    runs[7] = _run(runs[7].at, level=notify.ABNORMAL, sent=True)  # 09-08 に出した
    after = _daily(15)[8:]  # 09-09 〜 09-15 は正常
    lines = _lines(_loaded(runs + after), _run(START + timedelta(days=15)))

    assert lines[0] == "【週のまとめ】09-09〜09-16（8日）"
    assert "・判定: 正常 8・注意 0・異常 0" in lines


def test_levels_and_unknown_level_is_kept() -> None:
    """D：知らない判定は「分からない」として残す。"""
    runs = [
        _run(START, level=notify.ATTENTION),
        _run(START + timedelta(days=1), level=notify.ABNORMAL),
        _run(START + timedelta(days=2), level="weird"),
    ]
    lines = _lines(_loaded(runs), _run(START + timedelta(days=7)))
    assert "・判定: 正常 1・注意 1・異常 1・分からない 1" in lines


def test_source_statuses_per_source_and_unknown_status_is_kept() -> None:
    zenn_empty = _facts(sources=(("qiita", fetch.OK), ("zenn", fetch.EMPTY)))
    qiita_failed = _facts(sources=(("qiita", fetch.FAILED), ("zenn", "teapot")))
    runs = [_run(START, facts=zenn_empty), _run(START + timedelta(days=1), facts=qiita_failed)]
    lines = _lines(_loaded(runs), _run(START + timedelta(days=7)))
    assert "・取得: qiita ok 2・空 0・失敗 1／zenn ok 1・空 1・失敗 0・teapot 1" in lines


def test_runs_that_stopped_midway_are_counted_separately() -> None:
    runs = [_run(START, level=notify.ABNORMAL, facts=weekly.UNKNOWN)]
    lines = _lines(_loaded(runs), _run(START + timedelta(days=7)))
    assert "・中身の記録が無い回 1（途中で止まった・数えられなかった）" in lines
    # 中身の無い回は、取得・要約・切れ目のどれにも数えない
    assert "・取得: qiita ok 1・空 0・失敗 0／zenn ok 1・空 0・失敗 0" in lines
    assert "・上限の切れ目が同点の中にあった回: 0/1" in lines


def test_a_week_of_stopped_runs_says_there_is_no_record() -> None:
    runs = [_run(START, level=notify.ABNORMAL, facts=weekly.UNKNOWN)]
    lines = _lines(_loaded(runs), _run(START + timedelta(days=7), level=notify.ABNORMAL, facts=weekly.UNKNOWN))
    assert "・取得: 記録なし" in lines
    assert "・要約 0・照合: 裏付け 0・本文に無い 0・確かめられない 0" in lines


def test_verdicts_including_unknown() -> None:
    facts = _facts(
        summarized=4,
        verdicts=((verify_source.CONFIRMED, 1), (verify_source.MISMATCH, 1), (verify_source.UNVERIFIABLE, 1), ("new", 1)),
    )
    lines = _lines(_loaded([_run(START, facts=facts)]), _run(START + timedelta(days=7), facts=facts))
    assert "・要約 8・照合: 裏付け 2・本文に無い 2・確かめられない 2・分からない 2" in lines


def test_flat_ruler_is_counted() -> None:
    """F：物差しが平らに戻った回を数える（U22）。"""
    runs = [_run(START + timedelta(days=d), facts=_facts(cut_in_tie=d < 3)) for d in range(7)]
    lines = _lines(_loaded(runs), _run(START + timedelta(days=7)))
    assert "・上限の切れ目が同点の中にあった回: 3/8" in lines


def test_broken_lines_are_reported() -> None:
    lines = _lines(_loaded(_daily(7), bad=2), _run(START + timedelta(days=7)))
    assert "・読めなかった記録 2 行" in lines


def test_long_absence_lists_a_few_days_and_counts_the_rest() -> None:
    """何週間も止まっていたら、日付を全部並べない。"""
    lines = _lines(_loaded([_run(START)]), _run(START + timedelta(days=30)))
    missing = [line for line in lines if line.startswith("・走った日")]
    assert missing == ["・走った日 2/31（走らなかった日: 09-02・09-03・09-04・09-05・09-06・09-07・09-08 ほか 22 日）"]
