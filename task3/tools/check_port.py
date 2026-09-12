#!/usr/bin/env python3
"""移植したコードが、移植元と**文字単位で同じ**であることを確かめる。

使い方::

    .venv\\Scripts\\python.exe task3\\tools\\check_port.py

`common/docs_client.py` とそのテストは `lesson-4-3-2/task2` から持ってきた。
**「そのまま」は主張であって、証拠ではない。** 目視では、空白1つの違いも、
条件を1つ緩めた違いも見落とす。ここで機械に言わせる。

なぜ必要か
------------------------------------------------------------------

`common/google_auth.py` の移植では、docstring にこう書いた:

    実装は diff の docstring 部分以外が空になることで確認している

**それは1回やった話で、いま成り立っている保証にはならない。** 移植先を
あとから直せば静かに崩れる。*「移植した」と書いた文は、書いた瞬間から古くなる。*

**テストも対象にする。** 2026-09-13、移植したテストに一括置換をかけて
本体を書き換えてしまった（`batch_update_calls` → `batch_calls`）。
*実装だけ照合していたら、その事故は素通りしていた。*

移植元が別リポジトリなので、CI では回せない。**手元にある時だけ確かめる**
——移植元が見つからなければ「確かめられなかった」と言って終わる。
*見つからないことを「一致」として扱わない。*
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT.parent / "lesson-4-3-2" / "task2"

CREATE = SRC / "create_doc.py"
VERIFY = SRC / "verify_doc.py"

#: 移植先ごとに「どこから何を持ってきたか」と「新しく足したもの」を書く。
#: **`added` に書いていないものが現れたら、こっそり書き足されている。**
PORTS = [
    {
        "dst": ROOT / "common" / "docs_client.py",
        "from": [
            (CREATE, [
                "DocError", "normalize_newlines", "build_insert_requests",
                "document_url", "_api_message", "_looks_like_api_disabled",
                "_translate_http_error", "create_document", "insert_text",
                "create_document_with_text", "build_service",
            ]),
            (VERIFY, ["VerifyError", "fetch_document"]),
        ],
        "added": {"ensure_insertable", "insert_text_checked"},
        "constants": (CREATE, ["DEFAULT_SCOPES", "BODY_START_INDEX", "DOCUMENT_URL_TEMPLATE"]),
    },
    {
        "dst": ROOT / "common" / "tests" / "test_docs_client.py",
        "from": [
            (SRC / "tests" / "test_create_doc.py", [
                "FakeResponse", "make_http_error", "FakeRequest", "FakeDocuments",
                "FakeService", "documents", "service",
                "TestNormalizeNewlines", "TestBuildInsertRequests", "TestCreateDocument",
                "TestInsertText", "TestCreateDocumentWithText", "TestDocumentUrl",
                "TestErrors",
            ]),
        ],
        "added": {"TestEnsureInsertable"},
        "constants": None,
    },
    {
        "dst": ROOT / "common" / "tests" / "test_docs_client_read.py",
        "from": [
            (SRC / "tests" / "test_verify_doc.py", [
                "FakeResponse", "make_http_error", "FakeRequest", "FakeDocuments",
                "FakeService", "paragraph", "make_document", "document_with",
                "TestFetchDocument",
            ]),
        ],
        "added": set(),
        "constants": None,
    },
]


def sources_of(path: Path) -> dict[str, str]:
    """トップレベルの関数とクラスを、**元のソースの文字列のまま**取り出す。

    `ast.unparse` は使わない。整形し直すので、空白やコメントの違いが消えて
    **「同じに見える」ようになってしまう**。
    """
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines(keepends=True)
    found: dict[str, str] = {}
    for node in ast.parse(text).body:
        name = getattr(node, "name", None)
        if name is None:
            continue
        start = min([node.lineno] + [d.lineno for d in getattr(node, "decorator_list", [])]) - 1
        found[name] = "".join(lines[start : node.end_lineno]).rstrip(chr(10))
    return found


def values_of(path: Path, names: list[str]) -> dict[str, object]:
    """モジュールを実行せずに、単純な代入の値だけを読む。"""
    text = path.read_text(encoding="utf-8")
    out: dict[str, object] = {}
    for node in ast.parse(text).body:
        targets: list[str] = []
        if isinstance(node, ast.Assign):
            targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            targets = [node.target.id]
        for name in targets:
            if name in names and node.value is not None:
                try:
                    out[name] = ast.literal_eval(node.value)
                except ValueError:
                    pass
    return out


def main() -> int:
    if not SRC.exists():
        print("移植元が見つかりません: {}".format(SRC), file=sys.stderr)
        print("**確かめられませんでした。** 一致とは報告しません。", file=sys.stderr)
        return 1

    same = 0
    problems: list[str] = []

    for port in PORTS:
        dst_path: Path = port["dst"]
        if not dst_path.exists():
            problems.append("移植先が無い: {}".format(dst_path))
            continue
        dst = sources_of(dst_path)
        ported: set[str] = set()

        for src_path, names in port["from"]:
            src = sources_of(src_path)
            for name in names:
                ported.add(name)
                if name not in src:
                    problems.append("{}: 移植元に無い（{}）".format(name, src_path.name))
                elif name not in dst:
                    problems.append("{}: 移植先に無い（{}）".format(name, dst_path.name))
                elif dst[name] != src[name]:
                    problems.append(
                        "{}: 移植元と違う（{} → {}）".format(name, src_path.name, dst_path.name)
                    )
                else:
                    same += 1

        if port["constants"]:
            const_src, const_names = port["constants"]
            got, want = values_of(dst_path, const_names), values_of(const_src, const_names)
            for name in const_names:
                ported.add(name)
                if name not in want or name not in got:
                    problems.append("{}: 定数が片方に無い".format(name))
                elif got[name] != want[name]:
                    problems.append(
                        "{}: 定数の値が違う（{!r} vs {!r}）".format(name, want[name], got[name])
                    )
                else:
                    same += 1

        # **足したものが増えていないか。** 一覧に無いものが現れたら、
        # *移植のふりをして書き足されている*。
        extra = sorted(n for n in dst if n not in ported and n not in port["added"])
        if extra:
            problems.append(
                "{}: 一覧に無いものがある: {}".format(dst_path.name, ", ".join(extra))
            )

    print("照合した項目: {}".format(same + len(problems)))
    print("  一致      : {}".format(same))
    print("  食い違い  : {}".format(len(problems)))
    for port in PORTS:
        if port["added"]:
            print("  新規（対象外・{}）: {}".format(
                port["dst"].name, ", ".join(sorted(port["added"]))))
    if problems:
        print("")
        print("食い違い:")
        for p in problems:
            print("  -", p)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
