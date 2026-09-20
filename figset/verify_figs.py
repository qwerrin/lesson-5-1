"""対応表の主張が、**絵そのものに実在するか**を照合する段。

なぜこの段が要るのか
--------------------------------------------------------------------------

課題2（5-1-1）の講評:

    ただ商品リンクにアクセスし、**取得した情報が正しいかどうかまで確認**して
    スクショをとれるとより良いですね！

**同じ指摘が3回続いた**（教訓 `assignment-verify-against-the-source`）。
課題3 で `verify_source.py` を作って答え、課題3 の講評では消えた。

`figset` の出力（`README.md` / `figs.html`）を読み返すだけでは、
*自分が書いたものを自分で読み直している*にすぎない。
**ソースは画像**であって、対応表ではない。

見る相手が3つある
--------------------------------------------------------------------------

=============== ==============================================================
定義 ↔ 出力      定義にある図版が `docs/` にあるか／`docs/` の絵が定義にあるか
出力 ↔ 出力      README の行・`figs.html` の `src` が実在するか
**ソース側**     対応表の主張を持って**絵そのものを開き**、中に実在するか
=============== ==============================================================

判定を安全側に倒す規則
--------------------------------------------------------------------------

1. **読めなかった項目を合格にしない**（M8）
2. **照合0件を「一致」にしない**（M4）——`expects` が空の図版は*確認不能*
3. **`docs/` ごと無いときは「欠け」ではなく「確認不能」**（M7）。
   物差しが無い状態で不合格とも合格とも言わない
4. **件数を必ず出す。** 「問題なし」だけでは、何件見たかが分からない
5. **不一致 > 確認不能 > 合格。** 危ないと分かったものを保留にしない

問い方
--------------------------------------------------------------------------

2026-09-19 の疎通確認（`DESIGN.md` 9-1）で決めた形——**主張を投げて
「一致/不一致」を返させる**。対応表に `117` と書いて画像が `119` のとき
**「不一致」＋実際の値 119** が返ることを実測してある。
*照合ツールの本当の失敗は、主張に同意してしまうこと*だった。

読み手は差し込み式にする。`guard` の `ocr` と同じで、
**渡さなければソース側は見ず、見ていないと申告する**。
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from emit import HTML, LEDGER, README
from layout import Figure

#: 1つの主張に対する、ソース側（画像）からの答え。
MATCH = "match"
MISMATCH = "mismatch"
UNREADABLE = "unreadable"

#: 見る層。**見ていない層を隠さない**（`guard` と同じ）。
OUTPUT = "output"
SOURCE = "source"

#: 全体の判定。**不一致 > 確認不能 > 合格。**
VERIFIED = "verified"
FAILED = "failed"
UNKNOWN = "unknown"

#: `docs/` にあっても図版ではないもの。
NOT_FIGURES = frozenset({README, HTML, LEDGER})


@dataclass(frozen=True)
class Answer:
    """ソース側に問うた結果。**読めなかったことを不一致に混ぜない。**"""

    verdict: str
    note: str = ""


#: (**書き出した絵**, 主張) → 答え。原本ではなく `docs/` のファイルを渡す。
Reader = Callable[[Path, str], Answer]


@dataclass(frozen=True)
class Check:
    figure_key: str
    expect: str
    verdict: str
    note: str


@dataclass(frozen=True)
class Report:
    checks: tuple[Check, ...]
    #: 定義にあるのに `docs/` に無い（M1）。
    missing_files: tuple[str, ...]
    #: `docs/` にあるのに定義に無い（M2）。
    stray_files: tuple[str, ...]
    #: 1つの図版に複数のファイル（拡張子違いなど）。
    ambiguous_files: tuple[str, ...]
    #: README の表に出ていない。
    unlisted: tuple[str, ...]
    #: `figs.html` の `src` に出ていない（M3）。
    unlinked: tuple[str, ...]
    #: 主張が1つも書かれていない図版（**照合0件**）。
    no_expectations: tuple[str, ...]
    #: 見ていない層。
    unchecked: tuple[str, ...]

    @property
    def total(self) -> int:
        return len(self.checks)

    @property
    def matched(self) -> int:
        return sum(1 for check in self.checks if check.verdict == MATCH)

    @property
    def status(self) -> str:
        if self._broken or any(c.verdict == MISMATCH for c in self.checks):
            return FAILED
        # **「照合0件」を別に数えない。** 主張が無い図版は `no_expectations` に、
        # 読み手を渡していない場合は `unchecked` に必ず入るので、
        # `not self.checks` を足しても到達しない枝が増えるだけ
        # ——2026-09-21 のミューテーションで、消しても誰も困らないことが出た。
        if (
            self.unchecked
            or self.no_expectations
            or any(c.verdict != MATCH for c in self.checks)
        ):
            return UNKNOWN
        return VERIFIED

    @property
    def _broken(self) -> bool:
        return bool(
            self.missing_files
            or self.stray_files
            or self.ambiguous_files
            or self.unlisted
            or self.unlinked
        )

    @property
    def summary(self) -> str:
        """**件数を必ず出す。** 「問題なし」だけでは、何件見たかが分からない。"""
        return (
            f"{self.status}: 主張 {self.total} 件中 {self.matched} 件が絵と一致"
            f"／欠け {len(self.missing_files)}"
            f"・紛れ込み {len(self.stray_files)}"
            f"・表に無い {len(self.unlisted)}"
            f"・貼られていない {len(self.unlinked)}"
            f"・主張なし {len(self.no_expectations)}"
            f"／未検査 {', '.join(self.unchecked) if self.unchecked else 'なし'}"
        )


def verify(
    figures: Sequence[Figure],
    docs: Path,
    *,
    reader: Reader | None = None,
) -> Report:
    """定義・出力・絵の中身を突き合わせる。

    `reader` を渡さなければ**ソース側は見ず、見ていないと申告する**。
    """
    if not figures:
        raise ValueError(
            "図版の定義が0件。見る対象が空でも、検査は「異常なし」と答えてしまう"
        )

    unchecked: list[str] = []
    if reader is None:
        unchecked.append(SOURCE)

    if not docs.is_dir():
        # **物差しが無い。** 欠けているのではなく、確かめられない（M7）。
        unchecked.append(OUTPUT)
        return Report(
            checks=(),
            missing_files=(),
            stray_files=(),
            ambiguous_files=(),
            unlisted=(),
            unlinked=(),
            no_expectations=(),
            unchecked=tuple(unchecked),
        )

    listed = _text(docs / README)
    linked = _sources(_text(docs / HTML))

    missing: list[str] = []
    ambiguous: list[str] = []
    unlisted: list[str] = []
    unlinked: list[str] = []
    no_expectations: list[str] = []
    checks: list[Check] = []
    claimed: set[str] = set()

    for figure in sorted(figures, key=lambda f: f.article_no):
        stem = f"{figure.article_no:02d}-{figure.slug}"
        found = sorted(p for p in docs.glob(f"{stem}.*") if p.name not in NOT_FIGURES)

        if not found:
            missing.append(f"{stem}.png")  # 拡張子は分からないので既定を出す
            continue
        if len(found) > 1:
            # **どれを貼ったのか決まらない。** 勝手に1枚選ばない。
            ambiguous.extend(p.name for p in found)
            claimed.update(p.name for p in found)
            continue

        target = found[0]
        claimed.add(target.name)
        if target.name not in listed:
            unlisted.append(target.name)
        if target.name not in linked:
            unlinked.append(target.name)

        if not figure.expects:
            # **照合0件を「一致」にしない。** 問う相手が無い（M4）。
            no_expectations.append(figure.key)
            continue
        if reader is None:
            continue
        for expect in figure.expects:
            answer = reader(target, expect)
            checks.append(Check(figure.key, expect, answer.verdict, answer.note))

    stray = sorted(
        p.name for p in docs.iterdir()
        if p.is_file() and p.name not in NOT_FIGURES and p.name not in claimed
    )

    return Report(
        checks=tuple(checks),
        missing_files=tuple(missing),
        stray_files=tuple(stray),
        ambiguous_files=tuple(ambiguous),
        unlisted=tuple(unlisted),
        unlinked=tuple(unlinked),
        no_expectations=tuple(no_expectations),
        unchecked=tuple(unchecked),
    )


def _text(path: Path) -> str:
    """**読めなかったものを空と混ぜない**……が、ここでは空で足りる。

    README や HTML が無ければ、そこに載っていないのは事実である
    （*載せる側が書いていない*のと区別する必要がない）。
    """
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def _sources(html: str) -> set[str]:
    return set(re.findall(r'<img[^>]*\ssrc="([^"]+)"', html))
