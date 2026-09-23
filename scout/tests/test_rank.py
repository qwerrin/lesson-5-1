"""scout/rank のテスト。**実装より先に書いた。**

`rank` は `dedupe` を通った記事に**読む価値の順**を付け、上限を超えたぶんを落とす段。

捨てる判断に、いいね数を使わない
--------------------------------------------------------------------------

**2026-09-23 の実測**（Qiita API・`created:>=2026-09-22`・新しい順に100件）:

============ ====================================================================
公開からの経過  **0.1〜4.6 時間**（864件のうち最新100件）
いいね         **0 が 96件・1 が 4件**・最大 1
ストック       0 が 92件・1 が 8件
タグ           **全件が1個以上**（5個が47件で最多）
============ ====================================================================

`fetch` は前回実行以降の記事しか取らないので、**来る記事はほぼ全部が生まれたて**。
いいね数でしきい値を切ると、*新しい記事を丸ごと捨てる*——`dedupe` で「重いほう」と
決めた M4（捨てすぎ）と同じ形になる。だから**いいね数は同点の並べ替えにだけ使う**。

*12〜24時間経った記事は測っていない*（最新100件が 4.6 時間ぶんしか遡らなかった）。

============ ====================================================================
根拠         ここで守ること
============ ====================================================================
H8           **捨てたものを消さない。** 理由と点の内訳つきで残す
M4           **点が低いことを理由に捨てない。** 捨てるのはミュートと上限超えだけ
2-4          **物差しが無いもの（Zenn）を0点にしない。** 点を付けずに通す
U8           **取得元が返した順を意味に使わない。** 並びは自分で決める
============ ====================================================================
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
import rank  # noqa: E402

WHEN = datetime(2026, 9, 22, 10, 0, 0)


def _qiita(
    url: str,
    *,
    title: str = "記事",
    tags: tuple[str, ...] = ("misc",),
    likes: int = 0,
    stocks: int = 0,
    published_at: datetime = WHEN,
) -> dedupe.Kept:
    article = fetch.Article(
        source="qiita",
        url=url,
        title=title,
        body="本文",
        published_at=published_at,
        updated_at=None,
        author="someone",
        tags=tags,
        metrics={"likes": likes, "stocks": stocks},
    )
    return dedupe.Kept(article=article, key=dedupe.normalize(url))


def _zenn(url: str, *, title: str = "見出し") -> dedupe.Kept:
    """**タグもいいね数も無い。** Zenn のフィードは返さない（`DESIGN.md` 2-4）。"""
    article = fetch.Article(
        source="zenn",
        url=url,
        title=title,
        body=None,
        published_at=WHEN,
        updated_at=None,
        author="someone",
        tags=(),
        metrics={},
    )
    return dedupe.Kept(article=article, key=dedupe.normalize(url))


def _profile(**overrides: object) -> rank.Profile:
    base: dict[str, object] = {
        "tags": frozenset({"python", "claudecode"}),
        "keywords": ("Claude",),
        "mute_tags": frozenset(),
        "mute_keywords": (),
        "cap": 10,
    }
    base.update(overrides)
    return rank.Profile(**base)  # type: ignore[arg-type]


def _urls(items: object) -> list[str]:
    """`picked`・`dropped` は `Kept` を包んでいる。**`unranked` は `Kept` そのもの。**"""
    return [
        (item.kept if hasattr(item, "kept") else item).article.url  # type: ignore[attr-defined]
        for item in items  # type: ignore[attr-defined]
    ]


# --------------------------------------------------------------------------
# 点の付け方
# --------------------------------------------------------------------------


def test_興味のタグに合うものが上に来る() -> None:
    got = rank.rank(
        [_qiita("https://q.com/other"), _qiita("https://q.com/py", tags=("Python",))],
        _profile(),
    )

    assert _urls(got.picked) == ["https://q.com/py", "https://q.com/other"]


def test_タグの大小を問わない() -> None:
    """Qiita のタグは `Python` と `python` が混ざる。**大小で取りこぼさない。**"""
    got = rank.rank([_qiita("https://q.com/x", tags=("PYTHON",))], _profile())

    assert got.picked[0].score == 1


def test_タグは完全一致で見る() -> None:
    """`python` に興味があっても `python3` は別のタグ。**部分一致で広げない。**"""
    got = rank.rank([_qiita("https://q.com/x", tags=("python3",))], _profile())

    assert got.picked[0].score == 0


def test_タイトルのキーワードでも点が付く() -> None:
    got = rank.rank([_qiita("https://q.com/x", title="claude を使ってみた")], _profile())

    assert got.picked[0].score == 1


def test_点の内訳を残す() -> None:
    """**H8 の裏側。** 月末に選別を見返す（発展アイデア1）には、*なぜその点か*が要る。"""
    got = rank.rank(
        [_qiita("https://q.com/x", title="Claude の話", tags=("python", "misc"))],
        _profile(),
    )

    assert got.picked[0].score == 2
    assert set(got.picked[0].hits) == {"tag:python", "keyword:Claude"}


def test_いいね数だけでは上に来ない() -> None:
    """**★ 実測の帰結。** 生まれたての記事はいいねが0——いいね数で順位を決めると、
    *興味に合う新しい記事が、合わない古い記事の下に沈む*。
    """
    popular = _qiita("https://q.com/popular", likes=50, stocks=30)
    relevant = _qiita("https://q.com/relevant", tags=("python",))

    got = rank.rank([popular, relevant], _profile())

    assert _urls(got.picked) == ["https://q.com/relevant", "https://q.com/popular"]


# --------------------------------------------------------------------------
# 並び（U8：取得元が返した順を意味に使わない）
# --------------------------------------------------------------------------


def test_同点はいいねとストックの多い順() -> None:
    got = rank.rank(
        [
            _qiita("https://q.com/few", likes=1),
            _qiita("https://q.com/many", likes=1, stocks=3),
        ],
        _profile(),
    )

    assert _urls(got.picked) == ["https://q.com/many", "https://q.com/few"]


def test_それも同じなら新しい順() -> None:
    got = rank.rank(
        [
            _qiita("https://q.com/old", published_at=datetime(2026, 9, 22, 8, 0)),
            _qiita("https://q.com/new", published_at=datetime(2026, 9, 22, 9, 0)),
        ],
        _profile(),
    )

    assert _urls(got.picked) == ["https://q.com/new", "https://q.com/old"]


def test_渡した順を変えても結果が変わらない() -> None:
    """**U8：`query` 併用時の Qiita の並びは明記されていない。** 自分で決める。

    *全部同点でも決まる*ように、最後は正規化した URL で並べる。
    """
    items = [_qiita(f"https://q.com/{name}") for name in ("c", "a", "b")]

    forward = rank.rank(items, _profile())
    backward = rank.rank(list(reversed(items)), _profile())

    assert _urls(forward.picked) == _urls(backward.picked) == [
        "https://q.com/a",
        "https://q.com/b",
        "https://q.com/c",
    ]


# --------------------------------------------------------------------------
# 上限（捨てるのは上限超えとミュートだけ）
# --------------------------------------------------------------------------


def test_上限を超えたぶんを落とす() -> None:
    items = [_qiita("https://q.com/py", tags=("python",)), _qiita("https://q.com/misc")]

    got = rank.rank(items, _profile(cap=1))

    assert _urls(got.picked) == ["https://q.com/py"]
    assert [d.reason for d in got.dropped] == [rank.OVER_CAP]


def test_上限で落としたものも点と内訳を残す() -> None:
    """**H8：捨てたものは記録されない、を止める。** 点が分からないと、上限が妥当か検証できない。"""
    items = [
        _qiita("https://q.com/a", tags=("python", "claudecode")),
        _qiita("https://q.com/b", tags=("python",)),
    ]

    got = rank.rank(items, _profile(cap=1))

    assert got.dropped[0].kept.article.url == "https://q.com/b"
    assert got.dropped[0].score == 1
    assert got.dropped[0].hits == ("tag:python",)


def test_点が0でも上限の内なら残す() -> None:
    """**★ M4。** 点が低いことは捨てる理由にならない。*その記事は二度と来ない。*"""
    got = rank.rank([_qiita("https://q.com/x", tags=("misc",))], _profile())

    assert _urls(got.picked) == ["https://q.com/x"]
    assert got.dropped == ()


def test_上限0を受け付けない() -> None:
    """**0 で回すと、全部を捨てて「上限どおり」と答える。**"""
    with pytest.raises(ValueError):
        rank.rank([_qiita("https://q.com/x")], _profile(cap=0))


def test_負の上限を受け付けない() -> None:
    with pytest.raises(ValueError):
        rank.rank([_qiita("https://q.com/x")], _profile(cap=-1))


# --------------------------------------------------------------------------
# 物差しが無いもの（2-4）
# --------------------------------------------------------------------------


def test_タグの無い記事に点を付けない() -> None:
    """**★ 測れないことを、価値が無いことにしない。**

    Zenn はタグもいいね数も返さない。点を付けると全部0点になり、
    *上限を超えた日に黙って全部落ちる*。
    """
    got = rank.rank([_zenn("https://zenn.dev/a/articles/x")], _profile())

    assert _urls(got.unranked) == ["https://zenn.dev/a/articles/x"]
    assert got.picked == ()
    assert got.dropped == ()


def test_点を付けない記事は上限を使わない() -> None:
    items = [
        _zenn("https://zenn.dev/a/articles/x"),
        _zenn("https://zenn.dev/a/articles/y"),
        _qiita("https://q.com/x"),
    ]

    got = rank.rank(items, _profile(cap=1))

    assert _urls(got.picked) == ["https://q.com/x"]
    assert len(got.unranked) == 2
    assert got.dropped == ()


def test_点を付けない記事は渡された順のまま() -> None:
    """並べる物差しが無いので、**並べ替えない**。フィードの新着順を壊さない。"""
    urls = ["https://zenn.dev/c", "https://zenn.dev/a", "https://zenn.dev/b"]

    got = rank.rank([_zenn(u) for u in urls], _profile())

    assert _urls(got.unranked) == urls


# --------------------------------------------------------------------------
# ミュート
# --------------------------------------------------------------------------


def test_ミュートのタグで落とす() -> None:
    got = rank.rank(
        [_qiita("https://q.com/x", tags=("ポエム",))],
        _profile(mute_tags=frozenset({"ポエム"})),
    )

    assert got.picked == ()
    assert [d.reason for d in got.dropped] == [rank.MUTED]
    assert got.dropped[0].detail == "tag:ポエム"


def test_ミュートの語でタイトルから落とす() -> None:
    """**点を付けない記事にも効く。** Zenn にもタイトルはある。"""
    got = rank.rank(
        [_zenn("https://zenn.dev/x", title="【PR】広告記事")],
        _profile(mute_keywords=("【pr】",)),
    )

    assert got.unranked == ()
    assert [d.reason for d in got.dropped] == [rank.MUTED]


def test_興味にもミュートにも当たればミュート() -> None:
    """**理由は1つに決める。** 本人が「見たくない」と書いたほうを優先する。"""
    got = rank.rank(
        [_qiita("https://q.com/x", tags=("python", "ポエム"))],
        _profile(mute_tags=frozenset({"ポエム"})),
    )

    assert [d.reason for d in got.dropped] == [rank.MUTED]


def test_ミュートしたものは上限を使わない() -> None:
    items = [
        _qiita("https://q.com/muted", tags=("python", "ポエム")),
        _qiita("https://q.com/x"),
    ]

    got = rank.rank(items, _profile(cap=1, mute_tags=frozenset({"ポエム"})))

    assert _urls(got.picked) == ["https://q.com/x"]


def test_空のミュート語で全部を落とさない() -> None:
    """**空文字はどのタイトルにも含まれる。**

    設定ファイルに空の行が1つ入るだけで、*その日の記事が全部消える*
    （`dedupe` の「台帳の空行」と同じ形）。
    """
    got = rank.rank(
        [_qiita("https://q.com/x"), _zenn("https://zenn.dev/y")],
        _profile(mute_keywords=("", "  ")),
    )

    assert got.dropped == ()


def test_空の興味語で全部に点を付けない() -> None:
    got = rank.rank([_qiita("https://q.com/x")], _profile(keywords=("",)))

    assert got.picked[0].score == 0


# --------------------------------------------------------------------------
# 件数
# --------------------------------------------------------------------------


def test_どの記事も必ずどこか1つに入る() -> None:
    """**入れた件数と出た件数を合わせる。** 合わなければ、どこかで黙って消えている。"""
    items = [
        _qiita("https://q.com/a", tags=("python",)),
        _qiita("https://q.com/b"),
        _qiita("https://q.com/c", tags=("ポエム",)),
        _zenn("https://zenn.dev/d"),
    ]

    got = rank.rank(items, _profile(cap=1, mute_tags=frozenset({"ポエム"})))

    assert got.total == 4
    assert (len(got.picked), len(got.unranked), len(got.dropped)) == (1, 1, 2)


def test_件数と理由ごとの内訳を出す() -> None:
    items = [
        _qiita("https://q.com/a", tags=("python",)),
        _qiita("https://q.com/b"),
        _qiita("https://q.com/c", tags=("ポエム",)),
        _zenn("https://zenn.dev/d"),
    ]

    got = rank.rank(items, _profile(cap=1, mute_tags=frozenset({"ポエム"})))

    assert got.reasons == {rank.MUTED: 1, rank.OVER_CAP: 1}
    assert "4 件中 1 件を選んだ" in got.summary
    assert "点を付けなかった 1 件" in got.summary
    assert rank.OVER_CAP in got.summary


def test_記事が0件でも落ちない() -> None:
    """**0件は異常ではない。** 前回から新しい記事が無い日は普通にある。"""
    got = rank.rank([], _profile())

    assert (got.picked, got.unranked, got.dropped) == ((), (), ())
    assert "0 件中 0 件" in got.summary
