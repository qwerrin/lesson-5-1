"""scout/emit のテスト。**実装より先に書いた。**

`emit` は1回の実行の結果を `01_Inbox/YYYY-MM-DD-scout.md` へ**追記**し、
**読み戻して**書いたつもりのものが本当にそこにあるかを確かめる段（M6）。

決めたこと（2026-10-03）
--------------------------------------------------------------------------

============ ====================================================================
A            frontmatter は**ファイルを作るときだけ**書く。中身は固定。
             件数を入れると2回目の実行で全体を書き戻すことになり、*読んだ後の
             書き込みを消す*（教訓 `read-modify-write-drops-concurrent-edits`）
B            1回の実行は**1回の書き込み**で末尾に足す。読み戻しは**自分の run-id** で探す
C            **0件でも節を書く**（M7・M9）。無音にすると「収集が死んだ」と区別できない
D            外から来た文字列（タイトル・要約）は**節の構造を壊せない**形にする。
             `[[` が残ると vault のリンク検査に*偽のリンク切れ*が出る
E            段どうしの件数・同一性が合わないなら**書く前に止める**
             （教訓 `validate-before-writing`）
F            読み戻しは**入力と**突き合わせる。書いた文字列と比べるだけだと、
             描画が記事を1件落としても「書いたとおり」と答える
G            読み戻しの失敗は**例外にせず結果に入れる**。例外だと `notify` まで届かない
============ ====================================================================

日付の期待値に「今日」を使わない（教訓 `today-as-expected-value-hides-date-bugs`）。
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scout"))

import dedupe  # noqa: E402
import emit  # noqa: E402
import fetch  # noqa: E402
import rank  # noqa: E402
import split  # noqa: E402
import summarize  # noqa: E402
import verify_source  # noqa: E402

#: **実行日と違う日**にしておく。`today()` を使う実装はこれで落ちる。
AT = datetime(2026, 9, 22, 21, 5, 0)
NAME = "2026-09-22-scout.md"
STAGES = ("fetch: qiita ok 3 件", "dedupe: 3 件中 3 件を残した")


# ---------------------------------------------------------------------------
# 組み立て
# ---------------------------------------------------------------------------


def _kept(url: str, *, title: str = "記事", source: str = "qiita") -> dedupe.Kept:
    article = fetch.Article(
        source=source,
        url=url,
        title=title,
        body="本文" * 300,
        published_at=datetime(2026, 9, 22, 9, 0, 0),
        updated_at=None,
        author="someone",
        tags=("python",),
        metrics={},
    )
    return dedupe.Kept(article=article, key=url)


def _scored(url: str, *, score: int = 3, **kwargs: str) -> rank.Scored:
    return rank.Scored(kept=_kept(url, **kwargs), score=score, hits=())


def _summary(scored: rank.Scored, text: str = "要約の文。") -> summarize.Summary:
    return summarize.Summary(scored=scored, text=text, quotes=(), prompt_tokens=10, output_tokens=5)


def _check(
    s: summarize.Summary, verdict: str = verify_source.CONFIRMED, missing: tuple[str, ...] = ()
) -> verify_source.Check:
    return verify_source.Check(summary=s, verdict=verdict, found=(), missing=missing, quotes_missing=())


def _run(
    *,
    done: tuple[tuple[rank.Scored, str, str, tuple[str, ...]], ...] = (),
    failed: tuple[tuple[rank.Scored, str], ...] = (),
    headline: tuple[tuple[dedupe.Kept, str, int | None], ...] = (),
) -> dict[str, object]:
    """`done` は (記事, 要約文, 判定, 本文に無い主張)。"""
    summaries = [(_summary(sc, text), verdict, missing) for sc, text, verdict, missing in done]
    failures = tuple(
        summarize.Failure(scored=sc, reason=reason, detail="詳細") for sc, reason in failed
    )
    return {
        "split": split.Split(
            summarize=tuple(sc for sc, *_ in done) + tuple(sc for sc, _ in failed),
            headline=tuple(split.Headline(kept=k, reason=r, score=p) for k, r, p in headline),
        ),
        "digest": summarize.Digest(done=tuple(s for s, *_ in summaries), failed=failures),
        "audit": verify_source.Audit(checks=tuple(_check(s, v, m) for s, v, m in summaries)),
    }


def _emit(inbox: Path, *, at: datetime = AT, run_id: str = "r1", stages=STAGES, **run) -> emit.Emitted:
    parts = run or _run()
    return emit.emit(inbox, at=at, run_id=run_id, stages=stages, **parts)  # type: ignore[arg-type]


def _text(inbox: Path) -> str:
    return (inbox / NAME).read_bytes().decode("utf-8")


def _three() -> dict[str, object]:
    return _run(
        done=((_scored("https://qiita.com/a/items/1", title="要約した記事"), "要約A", verify_source.CONFIRMED, ()),),
        failed=((_scored("https://qiita.com/a/items/2", title="失敗した記事"), summarize.NOT_FINISHED),),
        headline=((_kept("https://zenn.dev/b/articles/3", title="見出しの記事", source="zenn"), split.NO_BODY, None),),
    )


# ---------------------------------------------------------------------------
# A：ファイルと frontmatter
# ---------------------------------------------------------------------------


def test_file_name_comes_from_at_not_from_today(tmp_path: Path) -> None:
    result = _emit(tmp_path)
    assert result.path == tmp_path / NAME
    assert (tmp_path / NAME).exists()


def test_new_file_starts_with_frontmatter(tmp_path: Path) -> None:
    result = _emit(tmp_path)
    text = _text(tmp_path)
    assert text.startswith("---\n")
    head = text.split("\n---\n", 1)[0]
    assert "  - scout" in head
    assert "  - inbox" in head
    assert "date: 2026-09-22" in head
    assert result.created is True
    assert result.ok is True


def test_second_run_appends_and_keeps_first_block_byte_for_byte(tmp_path: Path) -> None:
    _emit(tmp_path, run_id="r1")
    first = _text(tmp_path)
    result = _emit(tmp_path, run_id="r2", at=datetime(2026, 9, 22, 23, 0, 0))
    second = _text(tmp_path)
    assert second.startswith(first)
    assert second.count("\n---\n") == 1  # frontmatter は1つだけ
    assert result.created is False
    assert result.ok is True


def test_written_as_utf8_lf_without_bom(tmp_path: Path) -> None:
    _emit(tmp_path, **_three())
    raw = (tmp_path / NAME).read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf")
    assert b"\r\n" not in raw


def test_missing_inbox_is_not_created(tmp_path: Path) -> None:
    inbox = tmp_path / "nope"
    with pytest.raises(ValueError):
        _emit(inbox)
    assert not inbox.exists()


def test_existing_file_without_trailing_newline_still_gets_heading_at_line_start(tmp_path: Path) -> None:
    _emit(tmp_path, run_id="r1")
    path = tmp_path / NAME
    path.write_bytes(path.read_bytes() + "手で書いたメモ".encode("utf-8"))
    result = _emit(tmp_path, run_id="r2")
    assert "\n## 21:05 実行 `r2`" in _text(tmp_path)
    assert "手で書いたメモ## " not in _text(tmp_path)
    assert result.ok is True


# ---------------------------------------------------------------------------
# B・C：1回の実行の節
# ---------------------------------------------------------------------------


def test_section_header_has_local_time_and_run_id(tmp_path: Path) -> None:
    _emit(tmp_path, run_id="r1")
    assert "\n## 21:05 実行 `r1`\n" in _text(tmp_path)
    assert "<!-- scout:end r1 -->" in _text(tmp_path)


def test_zero_articles_still_writes_a_section(tmp_path: Path) -> None:
    result = _emit(tmp_path)
    text = _text(tmp_path)
    assert "## 21:05 実行 `r1`" in text
    assert "記事 0 件" in text
    for stage in STAGES:
        assert f"- {stage}" in text
    assert result.entries == 0
    assert result.ok is True


def test_stage_lines_are_flattened_to_one_line(tmp_path: Path) -> None:
    _emit(tmp_path, stages=("fetch: 1行目\n## 偽の見出し",))
    text = _text(tmp_path)
    assert "- fetch: 1行目 ## 偽の見出し" in text
    assert "\n## 偽の見出し" not in text


def test_every_article_appears_once_with_its_url(tmp_path: Path) -> None:
    result = _emit(tmp_path, **_three())
    text = _text(tmp_path)
    for url in ("https://qiita.com/a/items/1", "https://qiita.com/a/items/2", "https://zenn.dev/b/articles/3"):
        assert text.count(f"]({url})") == 1
    assert text.count("\n### ") == 3
    assert result.entries == 3
    assert "記事 3 件（要約 1・要約できなかった 1・見出しだけ 1）" in text


def test_order_is_summaries_then_failures_then_headlines(tmp_path: Path) -> None:
    _emit(tmp_path, **_three())
    text = _text(tmp_path)
    assert text.index("要約した記事") < text.index("失敗した記事") < text.index("見出しの記事")


def test_meta_lines_say_why(tmp_path: Path) -> None:
    run = _run(
        done=(
            (_scored("https://qiita.com/x/items/1", score=7), "a", verify_source.CONFIRMED, ()),
            (_scored("https://qiita.com/x/items/2"), "b", verify_source.MISMATCH, ("2倍", "Rust")),
            (_scored("https://qiita.com/x/items/3"), "c", verify_source.UNVERIFIABLE, ()),
        ),
        failed=((_scored("https://qiita.com/x/items/4"), summarize.BROKEN_REPLY),),
        headline=(
            (_kept("https://zenn.dev/y/articles/5", source="zenn"), split.NOT_SUMMARIZABLE, None),
            (_kept("https://qiita.com/x/items/6"), split.TOO_SHORT, 2),
        ),
    )
    _emit(tmp_path, **run)
    text = _text(tmp_path)
    assert "- qiita・点 7・照合: 照合できた" in text
    assert "照合: 本文に無い主張あり（「2倍」・「Rust」）" in text
    assert "照合: 確認できない" in text
    assert "要約できなかった（broken_reply）" in text
    assert "- zenn・点 なし・見出しだけ（not_summarizable）" in text
    assert "- qiita・点 2・見出しだけ（too_short）" in text
    # **件数が全部ちがう組**にする。同じ数だと、取り違えても区別できない。
    assert "記事 6 件（要約 3・要約できなかった 1・見出しだけ 2）" in text


def test_score_zero_is_not_shown_as_none(tmp_path: Path) -> None:
    run = _run(headline=((_kept("https://qiita.com/x/items/1"), split.TOO_SHORT, 0),))
    _emit(tmp_path, **run)
    assert "点 0・" in _text(tmp_path)


# ---------------------------------------------------------------------------
# D：外から来た文字列が構造を壊さない
# ---------------------------------------------------------------------------


def test_summary_lines_are_quoted_so_headings_inside_do_nothing(tmp_path: Path) -> None:
    hostile = "1行目\n## 偽の節\n---\n### 偽の記事"
    run = _run(done=((_scored("https://qiita.com/x/items/1"), hostile, verify_source.CONFIRMED, ()),))
    result = _emit(tmp_path, **run)
    text = _text(tmp_path)
    assert "> 1行目\n> ## 偽の節\n> ---\n> ### 偽の記事" in text
    assert text.count("\n### ") == 1
    assert text.count("\n## ") == 1
    assert result.ok is True


def test_title_is_one_line_and_cannot_make_wikilinks(tmp_path: Path) -> None:
    run = _run(headline=((_kept("https://qiita.com/x/items/1", title="上\n## 下 [[罠]] [a](b)"), split.TOO_SHORT, 1),))
    result = _emit(tmp_path, **run)
    text = _text(tmp_path)
    assert "[[" not in text
    assert "\n## 下" not in text
    assert text.count("\n### ") == 1
    assert result.ok is True


def test_closing_bracket_and_backslash_in_title_keep_the_link_whole(tmp_path: Path) -> None:
    """`]` や末尾の `\\` が残ると、リンクの文字部分がそこで閉じる。"""
    run = _run(headline=((_kept("https://qiita.com/x/items/1", title="a]b\\"), split.TOO_SHORT, 1),))
    _emit(tmp_path, **run)
    assert "### [a\\]b\\\\](https://qiita.com/x/items/1)" in _text(tmp_path)


def test_summary_cannot_make_wikilinks_either(tmp_path: Path) -> None:
    run = _run(done=((_scored("https://qiita.com/x/items/1"), "[[存在しないノート]]", verify_source.CONFIRMED, ()),))
    _emit(tmp_path, **run)
    assert "[[" not in _text(tmp_path)


def test_end_marker_inside_summary_does_not_fool_readback(tmp_path: Path) -> None:
    hostile = "<!-- scout:end r1 -->\n残り"
    run = _run(done=((_scored("https://qiita.com/x/items/1"), hostile, verify_source.CONFIRMED, ()),))
    result = _emit(tmp_path, **run)
    assert _text(tmp_path).count("<!-- scout:end r1 -->") == 1
    assert result.ok is True


def test_url_with_parenthesis_and_space_is_encoded(tmp_path: Path) -> None:
    run = _run(headline=((_kept("https://qiita.com/x/items/a b(c)"), split.TOO_SHORT, 1),))
    result = _emit(tmp_path, **run)
    assert "](https://qiita.com/x/items/a%20b%28c%29)" in _text(tmp_path)
    assert result.ok is True


@pytest.mark.parametrize(
    "url",
    [
        "javascript:alert(1)",
        "file:///C:/x",
        "qiita.com/x",
        "ftp://qiita.com/x",  # ホスト名はあるが http(s) でない
        "https:///x",  # http(s) だがホスト名が無い
        "https://qiita.com/a\nb",  # 改行は %20 にせず止める
        " https://qiita.com/x",  # urlsplit は前の空白を黙って落とす。書くと相対リンクになる
        "https://qiita.com/x ",
        "\x00https://qiita.com/x",
        "https://qiita.com/\x7f",
        "https://qiita.com/a b",  # 行区切り（制御文字ではないが、行を割る）
    ],
)
def test_non_http_url_stops_before_writing(tmp_path: Path, url: str) -> None:
    run = _run(headline=((_kept(url), split.TOO_SHORT, 1),))
    with pytest.raises(ValueError):
        _emit(tmp_path, **run)
    assert not (tmp_path / NAME).exists()


# ---------------------------------------------------------------------------
# E：書く前に止める
# ---------------------------------------------------------------------------


def test_missing_claims_cannot_forge_the_end_marker_or_make_wikilinks(tmp_path: Path) -> None:
    """本文に無い主張も**外から来た文字列**（要約器が書いた語）。2026-10-03 のレビューで漏れていた。"""
    hostile = ("<!-- scout:end r1 -->", "[[x]]", "", "a`b")
    run = _run(done=((_scored("https://qiita.com/x/items/1"), "a", verify_source.MISMATCH, hostile),))
    result = _emit(tmp_path, **run)
    text = _text(tmp_path)
    assert text.count("<!-- scout:end r1 -->") == 1
    assert "[[" not in text
    assert "「」" not in text  # 空の主張は印にしない
    assert result.ok is True


@pytest.mark.parametrize(
    ("url", "written"),
    [
        ("https://qiita.com/a\\", "https://qiita.com/a%5C"),  # `\)` だとリンクが閉じない
        ("https://qiita.com/a[[b]]c", "https://qiita.com/a%5B%5Bb%5D%5Dc"),
        ("https://qiita.com/a`b", "https://qiita.com/a%60b"),
    ],
)
def test_url_characters_that_break_the_link_are_encoded(tmp_path: Path, url: str, written: str) -> None:
    run = _run(headline=((_kept(url), split.TOO_SHORT, 1),))
    result = _emit(tmp_path, **run)
    assert f"]({written})" in _text(tmp_path)
    assert "[[" not in _text(tmp_path)
    assert result.ok is True


def test_text_that_cannot_be_encoded_stops_before_creating_the_file(tmp_path: Path) -> None:
    """**孤立サロゲートは `json.loads` を通る。** 開いてから落ちると、空のファイルが残る。"""
    run = _run(headline=((_kept("https://qiita.com/x/items/1", title="\ud800"), split.TOO_SHORT, 1),))
    with pytest.raises(ValueError):
        _emit(tmp_path, **run)
    assert not (tmp_path / NAME).exists()


def test_existing_crlf_file_is_appended_without_false_alarm(tmp_path: Path) -> None:
    """vault は `core.autocrlf=true`。**git が触ったファイルは CRLF で戻ってくる。**"""
    _emit(tmp_path, run_id="r1")
    path = tmp_path / NAME
    path.write_bytes(path.read_bytes().replace(b"\n", b"\r\n"))
    result = _emit(tmp_path, run_id="r2", **_three())
    assert result.ok is True, result.problems


def test_reused_run_id_is_found_in_a_crlf_file(tmp_path: Path) -> None:
    _emit(tmp_path, run_id="r1")
    path = tmp_path / NAME
    path.write_bytes(path.read_bytes().replace(b"\n", b"\r\n"))
    with pytest.raises(ValueError):
        _emit(tmp_path, run_id="r1")


def test_existing_file_with_bom_is_appended_without_false_alarm(tmp_path: Path) -> None:
    _emit(tmp_path, run_id="r1")
    path = tmp_path / NAME
    path.write_bytes(b"\xef\xbb\xbf" + path.read_bytes())
    result = _emit(tmp_path, run_id="r2")
    assert result.ok is True, result.problems


def test_headline_that_is_also_summarized_stops_before_writing(tmp_path: Path) -> None:
    run = _three()
    sp = run["split"]
    twice = split.Headline(kept=sp.summarize[0].kept, reason=split.TOO_SHORT, score=1)  # type: ignore[attr-defined]
    run["split"] = split.Split(summarize=sp.summarize, headline=(*sp.headline, twice))  # type: ignore[attr-defined]
    with pytest.raises(ValueError):
        _emit(tmp_path, **run)
    assert not (tmp_path / NAME).exists()


def test_audit_of_a_different_summary_text_stops(tmp_path: Path) -> None:
    """**キーが同じでも、照合した要約が別物なら止める。** 書く要約と照合した要約がずれる。"""
    run = _three()
    done = run["digest"].done[0]  # type: ignore[attr-defined]
    other = summarize.Summary(scored=done.scored, text="別の要約", quotes=(), prompt_tokens=1, output_tokens=1)
    run["audit"] = verify_source.Audit(checks=(_check(other),))
    with pytest.raises(ValueError):
        _emit(tmp_path, **run)


def test_digest_missing_an_item_stops_before_writing(tmp_path: Path) -> None:
    run = _three()
    run["digest"] = summarize.Digest(done=run["digest"].done, failed=())  # type: ignore[attr-defined]
    with pytest.raises(ValueError):
        _emit(tmp_path, **run)
    assert not (tmp_path / NAME).exists()


def test_digest_with_an_extra_item_stops_before_writing(tmp_path: Path) -> None:
    run = _three()
    extra = summarize.Failure(scored=_scored("https://qiita.com/z/items/9"), reason="x", detail="")
    digest = run["digest"]
    run["digest"] = summarize.Digest(done=digest.done, failed=(*digest.failed, extra))  # type: ignore[attr-defined]
    with pytest.raises(ValueError):
        _emit(tmp_path, **run)


def test_digest_with_same_count_but_different_item_stops(tmp_path: Path) -> None:
    run = _three()
    other = summarize.Failure(scored=_scored("https://qiita.com/z/items/9"), reason="x", detail="")
    run["digest"] = summarize.Digest(done=run["digest"].done, failed=(other,))  # type: ignore[attr-defined]
    with pytest.raises(ValueError):
        _emit(tmp_path, **run)


def test_audit_not_matching_digest_stops_before_writing(tmp_path: Path) -> None:
    run = _three()
    run["audit"] = verify_source.Audit(checks=())
    _emit(tmp_path, run_id="r0")
    before = _text(tmp_path)
    with pytest.raises(ValueError):
        _emit(tmp_path, run_id="r1", **run)
    assert _text(tmp_path) == before


def test_audit_with_a_different_summary_stops(tmp_path: Path) -> None:
    run = _three()
    stranger = _summary(_scored("https://qiita.com/z/items/9"))
    run["audit"] = verify_source.Audit(checks=(_check(stranger),))
    with pytest.raises(ValueError):
        _emit(tmp_path, **run)


def test_aware_datetime_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        _emit(tmp_path, at=datetime(2026, 9, 22, 12, 5, tzinfo=timezone.utc))


@pytest.mark.parametrize("run_id", ["", "a b", "a\nb", "-x", "a`b", "a-->b", "a--b"])
def test_bad_run_id_is_refused(tmp_path: Path, run_id: str) -> None:
    with pytest.raises(ValueError):
        _emit(tmp_path, run_id=run_id)
    assert not (tmp_path / NAME).exists()


def test_reused_run_id_is_refused_and_file_unchanged(tmp_path: Path) -> None:
    _emit(tmp_path, run_id="r1")
    before = _text(tmp_path)
    with pytest.raises(ValueError):
        _emit(tmp_path, run_id="r1")
    assert _text(tmp_path) == before


def test_title_that_looks_like_a_section_label_is_not_the_section(tmp_path: Path) -> None:
    """**節は見出しの行で探す。** 部分一致だと、前の実行のタイトルに当たる。"""
    run = _run(headline=((_kept("https://qiita.com/x/items/1", title="実行 `r2`"), split.TOO_SHORT, 1),))
    _emit(tmp_path, run_id="r1", **run)
    result = _emit(tmp_path, run_id="r2", **_three())
    assert result.ok is True, result.problems


def test_run_id_that_is_a_prefix_of_another_is_not_a_reuse(tmp_path: Path) -> None:
    _emit(tmp_path, run_id="r10")
    result = _emit(tmp_path, run_id="r1")
    assert result.ok is True


# ---------------------------------------------------------------------------
# F・G：読み戻し
# ---------------------------------------------------------------------------


def test_existing_file_without_frontmatter_is_appended_but_reported(tmp_path: Path) -> None:
    (tmp_path / NAME).write_bytes("# 手で作ったファイル\n".encode("utf-8"))
    result = _emit(tmp_path)
    assert "## 21:05 実行 `r1`" in _text(tmp_path)
    assert _text(tmp_path).startswith("# 手で作ったファイル\n")  # 直さない
    assert result.ok is False
    assert any("frontmatter" in p for p in result.problems)


def _block(**run: object) -> tuple[str, tuple[str, ...]]:
    """読み戻しへ渡す (書いた節, 期待する URL)。"""
    return emit.render(at=AT, run_id="r1", stages=STAGES, **run)  # type: ignore[arg-type]


DAY = "2026-09-22"
HEAD = "---\ntags:\n  - scout\n  - inbox\ndate: 2026-09-22\n---\n\n# 2026-09-22 scout\n"


def test_readback_accepts_what_render_wrote() -> None:
    block, urls = _block(**_three())
    assert emit.readback(HEAD + block, block=block, run_id="r1", urls=urls, entries=3, day=DAY) == ()


def test_readback_reports_unclosed_frontmatter() -> None:
    block, urls = _block(**_three())
    text = HEAD.replace("\n---\n\n#", "\n\n#") + block
    assert any("frontmatter" in p for p in emit.readback(text, block=block, run_id="r1", urls=urls, entries=3, day=DAY))


def test_readback_reports_frontmatter_without_scout_tag() -> None:
    block, urls = _block(**_three())
    text = HEAD.replace("  - scout\n", "") + block
    assert any("frontmatter" in p for p in emit.readback(text, block=block, run_id="r1", urls=urls, entries=3, day=DAY))


def test_readback_reports_frontmatter_with_bad_date() -> None:
    block, urls = _block(**_three())
    text = HEAD.replace("date: 2026-09-22", "date: 22/09/2026") + block
    assert any("frontmatter" in p for p in emit.readback(text, block=block, run_id="r1", urls=urls, entries=3, day=DAY))


def test_readback_reports_missing_section() -> None:
    block, urls = _block(**_three())
    problems = emit.readback(HEAD, block=block, run_id="r1", urls=urls, entries=3, day=DAY)
    assert problems
    assert any("r1" in p for p in problems)


def test_readback_reports_section_written_twice() -> None:
    block, urls = _block(**_three())
    assert emit.readback(HEAD + block + block, block=block, run_id="r1", urls=urls, entries=3, day=DAY)


def test_readback_accepts_crlf_because_git_may_have_converted_the_file() -> None:
    """vault は `core.autocrlf=true`。**git が触れば CRLF になる**のは改ざんではない。"""
    block, urls = _block(**_three())
    text = (HEAD + block).replace("\n", "\r\n")
    assert emit.readback(text, block=block, run_id="r1", urls=urls, entries=3, day=DAY) == ()


def test_readback_accepts_flow_list_and_quoted_date() -> None:
    """Obsidian のプロパティ画面や手で直すと、**同じ意味の別の書き方**になる。誤報にしない。"""
    block, urls = _block(**_three())
    head = '---\ntags: [scout, inbox]\ndate: "2026-09-22"\n---\n'
    assert emit.readback(head + block, block=block, run_id="r1", urls=urls, entries=3, day=DAY) == ()


def test_readback_reports_frontmatter_date_of_another_day() -> None:
    block, urls = _block(**_three())
    text = HEAD.replace("date: 2026-09-22", "date: 2026-09-21") + block
    problems = emit.readback(text, block=block, run_id="r1", urls=urls, entries=3, day=DAY)
    assert any("2026-09-21" in p for p in problems)


def test_readback_is_not_fooled_by_a_link_inside_a_title() -> None:
    """タイトルに `](URL)` を仕込むと、**部分一致では本物のリンクが無くても通る**。"""
    fake = "https://qiita.com/z/items/9"
    run = _run(headline=((_kept("https://qiita.com/x/items/1", title=f"x]({fake})"), split.TOO_SHORT, 1),))
    block, _ = _block(**run)
    problems = emit.readback(HEAD + block, block=block, run_id="r1", urls=(fake,), entries=1, day=DAY)
    assert any(fake in p for p in problems)


def test_readback_counts_duplicate_urls() -> None:
    block, urls = _block(**_three())
    twice = (urls[0], urls[0], urls[1])
    assert emit.readback(HEAD + block, block=block, run_id="r1", urls=twice, entries=3, day=DAY)


def test_readback_reports_changed_content_even_when_section_is_found() -> None:
    """節の位置も件数も合うのに、中身だけ違う。**位置で見つかることは、書いたとおりの証拠ではない。**"""
    block, urls = _block(**_three())
    text = HEAD + block.replace("要約A", "要約B")
    assert emit.readback(text, block=block, run_id="r1", urls=urls, entries=3, day=DAY)


def test_readback_reports_frontmatter_that_is_not_at_the_top() -> None:
    block, urls = _block(**_three())
    text = "# 手で足した行\n" + HEAD + block
    # 「frontmatter」を含むかだけで見ると、tags の問題でも通る。**先頭に無いこと**を言わせる。
    assert any("先頭" in p for p in emit.readback(text, block=block, run_id="r1", urls=urls, entries=3, day=DAY))


def test_readback_counts_against_inputs_not_against_what_was_written() -> None:
    """**描画が1件落としても、書いたとおりには読める。** 期待値は入力から取る（F）。"""
    block, urls = _block(**_three())
    assert any("件" in p for p in emit.readback(HEAD + block, block=block, run_id="r1", urls=urls, entries=4, day=DAY))


def test_readback_reports_missing_url() -> None:
    block, urls = _block(**_three())
    more = (*urls, "https://qiita.com/never/items/0")
    problems = emit.readback(HEAD + block, block=block, run_id="r1", urls=more, entries=3, day=DAY)
    assert any("https://qiita.com/never/items/0" in p for p in problems)


def test_render_lists_every_url_of_the_inputs() -> None:
    _, urls = _block(**_three())
    assert urls == (
        "https://qiita.com/a/items/1",
        "https://qiita.com/a/items/2",
        "https://zenn.dev/b/articles/3",
    )


# ---------------------------------------------------------------------------
# 結果の文言
# ---------------------------------------------------------------------------


def test_summary_says_count_file_and_new_or_append(tmp_path: Path) -> None:
    first = _emit(tmp_path, run_id="r1", **_three())
    assert NAME in first.summary
    assert "3 件" in first.summary
    assert "新規" in first.summary
    second = _emit(tmp_path, run_id="r2")
    assert "追記" in second.summary
    assert "問題なし" in second.summary


def test_summary_shows_problems(tmp_path: Path) -> None:
    (tmp_path / NAME).write_bytes(b"# x\n")
    result = _emit(tmp_path)
    assert "問題 1 件" in result.summary
    assert "frontmatter" in result.summary
