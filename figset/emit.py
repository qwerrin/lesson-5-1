"""`docs/` へ図版・対応表・貼り付け用 HTML・台帳を書き出す段。**まだ骨だけ。**

`tests/test_emit.py` を先に書いてある。ここにあるのは、
そのテストが1件ずつ落ちるようにするための**形だけ**で、中身はまだ無い。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from guard import Policy
from layout import Layout

#: 書き込みの結果。**同名を黙って上書きしない**ために3つに分ける（M6）。
CREATED = "created"
REPLACED = "replaced"
UNCHANGED = "unchanged"

README = "README.md"
HTML = "figs.html"
LEDGER = "figset.json"


@dataclass(frozen=True)
class Written:
    path: Path
    action: str


@dataclass(frozen=True)
class Emission:
    docs: Path
    files: tuple[Written, ...]
    #: 書いたあと**読み戻してハッシュが一致した**枚数。
    verified: int
    status: str

    def action_of(self, name: str) -> str:
        raise NotImplementedError


def emit(plan: Layout, docs: Path, policy: Policy) -> Emission:
    """`docs/` へ4種類を書き出す。

    **`policy` に既定値を置かない。** `docs/` は記事に貼る場所なので、
    書くものは全部 `guard.redact` に通す。
    """
    raise NotImplementedError
