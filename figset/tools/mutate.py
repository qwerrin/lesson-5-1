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

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PYTHON = ROOT / ".venv" / "Scripts" / "python.exe"

COLLECT = "figset/collect.py"
GUARD = "figset/guard.py"
LAYOUT = "figset/layout.py"

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
    # ================================================================== guard
    # **ここで狙うのは「止めなかったこと」と「言わなかったこと」。**
    # どちらも例外を出さず、画面上は平穏に見える。
    # ------------------------------------------------------------ ポリシー
    (
        GUARD,
        "空のポリシーを許す（何を通しても見つかりませんでしたと答える）",
        "        if not self.rules:",
        "        if False:",
    ),
    (
        GUARD,
        "規則の大小を区別する（`C:\\USERS\\...` が静かに素通りする）",
        "    return Rule(name, re.compile(re.escape(text), re.IGNORECASE))",
        "    return Rule(name, re.compile(re.escape(text)))",
    ),
    (
        GUARD,
        "そのままの文字列を正規表現として扱う（記号が意味を持ってしまう）",
        "re.compile(re.escape(text), re.IGNORECASE)",
        "re.compile(text, re.IGNORECASE)",
    ),
    (
        GUARD,
        "ポリシーに既定値を置く（値がずれたとき黙って検査ゼロ）",
        "    policy: Policy,",
        "    policy: Policy = Policy((Rule('なし', re.compile(r'(?!x)x')),)),",
    ),
    # ---------------------------------------------------------------- 走査
    (
        GUARD,
        "当たっていない規則まで返す（全部が秘匿に見える）",
        "    return tuple(rule for rule in policy.rules if rule.pattern.search(text))",
        "    return tuple(rule for rule in policy.rules)",
    ),
    (
        GUARD,
        "何も当てない（すべて綺麗に見える）",
        "    return tuple(rule for rule in policy.rules if rule.pattern.search(text))",
        "    return ()",
    ),
    # ------------------------------------------------------------- path 層
    (
        GUARD,
        "パスを見ない（名前に入った秘匿を見逃す）",
        "    for rule in _scan(str(path), policy):",
        '    for rule in _scan("", policy):',
    ),
    (
        GUARD,
        "パスを見たと申告しない（見た層と見ていない層の区別が消える）",
        "    checked.append(PATH)",
        "    pass",
    ),
    # --------------------------------------------------------- metadata 層
    (
        GUARD,
        "読めなかったメタデータを見たことにする（欠けたぶんが安全側に化ける）",
        "        unchecked.append(METADATA)",
        "        checked.append(METADATA)",
    ),
    (
        GUARD,
        "空と読めなかったを混ぜる（文字チャンクが無いだけで未検査になる）",
        "    if texts is None:",
        "    if not texts:",
    ),
    (
        GUARD,
        "PNG の署名を見ない（PNG でないものを読んだことにする）",
        "    if not data.startswith(_PNG_SIGNATURE):",
        "    if False:",
    ),
    (
        GUARD,
        "途中で切れていても読めたぶんで済ます（切れた先が安全側に化ける）",
        "        if end + 4 > len(data):\n            return None  # 途中で切れている",
        "        if False:\n            return None  # 途中で切れている",
    ),
    (
        GUARD,
        "IEND に辿り着かなくても読めたことにする",
        "    return None  # IEND に辿り着いていない",
        "    return chunks  # IEND に辿り着いていない",
    ),
    (
        GUARD,
        "zTXt を見ない（圧縮された文字チャンクが素通りする）",
        '        elif tag == b"zTXt":',
        "        elif False:",
    ),
    (
        GUARD,
        "iTXt を見ない（日本語の文字チャンクが素通りする）",
        '        elif tag == b"iTXt":',
        "        elif False:",
    ),
    (
        GUARD,
        "チャンクのキーを走査しない（キー自体が秘匿のとき当たらない）",
        '            for rule in _scan(f"{key}\\n{value}", policy):',
        "            for rule in _scan(value, policy):",
    ),
    (
        GUARD,
        "当たった場所の名前を伏せない（場所の名前が秘匿でありうる）",
        '            where = f"{tag}:{redact(key, policy)}"',
        '            where = f"{tag}:{key}"',
    ),
    # ------------------------------------------------------------ pixels 層
    (
        GUARD,
        "OCR を渡しても画素を見ない（差し込んだ検査が繋がっていない）",
        "        for rule in _scan(ocr(path), policy):",
        '        for rule in _scan("", policy):',
    ),
    (
        GUARD,
        "OCR 無しでも画素を見たことにする（見ていないのに安全と言う）",
        "        unchecked.append(PIXELS)",
        "        checked.append(PIXELS)",
    ),
    # ---------------------------------------------------------------- 判定
    (
        GUARD,
        "見つかっても止めない（危ないと分かったものを保留にする）",
        "        if self.findings:",
        "        if False:",
    ),
    (
        GUARD,
        "見ていない層があっても安全と言う（未検査が成功に化ける）",
        # `if self.unchecked:` は status と report の2箇所にある。
        # **1行だけで書くと置換先が2件見つかり、何も壊さず「異常なし」に見える。**
        "        if self.unchecked:\n            return UNKNOWN",
        "        if False:\n            return UNKNOWN",
    ),
    # ---------------------------------------------------------------- 報告
    (
        GUARD,
        "報告にフルパスを載せる（報告そのものが漏洩経路になる）",
        '        lines = [f"{self.label}: {self.status}"]',
        '        lines = [f"{self.path}: {self.status}"]',
    ),
    (
        GUARD,
        "名前を伏せずに載せる（ファイル名そのものが秘匿でありうる）",
        "        label=redact(path.name, policy),",
        "        label=path.name,",
    ),
    (
        GUARD,
        "伏せ字が全部を塗りつぶす（人が場所を追えなくなる）",
        "        out = rule.pattern.sub(MASK, out)",
        "        out = MASK",
    ),
    (
        GUARD,
        "伏せ字が何もしない（伏せたつもりで素通り）",
        "        out = rule.pattern.sub(MASK, out)",
        "        pass",
    ),
    # ================================================================= layout
    # **ここで狙うのは「2系統の番号を混ぜること」と「決めきれていないものを
    # 決めたふりで通すこと」。** どちらも出力は揃って見える。
    # ------------------------------------------------------------ 定義の検証
    (
        LAYOUT,
        "空の定義を通す（何を渡しても全部没・欠け無しで成功する）",
        "    if not figures:",
        "    if False:",
    ),
    (
        LAYOUT,
        "記事番号の連番を要求しない（重複も抜けも通る。1枚が静かに消える）",
        "    if article_numbers != list(range(1, len(figures) + 1)):",
        "    if False:",
    ),
    (
        LAYOUT,
        "実行番号の重複を許す（撮影の段取りが取り違う）",
        "    if len(set(shots_numbers)) != len(shots_numbers):",
        "    if False:",
    ),
    (
        LAYOUT,
        "番号の無い図版を重複として数える（手で撮ったものが2枚あると弾かれる）",
        "    shots_numbers = [f.shots_no for f in figures if f.shots_no is not None]",
        "    shots_numbers = [f.shots_no for f in figures]",
    ),
    # ---------------------------------------------------------------- 割付
    (
        LAYOUT,
        "名前で割り付ける（名前は変わる。ハッシュで指さないと迷子になる）",
        "        key = assignments.get(shot.sha256)",
        "        key = assignments.get(shot.path.name)",
    ),
    (
        LAYOUT,
        "記事順に並べない（定義に書いた順がそのまま記事の順になる）",
        "    for figure in sorted(figures, key=lambda f: f.article_no):",
        "    for figure in figures:",
    ),
    # ------------------------------------------------------------ 2系統の番号
    (
        LAYOUT,
        "ファイル名に実行番号を使う（**2系統を混ぜる**）",
        "        return f\"{self.figure.article_no:02d}-{self.figure.slug}",
        "        return f\"{self.figure.shots_no:02d}-{self.figure.slug}",
    ),
    (
        LAYOUT,
        "記事番号を0詰めしない（1 と 10 が並び替えで入れ替わる）",
        "{self.figure.article_no:02d}-",
        "{self.figure.article_no}-",
    ),
    (
        LAYOUT,
        "拡張子を png に決め打つ（中身と名前が食い違う）",
        "{self.shot.path.suffix.lower()}",
        ".png",
    ),
    (
        LAYOUT,
        "実行順で番号の無いものを先頭に回す（手撮りが実行の列に割り込む）",
        "                    p.figure.shots_no is None,",
        "                    False,",
    ),
    (
        LAYOUT,
        "実行順が実行番号を見ない（記事順と同じものを返す）",
        "                    p.figure.shots_no if p.figure.shots_no is not None else 0,",
        "                    0,",
    ),
    # ------------------------------------------------------ 没・孤児・欠け・食い違い
    (
        LAYOUT,
        "没を捨てる（撮ったが使わなかった、と撮っていない、が混ざる）",
        "            rejected.append(shot)\n            continue",
        "            continue",
    ),
    (
        LAYOUT,
        "没を並べ替えない（撮影順が保たれない）",
        "    rejected.sort(key=lambda s: (s.captured_at, s.path.name))",
        "    pass",
    ),
    (
        LAYOUT,
        "孤児を没に混ぜる（割り付けた意思が消える）",
        "            if key not in orphans:\n                orphans.append(key)",
        "            rejected.append(shot)",
    ),
    (
        LAYOUT,
        "知らないキーを見ない（割り付けた絵が黙って消える）",
        "        if key not in known:",
        "        if False:",
    ),
    (
        LAYOUT,
        "欠けを出さない（撮り忘れが表に出ない）",
        "            missing.append(figure)",
        "            pass",
    ),
    (
        LAYOUT,
        "食い違いを黙って1枚に決める（どちらを使ったか誰も知らない）",
        "            conflicts[figure.key] = tuple(found)",
        "            placements.append(Placement(figure, found[0]))",
    ),
    # ---------------------------------------------------------------- 判定
    (
        LAYOUT,
        "欠けがあっても完成と言う",
        "        if self.missing or self.orphans or self.conflicts:",
        "        if self.orphans or self.conflicts:",
    ),
    (
        LAYOUT,
        "孤児があっても完成と言う",
        "        if self.missing or self.orphans or self.conflicts:",
        "        if self.missing or self.conflicts:",
    ),
    (
        LAYOUT,
        "食い違いがあっても完成と言う",
        "        if self.missing or self.orphans or self.conflicts:",
        "        if self.missing or self.orphans:",
    ),
    (
        LAYOUT,
        "没を未完成に数える（撮り直しが異常になる）",
        "        if self.missing or self.orphans or self.conflicts:",
        "        if self.missing or self.orphans or self.conflicts or self.rejected:",
    ),
]


#: pytest の終了コード。**「テストが落ちた」は 1 だけ。**
#: 2=中断 / 3=内部エラー / 4=使い方の誤り / 5=1件も集まらなかった。
#: これらを kill と数えると、**壊していないのに「壊したら落ちた」と報告する**。
PYTEST_FAILED = 1


def run_tests(work: Path) -> tuple[int, str]:
    """写した側でテストを回して、終了コードと出力の末尾を返す。

    **`!= 0` を kill と数えない。** この課題のリポジトリでは、後片付けで
    `PermissionError: [WinError 5]` が出ることがある（README に既出）。
    それはテストの失敗ではないのに終了コードは 0 でなくなるので、
    *壊していない変更まで「テストが落ちた」に化ける*。

    バイトコードのキャッシュを止める理由
    ------------------------------------------------------------------

    **`.pyc` は (mtime の秒, ファイルサイズ) で有効性を判断する。**
    置換前と置換後が**同じ長さ**のミューテーションを当てて、同じ秒のうちに
    書き戻すと、**壊れたままの `.pyc` が生き残る**。しかもその後そのファイルを
    書き換えないかぎり、**以降ずっと壊れたコードが走る**。

    2026-09-20 に実際に踏んだ。`NAME_TIME_TOLERANCE = timedelta(seconds=2)` と
    `NAME_TIME_TOLERANCE = timedelta(days=3650)` は**どちらも42文字**で、
    これを当てた #20 のあと `collect.py` は最後まで壊れたままになり、
    **#21 以降の25件が全部「kill」に化けた**（`-x` が毎回そこで止まるため）。

    *同じ長さに置換した1件が、それ以降の全件を汚染する。*
    しかも症状は「全部 kill＝完璧」に見えるので、**成功の顔をして現れる**。
    """
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
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
        env=env,
    )
    tail = "\n".join((proc.stdout + proc.stderr).splitlines()[-6:])
    return proc.returncode, tail


def main() -> int:
    if not PYTHON.exists():
        print(f"仮想環境の Python が見つかりません: {PYTHON}", file=sys.stderr)
        return 1

    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp) / "repo"
        shutil.copytree(ROOT, work, ignore=IGNORE)

        baseline, tail = run_tests(work)
        if baseline != 0:
            print("壊す前からテストが落ちています。先にそちらを直してください。", file=sys.stderr)
            print(tail, file=sys.stderr)
            return 1

        killed: list[str] = []
        survived: list[str] = []
        not_found: list[str] = []
        errored: list[str] = []

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
            code, tail = run_tests(work)
            if code == PYTEST_FAILED:
                killed.append(f"{index:3}. {label}")
            elif code == 0:
                survived.append(f"{index:3}. {label}（{target}）")
            else:
                # **測れなかったものを kill に混ぜない。**
                errored.append(f"{index:3}. {label}（exit {code}）\n      {tail}")
            path.write_text(original, encoding="utf-8", newline="")

        print(f"壊した箇所: {len(MUTATIONS)}")
        print(f"  kill（テストが落ちた）: {len(killed)}")
        print(f"  素通り: {len(survived)}")
        print(f"  置換先なし: {len(not_found)}")
        print(f"  測定不能: {len(errored)}")

        if survived:
            print("\n素通りしたもの（テストが守っていない）:")
            for line in survived:
                print(f"  {line}")
        if not_found:
            print("\n置換先が見つからなかったもの（壊しかたが古い）:")
            for line in not_found:
                print(f"  {line}")
        if errored:
            print("\n測定不能だったもの（テストの失敗ではない理由で終了した）:")
            for line in errored:
                print(f"  {line}")

    # **置換先なしも測定不能も、成功にしない。**
    # 何も壊さずに全部通ると「穴ゼロ」に見えるし、
    # 測れなかったものを kill に数えると「守られている」に化ける。
    return 0 if not (survived or not_found or errored) else 1


if __name__ == "__main__":
    raise SystemExit(main())
