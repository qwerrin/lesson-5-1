"""取得元から記事の一覧を取る段。

この段で守ること
--------------------------------------------------------------------------

**「取れなかったことを、無かったことにしない」の1点に尽きる。**

提供側は「無い」を成功として返す——HTTP 200 ＋ 空配列で、404 も例外も出ない
（教訓 `absence-can-return-success`）。だから結果を3値で持つ:

======== ====================================================================
`OK`     1件以上取れた
`EMPTY`  **取りに行けて、0件だった**
`FAILED` **取りに行けなかった**（非200・壊れた応答・例外）
======== ====================================================================

そして**取得元ごとに持つ**。1つ死んでも他は取りに行くが、
*他が生きていれば全体は成功に見える*ので、**全体は成功にしない**（M2）。

日付の物差しは1本
--------------------------------------------------------------------------

Qiita は ISO 8601（`2026-09-20T12:34:56+09:00`）、
Zenn は RFC822 の GMT（`Sat, 19 Sep 2026 08:26:20 GMT`）。
**どちらもローカルの素の `datetime` に揃えてから外へ出す**
（教訓 `one-date-basis-per-output`）。

本文が無いことを、短いことと混ぜない
--------------------------------------------------------------------------

Zenn のフィードの `description` は **300字で打ち切られる**（2026-09-19 実測）。
一部だけの本文を「本文」と呼ぶと、*要約の物差しにしてしまう*。
だから `body` は `None` にする——**短い本文と切られた本文は、長さでは区別できない。**

HTTP は差し込み式
--------------------------------------------------------------------------

`guard` の `ocr`・`verify_figs` の `reader` と同じ。
本物のネットワークに触らずに、取得元ごとの壊れ方を全部通せる。
"""

from __future__ import annotations

import json
import xml.etree.ElementTree as ElementTree
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from email.utils import parsedate_to_datetime
from urllib.parse import quote

#: 取得元の種類。**取ってよい根拠は `sources.yaml` 側にある**（`DESIGN.md` 2-1）。
QIITA = "qiita"
ZENN = "zenn"

#: 取得の結果。**「0件」と「取れなかった」を分けるためだけに3値**（H2・M3）。
OK = "ok"
EMPTY = "empty"
FAILED = "failed"

#: Qiita の `per_page` 上限（公式ドキュメントに明記・H13）。
QIITA_MAX_PER_PAGE = 100

#: Zenn の名前空間（`dc:creator`）。
DC = "{http://purl.org/dc/elements/1.1/}"


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
        """後段（`split`）の分かれ目。**空文字を本文として通さない。**"""
        return bool(self.body)


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
        return tuple(a for result in self.results for a in result.articles)

    @property
    def status(self) -> str:
        """**1つでも取りに行けなかったら、全体は成功にしない**（M2）。

        *他が生きていれば全体は成功に見える*のを止めるためだけの規則。
        """
        if any(result.status == FAILED for result in self.results):
            return FAILED
        if all(result.status == EMPTY for result in self.results):
            return EMPTY
        return OK


def harvest(
    sources: Sequence[Source],
    get: Fetcher,
    *,
    since: datetime | None = None,
) -> Harvest:
    """取得元ごとに取りに行く。**1つ死んでも、他は取る。全体は成功にしない。**"""
    if not sources:
        raise ValueError("取得元が0件。空で回すと、何も取らずに「異常なし」と答える")

    results = [_one(source, get, since=since) for source in sources]
    return Harvest(results=tuple(results))


def _one(source: Source, get: Fetcher, *, since: datetime | None) -> SourceResult:
    url = _url(source, since=since)
    try:
        reply = get(url)
    except Exception as error:  # noqa: BLE001
        # **握らないし、道連れにもしない。** この取得元の失敗として残して続ける。
        return _failed(source, f"{type(error).__name__}: {error}")

    if reply.status != 200:
        return _failed(source, f"HTTP {reply.status}")

    try:
        articles, total = _parse(source, reply)
    except Exception as error:  # noqa: BLE001
        return _failed(source, f"応答を読めない（{type(error).__name__}: {error}）")

    return SourceResult(
        source=source.name,
        status=OK if articles else EMPTY,
        articles=tuple(articles),
        detail="",
        total=total,
        more=total is not None and total > len(articles),
    )


def _failed(source: Source, detail: str) -> SourceResult:
    return SourceResult(
        source=source.name,
        status=FAILED,
        articles=(),
        detail=detail,
        total=None,
        more=False,
    )


def _url(source: Source, *, since: datetime | None) -> str:
    if source.kind == QIITA:
        query = source.query
        if since is not None:
            # **日付までしか指定できない**（H11）。同日の再取得は必ず起きるので、
            # `dedupe` は任意の最適化ではなく必須の部品になる。
            query = f"{query} created:>={since:%Y-%m-%d}"
        per_page = min(source.limit, QIITA_MAX_PER_PAGE)
        return f"https://qiita.com/api/v2/items?page=1&per_page={per_page}&query={quote(query)}"
    if source.kind == ZENN:
        return f"https://zenn.dev/topics/{quote(source.query)}/feed"
    raise ValueError(f"知らない取得元の種類: {source.kind}")


def _parse(source: Source, reply: Response) -> tuple[list[Article], int | None]:
    if source.kind == QIITA:
        return _parse_qiita(source, reply)
    if source.kind == ZENN:
        return _parse_zenn(source, reply), None
    raise ValueError(f"知らない取得元の種類: {source.kind}")


def _parse_qiita(source: Source, reply: Response) -> tuple[list[Article], int | None]:
    payload = json.loads(reply.text)
    if not isinstance(payload, list):
        raise ValueError("配列ではない応答")

    articles = [
        Article(
            source=source.name,
            url=item["url"],
            title=item["title"],
            body=item.get("body"),
            published_at=_local(datetime.fromisoformat(item["created_at"])),
            updated_at=(
                _local(datetime.fromisoformat(item["updated_at"]))
                if item.get("updated_at")
                else None
            ),
            author=(item.get("user") or {}).get("id", ""),
            tags=tuple(tag["name"] for tag in item.get("tags") or ()),
            metrics={
                "likes": item.get("likes_count", 0),
                "stocks": item.get("stocks_count", 0),
            },
        )
        for item in payload
    ]
    raw_total = reply.headers.get("Total-Count")
    # **返らない取得元もある。** 分からないことを 0 にしない。
    total = int(raw_total) if raw_total is not None else None
    return articles, total


def _parse_zenn(source: Source, reply: Response) -> list[Article]:
    root = ElementTree.fromstring(reply.text)
    articles = []
    for item in root.iter("item"):
        articles.append(
            Article(
                source=source.name,
                url=_text(item, "link"),
                title=_text(item, "title"),
                # **300字で切られたものを本文と呼ばない**（H10）。
                body=None,
                published_at=_local(parsedate_to_datetime(_text(item, "pubDate"))),
                updated_at=None,
                author=_text(item, f"{DC}creator"),
                tags=(),
                metrics={},
            )
        )
    return articles


def _text(item: ElementTree.Element, tag: str) -> str:
    found = item.find(tag)
    return (found.text or "").strip() if found is not None else ""


def _local(value: datetime) -> datetime:
    """**物差しを1本にする。** 時差つきで入ってきたものを、ローカルの素の日時へ。"""
    if value.tzinfo is None:
        return value
    return value.astimezone().replace(tzinfo=None)
