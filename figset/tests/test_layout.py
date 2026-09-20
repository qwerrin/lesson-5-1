"""figset/layout のテスト。**実装より先に書いた。**

`layout` は、集めた原本と図版の定義を突き合わせて**番号を割り付ける**段。

============ ====================================================================
根拠         ここで守ること
============ ====================================================================
H1           **ハッシュで指す。** 名前は索引ではない
H2           **撮影順と記事順は違う。** 並び順を意味に使わない
H3           没カットが混じる。**没も台帳に残す**（消さない）
H4           同じ中身が2枚あるとき、どちらを使うかを**曖昧なまま進めない**
M1           図版の定義はあるのに絵が無い（撮り忘れ）
M2           絵はあるのに図版の定義に無い（貼り忘れ）
M9           定義が空のまま「うまくいった」と言わない
============ ====================================================================

番号は2系統ある
--------------------------------------------------------------------------

`task3/docs/README.md` が自分で書いている——**「番号が2系統ある。混ぜると必ず取り違える」**。

- **記事番号**: 記事に並べる順。`01-verify-source.png` の `01`
- **実行番号**: `shots.py` を呼ぶ順

実測（課題3）では両者がずれていた。記事の `01` は実行の `04` で、
記事の `02` は実行の `05`。しかも `08-line` は**手動で撮った**ので実行番号が無い。

だから `layout` は:

- 記事番号に **1..N の連番**を要求する（抜けは*撮り忘れの別の顔*）
- 実行番号は **重複だけ禁じ、飛びも欠落も許す**（実測がそうなっている）
- **両方の順で取り出せる**ようにする。片方しか出せないと、呼ぶ側が並べ替えて取り違える
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "figset"))

import collect  # noqa: E402
import layout  # noqa: E402

BASE = datetime(2026, 9, 14, 23, 0, 0)


def _shot(tag: str, minutes: int, *, name: str | None = None, suffix: str = ".png") -> collect.Shot:
    """ハッシュと撮影時刻だけが意味を持つ原本。**名前はわざと当てにならない形にする。**"""
    when = BASE + timedelta(minutes=minutes)
    return collect.Shot(
        path=Path("原本") / (name or f"{when.strftime('%Y-%m-%d-%H-%M-%S-000')}{suffix}"),
        sha256=tag * 64,
        captured_at=when,
        name_at=None,
        size=100,
    )


def _fig(key: str, article_no: int, shots_no: int | None, slug: str) -> layout.Figure:
    return layout.Figure(
        key=key,
        article_no=article_no,
        shots_no=shots_no,
        slug=slug,
        caption=f"{key} を写す",
        claim=f"{key} が証明すること",
        expects=(),
    )


# --------------------------------------------------------------------------
# 定義の検証（M9）
# --------------------------------------------------------------------------


def test_図版の定義が空なら作れない() -> None:
    """**定義が0件なら、何を渡しても「全部没・欠け無し」で通ってしまう。**

    `guard` の空ポリシーと同じ形——*検査があるのに検査していない*。
    """
    with pytest.raises(ValueError):
        layout.plan([_shot("a", 0)], (), {})


def test_記事番号が重複していたら作れない() -> None:
    """**重複を別の検査で数えない。** 重複があれば 1..N の連番にはなりえない。

    最初は重複用の検査を別に置いていたが、2026-09-20 のミューテーションで
    *消しても誰も困らない*ことが出た（連番の検査が必ず先に捕まえる）。
    ここで見ているのは「重複が弾かれること」で、**どの行が弾くかではない**。
    """
    figs = (_fig("x", 1, 4, "x"), _fig("y", 1, 5, "y"))
    with pytest.raises(ValueError):
        layout.plan([], figs, {})


def test_記事番号が飛んでいたら作れない() -> None:
    """**記事番号は 1..N の連番。** 抜けは*撮り忘れの別の顔*で、静かに1枚落ちる。"""
    figs = (_fig("x", 1, 4, "x"), _fig("y", 3, 5, "y"))
    with pytest.raises(ValueError):
        layout.plan([], figs, {})


def test_記事番号が1から始まらなければ作れない() -> None:
    figs = (_fig("x", 2, 4, "x"), _fig("y", 3, 5, "y"))
    with pytest.raises(ValueError):
        layout.plan([], figs, {})


def test_実行番号が重複していたら作れない() -> None:
    figs = (_fig("x", 1, 4, "x"), _fig("y", 2, 4, "y"))
    with pytest.raises(ValueError):
        layout.plan([], figs, {})


def test_実行番号は飛んでいてよい() -> None:
    """課題3 の実測は **04 から始まって 10 まで**。1 から詰める理由は無い。"""
    figs = (_fig("x", 1, 4, "x"), _fig("y", 2, 9, "y"))
    got = layout.plan([], figs, {})
    assert [f.shots_no for f in got.missing] == [4, 9]


def test_実行番号が無い図版があってよい() -> None:
    """`08-line` は**手動で撮った**ので `shots.py` の番号を持たない。"""
    figs = (_fig("x", 1, 4, "x"), _fig("y", 2, None, "y"))
    got = layout.plan([], figs, {})
    assert [f.shots_no for f in got.missing] == [4, None]


def test_手動の図版は2つ以上あってよい() -> None:
    """**「番号が無い」は重複ではない。**

    手で撮ったものが2枚あるのは普通のこと。*欠けている者同士を
    「同じ番号だ」と数えると、正しい定義が弾かれる*。
    """
    figs = (_fig("x", 1, None, "x"), _fig("y", 2, None, "y"))
    got = layout.plan([], figs, {})
    assert [f.key for f in got.missing] == ["x", "y"]


# --------------------------------------------------------------------------
# 割付（H1・H2）
# --------------------------------------------------------------------------


def test_ハッシュで割り付ける() -> None:
    """**名前でも順番でもない。** 名前は変わりうるし、順番は意味を持たない。"""
    shot = _shot("a", 0)
    figs = (_fig("x", 1, 4, "verify-source"),)

    got = layout.plan([shot], figs, {shot.sha256: "x"})

    assert [p.figure.key for p in got.placements] == ["x"]
    assert got.placements[0].shot is shot


def test_撮影順と記事順が食い違っても記事順に並ぶ() -> None:
    """**これが課題3 の実測そのもの。**

    撮影1枚目が記事の `01`、撮影2枚目が記事の `04`、撮影3枚目が記事の `06`。
    *撮った順に並べると、記事の順にはならない。*
    """
    first, second, third = _shot("a", 0), _shot("b", 16), _shot("c", 17)
    figs = (
        _fig("verify_source", 1, 4, "verify-source"),
        _fig("to_doc", 2, 5, "to-doc"),
        _fig("verify_doc", 3, 6, "verify-doc"),
    )
    assignments = {
        first.sha256: "verify_source",
        second.sha256: "verify_doc",
        third.sha256: "to_doc",
    }

    got = layout.plan([first, second, third], figs, assignments)

    assert [p.figure.article_no for p in got.placements] == [1, 2, 3]
    assert [p.shot for p in got.placements] == [first, third, second]


def test_定義の並び順に関係なく記事順に並ぶ() -> None:
    """**上のテストだけでは、並べ替えを検査できていない。**

    定義を記事順に書いてしまうと、*並べ替えを消しても結果が変わらない*。
    `collect` の「名前順と撮影順がたまたま一致していた」と同じ罠なので、
    ここでは**定義をわざと逆順で渡す**。
    """
    a, b = _shot("a", 0), _shot("b", 1)
    figs = (_fig("y", 2, 5, "y"), _fig("x", 1, 4, "x"))

    got = layout.plan([a, b], figs, {a.sha256: "x", b.sha256: "y"})

    assert [p.figure.key for p in got.placements] == ["x", "y"]


def test_実行順でも取り出せる() -> None:
    """**両方の順で出せないと、呼ぶ側が並べ替えて取り違える。**"""
    a, b = _shot("a", 0), _shot("b", 1)
    figs = (_fig("x", 1, 9, "x"), _fig("y", 2, 4, "y"))

    got = layout.plan([a, b], figs, {a.sha256: "x", b.sha256: "y"})

    assert [p.figure.article_no for p in got.placements] == [1, 2]
    assert [p.figure.article_no for p in got.by_shots_order] == [2, 1]


def test_実行番号が無い図版は実行順の最後に回る() -> None:
    """手動で撮ったものは `shots.py` の列に居場所が無い。**落とさず、末尾に置く。**"""
    a, b = _shot("a", 0), _shot("b", 1)
    figs = (_fig("x", 1, None, "x"), _fig("y", 2, 4, "y"))

    got = layout.plan([a, b], figs, {a.sha256: "x", b.sha256: "y"})

    assert [p.figure.key for p in got.by_shots_order] == ["y", "x"]


def test_ファイル名は記事番号とスラグから作る() -> None:
    shot = _shot("a", 0)
    figs = (_fig("x", 1, 4, "verify-source"),)

    got = layout.plan([shot], figs, {shot.sha256: "x"})

    assert got.placements[0].filename == "01-verify-source.png"


def test_ファイル名の拡張子は原本から取る() -> None:
    """**原本が jpg なら jpg。** 勝手に png と名乗ると、中身と名前が食い違う。"""
    shot = _shot("a", 0, suffix=".jpg")
    figs = (_fig("x", 1, 4, "verify-source"),)

    got = layout.plan([shot], figs, {shot.sha256: "x"})

    assert got.placements[0].filename == "01-verify-source.jpg"


# --------------------------------------------------------------------------
# 没・欠け・孤児（H3・M1・M2）
# --------------------------------------------------------------------------


def test_割付の無い絵は没として残る() -> None:
    """**没を消さない。** 課題3 では10枚撮って2枚が没だった。

    消すと「撮ったが使わなかった」と「撮っていない」が区別できなくなる。
    """
    used, dropped = _shot("a", 0), _shot("b", 1)
    figs = (_fig("x", 1, 4, "x"),)

    got = layout.plan([used, dropped], figs, {used.sha256: "x"})

    assert got.rejected == (dropped,)


def test_没は撮影順のまま並ぶ() -> None:
    late, early = _shot("a", 30), _shot("b", 0)
    figs = (_fig("x", 1, 4, "x"),)
    used = _shot("c", 10)

    got = layout.plan([late, early, used], figs, {used.sha256: "x"})

    assert got.rejected == (early, late)


def test_絵の無い図版を欠けとして出す() -> None:
    """**M1：撮り忘れ。** 定義に行があるのに絵が無い。"""
    shot = _shot("a", 0)
    figs = (_fig("x", 1, 4, "x"), _fig("y", 2, 5, "y"))

    got = layout.plan([shot], figs, {shot.sha256: "x"})

    assert [f.key for f in got.missing] == ["y"]


def test_知らないキーへの割付を孤児として出す() -> None:
    """**M2：定義漏れ。** 割り付けた先が定義に無い。

    *片方向だけ検査すると必ず漏れる*（教訓 `consumers-do-not-prove-producers`）。
    """
    shot = _shot("a", 0)
    figs = (_fig("x", 1, 4, "x"),)

    got = layout.plan([shot], figs, {shot.sha256: "存在しない"})

    assert got.orphans == ("存在しない",)
    assert got.rejected == ()


def test_同じ図版に2枚割り付けられたら食い違いとして出す() -> None:
    """**どちらを使うか決まらないものを、黙って片方に決めない。**"""
    a, b = _shot("a", 0), _shot("b", 1)
    figs = (_fig("x", 1, 4, "x"),)

    got = layout.plan([a, b], figs, {a.sha256: "x", b.sha256: "x"})

    assert got.conflicts == {"x": (a, b)}
    assert got.placements == ()


def test_同じ中身の2枚は両方とも割り付けに当たる() -> None:
    """**H4：撮り直すと完全に同じ絵が2枚できる。**

    ハッシュで指している以上、どちらか1枚を指すことはできない。
    *曖昧さを曖昧なまま出す*のが正しく、勝手に1枚選ばない。
    """
    when_a = _shot("a", 0)
    same = collect.Shot(
        path=Path("原本") / "copy.png",
        sha256=when_a.sha256,
        captured_at=when_a.captured_at + timedelta(minutes=1),
        name_at=None,
        size=when_a.size,
    )
    figs = (_fig("x", 1, 4, "x"),)

    got = layout.plan([when_a, same], figs, {when_a.sha256: "x"})

    assert got.conflicts == {"x": (when_a, same)}


# --------------------------------------------------------------------------
# 判定
# --------------------------------------------------------------------------


def test_全部そろえば完成() -> None:
    a, b = _shot("a", 0), _shot("b", 1)
    figs = (_fig("x", 1, 4, "x"), _fig("y", 2, 5, "y"))

    got = layout.plan([a, b], figs, {a.sha256: "x", b.sha256: "y"})

    assert got.status == layout.COMPLETE


def test_没があっても完成を妨げない() -> None:
    """**没は異常ではない。** 撮り直しは普通に起きる。"""
    a, dropped = _shot("a", 0), _shot("b", 1)
    figs = (_fig("x", 1, 4, "x"),)

    got = layout.plan([a, dropped], figs, {a.sha256: "x"})

    assert got.rejected == (dropped,)
    assert got.status == layout.COMPLETE


def test_欠けがあれば未完成() -> None:
    a = _shot("a", 0)
    figs = (_fig("x", 1, 4, "x"), _fig("y", 2, 5, "y"))

    got = layout.plan([a], figs, {a.sha256: "x"})

    assert got.status == layout.INCOMPLETE


def test_孤児があれば未完成() -> None:
    a, b = _shot("a", 0), _shot("b", 1)
    figs = (_fig("x", 1, 4, "x"),)

    got = layout.plan([a, b], figs, {a.sha256: "x", b.sha256: "謎"})

    assert got.status == layout.INCOMPLETE


def test_食い違いがあれば未完成() -> None:
    a, b = _shot("a", 0), _shot("b", 1)
    figs = (_fig("x", 1, 4, "x"),)

    got = layout.plan([a, b], figs, {a.sha256: "x", b.sha256: "x"})

    assert got.status == layout.INCOMPLETE


def test_1枚も渡されなければ未完成() -> None:
    """**0件を成功にしない。** 全部が欠けているだけで、うまくいってはいない。"""
    figs = (_fig("x", 1, 4, "x"),)

    got = layout.plan([], figs, {})

    assert got.status == layout.INCOMPLETE
