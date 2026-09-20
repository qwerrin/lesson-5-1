"""図版の定義と原本を突き合わせて、番号を割り付ける段。**まだ骨だけ。**

`tests/test_layout.py` を先に書いてある。ここにあるのは、
そのテストが1件ずつ落ちるようにするための**形だけ**で、中身はまだ無い。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from collect import Shot

#: 判定。**欠け・孤児・食い違いのどれかがあれば未完成。**
COMPLETE = "complete"
INCOMPLETE = "incomplete"


@dataclass(frozen=True)
class Figure:
    """図版ひとつぶんの定義（`figs.yaml` の1行）。

    **番号が2系統ある。** `article_no` は記事に並べる順、
    `shots_no` は `shots.py` を呼ぶ順で、*混ぜると必ず取り違える*。
    """

    key: str
    article_no: int
    shots_no: int | None
    slug: str
    caption: str
    claim: str
    expects: tuple[str, ...]


@dataclass(frozen=True)
class Placement:
    figure: Figure
    shot: Shot

    @property
    def filename(self) -> str:
        raise NotImplementedError


@dataclass(frozen=True)
class Layout:
    placements: tuple[Placement, ...]
    rejected: tuple[Shot, ...]
    missing: tuple[Figure, ...]
    orphans: tuple[str, ...]
    conflicts: Mapping[str, tuple[Shot, ...]]

    @property
    def by_shots_order(self) -> tuple[Placement, ...]:
        raise NotImplementedError

    @property
    def status(self) -> str:
        raise NotImplementedError


def plan(
    shots: Sequence[Shot],
    figures: Sequence[Figure],
    assignments: Mapping[str, str],
) -> Layout:
    """`assignments`（**ハッシュ → 図版のキー**）で割り付ける。"""
    raise NotImplementedError
