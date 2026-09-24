"""`dedupe` を通った記事に読む価値の順を付け、上限を超えたぶんを落とす段。

捨てる判断に、いいね数を使わない
--------------------------------------------------------------------------

**来る記事はほぼ全部が生まれたて。** 2026-09-23 の実測で、公開 0.1〜4.6 時間の
100件のうち **いいね0 が96件**・最大1。しきい値を切ると*新しい記事を丸ごと捨てる*（M4）。
いいね数は**同点の並べ替えにだけ**使う。

捨てるのは2つだけ
--------------------------------------------------------------------------

=============== ================================================================
`MUTED`         本人が「見たくない」と書いたタグ・語に当たった
`OVER_CAP`      上限を超えた（要約に回す件数を抑える・U7）
=============== ================================================================

**点が低いことは捨てる理由にならない。** *その記事は二度と来ない。*

物差しが無いものを0点にしない
--------------------------------------------------------------------------

Zenn はタグもいいね数も返さない（`DESIGN.md` 2-4）。0点にすると
*上限を超えた日に黙って全部落ちる*。だから**点を付けずに通す**（`unranked`）。
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from dedupe import Kept

#: 捨てた理由。**1件につき1つに決める。**
MUTED = "muted"
OVER_CAP = "over_cap"


@dataclass(frozen=True)
class Profile:
    tags: frozenset[str]
    keywords: tuple[str, ...]
    mute_tags: frozenset[str]
    mute_keywords: tuple[str, ...]
    cap: int


@dataclass(frozen=True)
class Scored:
    kept: Kept
    score: int
    hits: tuple[str, ...]


@dataclass(frozen=True)
class Rejected:
    kept: Kept
    reason: str
    detail: str
    score: int | None
    hits: tuple[str, ...]


@dataclass(frozen=True)
class Ranking:
    picked: tuple[Scored, ...]
    unranked: tuple[Kept, ...]
    dropped: tuple[Rejected, ...]

    @property
    def total(self) -> int:
        return len(self.picked) + len(self.unranked) + len(self.dropped)

    @property
    def reasons(self) -> Mapping[str, int]:
        return dict(Counter(d.reason for d in self.dropped))

    @property
    def summary(self) -> str:
        """**件数を必ず出す。** 点を付けなかったぶんも、捨てたぶんとは別に数える。"""
        breakdown = "・".join(f"{r} {n}" for r, n in sorted(self.reasons.items()))
        return (
            f"{self.total} 件中 {len(self.picked)} 件を選んだ"
            f"／点を付けなかった {len(self.unranked)} 件"
            f"／落とした {len(self.dropped)} 件（{breakdown or 'なし'}）"
        )


def rank(kept: Sequence[Kept], profile: Profile) -> Ranking:
    """**捨てるのはミュートと上限超えだけ。** 物差しの無いものは点を付けずに通す。"""
    if profile.cap < 1:
        raise ValueError(f"上限が {profile.cap}。0 以下で回すと、全部を捨てて「上限どおり」と答える")

    mute_tags = _fold_set(profile.mute_tags)
    mute_words = _fold_words(profile.mute_keywords)
    tags = _fold_set(profile.tags)
    words = _fold_words(profile.keywords)

    scored: list[Scored] = []
    unranked: list[Kept] = []
    dropped: list[Rejected] = []

    for item in kept:
        # **ミュートを点より先に見る。** 本人が「見たくない」と書いたほうを優先し、
        # 上限の枠も使わせない。
        muted = _muted(item, mute_tags=mute_tags, mute_words=mute_words)
        if muted is not None:
            dropped.append(Rejected(kept=item, reason=MUTED, detail=muted, score=None, hits=()))
            continue
        if not item.article.tags:
            unranked.append(item)
            continue
        hits = _hits(item, tags=tags, words=words)
        scored.append(Scored(kept=item, score=len(hits), hits=hits))

    scored.sort(key=_order)
    over = [
        Rejected(kept=s.kept, reason=OVER_CAP, detail="", score=s.score, hits=s.hits)
        for s in scored[profile.cap :]
    ]
    return Ranking(
        picked=tuple(scored[: profile.cap]),
        unranked=tuple(unranked),
        dropped=tuple(dropped + over),
    )


def _muted(
    item: Kept, *, mute_tags: set[str], mute_words: list[tuple[str, str]]
) -> str | None:
    for tag in item.article.tags:
        if tag.casefold() in mute_tags:
            return f"tag:{tag}"
    title = item.article.title.casefold()
    for word, folded in mute_words:
        if folded in title:
            return f"keyword:{word}"
    return None


def _hits(item: Kept, *, tags: set[str], words: list[tuple[str, str]]) -> tuple[str, ...]:
    """**点の内訳。** タグは完全一致（`python` と `python3` は別物）、語はタイトルの部分一致。"""
    # **照合は大小を揃えるが、記録は揃えない。** 取得元が返したとおりに残す。
    found = [f"tag:{tag}" for tag in item.article.tags if tag.casefold() in tags]
    title = item.article.title.casefold()
    found += [f"keyword:{word}" for word, folded in words if folded in title]
    return tuple(found)


def _order(s: Scored) -> tuple[int, int, float, str]:
    """**取得元が返した順を意味に使わない**（U8）。全部同点でも決まるよう、最後は URL。"""
    metrics = s.kept.article.metrics
    popularity = metrics.get("likes", 0) + metrics.get("stocks", 0)
    return (-s.score, -popularity, -s.kept.article.published_at.timestamp(), s.kept.key)


def _fold_set(values: frozenset[str]) -> set[str]:
    """空のタグは捨てなくてよい——タグは完全一致なので、*空は何にも当たらない*。"""
    return {v.strip().casefold() for v in values}


def _fold_words(values: tuple[str, ...]) -> list[tuple[str, str]]:
    """**空の語を捨てる。** 空文字はどのタイトルにも含まれ、*その日の記事が全部消える*。

    元の表記も持つのは、内訳に**本人が書いたとおりの語**を残すため。
    """
    return [(v.strip(), v.strip().casefold()) for v in values if v.strip()]
