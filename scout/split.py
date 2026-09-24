"""`rank` が残した記事を、**要約へ回すもの**と**見出しだけ出すもの**に分ける段。

なぜ独立した段にするのか
--------------------------------------------------------------------------

「本文があるか」で経路が分かれることを、**流れの図に出す**（`DESIGN.md` 第5章）。
本文の有無だけで通すと、*取得元を直した日に、要約の段が黙って開く*。
だから**要約してよい取得元か**も見る（2-4 の「要約 ○／×」の列）。

薄い本文は要約しない
--------------------------------------------------------------------------

Qiita の本文は切れていない（2026-09-23 実測・最短 226字の完結した記事）。
しきい値は**高すぎれば見出しだけで出るだけ**、低すぎれば*ほぼ空の本文を「要約」する*。
**安全側は高いほう**なので 500字に置く。
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass

from dedupe import Kept
from rank import Ranking, Scored

#: 見出しだけ出す理由。**1件につき1つに決める。**
NOT_SUMMARIZABLE = "not_summarizable"
NO_BODY = "no_body"
TOO_SHORT = "too_short"
#: 本文も取得元も問題ないが、`rank` で点を付けなかった（**上限の外**）。
UNRANKED = "unranked"

#: 要約へ回す本文の最短（前後の空白を除いた字数）。
MIN_BODY = 500


@dataclass(frozen=True)
class Headline:
    kept: Kept
    reason: str
    #: `rank` で点を付けなかった記事は `None`。**0 と混ぜない。**
    score: int | None


@dataclass(frozen=True)
class Split:
    summarize: tuple[Scored, ...]
    headline: tuple[Headline, ...]

    @property
    def total(self) -> int:
        return len(self.summarize) + len(self.headline)

    @property
    def reasons(self) -> Mapping[str, int]:
        return dict(Counter(h.reason for h in self.headline))

    @property
    def summary(self) -> str:
        """**件数を必ず出す。** 見出しだけにした理由も数える。"""
        breakdown = "・".join(f"{r} {n}" for r, n in sorted(self.reasons.items()))
        return (
            f"{self.total} 件中 {len(self.summarize)} 件を要約へ"
            f"／見出しだけ {len(self.headline)} 件（{breakdown or 'なし'}）"
        )


def split(
    ranking: Ranking,
    *,
    summarizable: frozenset[str],
    min_body: int = MIN_BODY,
) -> Split:
    """**照合できないものは要約しない。** 見出しだけ出すものにも理由を残す。"""
    if not summarizable:
        raise ValueError("要約してよい取得元が0件。空で回すと、1件も要約せずに「異常なし」と答える")
    if min_body < 1:
        raise ValueError(f"しきい値が {min_body}。0 以下だと空白1字でも「本文」になる")

    summarize: list[Scored] = []
    headline: list[Headline] = []

    for scored in ranking.picked:
        reason = _why(scored.kept, summarizable=summarizable, min_body=min_body)
        if reason is None:
            summarize.append(scored)
        else:
            headline.append(Headline(kept=scored.kept, reason=reason, score=scored.score))

    for kept in ranking.unranked:
        # **点を付けなかった記事も同じ規則で見る。** 「Zenn だから見出し」と決め打ちしない。
        reason = _why(kept, summarizable=summarizable, min_body=min_body)
        if reason is None:
            # 点が無いものを要約へ回すと、`rank` の上限を素通りする。
            # **取得元のせいにしない**——理由が嘘だと、直し方を間違える。
            reason = UNRANKED
        headline.append(Headline(kept=kept, reason=reason, score=None))

    return Split(summarize=tuple(summarize), headline=tuple(headline))


def _why(kept: Kept, *, summarizable: frozenset[str], min_body: int) -> str | None:
    """**理由の順番を決めておく。** 取得元で止まるなら、本文の有無は直し方を変えない。"""
    article = kept.article
    # **完全一致で見る。** 取得元の名前は識別子で、大小を揃えると別の取得元が1つになる。
    # 設定の書き間違い（どの取得元にも当たらない名前）は、`cli` が読み込み時に止める。
    if article.source not in summarizable:
        return NOT_SUMMARIZABLE
    if not article.has_body:
        return NO_BODY
    if len((article.body or "").strip()) < min_body:
        return TOO_SHORT
    return None
