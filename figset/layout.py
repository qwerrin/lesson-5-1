"""図版の定義と原本を突き合わせて、番号を割り付ける段。

何で指すか
--------------------------------------------------------------------------

**SHA-256。** 名前でも順番でもない。

- 名前は変わる（ShareX のタイムスタンプ名は、採用時にリネームされる）
- 順番は意味を持たない——実測（課題3・2026-09-14）で
  **撮影順 1,2,3,5,6,8 が記事の 01,04,06,05,02,03** になっていた

番号が2系統ある
--------------------------------------------------------------------------

`task3/docs/README.md` が自分で書いている——**「混ぜると必ず取り違える」**。

============ ==================================== =============================
番号          何の順か                               規則
============ ==================================== =============================
`article_no` 記事に並べる順（`01-verify-source`）   **1..N の連番**
`shots_no`   `shots.py` を呼ぶ順                   重複だけ禁止。飛びも欠落も可
============ ==================================== =============================

`article_no` に連番を要求するのは、**抜けが撮り忘れの別の顔**だから。
`shots_no` を緩くするのは、実測がそうなっているから——課題3 は **04 から始まり**、
`08-line`（届いた LINE の受信画面）は**手で撮った**ので番号を持たない。

決めないことを、決めたふりで進めない
--------------------------------------------------------------------------

- **没**（割付の無い原本）は捨てず、撮影順のまま残す。
  消すと*「撮ったが使わなかった」と「撮っていない」*が区別できなくなる
- **欠け**（定義はあるが絵が無い）と**孤児**（絵はあるが定義に無い）を
  両方向で出す。*片方向だけ検査すると必ず漏れる*
- **食い違い**（1つの図版に2枚）は、黙って片方を選ばない。
  同じ中身の2枚をハッシュで指せば必ずこうなる（H4）
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from collect import Shot

#: 判定。**欠け・孤児・食い違いのどれかがあれば未完成。**
COMPLETE = "complete"
INCOMPLETE = "incomplete"


@dataclass(frozen=True)
class Figure:
    """図版ひとつぶんの定義（`figs.yaml` の1行）。

    **番号が2系統ある。** `article_no` は記事に並べる順、
    `shots_no` は `shots.py` を呼ぶ順で、*混ぜると必ず取り違える*。
    """

    key: str
    article_no: int
    shots_no: int | None
    slug: str
    caption: str
    claim: str
    expects: tuple[str, ...]


@dataclass(frozen=True)
class Placement:
    figure: Figure
    shot: Shot

    @property
    def filename(self) -> str:
        """`01-verify-source.png`。

        **拡張子は原本から取る。** 勝手に `.png` と名乗ると、中身と名前が食い違う。
        """
        return f"{self.figure.article_no:02d}-{self.figure.slug}{self.shot.path.suffix.lower()}"


@dataclass(frozen=True)
class Layout:
    #: 記事順。
    placements: tuple[Placement, ...]
    #: 没。**消さない**（撮影順）。
    rejected: tuple[Shot, ...]
    #: 定義はあるが絵が無い（M1・記事順）。
    missing: tuple[Figure, ...]
    #: 絵はあるが定義に無いキー（M2）。
    orphans: tuple[str, ...]
    #: 1つの図版に2枚以上（H4）。
    conflicts: Mapping[str, tuple[Shot, ...]]

    @property
    def by_shots_order(self) -> tuple[Placement, ...]:
        """`shots.py` を呼ぶ順。**番号を持たないものは末尾へ回す。**

        手で撮ったものは実行の列に居場所が無いが、*落とすのは別の話*。
        """
        return tuple(
            sorted(
                self.placements,
                key=lambda p: (
                    p.figure.shots_no is None,
                    p.figure.shots_no if p.figure.shots_no is not None else 0,
                    p.figure.article_no,
                ),
            )
        )

    @property
    def status(self) -> str:
        """**没は異常ではない。** 撮り直しは普通に起きる。

        未完成にするのは、*決めきれていないもの*が残っているときだけ。
        """
        if self.missing or self.orphans or self.conflicts:
            return INCOMPLETE
        return COMPLETE


def plan(
    shots: Sequence[Shot],
    figures: Sequence[Figure],
    assignments: Mapping[str, str],
) -> Layout:
    """`assignments`（**ハッシュ → 図版のキー**）で割り付ける。"""
    _check(figures)
    known = {figure.key: figure for figure in figures}

    claimed: dict[str, list[Shot]] = {}
    rejected: list[Shot] = []
    orphans: list[str] = []

    for shot in shots:
        key = assignments.get(shot.sha256)
        if key is None:
            rejected.append(shot)
            continue
        if key not in known:
            # **孤児は没ではない。** 割り付けた意思はあり、行き先が無いだけ。
            if key not in orphans:
                orphans.append(key)
            continue
        claimed.setdefault(key, []).append(shot)

    placements: list[Placement] = []
    missing: list[Figure] = []
    conflicts: dict[str, tuple[Shot, ...]] = {}

    for figure in sorted(figures, key=lambda f: f.article_no):
        found = claimed.get(figure.key, [])
        if not found:
            missing.append(figure)
        elif len(found) == 1:
            placements.append(Placement(figure, found[0]))
        else:
            conflicts[figure.key] = tuple(found)

    rejected.sort(key=lambda s: (s.captured_at, s.path.name))

    return Layout(
        placements=tuple(placements),
        rejected=tuple(rejected),
        missing=tuple(missing),
        orphans=tuple(orphans),
        conflicts=conflicts,
    )


def _check(figures: Sequence[Figure]) -> None:
    """定義そのものの誤りは、**走らせる前に止める**。

    空の定義を通すと、何を渡しても「全部没・欠け無し」で成功する
    ——*検査があるのに検査していない*状態になる。
    """
    if not figures:
        raise ValueError(
            "図版の定義が0件。これを通すと、何を渡しても「全部没・欠け無し」で成功する"
        )

    # **重複を別に数えない。** 重複があれば 1..N の連番にはなりえないので、
    # 下の1本で両方とも捕まる。*別に置くと、到達しても意味のない枝が残る*
    # ——2026-09-20 のミューテーションで、消しても誰も困らないことが出た。
    article_numbers = sorted(figure.article_no for figure in figures)
    if article_numbers != list(range(1, len(figures) + 1)):
        raise ValueError(
            f"記事番号は 1..{len(figures)} の連番でなければならない"
            f"（重複も、抜けも、ここで止まる）: {article_numbers}"
        )

    shots_numbers = [f.shots_no for f in figures if f.shots_no is not None]
    if len(set(shots_numbers)) != len(shots_numbers):
        raise ValueError(f"実行番号が重複している: {sorted(shots_numbers)}")
