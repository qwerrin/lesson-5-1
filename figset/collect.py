"""原本を集めて台帳にする段。

ShareX が吐いた生のスクリーンショットを読み、**中身のハッシュ付きで**並べる。
ここで守るのは「数え方」だけで、中身の判定（`classify`）にも
番号の割付（`layout`）にも踏み込まない。

日時の物差しは1本
--------------------------------------------------------------------------

使うのは**ファイルシステムの mtime だけ**で、すべてローカルの naive な
`datetime` として扱う（教訓 `one-date-basis-per-output`）。

ファイル名にも日時が入っているが、**それは物差しではなく second opinion** である。
*コピーすれば mtime は変わりうるし、リネームすれば名前は変わりうる。*
両方壊れうるので、`collect` はどちらが正しいかを決めず、**食い違いを記録するだけ**にする。

そして **「名前から読めなかった」と「読めたが食い違った」を別の状態にする**。
読めなかったものを食い違い扱いにすると、リネーム済みのファイルが毎回鳴り、
*本物の食い違いが出ても同じ見た目*になって、検査そのものが読まれなくなる。

数えきれなかったことを、件数に混ぜない
--------------------------------------------------------------------------

状態は3値（`OK` / `EMPTY` / `UNKNOWN`）。

- `EMPTY` は「全部見たが1枚も無かった」
- `UNKNOWN` は「**見られなかった場所がある**」

この2つを同じ顔にすると、*保存先を間違えて1枚も見ていない状態*が
順調と見分けられなくなる。`UNKNOWN` でも見えたぶんは返す——
捨てると、呼び出し側が「確認不能を承知で進める」判断をできなくなる。
"""

from __future__ import annotations

import hashlib
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
#: ShareX の名前はミリ秒までなので、揃っていても 1 秒未満はずれうる。
NAME_TIME_TOLERANCE = timedelta(seconds=2)

#: ハッシュを取るときの読み出し単位。
_CHUNK = 1 << 20


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
        if self.name_at is None:
            return False
        return abs(self.captured_at - self.name_at) > NAME_TIME_TOLERANCE


@dataclass(frozen=True)
class Collection:
    """台帳。**没も含めて全部載せる。**"""

    shots: tuple[Shot, ...]
    scanned_roots: tuple[Path, ...]
    missing_roots: tuple[Path, ...]
    duplicate_hashes: Mapping[str, tuple[Path, ...]]

    @property
    def status(self) -> str:
        """`UNKNOWN` → `EMPTY` → `OK` の順に判定する。

        **見られなかった場所があるときは、件数を言わない。**
        1枚も見ていないのか、本当に無いのかを、`collect` は区別できない。
        """
        if self.missing_roots:
            return UNKNOWN
        if not self.shots:
            return EMPTY
        return OK


def collect(
    roots: Sequence[Path],
    *,
    since: datetime | None = None,
    until: datetime | None = None,
) -> Collection:
    """`roots` を見て台帳を作る。

    **`roots` に既定値を置かない**（H7）。環境変数やユーザー名から組み立てると、
    値がずれたときに**黙って検査ゼロ**になる
    （教訓 `detector-inputs-must-not-come-from-env`）。

    `since` と `until` は**両端を含む**。「いつからいつまで」と言われて
    端が落ちるのは事故になる。
    """
    scanned: list[Path] = []
    missing: list[Path] = []
    shots: list[Shot] = []

    for root in roots:
        if not root.is_dir():
            missing.append(root)
            continue
        scanned.append(root)
        shots.extend(_walk(root, since=since, until=until))

    # 撮影順に並べる。同時刻は名前で決めて、実行ごとに順が変わらないようにする。
    shots.sort(key=lambda s: (s.captured_at, s.path.name))

    return Collection(
        shots=tuple(shots),
        scanned_roots=tuple(scanned),
        missing_roots=tuple(missing),
        duplicate_hashes=_duplicates(shots),
    )


def _walk(
    root: Path,
    *,
    since: datetime | None,
    until: datetime | None,
) -> list[Shot]:
    found: list[Shot] = []
    for path in root.iterdir():
        if not path.is_file():
            continue
        if path.suffix.lower() not in IMAGE_SUFFIXES:
            continue

        stat = path.stat()
        captured_at = datetime.fromtimestamp(stat.st_mtime)
        if since is not None and captured_at < since:
            continue
        if until is not None and captured_at > until:
            continue

        found.append(
            Shot(
                path=path,
                sha256=_sha256(path),
                captured_at=captured_at,
                name_at=_name_time(path),
                size=stat.st_size,
            )
        )
    return found


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def _name_time(path: Path) -> datetime | None:
    """ファイル名から日時を読む。読めなければ `None`。

    **読めないことは異常ではない。** 採用済みの図版は `01-verify-source.png` の
    ように既にリネームされている。
    """
    try:
        return datetime.strptime(path.stem, NAME_FORMAT)
    except ValueError:
        return None


def _duplicates(shots: Sequence[Shot]) -> dict[str, tuple[Path, ...]]:
    """同じ中身が2枚以上あるものだけを返す（H4）。

    **撮り直すとほぼ同じ絵が2枚できる。** 完全に同じなら中身で分かる。
    並びは `shots` の順（＝撮影順）をそのまま保つ。
    """
    by_hash: dict[str, list[Path]] = {}
    for shot in shots:
        by_hash.setdefault(shot.sha256, []).append(shot.path)
    return {h: tuple(paths) for h, paths in by_hash.items() if len(paths) > 1}
