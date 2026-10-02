"""要約の文に出る**数字と英語の語**が、記事の**本文に実在するか**を照合する段。

**出力を読み返すのは照合ではない。ソースを開く**（5-1-1 課題2 の講評・3回目）。
名前は 5-1-1 課題3 と揃えた——対象が議事録から記事本文に変わっただけ。

何を照合するか（`DESIGN.md` U12・U13）
--------------------------------------------------------------------------

- **引用は証拠にしない。** 要約器が自分で選ぶので、実在しても一貫性の検査でしかない
- **要約の文に出る数字・英語の語**を抜き、**境界つき**で本文と比べる。部分一致だと
  `2倍` が本文の `200倍` に当たって素通りする
- 本文は**書式記号だけ**落として比べる（強調 `**`・コードの印・リンクの URL）。
  *意味を持つ字は落とさない*

見ていないもの
--------------------------------------------------------------------------

**日本語の固有名詞・言い回し。** 出力の文言でそれを隠さない。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from summarize import Summary

CONFIRMED = "confirmed"
MISMATCH = "mismatch"
#: **照合0件・本文なしを「一致」にしない**（M8・M9）。
UNVERIFIABLE = "unverifiable"


@dataclass(frozen=True)
class Check:
    summary: Summary
    verdict: str
    found: tuple[str, ...]
    #: **消さずに印を付ける**（6-2）。
    missing: tuple[str, ...]
    #: 判定には使わない（U13）。要約器が本文に無い引用を作ったことを見せるだけ。
    quotes_missing: tuple[str, ...]


@dataclass(frozen=True)
class Audit:
    checks: tuple[Check, ...]

    @property
    def counts(self) -> Mapping[str, int]:
        raise NotImplementedError

    @property
    def ok(self) -> bool:
        raise NotImplementedError

    @property
    def summary(self) -> str:
        raise NotImplementedError


def claims(text: str) -> tuple[str, ...]:
    """要約の文から、照合できる主張（数字・英語の語）を出た順に抜く。"""
    raise NotImplementedError


def present(claim: str, body: str) -> bool:
    """**境界つき**で、主張が本文にあるかを答える。"""
    raise NotImplementedError


def verify(summaries: Sequence[Summary]) -> Audit:
    """要約ごとに照合する。**照合0件を一致にしない。**"""
    raise NotImplementedError
