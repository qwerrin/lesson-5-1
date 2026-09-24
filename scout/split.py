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

from collections.abc import Mapping
from dataclasses import dataclass

from dedupe import Kept
from rank import Ranking, Scored

#: 見出しだけ出す理由。**1件につき1つに決める。**
NOT_SUMMARIZABLE = "not_summarizable"
NO_BODY = "no_body"
TOO_SHORT = "too_short"

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
        raise NotImplementedError

    @property
    def reasons(self) -> Mapping[str, int]:
        raise NotImplementedError

    @property
    def summary(self) -> str:
        raise NotImplementedError


def split(
    ranking: Ranking,
    *,
    summarizable: frozenset[str],
    min_body: int = MIN_BODY,
) -> Split:
    """**照合できないものは要約しない。** 見出しだけ出すものにも理由を残す。"""
    raise NotImplementedError
