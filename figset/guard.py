"""外へ出す前に、ローカルだけで秘匿情報を見る段。**まだ骨だけ。**

`tests/test_guard.py` を先に書いてある。ここにあるのは、
そのテストが1件ずつ落ちるようにするための**形だけ**で、中身はまだ無い。
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

#: 見る層。**「見た」と「見ていない」を層ごとに持つ。**
PATH = "path"
METADATA = "metadata"
PIXELS = "pixels"
LAYERS = (PATH, METADATA, PIXELS)

#: 判定。**`OK` は全部の層を見たときにしか出ない。**
OK = "ok"
BLOCKED = "blocked"
UNKNOWN = "unknown"

#: 伏せ字。**規則に当たった所だけを置き換える。**
MASK = "***"


@dataclass(frozen=True)
class Rule:
    """探すものひとつ。`name` は**人に見せてよい名前**でなければならない。"""

    name: str
    pattern: re.Pattern[str]


def literal_rule(name: str, text: str) -> Rule:
    """そのままの文字列を探す規則。**大小は区別しない。**"""
    raise NotImplementedError


@dataclass(frozen=True)
class Policy:
    """探すものの一覧。**空では作れない。**"""

    rules: tuple[Rule, ...]

    def __post_init__(self) -> None:
        raise NotImplementedError


@dataclass(frozen=True)
class Finding:
    """当たったこと。**当たったもの（秘匿そのもの）は持たない。**"""

    layer: str
    rule: str
    where: str


@dataclass(frozen=True)
class Verdict:
    path: Path
    findings: tuple[Finding, ...]
    checked: tuple[str, ...]
    unchecked: tuple[str, ...]
    label: str

    @property
    def status(self) -> str:
        raise NotImplementedError

    @property
    def report(self) -> str:
        raise NotImplementedError


def redact(text: str, policy: Policy) -> str:
    """規則に当たった所を伏せる。**当たっていない所は残す。**"""
    raise NotImplementedError


def inspect(
    path: Path,
    policy: Policy,
    *,
    ocr: Callable[[Path], str] | None = None,
) -> Verdict:
    """`path` を**ローカルだけで**見る。

    **`policy` に既定値を置かない**（H7）。
    `ocr` を渡さなければ画素の層は見ず、**見ていないと申告する**。
    """
    raise NotImplementedError


def _scan(text: str, policy: Policy) -> Sequence[Rule]:
    raise NotImplementedError
