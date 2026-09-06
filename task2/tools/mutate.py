#!/usr/bin/env python3
"""課題2の実装を1か所ずつ壊して、テストが落ちることを確かめる。

**テストが通っていることは、守られていることの証拠にならない。**

使い方::

    .venv\\Scripts\\python.exe task2\\tools\\mutate.py

仕組みは課題1の ``task1/tools/mutate.py`` と同じ。リポジトリを一時ディレクトリへ
写し、**写した側だけ**を壊す。成果物には触らないので、途中で強制終了しても
壊れたまま残らない。**置換先が見つからない（NOT FOUND）は素通りと同じ扱い**にする
——実装を直して壊しかたを直し忘れると、何も壊さずに全部通って「穴ゼロ」と出る。

この課題で狙う失敗の形
------------------------------------------------------------------

守りたいのは「**例外にならず、静かに間違った値が残る**」失敗である。
シートは履歴なので、**間違った行は後から見ても間違いだと分からない**。

============================== ================================================
壊すと何が起きるか              なぜ静かなのか
============================== ================================================
改行を空白に置き換えない        API は正常終了する。セルが崩れて初めて分かる
実質価格の式を入れ替える        値は出る。**過去の行と比較できなくなるだけ**
価格が無いとき 0 を入れる        0 円は「値下がりした」に見える
失敗行の状態を「取得」にする    落ちた回が成功として履歴に残る
失敗行の列数を1つ減らす         追記でシートの列が丸ごとズレる
フラグを解釈して入れる          逆に解釈していたら履歴が全部嘘になる
時刻からオフセットを落とす      読む側の解釈次第で物差しが2本になる
============================== ================================================
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PYTHON = ROOT / ".venv" / "Scripts" / "python.exe"

TRANSFORM = "task2/transform.py"

IGNORE = shutil.ignore_patterns(
    ".venv", ".git", "__pycache__", ".pytest_cache", "docs", "*.png", "node_modules"
)

#: 課題2は common/ を壊さないので、回すのは課題2のテストだけでよい。
TEST_PATHS = ("task2/tests",)

# (対象ファイル, 壊した内容, 置換前, 置換後)
MUTATIONS: list[tuple[str, str, str, str]] = [
    (
        TRANSFORM,
        "改行・タブを空白に置き換えない（セルが壊れる）",
        '    for char in _BREAKING:\n        text = text.replace(char, " ")',
        "    pass",
    ),
    (
        TRANSFORM,
        "改行を空白ではなく削除する（語が繋がって別語になる）",
        '        text = text.replace(char, " ")',
        '        text = text.replace(char, "")',
    ),
    (
        TRANSFORM,
        "前後の空白を削らない",
        "    return text.strip()",
        "    return text",
    ),
    (
        TRANSFORM,
        "実質価格の式を「価格のN%」に入れ替える（100円未満の端数で割れる）",
        "    return item_price - (item_price // 100) * point_rate",
        "    return item_price - item_price * point_rate // 100",
    ),
    (
        TRANSFORM,
        "ポイントを引かない（実質価格が本体価格と同じになる）",
        "    return item_price - (item_price // 100) * point_rate",
        "    return item_price",
    ),
    (
        TRANSFORM,
        "価格が読めないときに 0 を入れる（値下がりに見える）",
        '        price = ""\n        rate_value = ""\n        effective = ""',
        "        price = 0\n        rate_value = 0\n        effective = 0",
    ),
    (
        TRANSFORM,
        "ポイント倍率が無いときに 1 とみなす（勝手に割り引く）",
        '    rate = item.get("pointRate", 0)',
        '    rate = item.get("pointRate", 1)',
    ),
    (
        TRANSFORM,
        "失敗行の状態を「取得」にする（落ちた回が成功として残る）",
        '    row[COLUMNS.index("状態")] = STATUS_FAILED',
        '    row[COLUMNS.index("状態")] = STATUS_OK',
    ),
    (
        TRANSFORM,
        "失敗行の理由の既定を空にする（区別できないことを書かない）",
        '    row[COLUMNS.index("理由")] = normalize_text(reason) or REASON_UNKNOWN',
        '    row[COLUMNS.index("理由")] = normalize_text(reason)',
    ),
    (
        TRANSFORM,
        "失敗行に itemCode を残さない（どれが落ちたか分からなくなる）",
        '    row[COLUMNS.index("itemCode")] = normalize_text(item_code)',
        "    pass",
    ),
    (
        TRANSFORM,
        "失敗行の列数を1つ減らす（追記でシートの列がズレる）",
        '    row: list[Any] = [""] * len(COLUMNS)',
        '    row: list[Any] = [""] * (len(COLUMNS) - 1)',
    ),
    (
        TRANSFORM,
        "送料フラグを解釈して入れる（逆だったら履歴が全部嘘になる）",
        '        item.get("postageFlag", ""),',
        '        "送料込み" if item.get("postageFlag") == 0 else "送料別",',
    ),
    (
        TRANSFORM,
        "時刻からオフセットを落とす（物差しが2本になる）",
        '    return datetime.now().astimezone().isoformat(timespec="seconds")',
        '    return datetime.now().isoformat(timespec="seconds")',
    ),
    (
        TRANSFORM,
        "本体価格を生で残さず実質価格だけ書く（式を変えたら過去と比較できない）",
        "        effective: Any = effective_price(price, rate_value)",
        "        effective = effective_price(price, rate_value)\n        price = effective",
    ),
]


def run_tests(work: Path) -> bool:
    """写した側でテストを回す。1件でも落ちたら True。"""
    proc = subprocess.run(
        [str(PYTHON), "-m", "pytest", *TEST_PATHS, "-x", "-q", "--no-header",
         "-p", "no:cacheprovider"],
        cwd=work,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return proc.returncode != 0


def main() -> int:
    if not PYTHON.exists():
        print(f"仮想環境の Python が見つかりません: {PYTHON}", file=sys.stderr)
        return 1

    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp) / "repo"
        shutil.copytree(ROOT, work, ignore=IGNORE)

        if run_tests(work):
            print("壊す前からテストが落ちています。先にそちらを直してください。", file=sys.stderr)
            return 1

        killed: list[str] = []
        survived: list[str] = []
        not_found: list[str] = []

        for index, (target, label, before, after) in enumerate(MUTATIONS, start=1):
            path = work / target
            original = path.read_text(encoding="utf-8", newline="")

            # **照合と書き込みは LF に正規化した文字列で行う。**
            # core.autocrlf の影響で .py が CRLF になりうる。newline="" のまま
            # `\n` を含むパターンを探すと複数行のパターンが一度もマッチせず、
            # 「置換先なし」が素通りと区別できないまま数字だけ出る。
            haystack = original.replace("\r\n", "\n")

            if haystack.count(before) != 1:
                not_found.append(
                    f"{index:3}. {label}（{target}・{haystack.count(before)}件一致）"
                )
                continue

            path.write_text(haystack.replace(before, after, 1), encoding="utf-8", newline="\n")
            if run_tests(work):
                killed.append(f"{index:3}. {label}")
            else:
                survived.append(f"{index:3}. {label}（{target}）")
            path.write_text(original, encoding="utf-8", newline="")

        print(f"壊した箇所: {len(MUTATIONS)}")
        print(f"  kill（テストが落ちた）: {len(killed)}")
        print(f"  素通り: {len(survived)}")
        print(f"  置換先なし: {len(not_found)}")

        if survived:
            print("\n素通りしたもの（テストが守っていない）:")
            for line in survived:
                print(f"  {line}")
        if not_found:
            print("\n置換先が見つからなかったもの（壊しかたが古い）:")
            for line in not_found:
                print(f"  {line}")

    # **置換先なしを成功にしない。** 何も壊さずに全部通ると「穴ゼロ」に見える。
    return 0 if not survived and not not_found else 1


if __name__ == "__main__":
    raise SystemExit(main())
