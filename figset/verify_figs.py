"""対応表の主張が、**絵そのものに実在するか**を照合する段。**まだ骨だけ。**

`tests/test_verify_figs.py` を先に書いてある。ここにあるのは、
そのテストが1件ずつ落ちるようにするための**形だけ**で、中身はまだ無い。
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

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
    missing_files: tuple[str, ...]
    stray_files: tuple[str, ...]
    ambiguous_files: tuple[str, ...]
    unlisted: tuple[str, ...]
    unlinked: tuple[str, ...]
    no_expectations: tuple[str, ...]
    unchecked: tuple[str, ...]

    @property
    def total(self) -> int:
        raise NotImplementedError

    @property
    def matched(self) -> int:
        raise NotImplementedError

    @property
    def status(self) -> str:
        raise NotImplementedError


def verify(
    figures: Sequence[Figure],
    docs: Path,
    *,
    reader: Reader | None = None,
) -> Report:
    """定義・出力・絵の中身を突き合わせる。

    `reader` を渡さなければ**ソース側は見ず、見ていないと申告する**。
    """
    raise NotImplementedError
