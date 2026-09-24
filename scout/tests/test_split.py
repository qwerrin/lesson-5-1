"""scout/split のテスト。**実装より先に書いた。**

`split` は `rank` が残した記事を、**要約へ回すもの**と**見出しだけ出すもの**に分ける段。

なぜ独立した段にするのか（`DESIGN.md` 第5章）
--------------------------------------------------------------------------

「本文があるか」で経路が分かれることを、コードの奥ではなく**流れの図に出す**。
ここを暗黙にすると、*将来 Zenn に本文があると思い込んだ誰か（＝半年後の自分）が
要約を通してしまう*。だから本文の有無だけでなく、**要約してよい取得元か**も見る
——2-4 の表の「要約 ○／×」の列を、そのまま設定にしたもの。

短い本文をどう扱うか（2026-09-23 の実測）
--------------------------------------------------------------------------

Qiita の最新100件で、本文が無いものは **0件**。最短は **226字**で、
*切れた本文ではなく、短い完結した記事*だった（H10：長さでは区別できない）。

============ ====================================================================
p5 / p10     571字 / 1337字
中央値        5471字
500字未満     **4件**
============ ====================================================================

しきい値の誤りの向きは:

- **高すぎる** → 見出しだけで出る。*Inbox には載るので、捨てたことにはならない*
- **低すぎる** → ほぼ空の本文を「要約」する。*それは要約ではなく推測*（H4）

**安全側は高いほう。500字に置く。**

============ ====================================================================
根拠         ここで守ること
============ ====================================================================
2-4          **照合できないものは要約しない。** 要約してよい取得元だけを通す
H10・M5      本文が無い／薄いものを要約へ回さない
6-1          「確認不能」を理由つきで残す（見出しだけ出す理由を1件につき1つ）
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
import split  # noqa: E402

WHEN = datetime(2026, 9, 22, 10, 0, 0)
LONG = "本文" * 300  # 600字。既定のしきい値（500字）を越える
ALLOWED = frozenset({"qiita"})


def _kept(url: str, *, source: str = "qiita", body: str | None = LONG) -> dedupe.Kept:
    article = fetch.Article(
        source=source,
        url=url,
        title="記事",
        body=body,
        published_at=WHEN,
        updated_at=None,
        author="someone",
        tags=("misc",) if source != "zenn" else (),
        metrics={},
    )
    return dedupe.Kept(article=article, key=dedupe.normalize(url))


def _scored(url: str, *, score: int = 0, **kwargs: object) -> rank.Scored:
    return rank.Scored(kept=_kept(url, **kwargs), score=score, hits=())  # type: ignore[arg-type]


def _ranking(
    picked: tuple[rank.Scored, ...] = (),
    unranked: tuple[dedupe.Kept, ...] = (),
    dropped: tuple[rank.Rejected, ...] = (),
) -> rank.Ranking:
    return rank.Ranking(picked=picked, unranked=unranked, dropped=dropped)


def _urls(items: object) -> list[str]:
    return [item.kept.article.url for item in items]  # type: ignore[attr-defined]


# --------------------------------------------------------------------------
# 経路の分かれ目
# --------------------------------------------------------------------------


def test_本文があれば要約へ回す() -> None:
    got = split.split(_ranking(picked=(_scored("https://q.com/x"),)), summarizable=ALLOWED)

    assert _urls(got.summarize) == ["https://q.com/x"]
    assert got.headline == ()


def test_本文が無ければ見出しだけ() -> None:
    """**H10：本文が無いものを要約へ回さない。**"""
    got = split.split(
        _ranking(picked=(_scored("https://q.com/x", body=None),)), summarizable=ALLOWED
    )

    assert got.summarize == ()
    assert [h.reason for h in got.headline] == [split.NO_BODY]


def test_点を付けなかった記事は見出しだけ() -> None:
    """Zenn は `rank` で点を付けずに通した記事。**本文も無い。**"""
    got = split.split(
        _ranking(unranked=(_kept("https://zenn.dev/x", source="zenn", body=None),)),
        summarizable=ALLOWED,
    )

    assert got.summarize == ()
    assert _urls(got.headline) == ["https://zenn.dev/x"]


def test_要約してよい取得元でなければ本文があっても見出しだけ() -> None:
    """**★ 半年後の自分を止める。**

    Zenn の本文を取るように `fetch` を変えても、*規約が「無断で転載または二次配布」を
    禁じていること*は変わらない（`DESIGN.md` 2-1）。本文の有無だけで通すと、
    **取得元を直した日に、要約の段が黙って開く。**
    """
    got = split.split(
        _ranking(picked=(_scored("https://zenn.dev/x", source="zenn", body=LONG),)),
        summarizable=ALLOWED,
    )

    assert got.summarize == ()
    assert [h.reason for h in got.headline] == [split.NOT_SUMMARIZABLE]


def test_取得元を先に見る() -> None:
    """**理由は1つに決める。** 取得元で止まるなら、本文の有無は直し方を変えない。"""
    got = split.split(
        _ranking(unranked=(_kept("https://zenn.dev/x", source="zenn", body=None),)),
        summarizable=ALLOWED,
    )

    assert [h.reason for h in got.headline] == [split.NOT_SUMMARIZABLE]


def test_要約してよい取得元が空なら受け付けない() -> None:
    """**空で回すと、1件も要約せずに「異常なし」と答える**（`fetch` の取得元0件と同じ形）。"""
    with pytest.raises(ValueError):
        split.split(_ranking(picked=(_scored("https://q.com/x"),)), summarizable=frozenset())


# --------------------------------------------------------------------------
# 薄い本文（M5）
# --------------------------------------------------------------------------


def test_既定のしきい値は500字() -> None:
    """**実測の帰結を固定する。** 動かすなら、測り直してから（このファイルの冒頭）。"""
    assert split.MIN_BODY == 500


def test_しきい値ちょうどは要約へ回す() -> None:
    got = split.split(
        _ranking(picked=(_scored("https://q.com/x", body="あ" * 500),)), summarizable=ALLOWED
    )

    assert _urls(got.summarize) == ["https://q.com/x"]


def test_しきい値に1字足りなければ見出しだけ() -> None:
    got = split.split(
        _ranking(picked=(_scored("https://q.com/x", body="あ" * 499),)), summarizable=ALLOWED
    )

    assert got.summarize == ()
    assert [h.reason for h in got.headline] == [split.TOO_SHORT]


def test_空白だけの本文を要約へ回さない() -> None:
    """**`has_body` は空文字を弾くが、空白は通す。** 空白だけの本文は、本文ではない。"""
    got = split.split(
        _ranking(picked=(_scored("https://q.com/x", body=" \n" * 400),)), summarizable=ALLOWED
    )

    assert [h.reason for h in got.headline] == [split.TOO_SHORT]


def test_前後の空白は長さに数えない() -> None:
    got = split.split(
        _ranking(picked=(_scored("https://q.com/x", body="\n" * 100 + "あ" * 499),)),
        summarizable=ALLOWED,
    )

    assert [h.reason for h in got.headline] == [split.TOO_SHORT]


def test_しきい値を渡せる() -> None:
    got = split.split(
        _ranking(picked=(_scored("https://q.com/x", body="あ" * 10),)),
        summarizable=ALLOWED,
        min_body=10,
    )

    assert _urls(got.summarize) == ["https://q.com/x"]


def test_しきい値0を受け付けない() -> None:
    """**0 にすると空白1字でも「本文」になる。**"""
    with pytest.raises(ValueError):
        split.split(_ranking(), summarizable=ALLOWED, min_body=0)


# --------------------------------------------------------------------------
# 並びと記録
# --------------------------------------------------------------------------


def test_要約へ回すものは選んだ順のまま() -> None:
    """**`rank` が決めた順を壊さない。** 並べ直すなら、それは `rank` の仕事。"""
    urls = ["https://q.com/c", "https://q.com/a", "https://q.com/b"]

    got = split.split(_ranking(picked=tuple(_scored(u) for u in urls)), summarizable=ALLOWED)

    assert _urls(got.summarize) == urls


def test_見出しは選んだものが先で点を付けなかったものが後() -> None:
    got = split.split(
        _ranking(
            picked=(
                _scored("https://q.com/short-b", body="短い"),
                _scored("https://q.com/short-a", body="短い"),
            ),
            unranked=(
                _kept("https://zenn.dev/y", source="zenn", body=None),
                _kept("https://zenn.dev/x", source="zenn", body=None),
            ),
        ),
        summarizable=ALLOWED,
    )

    assert _urls(got.headline) == [
        "https://q.com/short-b",
        "https://q.com/short-a",
        "https://zenn.dev/y",
        "https://zenn.dev/x",
    ]


def test_見出しにも点を残す() -> None:
    """**点が分からないと、「短いから要約しなかった」記事を見返せない。**"""
    got = split.split(
        _ranking(
            picked=(_scored("https://q.com/x", score=2, body="短い"),),
            unranked=(_kept("https://zenn.dev/y", source="zenn", body=None),),
        ),
        summarizable=ALLOWED,
    )

    assert [h.score for h in got.headline] == [2, None]


def test_捨てたものは運ばない() -> None:
    """`rank` が落としたものは `rank` の記録に残っている。**ここで生き返らせない。**"""
    rejected = rank.Rejected(
        kept=_kept("https://q.com/dropped"), reason=rank.OVER_CAP, detail="", score=0, hits=()
    )

    got = split.split(_ranking(dropped=(rejected,)), summarizable=ALLOWED)

    assert (got.summarize, got.headline) == ((), ())


# --------------------------------------------------------------------------
# 件数
# --------------------------------------------------------------------------


def test_どの記事も必ずどちらか1つに入る() -> None:
    """**入れた件数と出た件数を合わせる。** 合わなければ、どこかで黙って消えている。"""
    got = split.split(
        _ranking(
            picked=(
                _scored("https://q.com/a"),
                _scored("https://q.com/b", body="短い"),
                _scored("https://q.com/c", body=None),
            ),
            unranked=(_kept("https://zenn.dev/d", source="zenn", body=None),),
        ),
        summarizable=ALLOWED,
    )

    assert got.total == 4
    assert (len(got.summarize), len(got.headline)) == (1, 3)


def test_件数と理由ごとの内訳を出す() -> None:
    got = split.split(
        _ranking(
            picked=(
                _scored("https://q.com/a"),
                _scored("https://q.com/b", body="短い"),
                _scored("https://q.com/c", body=None),
            ),
            unranked=(_kept("https://zenn.dev/d", source="zenn", body=None),),
        ),
        summarizable=ALLOWED,
    )

    assert got.reasons == {split.TOO_SHORT: 1, split.NO_BODY: 1, split.NOT_SUMMARIZABLE: 1}
    assert "4 件中 1 件を要約へ" in got.summary
    assert "見出しだけ 3 件" in got.summary
    assert split.NOT_SUMMARIZABLE in got.summary


def test_記事が0件でも落ちない() -> None:
    """**0件は異常ではない。** 前回から新しい記事が無い日は普通にある。"""
    got = split.split(_ranking(), summarizable=ALLOWED)

    assert (got.summarize, got.headline) == ((), ())
    assert "0 件中 0 件" in got.summary
