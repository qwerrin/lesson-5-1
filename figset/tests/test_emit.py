"""figset/emit のテスト。**実装より先に書いた。**

`emit` は `docs/` に **4種類**を書き出す段。

=================== ==========================================================
出すもの             中身
=================== ==========================================================
`NN-slug.ext`       図版（**原本のコピー**。移動も削除もしない）
`README.md`         対応表（**2系統の番号を両方持つ**）
`figs.html`         記事に貼る `<img>` 断片
`figset.json`       台帳（採用も没も載せる）
=================== ==========================================================

============ ====================================================================
根拠         ここで守ること
============ ====================================================================
M5・M10      **`docs/` は記事に貼る場所。** 書くもの全部を伏せて通す
M6           同名を黙って上書きしない。created / replaced / unchanged を分ける
M1・M2       欠け・孤児・食い違いを**対応表に出す**。完成した顔をさせない
H3           没も台帳に残す（枚数だけでなく中身）
M9           1枚も書き出さないのを成功にしない
============ ====================================================================

書けたことは、読み戻せることの証拠にならない
--------------------------------------------------------------------------

教訓 `write-is-not-readback`。コピーしたら**読み戻してハッシュを照合する**。
`shutil.copy` が例外を出さなかったことは、*中身が同じであること*を言わない。

伏せるのは、ここでも同じ理由
--------------------------------------------------------------------------

`guard` は「送る前に見る」段だったが、`emit` は「**貼る前に書く**」段である。
`docs/README.md` は記事にそのまま引用されるので、
**原本のフルパス（＝ホームのパス）を書いた瞬間に漏れる**。
だから `emit` も `guard.Policy` を受け取り、**書くものを全部 `redact` に通す**。
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "figset"))

import collect  # noqa: E402
import emit  # noqa: E402
import guard  # noqa: E402
import layout  # noqa: E402

BASE = datetime(2026, 9, 14, 23, 0, 0)

#: **架空**のホーム。実在のものを使うと tmp_path 自身が当たる（test_guard と同じ理由）。
SECRET = r"C:\Users\dummyuser"
USER = "dummyuser"


def _policy() -> guard.Policy:
    return guard.Policy((guard.literal_rule("利用者名", USER),))


def _src(root: Path, name: str, body: bytes, minutes: int = 0) -> collect.Shot:
    root.mkdir(parents=True, exist_ok=True)
    path = root / name
    path.write_bytes(body)
    return collect.Shot(
        path=path,
        sha256=hashlib.sha256(body).hexdigest(),
        captured_at=BASE + timedelta(minutes=minutes),
        name_at=None,
        size=len(body),
    )


def _fig(
    key: str,
    article_no: int,
    shots_no: int | None,
    slug: str,
    *,
    caption: str = "",
    claim: str = "",
) -> layout.Figure:
    return layout.Figure(
        key=key,
        article_no=article_no,
        shots_no=shots_no,
        slug=slug,
        caption=caption or f"{key} を写す",
        claim=claim or f"{key} が証明すること",
        expects=(),
    )


def _one(tmp_path: Path) -> tuple[layout.Layout, Path]:
    """図版1枚だけの、完成した割付。"""
    shot = _src(tmp_path / "原本", "a.png", b"AAA")
    figs = (_fig("verify_source", 1, 4, "verify-source"),)
    plan = layout.plan([shot], figs, {shot.sha256: "verify_source"})
    return plan, tmp_path / "docs"


def _read(docs: Path, name: str) -> str:
    return (docs / name).read_text(encoding="utf-8")


# --------------------------------------------------------------------------
# コピー（原本を壊さない・読み戻す）
# --------------------------------------------------------------------------


def test_図版を記事番号の名前で書き出す(tmp_path: Path) -> None:
    plan, docs = _one(tmp_path)

    emit.emit(plan, docs, _policy())

    assert (docs / "01-verify-source.png").read_bytes() == b"AAA"


def test_原本を消さないし動かさない(tmp_path: Path) -> None:
    """**コピーであって、移動ではない。** 原本は照合の物差しとして残す。"""
    plan, docs = _one(tmp_path)
    source = plan.placements[0].shot.path

    emit.emit(plan, docs, _policy())

    assert source.exists()
    assert source.read_bytes() == b"AAA"


def test_書いたものを読み戻してハッシュで照合する(tmp_path: Path) -> None:
    """**例外が出なかったことは、中身が同じであることを言わない。**"""
    plan, docs = _one(tmp_path)

    got = emit.emit(plan, docs, _policy())

    assert got.verified == 1


def test_原本が後から書き換わっていたら照合が通らない(tmp_path: Path) -> None:
    """**上のテストだけでは、読み戻しを検査できていない。**

    `verified` をただ数え上げるだけの実装でも、正常な場合は同じ値になる。
    *集めたあとで原本が書き換わった*場合にだけ差が出る——そしてこれは
    実際に起きる（撮り直して同じ名前で保存した、など）。
    """
    shot = _src(tmp_path / "原本", "a.png", b"AAA")
    shot.path.write_bytes(b"BBB")  # 集めたあとで中身が変わった
    figs = (_fig("x", 1, 4, "x"),)
    plan = layout.plan([shot], figs, {shot.sha256: "x"})

    got = emit.emit(plan, tmp_path / "docs", _policy())

    assert got.verified == 0
    assert (tmp_path / "docs" / "01-x.png").read_bytes() == b"BBB"


def test_中身が同じなら書き換えない(tmp_path: Path) -> None:
    plan, docs = _one(tmp_path)
    emit.emit(plan, docs, _policy())

    got = emit.emit(plan, docs, _policy())

    assert got.action_of("01-verify-source.png") == emit.UNCHANGED


def test_中身が違えば置き換えたと記録する(tmp_path: Path) -> None:
    """**M6：同名で上書きしたことを黙らない。** 差分に出ないと気づけない。"""
    plan, docs = _one(tmp_path)
    docs.mkdir(parents=True, exist_ok=True)
    (docs / "01-verify-source.png").write_bytes(b"ZZZ")

    got = emit.emit(plan, docs, _policy())

    assert got.action_of("01-verify-source.png") == emit.REPLACED


def test_はじめて書いたものは作成と記録する(tmp_path: Path) -> None:
    plan, docs = _one(tmp_path)

    got = emit.emit(plan, docs, _policy())

    assert got.action_of("01-verify-source.png") == emit.CREATED


def test_書き出したものごとに結果を返す(tmp_path: Path) -> None:
    """**1つの結果で全部を代表させない。** 図版はそのままで対応表だけ変わる、は普通に起きる。"""
    plan, docs = _one(tmp_path)
    emit.emit(plan, docs, _policy())
    (docs / emit.README).write_text("手で書き換えた", encoding="utf-8")

    got = emit.emit(plan, docs, _policy())

    assert got.action_of("01-verify-source.png") == emit.UNCHANGED
    assert got.action_of(emit.README) == emit.REPLACED


def test_書き出していない名前を聞かれたら失敗する(tmp_path: Path) -> None:
    """**知らないものに既定値を返さない。** 「変更なし」は嘘になる。"""
    plan, docs = _one(tmp_path)
    got = emit.emit(plan, docs, _policy())

    with pytest.raises(KeyError):
        got.action_of("そんなファイルは無い.png")


def test_書き出す図版が1枚も無ければ成功にしない(tmp_path: Path) -> None:
    """**0枚を成功にしない。** 割付が全部欠けていても、書く先は作れてしまう。"""
    figs = (_fig("x", 1, 4, "x"),)
    plan = layout.plan([], figs, {})

    got = emit.emit(plan, tmp_path / "docs", _policy())

    assert got.verified == 0
    assert got.status == layout.INCOMPLETE


# --------------------------------------------------------------------------
# 対応表（README）
# --------------------------------------------------------------------------


def test_対応表に2系統の番号を両方載せる(tmp_path: Path) -> None:
    """**片方しか載せないと、読む側が取り違える。**

    `task3/docs/README.md` が自分で「番号が2系統ある。混ぜると必ず取り違える」
    と書いている。対応表はその取り違えを止めるために在る。
    """
    plan, docs = _one(tmp_path)

    emit.emit(plan, docs, _policy())
    text = _read(docs, "README.md")

    assert "01-verify-source.png" in text
    assert re.search(r"\|\s*04\s*\|", text)


def test_対応表に何を写すかと何を証明するかを載せる(tmp_path: Path) -> None:
    shot = _src(tmp_path / "原本", "a.png", b"AAA")
    figs = (
        _fig("x", 1, 4, "x", caption="議事録を台本と突き合わせる", claim="8件中2件しか一致しない"),
    )
    plan = layout.plan([shot], figs, {shot.sha256: "x"})

    emit.emit(plan, tmp_path / "docs", _policy())
    text = _read(tmp_path / "docs", "README.md")

    assert "議事録を台本と突き合わせる" in text
    assert "8件中2件しか一致しない" in text


def test_対応表の先頭で完成していないと言う(tmp_path: Path) -> None:
    """**完成した顔をさせない。** 欠けたまま記事に貼られるのを止める。"""
    shot = _src(tmp_path / "原本", "a.png", b"AAA")
    figs = (_fig("x", 1, 4, "x"), _fig("y", 2, 5, "y"))
    plan = layout.plan([shot], figs, {shot.sha256: "x"})

    emit.emit(plan, tmp_path / "docs", _policy())
    head = _read(tmp_path / "docs", "README.md").splitlines()[:12]

    assert any(layout.INCOMPLETE in line for line in head)


def test_対応表に欠けを載せる(tmp_path: Path) -> None:
    """**期待値を、出力側の固定文と衝突させない。**

    最初この図版のキーを `撮り忘れ` にしていたが、節の見出しが
    「絵が無い図版（**撮り忘れ**）」なので、*一覧を空にしても文字列は見つかった*
    ——2026-09-20 のミューテーションで素通りして分かった。
    **テンプレートに無い語で探す。**
    """
    shot = _src(tmp_path / "原本", "a.png", b"AAA")
    figs = (_fig("x", 1, 4, "x"), _fig("まだ撮っていない絵", 2, 5, "y"))
    plan = layout.plan([shot], figs, {shot.sha256: "x"})

    emit.emit(plan, tmp_path / "docs", _policy())

    assert "まだ撮っていない絵" in _read(tmp_path / "docs", "README.md")


def test_対応表に孤児を載せる(tmp_path: Path) -> None:
    shot = _src(tmp_path / "原本", "a.png", b"AAA")
    other = _src(tmp_path / "原本", "b.png", b"BBB", minutes=1)
    figs = (_fig("x", 1, 4, "x"),)
    plan = layout.plan([shot, other], figs, {shot.sha256: "x", other.sha256: "定義に無い名"})

    emit.emit(plan, tmp_path / "docs", _policy())

    assert "定義に無い名" in _read(tmp_path / "docs", "README.md")


def test_対応表に食い違いを載せる(tmp_path: Path) -> None:
    a = _src(tmp_path / "原本", "a.png", b"AAA")
    b = _src(tmp_path / "原本", "b.png", b"BBB", minutes=1)
    figs = (_fig("ぶつかり", 1, 4, "x"),)
    plan = layout.plan([a, b], figs, {a.sha256: "ぶつかり", b.sha256: "ぶつかり"})

    emit.emit(plan, tmp_path / "docs", _policy())

    assert "ぶつかり" in _read(tmp_path / "docs", "README.md")


def test_空の節も見出しを残す(tmp_path: Path) -> None:
    """**節ごと消すと、「見ていない」のか「無い」のかが分からない。**

    欠けが0件のとき見出しごと落とすと、*検査したうえで0件だった*ことが
    伝わらない。`journal` の空の見出しを消さない規則と同じ形。
    """
    plan, docs = _one(tmp_path)

    emit.emit(plan, docs, _policy())
    text = _read(docs, "README.md")

    assert "絵が無い図版" in text
    assert "（なし）" in text


def test_対応表に没を載せる(tmp_path: Path) -> None:
    """**没を消さない以上、対応表にも出す。** 10枚撮って2枚没は普通のこと。"""
    used = _src(tmp_path / "原本", "a.png", b"AAA")
    dropped = _src(tmp_path / "原本", "b.png", b"BBB", minutes=1)
    figs = (_fig("x", 1, 4, "x"),)
    plan = layout.plan([used, dropped], figs, {used.sha256: "x"})

    emit.emit(plan, tmp_path / "docs", _policy())
    text = _read(tmp_path / "docs", "README.md")

    assert dropped.sha256[:12] in text


# --------------------------------------------------------------------------
# 貼り付け用の HTML
# --------------------------------------------------------------------------


def test_HTMLは記事順に並ぶ(tmp_path: Path) -> None:
    a = _src(tmp_path / "原本", "a.png", b"AAA")
    b = _src(tmp_path / "原本", "b.png", b"BBB", minutes=1)
    figs = (_fig("x", 1, 9, "first"), _fig("y", 2, 4, "second"))
    plan = layout.plan([a, b], figs, {a.sha256: "x", b.sha256: "y"})

    emit.emit(plan, tmp_path / "docs", _policy())
    html = _read(tmp_path / "docs", "figs.html")

    assert html.index("01-first") < html.index("02-second")


def test_HTMLのsrcは書き出したファイル名(tmp_path: Path) -> None:
    plan, docs = _one(tmp_path)

    emit.emit(plan, docs, _policy())

    assert 'src="01-verify-source.png"' in _read(docs, "figs.html")


def test_HTMLは特殊文字を逃がす(tmp_path: Path) -> None:
    """**記事にそのまま貼るものなので、壊れた HTML を出さない。**

    `&` や `<` を素で出すと、貼った先で表示が崩れるか、消える。
    *画面で見た値は、保存されている値ではない*——崩れは後から気づく。
    """
    shot = _src(tmp_path / "原本", "a.png", b"AAA")
    figs = (_fig("x", 1, 4, "x", caption='A & B <script> "引用"'),)
    plan = layout.plan([shot], figs, {shot.sha256: "x"})

    emit.emit(plan, tmp_path / "docs", _policy())
    html = _read(tmp_path / "docs", "figs.html")

    assert "&amp;" in html
    assert "&lt;script&gt;" in html
    assert "<script>" not in html


# --------------------------------------------------------------------------
# 台帳
# --------------------------------------------------------------------------


def test_台帳に採用と没の両方を載せる(tmp_path: Path) -> None:
    used = _src(tmp_path / "原本", "a.png", b"AAA")
    dropped = _src(tmp_path / "原本", "b.png", b"BBB", minutes=1)
    figs = (_fig("x", 1, 4, "x"),)
    plan = layout.plan([used, dropped], figs, {used.sha256: "x"})

    emit.emit(plan, tmp_path / "docs", _policy())
    ledger = json.loads(_read(tmp_path / "docs", "figset.json"))

    assert [p["sha256"] for p in ledger["placements"]] == [used.sha256]
    assert [r["sha256"] for r in ledger["rejected"]] == [dropped.sha256]


def test_台帳はハッシュで原本を指す(tmp_path: Path) -> None:
    """**名前ではなくハッシュ。** 名前は採用時に変わる。"""
    plan, docs = _one(tmp_path)

    emit.emit(plan, docs, _policy())
    ledger = json.loads(_read(docs, "figset.json"))

    assert ledger["placements"][0]["sha256"] == plan.placements[0].shot.sha256


def test_台帳に状態を載せる(tmp_path: Path) -> None:
    plan, docs = _one(tmp_path)

    emit.emit(plan, docs, _policy())
    ledger = json.loads(_read(docs, "figset.json"))

    assert ledger["status"] == layout.COMPLETE
    assert ledger["complete"] is True


def test_台帳に欠けと孤児と食い違いを載せる(tmp_path: Path) -> None:
    """**台帳は機械で読む記録。** 対応表だけに書くと、道具からは見えない。

    *読む側（対応表）に出ていることは、書く側（台帳）に入っている証拠にならない*
    （教訓 `consumers-do-not-prove-producers`）。
    """
    a = _src(tmp_path / "原本", "a.png", b"AAA")
    b = _src(tmp_path / "原本", "b.png", b"BBB", minutes=1)
    c = _src(tmp_path / "原本", "c.png", b"CCC", minutes=2)
    figs = (_fig("ぶつかり", 1, 4, "x"), _fig("撮り忘れ", 2, 5, "y"))
    plan = layout.plan(
        [a, b, c],
        figs,
        {a.sha256: "ぶつかり", b.sha256: "ぶつかり", c.sha256: "定義に無い名"},
    )

    emit.emit(plan, tmp_path / "docs", _policy())
    ledger = json.loads(_read(tmp_path / "docs", "figset.json"))

    assert ledger["missing"] == ["撮り忘れ"]
    assert ledger["orphans"] == ["定義に無い名"]
    assert ledger["conflicts"] == {"ぶつかり": [a.sha256, b.sha256]}


def test_台帳は未完成を隠さない(tmp_path: Path) -> None:
    """**上のテストだけでは、状態を書き写しているかが分からない。**

    常に `complete` と書く実装でも、完成した場合は同じ値になる。
    *欠けている場合に差が出る*ので、そちらを見る。
    """
    shot = _src(tmp_path / "原本", "a.png", b"AAA")
    figs = (_fig("x", 1, 4, "x"), _fig("y", 2, 5, "y"))
    plan = layout.plan([shot], figs, {shot.sha256: "x"})

    emit.emit(plan, tmp_path / "docs", _policy())
    ledger = json.loads(_read(tmp_path / "docs", "figset.json"))

    assert ledger["status"] == layout.INCOMPLETE
    assert ledger["complete"] is False


# --------------------------------------------------------------------------
# 書き出すものが漏らさない（M5・M10）
# --------------------------------------------------------------------------


def test_原本のフルパスを書き出さない(tmp_path: Path) -> None:
    """**`docs/` は記事に貼る場所。** 原本のパスはホームの下にある。"""
    plan, docs = _one(tmp_path)
    source = plan.placements[0].shot.path

    emit.emit(plan, docs, _policy())

    for name in ("README.md", "figs.html", "figset.json"):
        assert str(source.parent) not in _read(docs, name)


def test_書き出すもの全部を伏せて通す(tmp_path: Path) -> None:
    """説明文にも原本の名前にも秘匿は入りうる。**出口を1つに絞って伏せる。**"""
    shot = _src(tmp_path / "原本", f"{USER}-の画面.png", b"AAA")
    figs = (_fig("x", 1, 4, "x", caption=f"保存先は {SECRET}", claim=f"{USER} の画面"),)
    plan = layout.plan([shot], figs, {shot.sha256: "x"})

    emit.emit(plan, tmp_path / "docs", _policy())

    for name in ("README.md", "figs.html", "figset.json"):
        assert USER not in _read(tmp_path / "docs", name)


def test_伏せてもファイル名は読める(tmp_path: Path) -> None:
    """伏せるのと、何も言わないのは別（`guard` と同じ）。"""
    plan, docs = _one(tmp_path)

    emit.emit(plan, docs, _policy())

    assert "01-verify-source.png" in _read(docs, "README.md")


def test_ポリシーを渡さずには呼べない(tmp_path: Path) -> None:
    plan, docs = _one(tmp_path)
    with pytest.raises(TypeError):
        emit.emit(plan, docs)  # type: ignore[call-arg]
