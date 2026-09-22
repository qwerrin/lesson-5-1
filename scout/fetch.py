"""取得元から記事の一覧を取る段。**まだ骨だけ。**

`tests/test_fetch.py` を先に書いてある。ここにあるのは、
そのテストが1件ずつ落ちるようにするための**形だけ**で、中身はまだ無い。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime

#: 取得元の種類。**取ってよい根拠は `sources.yaml` 側にある**（`DESIGN.md` 2-1）。
QIITA = "qiita"
ZENN = "zenn"

#: 取得の結果。**「0件」と「取れなかった」を分けるためだけに3値**（H2・M3）。
OK = "ok"
EMPTY = "empty"
FAILED = "failed"

#: Qiita の `per_page` 上限（公式ドキュメントに明記・H13）。
QIITA_MAX_PER_PAGE = 100


@dataclass(frozen=True)
class Response:
    status: int
    text: str
    headers: Mapping[str, str]


#: URL → 応答。**本物のネットワークに触らずに壊れ方を全部通せる**ように差し込み式。
Fetcher = Callable[[str], Response]


@dataclass(frozen=True)
class Source:
    name: str
    kind: str
    #: Qiita は検索式、Zenn は**トピックのスラッグ**。
    #: *共通の抽象を被せると両方の説明が嘘になる*（`DESIGN.md` 4-1）。
    query: str
    limit: int


@dataclass(frozen=True)
class Article:
    source: str
    url: str
    title: str
    #: **本文が無いことと、短いことは別。** 取れなければ `None`（H10）。
    body: str | None
    published_at: datetime
    updated_at: datetime | None
    author: str
    tags: tuple[str, ...]
    metrics: Mapping[str, int]

    @property
    def has_body(self) -> bool:
        raise NotImplementedError


@dataclass(frozen=True)
class SourceResult:
    source: str
    status: str
    articles: tuple[Article, ...]
    detail: str
    #: 総数。**分からないことを 0 と混ぜない**ので `None` を許す。
    total: int | None
    #: まだ先があるか（H1）。
    more: bool


@dataclass(frozen=True)
class Harvest:
    results: tuple[SourceResult, ...]

    @property
    def articles(self) -> tuple[Article, ...]:
        raise NotImplementedError

    @property
    def status(self) -> str:
        raise NotImplementedError


def harvest(
    sources: Sequence[Source],
    get: Fetcher,
    *,
    since: datetime | None = None,
) -> Harvest:
    """取得元ごとに取りに行く。**1つ死んでも、他は取る。全体は成功にしない。**"""
    raise NotImplementedError
