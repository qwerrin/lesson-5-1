"""原本を集めて台帳にする段。**まだ骨だけ。**

`tests/test_collect.py` を先に書いてある。ここにあるのは、
そのテストが1件ずつ落ちるようにするための**形だけ**で、中身はまだ無い。

なぜ形だけ先に置くか
--------------------------------------------------------------------------

実装が1文字も無い状態だと、テストは `ModuleNotFoundError` で
**まとめて1件のエラー**になる。それでは「どのテストが、何を理由に落ちたか」が出ない。
*RED は「落ちたこと」ではなく「落ち方」を見るための段*なので、
15件が15件ぶんの理由で落ちる形にしてから実装に入る。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

#: 台帳の状態。**3値にするのは「0件」と「数えきれていない」を分けるため**（M7・M9）。
OK = "ok"
EMPTY = "empty"
UNKNOWN = "unknown"

#: 図版になりうる拡張子。`.pdf` は Gemini が読めるが**スクリーンショットではない**。
IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".webp"})

#: ShareX の既定のファイル名（`2026-09-14-23-11-01-291.png`）。
NAME_FORMAT = "%Y-%m-%d-%H-%M-%S-%f"

#: 名前の日時と mtime の許容差。これを超えたら食い違いとして記録する。
NAME_TIME_TOLERANCE = timedelta(seconds=2)


@dataclass(frozen=True)
class Shot:
    """原本1枚。**名前は索引ではない**（H1）ので、名前由来の日時は別に持つ。"""

    path: Path
    sha256: str
    captured_at: datetime
    name_at: datetime | None
    size: int

    @property
    def time_mismatch(self) -> bool:
        """名前の日時と mtime が食い違っているか。

        **読めなかったとき（`name_at is None`）は食い違いにしない。**
        判定できなかったことを、判定の結果に混ぜない。
        """
        raise NotImplementedError


@dataclass(frozen=True)
class Collection:
    """台帳。**没も含めて全部載せる。**"""

    shots: tuple[Shot, ...]
    scanned_roots: tuple[Path, ...]
    missing_roots: tuple[Path, ...]
    duplicate_hashes: Mapping[str, tuple[Path, ...]]

    @property
    def status(self) -> str:
        raise NotImplementedError


def collect(
    roots: Sequence[Path],
    *,
    since: datetime | None = None,
    until: datetime | None = None,
) -> Collection:
    """`roots` を見て台帳を作る。

    **`roots` に既定値を置かない**（H7）。環境変数やユーザー名から組み立てると、
    値がずれたときに**黙って検査ゼロ**になる。
    """
    raise NotImplementedError
