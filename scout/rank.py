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
        raise NotImplementedError

    @property
    def reasons(self) -> Mapping[str, int]:
        raise NotImplementedError

    @property
    def summary(self) -> str:
        raise NotImplementedError


def rank(kept: Sequence[Kept], profile: Profile) -> Ranking:
    """**捨てるのはミュートと上限超えだけ。** 物差しの無いものは点を付けずに通す。"""
    raise NotImplementedError
