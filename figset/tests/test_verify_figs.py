"""figset/verify_figs のテスト。**実装より先に書いた。**

ここが **課題2（5-1-1）の講評への答え**である。

> ただ商品リンクにアクセスし、**取得した情報が正しいかどうかまで確認**して
> スクショをとれるとより良いですね！

この指摘は **3回続いた**（教訓 `assignment-verify-against-the-source`）。
課題3 で `verify_source.py` を作って答え、課題3 の講評では消えた。
**`figset` でも同じ筋を通す。**

見る相手が3つある
--------------------------------------------------------------------------

=============== ==============================================================
定義 ↔ 出力      定義にある図版が `docs/` にあるか／`docs/` の絵が定義にあるか
出力 ↔ 出力      README の行・`figs.html` の `src` が実在するか
**ソース側**     対応表の主張を持って**絵そのものを開き**、中に実在するか
=============== ==============================================================

**README を読み返すのは照合ではない。ソースは画像。**

============ ====================================================================
根拠         ここで守ること
============ ====================================================================
M1           定義に行があるのに絵が無い（撮り忘れ）
M2           絵があるのに定義に無い（貼り忘れ）。**双方向で見る**
M3           記事に貼ったが表示されていない（`src` の指す先が無い）
M4           **照合0件を「一致」にしない**
M8           **読めなかったものを「問題なし」に倒さない**
============ ====================================================================

読み手は差し込み式
--------------------------------------------------------------------------

`guard` の `ocr` と同じ。渡さなければ**ソース側は見ていない**と申告し、
*見ていない層があるかぎり合格にしない*。

問い方は 2026-09-19 の疎通確認（`DESIGN.md` 9-1）で決めた形を使う——
**主張を投げて「一致/不一致」を返させる**。#5 で、対応表に `117` と書いて
画像が `119` のとき**「不一致」＋実際の値**が返ることを確かめてある。
*照合ツールの本当の失敗は、主張に同意してしまうこと*だった。
"""

from __future__ import annotations

import hashlib
import sys
from datetime import datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "figset"))

import collect  # noqa: E402
import emit  # noqa: E402
import guard  # noqa: E402
import layout  # noqa: E402
import verify_figs  # noqa: E402

BASE = datetime(2026, 9, 14, 23, 0, 0)


def _policy() -> guard.Policy:
    return guard.Policy((guard.literal_rule("利用者名", "dummyuser"),))


def _fig(
    key: str,
    article_no: int,
    slug: str,
    *,
    expects: tuple[str, ...] = ("8件中2件",),
) -> layout.Figure:
    return layout.Figure(
        key=key,
        article_no=article_no,
        shots_no=article_no + 3,
        slug=slug,
        caption=f"{key} を写す",
        claim=f"{key} が証明すること",
        expects=expects,
    )


def _docs(tmp_path: Path, figures: tuple[layout.Figure, ...]) -> Path:
    """**本物の `emit` で `docs/` を作る。** 手で組むと、出力の形がずれても気づけない。"""
    shots = []
    assignments = {}
    for index, figure in enumerate(figures):
        body = f"絵{index}".encode()
        path = tmp_path / "原本" / f"{index}.png"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
        shot = collect.Shot(
            path=path,
            sha256=hashlib.sha256(body).hexdigest(),
            captured_at=BASE,
            name_at=None,
            size=len(body),
        )
        shots.append(shot)
        assignments[shot.sha256] = figure.key

    plan = layout.plan(shots, figures, assignments)
    docs = tmp_path / "docs"
    emit.emit(plan, docs, _policy())
    return docs


def _reader(verdict: str, note: str = "") -> verify_figs.Reader:
    return lambda _path, _expect: verify_figs.Answer(verdict, note)


# --------------------------------------------------------------------------
# 定義 ↔ 出力（M1・M2）
# --------------------------------------------------------------------------


def test_定義にある図版のファイルが無ければ不合格(tmp_path: Path) -> None:
    """**M1：撮り忘れ。** 対応表に行があるのに絵が無い。"""
    figs = (_fig("x", 1, "x"), _fig("y", 2, "y"))
    docs = _docs(tmp_path, figs)
    (docs / "02-y.png").unlink()

    got = verify_figs.verify(figs, docs, reader=_reader(verify_figs.MATCH))

    assert got.missing_files == ("02-y.png",)
    assert got.status == verify_figs.FAILED


def test_定義に無いファイルが混ざっていたら不合格(tmp_path: Path) -> None:
    """**M2：貼り忘れ。** *片方向だけ検査すると必ず漏れる*。"""
    figs = (_fig("x", 1, "x"),)
    docs = _docs(tmp_path, figs)
    (docs / "09-まぎれこみ.png").write_bytes(b"?")

    got = verify_figs.verify(figs, docs, reader=_reader(verify_figs.MATCH))

    assert got.stray_files == ("09-まぎれこみ.png",)
    assert got.status == verify_figs.FAILED


def test_同じ図版に複数のファイルがあれば不合格(tmp_path: Path) -> None:
    """**どれを貼ったのか決まらない。** 拡張子違いは静かに増える。

    `status` だけ見ると弱い——**1枚を勝手に選んで残りを「紛れ込み」に落としても
    不合格にはなる**ので、*曖昧として挙げたこと*まで確かめる。
    """
    figs = (_fig("x", 1, "x"),)
    docs = _docs(tmp_path, figs)
    (docs / "01-x.jpg").write_bytes(b"?")

    got = verify_figs.verify(figs, docs, reader=_reader(verify_figs.MATCH))

    assert got.ambiguous_files == ("01-x.jpg", "01-x.png")
    assert got.stray_files == ()
    assert got.status == verify_figs.FAILED


def test_定義の並び順に関係なく記事順に報告する(tmp_path: Path) -> None:
    """**定義を記事順に書いていると、並べ替えを消しても結果が変わらない。**

    `collect` と `layout` で2回踏んだ罠なので、ここでは**わざと逆順で渡す**。
    """
    figs = (_fig("y", 2, "y"), _fig("x", 1, "x"))
    docs = _docs(tmp_path, figs)
    (docs / "01-x.png").unlink()
    (docs / "02-y.png").unlink()

    got = verify_figs.verify(figs, docs, reader=_reader(verify_figs.MATCH))

    assert got.missing_files == ("01-x.png", "02-y.png")


# --------------------------------------------------------------------------
# 出力 ↔ 出力（M3）
# --------------------------------------------------------------------------


def test_対応表に行が無ければ不合格(tmp_path: Path) -> None:
    figs = (_fig("x", 1, "x"),)
    docs = _docs(tmp_path, figs)
    (docs / emit.README).write_text("# 図版\n（表が消えた）\n", encoding="utf-8")

    got = verify_figs.verify(figs, docs, reader=_reader(verify_figs.MATCH))

    assert got.unlisted == ("01-x.png",)
    assert got.status == verify_figs.FAILED


def test_HTMLにsrcが無ければ不合格(tmp_path: Path) -> None:
    """**M3：貼ったのに表示されない。** `src` の指す先が無いのと同じ形。"""
    figs = (_fig("x", 1, "x"),)
    docs = _docs(tmp_path, figs)
    (docs / emit.HTML).write_text("<figure></figure>\n", encoding="utf-8")

    got = verify_figs.verify(figs, docs, reader=_reader(verify_figs.MATCH))

    assert got.unlinked == ("01-x.png",)
    assert got.status == verify_figs.FAILED


# --------------------------------------------------------------------------
# ソース側（★ここが本題）
# --------------------------------------------------------------------------


def test_主張が画像にあれば一致(tmp_path: Path) -> None:
    figs = (_fig("x", 1, "x", expects=("8件中2件",)),)
    docs = _docs(tmp_path, figs)

    got = verify_figs.verify(figs, docs, reader=_reader(verify_figs.MATCH))

    assert [c.verdict for c in got.checks] == [verify_figs.MATCH]
    assert got.status == verify_figs.VERIFIED


def test_主張が画像に無ければ不一致(tmp_path: Path) -> None:
    """**対応表に 117 と書いて画像が 119 のとき、不一致と言えること。**

    *照合ツールの本当の失敗は、主張に同意してしまうこと*
    （`DESIGN.md` 9-1 の #5 で実測済み）。
    """
    figs = (_fig("x", 1, "x", expects=("kill 117件",)),)
    docs = _docs(tmp_path, figs)

    got = verify_figs.verify(figs, docs, reader=_reader(verify_figs.MISMATCH, "実際は 119"))

    assert [c.verdict for c in got.checks] == [verify_figs.MISMATCH]
    assert got.checks[0].note == "実際は 119"
    assert got.status == verify_figs.FAILED


def test_読めなかったものを一致にしない(tmp_path: Path) -> None:
    """**M8：読めなかったことと、問題が無かったことは別。**"""
    figs = (_fig("x", 1, "x"),)
    docs = _docs(tmp_path, figs)

    got = verify_figs.verify(figs, docs, reader=_reader(verify_figs.UNREADABLE))

    assert got.matched == 0
    assert got.status == verify_figs.UNKNOWN


def test_読み手を渡さなければソース側は確認不能(tmp_path: Path) -> None:
    """**見ていない層があるかぎり合格にしない**（`guard` と同じ規律）。"""
    figs = (_fig("x", 1, "x"),)
    docs = _docs(tmp_path, figs)

    got = verify_figs.verify(figs, docs)

    assert verify_figs.SOURCE in got.unchecked
    assert got.status == verify_figs.UNKNOWN


def test_主張が1つも無い図版を確認不能にする(tmp_path: Path) -> None:
    """**★ 照合0件を「一致」にしない**（M4・`verify_doc` の 5-O と同じ形）。

    `expects` が空だと問う相手が無いので、*何も聞かずに全部一致*になる。
    """
    figs = (_fig("x", 1, "x", expects=()),)
    docs = _docs(tmp_path, figs)

    got = verify_figs.verify(figs, docs, reader=_reader(verify_figs.MATCH))

    assert got.no_expectations == ("x",)
    assert got.status == verify_figs.UNKNOWN


def test_主張ごとに1件数える(tmp_path: Path) -> None:
    """**件数を必ず出す。** 「問題なし」だけでは、何件見たかが分からない。"""
    figs = (_fig("x", 1, "x", expects=("8件中2件", "所見7件", "119/119")),)
    docs = _docs(tmp_path, figs)

    got = verify_figs.verify(figs, docs, reader=_reader(verify_figs.MATCH))

    assert got.matched == 3
    assert got.total == 3


def test_読み手には書き出した絵と主張を渡す(tmp_path: Path) -> None:
    """**ソースは「貼る絵」。** 原本ではなく `docs/` のファイルを読ませる。

    記事を読む人が見るのは `docs/` に置いたほうで、*原本ではない*。
    """
    figs = (_fig("x", 1, "x", expects=("8件中2件",)),)
    docs = _docs(tmp_path, figs)
    seen: list[tuple[Path, str]] = []

    def watcher(path: Path, expect: str) -> verify_figs.Answer:
        seen.append((path, expect))
        return verify_figs.Answer(verify_figs.MATCH)

    verify_figs.verify(figs, docs, reader=watcher)

    assert seen == [(docs / "01-x.png", "8件中2件")]


def test_絵が無い図版はソース側に問わない(tmp_path: Path) -> None:
    """**無いものを読ませない。** 読めるはずがないので、不一致でも確認不能でもない。"""
    figs = (_fig("x", 1, "x"),)
    docs = _docs(tmp_path, figs)
    (docs / "01-x.png").unlink()
    seen: list[Path] = []

    def watcher(path: Path, _expect: str) -> verify_figs.Answer:
        seen.append(path)
        return verify_figs.Answer(verify_figs.MATCH)

    got = verify_figs.verify(figs, docs, reader=watcher)

    assert seen == []
    assert got.status == verify_figs.FAILED


# --------------------------------------------------------------------------
# 判定
# --------------------------------------------------------------------------


def test_全部そろって全部一致なら合格(tmp_path: Path) -> None:
    figs = (_fig("x", 1, "x"), _fig("y", 2, "y"))
    docs = _docs(tmp_path, figs)

    got = verify_figs.verify(figs, docs, reader=_reader(verify_figs.MATCH))

    assert got.status == verify_figs.VERIFIED
    assert got.unchecked == ()


def test_不一致は確認不能より強い(tmp_path: Path) -> None:
    """**危ないと分かったものを保留にしない**（`guard` と同じ順序）。"""
    figs = (_fig("x", 1, "x"), _fig("y", 2, "y", expects=()))
    docs = _docs(tmp_path, figs)

    got = verify_figs.verify(figs, docs, reader=_reader(verify_figs.MISMATCH))

    assert got.no_expectations == ("y",)
    assert got.status == verify_figs.FAILED


def test_図版が1つも無ければ合格にしない(tmp_path: Path) -> None:
    """**0件を合格にしない。** 見る対象が空でも、検査は「異常なし」と答えてしまう。"""
    docs = tmp_path / "docs"
    docs.mkdir()

    with pytest.raises(ValueError):
        verify_figs.verify((), docs, reader=_reader(verify_figs.MATCH))


def test_書き出していない場所は確認不能(tmp_path: Path) -> None:
    """**M7：物差しが無い状態で「一致」と言わない。**

    `docs/` ごと無いときは、*欠けている*のではなく*確かめられない*。
    """
    figs = (_fig("x", 1, "x"),)

    got = verify_figs.verify(figs, tmp_path / "まだ無い", reader=_reader(verify_figs.MATCH))

    assert got.status == verify_figs.UNKNOWN
    assert verify_figs.OUTPUT in got.unchecked
