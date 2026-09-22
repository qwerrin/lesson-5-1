"""scout/dedupe のテスト。**実装より先に書いた。**

`dedupe` は URL を正規化して、**台帳**と**vault** に既にあるものを落とす段。

なぜ必須の部品なのか
--------------------------------------------------------------------------

Qiita の `created:>=` は**日付までしか指定できない**（H11・2026-09-19 実測）。
前回の実行と同じ日に走らせれば、**同じ記事を必ず再取得する**。
*任意の最適化ではなく、無いと毎日同じものが積み上がる。*

============ ====================================================================
根拠         ここで守ること
============ ====================================================================
H5           `?utm_source=` 違いを**別物として重複させない**
H8           **捨てたものを消さない。** 理由つきで残す
H9           台帳だけでなく **vault に既にあるもの**とも突き合わせる
**M4**       **効きすぎて新しい記事を捨てない**
============ ====================================================================

どちらの誤りが重いか
--------------------------------------------------------------------------

**M4（捨てすぎ）が H5（重複を持ってくる）より重い。**
重複は `01_Inbox/` に1行余計に出るだけだが、捨てすぎると*その記事は二度と来ない*
——Qiita の検索は期間で絞るので、一度飛ばした日には戻らない。

だから正規化の方針は **「迷ったら別物として扱う」**:

- 落とすのは**既知の追跡パラメータだけ**。知らないクエリは**残す**
- パスの大小は**揃えない**（大小が意味を持つ URL がある）
- ホストの `www.` は**落とさない**。落として困る場合が理論上ありうるので、
  *実際の取得元が使っていない*ことを理由に踏み込まない
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scout"))

import dedupe  # noqa: E402
import fetch  # noqa: E402

WHEN = datetime(2026, 9, 22, 10, 0, 0)


def _article(url: str, title: str = "記事") -> fetch.Article:
    return fetch.Article(
        source="qiita",
        url=url,
        title=title,
        body="本文",
        published_at=WHEN,
        updated_at=None,
        author="someone",
        tags=(),
        metrics={},
    )


# --------------------------------------------------------------------------
# 正規化（H5）
# --------------------------------------------------------------------------


def test_追跡パラメータを落とす() -> None:
    """**H5：`?utm_source=` 違いが別物として重複する。**"""
    plain = "https://qiita.com/a/items/x"
    tracked = "https://qiita.com/a/items/x?utm_source=twitter&utm_medium=social"

    assert dedupe.normalize(tracked) == dedupe.normalize(plain)


def test_知らないクエリは残す() -> None:
    """**★ M4 の核心。** 意味を持つクエリを落とすと、別の記事が同じものになる。

    *捨てすぎは、その記事が二度と来ないことを意味する。*
    """
    first = "https://example.com/articles?page=1"
    second = "https://example.com/articles?page=2"

    assert dedupe.normalize(first) != dedupe.normalize(second)


def test_残ったクエリを並べ替える() -> None:
    """**順番違いを別物にしない。** 意味は同じで、並びだけが違う。"""
    one = "https://example.com/x?b=2&a=1"
    other = "https://example.com/x?a=1&b=2"

    assert dedupe.normalize(one) == dedupe.normalize(other)


def test_フラグメントを落とす() -> None:
    """`#section` は**同じページの中の位置**であって、別の記事ではない。"""
    assert dedupe.normalize("https://q.com/x#見出し") == dedupe.normalize("https://q.com/x")


def test_末尾のスラッシュを落とす() -> None:
    assert dedupe.normalize("https://q.com/a/x/") == dedupe.normalize("https://q.com/a/x")


def test_根のスラッシュは残す() -> None:
    """`https://q.com/` を `https://q.com` にすると、**別物に見えるだけで得が無い。**"""
    assert dedupe.normalize("https://q.com/").endswith("/")


def test_ホストとスキームの大小を揃える() -> None:
    assert dedupe.normalize("HTTPS://Qiita.COM/a/x") == dedupe.normalize("https://qiita.com/a/x")


def test_パスの大小は揃えない() -> None:
    """**パスは大小が意味を持つ。** 揃えると別の記事が同じになる（M4）。"""
    assert dedupe.normalize("https://q.com/A") != dedupe.normalize("https://q.com/a")


def test_wwwは落とさない() -> None:
    """**踏み込まない。** 落として困る場合が理論上あり、実際の取得元は使っていない。"""
    assert dedupe.normalize("https://www.q.com/x") != dedupe.normalize("https://q.com/x")


def test_似ているが別の記事を同じにしない() -> None:
    assert dedupe.normalize("https://q.com/a/items/x") != dedupe.normalize("https://q.com/a/items/y")


# --------------------------------------------------------------------------
# 突き合わせ
# --------------------------------------------------------------------------


def test_初めてのものは残る() -> None:
    got = dedupe.sift([_article("https://q.com/x")])

    assert [k.article.url for k in got.kept] == ["https://q.com/x"]
    assert got.dropped == ()


def test_同じ取り込みの中の重複を落とす() -> None:
    """**H11：同じ日に2回走れば、同じ記事が2回来る。**"""
    articles = [
        _article("https://q.com/x", "先に来たほう"),
        _article("https://q.com/x?utm_source=rss", "後から来たほう"),
    ]

    got = dedupe.sift(articles)

    assert [k.article.title for k in got.kept] == ["先に来たほう"]
    assert [d.reason for d in got.dropped] == [dedupe.SAME_BATCH]


def test_台帳にあるものを落とす() -> None:
    got = dedupe.sift([_article("https://q.com/x")], seen=["https://q.com/x?utm_medium=feed"])

    assert got.kept == ()
    assert [d.reason for d in got.dropped] == [dedupe.SEEN_BEFORE]


def test_vaultにあるものを落とす() -> None:
    """**H9：Clippings の `source:` に既にある記事を、また持ってこない。**

    2026-09-21 の実測で、`Clippings/` 47件のうち **43件が `source:` URL を持つ**。
    """
    got = dedupe.sift([_article("https://q.com/x")], known=["https://q.com/x"])

    assert [d.reason for d in got.dropped] == [dedupe.IN_VAULT]


def test_台帳とvaultの両方にあれば台帳を理由にする() -> None:
    """**理由は1つに決める。** 並べると、読む側がどちらを直せばよいか分からない。"""
    got = dedupe.sift(
        [_article("https://q.com/x")],
        seen=["https://q.com/x"],
        known=["https://q.com/x"],
    )

    assert [d.reason for d in got.dropped] == [dedupe.SEEN_BEFORE]


def test_捨てたものも記事ごと残す() -> None:
    """**H8：捨てたものは記録されない、を止める。**

    *何を捨てたかが残らないと、選別が正しいか後から検証できない。*
    """
    dropped_url = "https://q.com/x"
    got = dedupe.sift([_article(dropped_url, "捨てられるほう")], seen=[dropped_url])

    assert got.dropped[0].article.title == "捨てられるほう"
    assert got.dropped[0].key == dedupe.normalize(dropped_url)


def test_残る順は渡された順() -> None:
    urls = ["https://q.com/c", "https://q.com/a", "https://q.com/b"]
    got = dedupe.sift([_article(u) for u in urls])

    assert [k.article.url for k in got.kept] == urls


def test_台帳もvaultも空で動く() -> None:
    got = dedupe.sift([_article("https://q.com/x")])

    assert len(got.kept) == 1


def test_記事が0件でも落ちない() -> None:
    """**0件は異常ではない。** 前回から新しい記事が無い日は普通にある。"""
    got = dedupe.sift([])

    assert got.kept == ()
    assert got.dropped == ()


# --------------------------------------------------------------------------
# 効きすぎない（M4）
# --------------------------------------------------------------------------


def test_追跡パラメータ違いは同じ記事として落ちる() -> None:
    articles = [
        _article("https://q.com/x?utm_campaign=a"),
        _article("https://q.com/x?utm_campaign=b"),
    ]

    got = dedupe.sift(articles)

    assert len(got.kept) == 1
    assert len(got.dropped) == 1


def test_意味のあるクエリ違いは別の記事として残る() -> None:
    """**★ 効きすぎない。** ここが緩いと、その記事は二度と来ない。"""
    articles = [
        _article("https://example.com/list?page=1"),
        _article("https://example.com/list?page=2"),
    ]

    got = dedupe.sift(articles)

    assert len(got.kept) == 2
    assert got.dropped == ()


# --------------------------------------------------------------------------
# 件数
# --------------------------------------------------------------------------


def test_件数を出す() -> None:
    """**「重複を除きました」だけでは、何件見て何件落としたか分からない。**"""
    articles = [
        _article("https://q.com/x"),
        _article("https://q.com/x?utm_source=a"),
        _article("https://q.com/y"),
    ]

    got = dedupe.sift(articles, seen=["https://q.com/y"])

    assert got.total == 3
    assert len(got.kept) == 1
    assert "3" in got.summary
    assert dedupe.SEEN_BEFORE in got.summary


def test_理由ごとの内訳を出す() -> None:
    articles = [
        _article("https://q.com/x"),
        _article("https://q.com/x?utm_source=a"),
        _article("https://q.com/y"),
        _article("https://q.com/z"),
    ]

    got = dedupe.sift(articles, seen=["https://q.com/y"], known=["https://q.com/z"])

    assert got.reasons == {
        dedupe.SAME_BATCH: 1,
        dedupe.SEEN_BEFORE: 1,
        dedupe.IN_VAULT: 1,
    }


def test_壊れたURLを黙って捨てない() -> None:
    """**正規化できないものを「重複」にしない。** 理由が変われば、直し方も変わる。"""
    got = dedupe.sift([_article("これはURLではない")])

    assert [k.article.url for k in got.kept] == ["これはURLではない"]


def test_台帳の壊れたURLで落とさない() -> None:
    """台帳側が壊れていても、**まともな記事を巻き込まない。**"""
    got = dedupe.sift([_article("https://q.com/x")], seen=["", "  ", "://"])

    assert len(got.kept) == 1


def test_記事を渡さずには呼べない() -> None:
    with pytest.raises(TypeError):
        dedupe.sift()  # type: ignore[call-arg]
