"""URL を正規化して、台帳と vault に既にあるものを落とす段。

なぜ必須の部品なのか
--------------------------------------------------------------------------

Qiita の `created:>=` は**日付までしか指定できない**（H11・2026-09-19 実測）。
前回の実行と同じ日に走らせれば、**同じ記事を必ず再取得する**。
*任意の最適化ではなく、無いと毎日同じものが積み上がる。*

どちらの誤りが重いか
--------------------------------------------------------------------------

**M4（捨てすぎ）が H5（重複を持ってくる）より重い。**

重複は `01_Inbox/` に1行余計に出るだけで、読む人が飛ばせば済む。
捨てすぎると**その記事は二度と来ない**——Qiita の検索は期間で絞るので、
*一度飛ばした日には戻らない*。

だから正規化の方針は **「迷ったら別物として扱う」**:

=========================== ====================================================
落とす                       フラグメント／末尾スラッシュ／**既知の**追跡パラメータ
揃える                       スキームとホストの大小／残ったクエリの並び
**落とさない・揃えない**      パスの大小／`www.`／**知らないクエリ**
=========================== ====================================================

`www.` に踏み込まないのは、落として困る場合が理論上あるから。
*実際の取得元（Qiita・Zenn）はどちらも使っていない*ので、いま差は出ない。

捨てたものを消さない
--------------------------------------------------------------------------

H8。**何を捨てたかが残らないと、選別が正しいか後から検証できない。**
理由は**1件につき1つ**に決める——並べると、読む側がどこを直せばよいか分からない。
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlsplit, urlunsplit

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
        return len(self.kept) + len(self.dropped)

    @property
    def reasons(self) -> Mapping[str, int]:
        return dict(Counter(d.reason for d in self.dropped))

    @property
    def summary(self) -> str:
        """**件数を必ず出す。** 「重複を除きました」では、何件見たか分からない。"""
        breakdown = "・".join(f"{r} {n}" for r, n in sorted(self.reasons.items()))
        return (
            f"{self.total} 件中 {len(self.kept)} 件を残した"
            f"／落とした {len(self.dropped)} 件（{breakdown or 'なし'}）"
        )


def normalize(url: str) -> str:
    """**迷ったら別物として扱う。** 落とすのは既知の追跡パラメータだけ。

    正規化できない文字列は**そのまま返す**——「壊れている」を「重複」に
    化けさせない。*理由が変われば、直し方も変わる。*
    """
    text = url.strip()
    if not text:
        return text
    try:
        parts = urlsplit(text)
    except ValueError:
        return text
    if not parts.scheme or not parts.netloc:
        return text

    path = parts.path
    if len(path) > 1 and path.endswith("/"):
        # 根の `/` は残す。落としても得が無く、別物に見えるだけ。
        path = path.rstrip("/")

    kept = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if key.lower() not in TRACKING_PARAMS
    ]
    # **並びだけが違うものを別物にしない。** 意味は同じ。
    query = "&".join(f"{key}={value}" for key, value in sorted(kept))

    return urlunsplit(
        (
            # **`urlsplit` がスキームを既に小文字にしている。** ここで重ねても
            # 何も変わらない——2026-09-23 のミューテーションで素通りして分かった。
            parts.scheme,
            parts.netloc.lower(),
            path,  # **パスの大小は揃えない**（大小が意味を持つ URL がある）
            query,
            "",  # フラグメントは同じページの中の位置であって、別の記事ではない
        )
    )


def sift(
    articles: Sequence[Article],
    *,
    seen: Iterable[str] = (),
    known: Iterable[str] = (),
) -> Sifted:
    """台帳（`seen`）と vault（`known`）に無いものだけを残す。**捨てたものも返す。**"""
    before = _keys(seen)
    in_vault = _keys(known)

    kept: list[Kept] = []
    dropped: list[Dropped] = []
    batch: set[str] = set()

    for article in articles:
        key = normalize(article.url)
        reason = _why(key, before=before, in_vault=in_vault, batch=batch)
        if reason is None:
            batch.add(key)
            kept.append(Kept(article=article, key=key))
        else:
            dropped.append(Dropped(article=article, key=key, reason=reason))

    return Sifted(kept=tuple(kept), dropped=tuple(dropped))


def _why(key: str, *, before: set[str], in_vault: set[str], batch: set[str]) -> str | None:
    """**理由の順番を決めておく。** 複数当たっても、報告するのは1つ。

    台帳を先にするのは、**こちらが書いた記録のほうが直しやすい**から。
    """
    if key in before:
        return SEEN_BEFORE
    if key in in_vault:
        return IN_VAULT
    if key in batch:
        return SAME_BATCH
    return None


def _keys(urls: Iterable[str]) -> set[str]:
    """**壊れたものを鍵にしない。** 台帳が壊れていても、まともな記事を巻き込まない。"""
    keys = set()
    for url in urls:
        key = normalize(url)
        if key:
            keys.add(key)
    return keys
