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

**単位の字が、別の語の頭になっている場合**（2026-10-02 のレビュー・**承知で残す**）。
`3分` は本文の `3分類` で、`2人` は `2人目` で裏付けられてしまう。
日本語は語の切れ目に空白が無いので、閉じる方法がどれも別の場所で漏れる:

- 単位を減らす → その数は裸に戻り、`5分` が本文のどの `5` でも裏付けられる（直したばかりの穴）
- 単位の後ろに漢字が来たら弾く → 実データの中央値の記事にある `200倍速く` まで「本文に無い」になる

起きるのは**同じ数＋その単位の字で始まる別の語**が本文にあるときだけ。出力の文言にも書く。
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
    #: **見つかっても何も証明しない主張**（単位の無い1桁の数）。数えないが、隠さない。
    weak: tuple[str, ...] = ()


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
            f"／弱くて照合に使わなかった数 {sum(len(c.weak) for c in self.checks)} 個"
            "（照合したのは数字と英語の語だけ。日本語の言い回しと、"
            "単位の字が別の語の頭かは見ていない）"
        )


#: 数のすぐ後ろに来たら**一緒に照合する**単位。**ここに無い字は単位にしない**
#: ——知らない字（`3つ` の `つ`）まで付けると、照合が厳しすぎて外れる。
#: 2026-10-02 に `2倍速い` が本文の `System 1/2` で裏付けられたので足した（A）。
UNITS = (
    "倍", "件", "個", "%", "秒", "分", "時間", "回", "年", "か月", "ヶ月", "カ月", "月",
    "日", "週", "人", "行", "字", "文字", "円", "万", "億", "兆", "ドル", "割", "本",
    "台", "社", "歳", "度", "点", "位", "ms", "KB", "MB", "GB", "TB", "px", "fps",
)
#: いまの単位は**頭の字がどれも重ならない**ので、並び順で結果は変わらない。
#: 頭が重なる単位（`時` と `時間` など）を足すときは、**長いほうを先に**並べること。
_UNIT = "|".join(re.escape(u) for u in UNITS)
#: 通貨記号 → 単位。要約器は本文の `$250` を `250ドル` と**言い換える**（2026-10-04 の実物・U19）。
#: 記号は NFKC の後の形（`￥` は `¥`、`＄` は `$` になる）。
CURRENCY = {"$": "ドル", "¥": "円"}
_SIGN_OF = {unit: sign for sign, unit in CURRENCY.items()}
#: 数（小数・桁区切りを含む）＋**単位があれば単位**、または英字で始まる語
#: （`C++`・`C#`・`Node.js` の記号を含む）。英字の単位の後ろに英字が続けば、単位ではない。
TOKEN = re.compile(
    r"(?:(?P<sign>[$¥])[ \t]*)?"
    # 桁区切りは**3桁ずつの組だけ**。`1,2,3` は3つの数で、`123` ではない。
    r"(?P<num>[0-9]{1,3}(?:,[0-9]{3})+(?![0-9])(?:\.[0-9]+)?|[0-9]+(?:\.[0-9]+)*)"
    rf"(?:[ \t]*(?P<unit>{_UNIT})(?![A-Za-z])"
    # 単位の表に無くても、**英字が直接続けば数ごと1つ**（`1M`・`5x`・`3D`・U19）。
    # 分けると数は弱い数で数えられず、英字だけを照合して、本文の `1M` の `M` に境界で外れる。
    r"|(?P<suffix>[A-Za-z]+)(?![A-Za-z0-9]))?"
    r"|(?P<word>[A-Za-z][A-Za-z0-9_.+#-]*)"
)
#: 照合する側で、主張を数と単位に分ける。
_NUMBER_AND_UNIT = re.compile(r"([0-9]+(?:\.[0-9]+)*)(.*)")
#: 桁区切りの組。**`1,000` と `1000` を同じ数にする**が、`1,2,3` や `1,23` はつなげない。
THOUSANDS = re.compile(r"(?<![0-9,])[0-9]{1,3}(?:,[0-9]{3})+(?![0-9])")
#: 照合の境界で使う、**ASCII の**数字。`str.isdigit()` は `٣` も数字と答える。
DIGITS = "0123456789"
#: これ以下の長さの語は**大小を区別する**。`Go` を英文の `go` で裏付けない。
SHORT_WORD = 3
#: NFKC が**本文に無い数字**に変えてしまう字の種類（`²` → `2`、`½` → `1⁄2`）。
_PHANTOM = ("<super>", "<sub>", "<fraction>")
#: その字の代わりに置く区切り。**数とも単位とも語とも組まない字。**
_SEVER = "|"
#: `[文字](URL)` → `文字`。**要約器はリンクを描画後の見た目で読む**（U12）。
LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")
#: むき出しの URL。**ホスト名は本文の主張ではない。**
BARE_URL = re.compile(r"https?://[^\s)\]]+")
#: 書式記号**だけ**。意味を持つ字は落とさない。
DECOR = re.compile(r"\*\*|__|`")


def claims(text: str) -> tuple[str, ...]:
    """要約の文から、照合できる主張（数字・英語の語）を出た順に抜く。"""
    found: list[str] = []
    for match in TOKEN.finditer(_nfkc(text)):
        if match["num"]:
            # **数は単位ごと**（A）。`200 倍` の空白は詰める。
            unit = match["unit"] or match["suffix"] or ""
            if match["sign"] and not unit:
                # `$250` は `250ドル` として抜く。照合は記号と単位のどちらでも裏付ける。
                unit = CURRENCY[match["sign"]]
            claim = match["num"].replace(",", "") + unit
        else:
            # 文末の `.` や `-` は語ではない。`C++`・`C#` の記号は残す。
            claim = match["word"].rstrip(".-")
        # 抜いた語は必ず英数字で始まるので、空にはならない。
        if claim not in found:
            found.append(claim)
    return tuple(found)


def weak(claim: str) -> bool:
    """**見つかっても何も証明しない主張か。** 単位の無い1桁の数（B）。

    見出し番号・箇条書き・`System 1` に必ずある。2026-10-02 の実データで、
    中央値の記事の要約の `1` がこの形で「実在」になっていた。
    """
    return re.fullmatch(r"[0-9]", _nfkc(claim)) is not None


def present(claim: str, body: str) -> bool:
    """**境界つき**で、主張が本文にあるかを答える。

    部分一致にしないのは U13 の罠のため——`2倍` が本文の `200倍` の `2` に当たる。
    """
    target = _ungroup(_nfkc(claim))
    if not target:
        # **空文字はどこにでもある。** `""[:1] in DIGITS` も真になる。
        return False
    text = _clean(body)
    if target[:1] in DIGITS:
        number, unit = _NUMBER_AND_UNIT.fullmatch(target).groups()  # type: ignore[union-attr]
        # 前後に数字が無い。`2.5` の `2` や `5` にも当てない。
        bare = rf"{re.escape(number)}(?![0-9])(?!\.[0-9])"
        pattern = rf"(?<![0-9])(?<![0-9]\.){bare}"
        if unit:
            # **単位ごと照合する**（A）。本文の `200 倍` の空白は許すが、**行はまたがない**
            # ——行末の見出し番号が、次の行の頭の字と組んでしまう。
            # 英字の単位は後ろに英字が続けば別の単位（`5m` を `5ms` で裏付けない）。
            pattern += rf"[ \t]*{re.escape(unit)}" + ("(?![A-Za-z])" if unit[-1].isascii() else "")
            sign = _SIGN_OF.get(unit)
            if sign:
                # `250ドル` は本文の `$250` でも裏付ける。**後ろに英字が続けば別の額**（`$250M`）。
                pattern = rf"(?:{pattern}|{re.escape(sign)}[ \t]*{bare}(?![A-Za-z]))"
        return re.search(pattern, text) is not None
    # 前後に英数字が無い。`C` を `C++` に、`Node` を `Node.js` に当てない。
    pattern = rf"(?<![A-Za-z0-9_]){re.escape(target)}(?![A-Za-z0-9_+#])(?![.\-][A-Za-z0-9])"
    # **短い語は大小を区別する。** `Go`・`IF`・`AI` は、小文字だと普通の英単語やコードになる。
    flags = 0 if len(target) <= SHORT_WORD else re.IGNORECASE
    return re.search(pattern, text, flags=flags) is not None


def verify(summaries: Sequence[Summary]) -> Audit:
    """要約ごとに照合する。**照合0件を一致にしない。**"""
    return Audit(checks=tuple(_check(s) for s in summaries))


def _check(summary: Summary) -> Check:
    body = summary.scored.kept.article.body
    if not body:
        # **M8：読めなかったことと、問題なかったことを分ける。** 見ていないので missing も空。
        return Check(summary=summary, verdict=UNVERIFIABLE, found=(), missing=(), quotes_missing=())

    every = claims(summary.text)
    # **弱い主張は数えないが、隠さない**（B）。見つかっても何も証明しない。
    weak_ones = tuple(c for c in every if weak(c))
    asserted = tuple(c for c in every if c not in weak_ones)
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
        weak=weak_ones,
    )


def _clean(text: str) -> str:
    """**書式記号だけ**を落とす。リンクは文字だけ残し、URL は根拠から外す。"""
    text = _nfkc(text)
    text = LINK.sub(r"\1", text)
    text = BARE_URL.sub(" ", text)
    text = DECOR.sub("", text)
    return _ungroup(text)


def _nfkc(text: str) -> str:
    """NFKC で全角をそろえる。**ただし上付き・下付き・分数は数にしない。**

    NFKC は `10²` を `102`、`1½` を `11⁄2` にする——*本文に無い数が生まれる*
    （2026-10-02 のレビューで `2倍` が `1½倍` に裏付けられた）。先に区切りへ置き換える。

    **空白にしない。** 空白だと `10² 回` が `10  回` になり、*数と単位が組んで*
    「10回」という主張が生まれる。数字でも英字でも空白でもない `|` は、どちらとも組まない。
    """
    kept = "".join(
        _SEVER if unicodedata.decomposition(ch).startswith(_PHANTOM) else ch for ch in text
    )
    return unicodedata.normalize("NFKC", kept)


def _ungroup(text: str) -> str:
    """**3桁ずつの桁区切りだけ**カンマを落とす。"""
    return THOUSANDS.sub(lambda m: m.group().replace(",", ""), text)
