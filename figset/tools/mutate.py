#!/usr/bin/env python3
"""figset を1か所ずつ壊して、テストが落ちることを確かめる。

**テストが通っていることは、守られていることの証拠にならない。**

使い方::

    .venv\\Scripts\\python.exe figset\\tools\\mutate.py

仕組みは課題1〜3 の ``mutate.py`` と同じ。リポジトリを一時ディレクトリへ写し、
**写した側だけ**を壊す。成果物には触らないので、途中で強制終了しても
壊れたまま残らない。**置換先が見つからない（NOT FOUND）は素通りと同じ扱い**にする
——実装を直して壊しかたを直し忘れると、何も壊さずに全部通って「穴ゼロ」と出る
（教訓 `a-mutation-must-actually-mutate`）。

`collect` で狙う失敗の形
------------------------------------------------------------------

`collect` は**数え方**の段なので、狙うのは「**件数は出るのに、その件数が嘘**」
という失敗になる。落ちないし、例外も出ない。

================================== ==============================================
壊すと何が起きるか                  なぜ静かなのか
================================== ==============================================
最初のルートしか見ない              月をまたいだぶんが**黙って消える**（H6）
`.pdf` も図版として拾う             枚数が増えるだけ。**エラーは出ない**
拡張子の大小を区別する              `.PNG` が**1枚も拾われない**
並べない                            番号の割付が**撮影順でなくなる**（H2）
同時刻の順を名前で決めない          実行のたびに**順が入れ替わる**
0件を成功にする                     保存先を間違えた状態が**順調に見える**（M9）
確認不能を成功にする                見ていない場所があるのに**件数を言う**（M7）
存在しないルートを見たことにする    同上。しかも `missing_roots` が空で出る
期間の下端を落とす                  境界の1枚が**静かに消える**
期間の上端を落とす                  同上
重複を全部挙げる                    1枚しかない絵まで「撮り直し」に見える
重複を挙げない                      **同じ絵が2枚あることに気づけない**（H4）
中身を読まずにハッシュを作る        全部同じハッシュ。**全部が重複に化ける**
読めない名前を mtime で埋める       *読めなかったこと*が**揃っていたこと**に化ける
読めない名前を食い違い扱いにする    リネーム済みが**毎回鳴る**。本物と同じ見た目
食い違いの閾値を無限にする          **どんなにズレても食い違いと言わない**
名前の日時を撮影時刻に採用する      リネームで**撮影時刻が変わる**（H1）
ルートに既定値を置く                値がずれたとき**黙って検査ゼロ**（H7）
フォルダを画像として拾う            枚数が増える。**中身を読む段で初めて落ちる**
================================== ==============================================
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PYTHON = ROOT / ".venv" / "Scripts" / "python.exe"

COLLECT = "figset/collect.py"

IGNORE = shutil.ignore_patterns(
    ".venv", ".git", "__pycache__", ".pytest_cache", ".pytest_tmp",
    "docs", "*.png", "*.wav", "_parts", "node_modules",
)

#: **範囲を広げ忘れると、壊したのにテストが1件も走らず「素通り」に見える。**
#: 判定が正しくても対象が空なら同じ緑になる（教訓 `scan-scope-misses-the-real-target`）。
TEST_PATHS = ("figset/tests",)

# (対象ファイル, 壊した内容, 置換前, 置換後)
MUTATIONS: list[tuple[str, str, str, str]] = [
    # ------------------------------------------------------------ 集める範囲
    (
        COLLECT,
        "最初のルートしか見ない（月をまたいだぶんが黙って消える）",
        "    for root in roots:",
        "    for root in roots[:1]:",
    ),
    (
        COLLECT,
        "`.pdf` も図版として拾う（スクショでないものが混ざる）",
        'IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".webp"})',
        'IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".webp", ".pdf"})',
    ),
    (
        COLLECT,
        "拡張子の大小を区別する（`.PNG` が1枚も拾われない）",
        "        if path.suffix.lower() not in IMAGE_SUFFIXES:",
        "        if path.suffix not in IMAGE_SUFFIXES:",
    ),
    (
        COLLECT,
        "フォルダを画像として拾う（中身を読む段で初めて落ちる）",
        "        if not path.is_file():",
        "        if False:",
    ),
    (
        COLLECT,
        "ルートに既定値を置く（値がずれたとき黙って検査ゼロ）",
        "    roots: Sequence[Path],",
        "    roots: Sequence[Path] = (),",
    ),
    # ------------------------------------------------------------------ 順序
    (
        COLLECT,
        "並べない（番号の割付が撮影順でなくなる）",
        "    shots.sort(key=lambda s: (s.captured_at, s.path.name))",
        "    pass",
    ),
    (
        COLLECT,
        "同時刻の順を名前で決めない（実行のたびに順が入れ替わる）",
        "    shots.sort(key=lambda s: (s.captured_at, s.path.name))",
        "    shots.sort(key=lambda s: s.captured_at)",
    ),
    # ------------------------------------------------------ 数えきれなかったこと
    (
        COLLECT,
        "0件を成功にする（保存先を間違えた状態が順調に見える）",
        "        if not self.shots:",
        "        if False:",
    ),
    (
        COLLECT,
        "確認不能を成功にする（見ていない場所があるのに件数を言う）",
        "        if self.missing_roots:",
        "        if False:",
    ),
    (
        COLLECT,
        "存在しないルートを見たことにする（missing_roots が空で出る）",
        "            missing.append(root)",
        "            scanned.append(root)",
    ),
    (
        COLLECT,
        "存在の確認をしない（無いフォルダを読みに行く）",
        "        if not root.is_dir():",
        "        if False:",
    ),
    # ------------------------------------------------------------------ 期間
    (
        COLLECT,
        "期間の下端を落とす（境界の1枚が静かに消える）",
        "        if since is not None and captured_at < since:",
        "        if since is not None and captured_at <= since:",
    ),
    (
        COLLECT,
        "期間の上端を落とす（境界の1枚が静かに消える）",
        "        if until is not None and captured_at > until:",
        "        if until is not None and captured_at >= until:",
    ),
    # ------------------------------------------------------------ ハッシュ・重複
    (
        COLLECT,
        "中身を読まずにハッシュを作る（全部が重複に化ける）",
        "            digest.update(chunk)",
        "            pass",
    ),
    (
        COLLECT,
        "重複を全部挙げる（1枚しかない絵まで撮り直しに見える）",
        "if len(paths) > 1}",
        "if len(paths) > 0}",
    ),
    (
        COLLECT,
        "重複を挙げない（同じ絵が2枚あることに気づけない）",
        "if len(paths) > 1}",
        "if False}",
    ),
    # --------------------------------------------------------- 名前と mtime（H1）
    (
        COLLECT,
        "名前の日時を撮影時刻に採用する（リネームで撮影時刻が変わる）",
        "        captured_at = datetime.fromtimestamp(stat.st_mtime)",
        "        captured_at = _name_time(path) or datetime.fromtimestamp(stat.st_mtime)",
    ),
    (
        COLLECT,
        "読めない名前を mtime で埋める（読めなかったことが揃っていたことに化ける）",
        "        return None",
        "        return datetime.fromtimestamp(path.stat().st_mtime)",
    ),
    (
        COLLECT,
        "読めない名前を食い違い扱いにする（リネーム済みが毎回鳴る）",
        "            return False",
        "            return True",
    ),
    (
        COLLECT,
        "食い違いの閾値を無限にする（どんなにズレても食い違いと言わない）",
        "NAME_TIME_TOLERANCE = timedelta(seconds=2)",
        "NAME_TIME_TOLERANCE = timedelta(days=3650)",
    ),
]


def run_tests(work: Path) -> bool:
    """写した側でテストを回す。1件でも落ちたら True。"""
    proc = subprocess.run(
        # --basetemp を写した側の中に置く。既定の %TEMP% を使うと後片付けで
        # PermissionError が出て、**壊す前から落ちている**ように見える。
        [str(PYTHON), "-m", "pytest", *TEST_PATHS, "-x", "-q", "--no-header",
         "-p", "no:cacheprovider", "--basetemp", str(work / ".pytest_tmp")],
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

            # **照合も書き込みも LF に正規化した文字列で行う。**
            # core.autocrlf で .py が CRLF になりうる。newline="" のまま複数行の
            # パターンを探すと一度もマッチせず、素通りと区別が付かない。
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
