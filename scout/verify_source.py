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

import re
import unicodedata
from collections import Counter
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
        return dict(Counter(c.verdict for c in self.checks))

    @property
    def ok(self) -> bool:
        """**全部が照合済みのときだけ。** 確認できなかったものも「問題なし」にしない。"""
        return all(c.verdict == CONFIRMED for c in self.checks)

    @property
    def summary(self) -> str:
        """**件数を必ず出す。** そして、何を見ていないかを隠さない。"""
        counts = self.counts
        found = sum(len(c.found) for c in self.checks)
        looked = found + sum(len(c.missing) for c in self.checks)
        return (
            f"{len(self.checks)} 件中 {counts.get(CONFIRMED, 0)} 件を照合できた"
            f"／本文に無い主張を含む {counts.get(MISMATCH, 0)} 件"
            f"／確認できない {counts.get(UNVERIFIABLE, 0)} 件"
            f"／主張 {looked} 個中 {found} 個が本文に実在"
            "（照合したのは数字と英語の語だけ。日本語の言い回しは見ていない）"
        )


#: 数字（小数・桁区切りを含む）か、英字で始まる語（`C++`・`C#`・`Node.js` の記号を含む）。
TOKEN = re.compile(r"[0-9]+(?:[.,][0-9]+)*|[A-Za-z][A-Za-z0-9_.+#-]*")
#: 桁区切りのカンマ。**`1,000` と `1000` を同じ数にする。**
THOUSANDS = re.compile(r"(?<=[0-9]),(?=[0-9])")
#: `[文字](URL)` → `文字`。**要約器はリンクを描画後の見た目で読む**（U12）。
LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")
#: むき出しの URL。**ホスト名は本文の主張ではない。**
BARE_URL = re.compile(r"https?://[^\s)\]]+")
#: 書式記号**だけ**。意味を持つ字は落とさない。
DECOR = re.compile(r"\*\*|__|`")


def claims(text: str) -> tuple[str, ...]:
    """要約の文から、照合できる主張（数字・英語の語）を出た順に抜く。"""
    found: list[str] = []
    for token in TOKEN.findall(unicodedata.normalize("NFKC", text)):
        if token[0].isdigit():
            claim = THOUSANDS.sub("", token)
        else:
            # 文末の `.` や `-` は語ではない。`C++`・`C#` の記号は残す。
            claim = token.rstrip(".-")
        # 抜いた語は必ず英数字で始まるので、空にはならない。
        if claim not in found:
            found.append(claim)
    return tuple(found)


def present(claim: str, body: str) -> bool:
    """**境界つき**で、主張が本文にあるかを答える。

    部分一致にしないのは U13 の罠のため——`2倍` が本文の `200倍` の `2` に当たる。
    """
    target = THOUSANDS.sub("", unicodedata.normalize("NFKC", claim))
    text = _clean(body)
    if target[:1].isdigit():
        # 前後に数字が無い。`2.5` の `2` や `5` にも当てない。
        pattern = rf"(?<![0-9])(?<![0-9]\.){re.escape(target)}(?![0-9])(?!\.[0-9])"
        return re.search(pattern, text) is not None
    # 前後に英数字が無い。`C` を `C++` に、`Node` を `Node.js` に当てない。
    pattern = rf"(?<![A-Za-z0-9_]){re.escape(target)}(?![A-Za-z0-9_+#])(?![.\-][A-Za-z0-9])"
    return re.search(pattern, text, flags=re.IGNORECASE) is not None


def verify(summaries: Sequence[Summary]) -> Audit:
    """要約ごとに照合する。**照合0件を一致にしない。**"""
    return Audit(checks=tuple(_check(s) for s in summaries))


def _check(summary: Summary) -> Check:
    body = summary.scored.kept.article.body
    if not body:
        # **M8：読めなかったことと、問題なかったことを分ける。** 見ていないので missing も空。
        return Check(summary=summary, verdict=UNVERIFIABLE, found=(), missing=(), quotes_missing=())

    asserted = claims(summary.text)
    found = tuple(c for c in asserted if present(c, body))
    missing = tuple(c for c in asserted if c not in found)
    cleaned = _clean(body)
    quotes_missing = tuple(q for q in summary.quotes if _clean(q) not in cleaned)

    if not asserted:
        verdict = UNVERIFIABLE
    elif missing:
        verdict = MISMATCH
    else:
        verdict = CONFIRMED
    return Check(
        summary=summary,
        verdict=verdict,
        found=found,
        missing=missing,
        quotes_missing=quotes_missing,
    )


def _clean(text: str) -> str:
    """**書式記号だけ**を落とす。リンクは文字だけ残し、URL は根拠から外す。"""
    text = unicodedata.normalize("NFKC", text)
    text = LINK.sub(r"\1", text)
    text = BARE_URL.sub(" ", text)
    text = DECOR.sub("", text)
    return THOUSANDS.sub("", text)
