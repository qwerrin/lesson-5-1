"""画面に出す文字列そのものを検査する。

**端末は文書ではない。** ソースの docstring やコメントに markdown を書くのは
読みやすさのためだが、`print` の中に入れると*記号がそのまま画面に出る*。
その画面はスクリーンショットになり、記事に貼られる。

2026-09-14、`verify_doc.py` の最後の行が
`**ただし、これは出力側の検査である。**` と出ているのを、
**撮った絵を見て**気づいた。*出す前に機械で見れば、絵を撮り直さずに済んだ。*

**docstring とコメントは対象にしない。** あそこは読む場所であって、
出す場所ではない——`ast` で `print` / `say` の引数だけを見る。
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

#: 画面に出す側のモジュール。**共有部品も入れる**——
#: 呼ばれ方が違うだけで、出る先は同じ画面である。
SOURCES = sorted(
    [*(ROOT / "task3").glob("*.py"), *(ROOT / "task3" / "tools").glob("*.py"),
     *(ROOT / "common").glob("*.py")]
)

#: 画面へ出す関数。`say` は落ちない表示のための薄い包み。
PRINTERS = {"print", "say"}

#: 画面に出してはいけない記号。
FORBIDDEN = ("**", "`")


def printed_strings(path: Path):
    """`print` / `say` に渡している文字列だけを、行番号つきで返す。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
        if name not in PRINTERS:
            continue
        for arg in ast.walk(node):
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                yield node.lineno, arg.value


def test_対象のモジュールが空でない():
    """**対象が空なら、判定が正しくても同じ緑になる。**"""
    assert len(SOURCES) >= 8
    assert any(p.name == "verify_doc.py" for p in SOURCES)


def test_画面に出す文字列にmarkdownを混ぜない():
    bad = []
    for path in SOURCES:
        for lineno, text in printed_strings(path):
            for mark in FORBIDDEN:
                if mark in text:
                    bad.append("{}:{} {!r}".format(path.name, lineno, text[:60]))
    assert not bad, "画面に markdown が出る:\n" + "\n".join(bad)


def test_この検査そのものが効いている(tmp_path):
    """**手元が綺麗な状態でだけ試すと、何も検出しない判定でも緑になる。**"""
    sample = tmp_path / "x.py"
    sample.write_text('print("**強調**")', encoding="utf-8")
    found = [t for _, t in printed_strings(sample)]
    assert any("**" in t for t in found)


def test_docstringは対象にしない(tmp_path):
    """読む場所と出す場所は別。**docstring の markdown は残してよい。**"""
    sample = tmp_path / "y.py"
    sample.write_text('"""**強調した説明**"""\nprint("ふつうの行")', encoding="utf-8")
    assert [t for _, t in printed_strings(sample)] == ["ふつうの行"]
