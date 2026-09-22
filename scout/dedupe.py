"""URL を正規化して、台帳と vault に既にあるものを落とす段。**まだ骨だけ。**

`tests/test_dedupe.py` を先に書いてある。ここにあるのは、
そのテストが1件ずつ落ちるようにするための**形だけ**で、中身はまだ無い。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

from fetch import Article

#: 捨てた理由。**1件につき1つに決める**——並べると、どこを直せばよいか分からない。
SAME_BATCH = "same_batch"
SEEN_BEFORE = "seen_before"
IN_VAULT = "in_vault"

#: 落としてよいと分かっている追跡パラメータ**だけ**。
#: *知らないものは残す*——落としすぎると、その記事は二度と来ない（M4）。
TRACKING_PARAMS = frozenset(
    {
        "utm_source",
        "utm_medium",
        "utm_campaign",
        "utm_term",
        "utm_content",
        "fbclid",
        "gclid",
    }
)


@dataclass(frozen=True)
class Kept:
    article: Article
    key: str


@dataclass(frozen=True)
class Dropped:
    article: Article
    key: str
    reason: str


@dataclass(frozen=True)
class Sifted:
    kept: tuple[Kept, ...]
    dropped: tuple[Dropped, ...]

    @property
    def total(self) -> int:
        raise NotImplementedError

    @property
    def reasons(self) -> Mapping[str, int]:
        raise NotImplementedError

    @property
    def summary(self) -> str:
        raise NotImplementedError


def normalize(url: str) -> str:
    """**迷ったら別物として扱う。** 落とすのは既知の追跡パラメータだけ。"""
    raise NotImplementedError


def sift(
    articles: Sequence[Article],
    *,
    seen: Iterable[str] = (),
    known: Iterable[str] = (),
) -> Sifted:
    """台帳（`seen`）と vault（`known`）に無いものだけを残す。**捨てたものも返す。**"""
    raise NotImplementedError
