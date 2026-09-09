#!/usr/bin/env python3
"""課題2 の README と実体を機械照合する。

    .venv/Scripts/python.exe task2/tools/check_docs.py

文章は目で読んでも合っているように見える。**数字とコードの食い違いは目視では出ない。**

このツールが確かめること
------------------------------------------------------------------

1. README のテスト件数が pytest の実測と一致する（課題2・合計・リポジトリ全体）
2. 層ごとのテスト件数とわざと壊す検査の数が、実測と一致する
3. **これから公開されうるファイルに本物の資格情報が入っていない**
4. サービスアカウントの鍵が公開の対象に入っていない
5. ``.env.example`` の変数名が実装の読み取り先と一致する
6. README が名前を出しているファイルが実在する
7. README のコマンドが壊れていない（存在しないパス・タブ化）
8. 終了コードの表が実装と一致する
9. 定期実行の設定（タスク名・時刻・記録の場所・生存通知の間隔）が実装と一致する
10. `.gitignore` が実行の記録を除外している
11. DESIGN が出した穴が「塞ぐ」欄に載っている
12. **公開されうるファイルにホームのパス（ユーザー名）が写り込んでいない**
13. **README が名乗る照合項目数が、実際の項目数と一致する**

**検査対象を環境変数から取らない。** ``os.environ`` 経由の値はシェルで変わり、
**空なら黙って 0 件を「問題なし」として表示する**。本物の資格情報は ``.env`` から
読み、**読めなければ検査を成功させずに落とす**。
"""

from __future__ import annotations

import importlib.util
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

TASK = ROOT / "task2"
if str(TASK) not in sys.path:
    sys.path.insert(0, str(TASK))

import run_daily  # noqa: E402
import transform  # noqa: E402

README = TASK / "README.md"
DESIGN = TASK / "DESIGN.md"
ENV_EXAMPLE = ROOT / ".env.example"
GITIGNORE = ROOT / ".gitignore"
REGISTER = TASK / "tools" / "register_task.ps1"
MUTATE = TASK / "tools" / "mutate.py"

PY_EXE = ROOT / ".venv/Scripts/python.exe"

#: README の状態表に並ぶモジュールと、その検査の置き場。
#: **表に行を足したらここにも足す。** 足し忘れると、その行だけ誰も見ない。
MODULE_TESTS = {
    "transform.py": "task2/tests/test_transform.py",
    "fetch_items.py": "task2/tests/test_fetch_items.py",
    "diff.py": "task2/tests/test_diff.py",
    "to_sheet.py": "task2/tests/test_to_sheet.py",
    "verify_sheet.py": "task2/tests/test_verify_sheet.py",
    "run_daily.py": "task2/tests/test_run_daily.py",
    "common/sheets_client.py": "common/tests/test_sheets_client.py",
}

#: 表の名前 → `mutate.py` が使う対象名。
MODULE_TARGETS = {
    name: (name if name.startswith("common/") else f"task2/{name}")
    for name in MODULE_TESTS
}

#: 課題2のテストの範囲。README の「課題2のテストは N 件」と対応する。
TASK_TESTS = "task2/tests"
#: 「合計は N 件」の範囲。
TOTAL_TESTS = ("task2/tests", "common/tests/test_sheets_client.py")

#: README が名乗る照合項目数。``照合 **N** 項目`` の形で書く。
SELF_COUNT_PATTERN = r"照合\s*\*\*(\d+)\*\*\s*項目"

#: `.env` のうち、**秘密ではない**もの。値がファイル名なので README にも出る。
NOT_SECRET = {"GOOGLE_SERVICE_ACCOUNT_FILE"}
#: **実在しないのが正しい名前。** 「もう使っていない」と説明する文の中に出る。
#: 廃止や不採用を説明した文を、参照切れとして数えない。
NOT_A_REFERENCE = {"token.json"}
#: これより短い値は照合しない。短い語はふつうの文章に紛れて誤検知になる。
MIN_SECRET_LENGTH = 8


class Failure(Exception):
    """検査を始められない。**不一致とは区別する。**"""


# ------------------------------------------------------------------ 数える


def collect_count(*targets: str) -> int:
    """pytest に数えさせる。**README の数字を物差しにしない。**"""
    result = subprocess.run(
        [str(PY_EXE), "-m", "pytest", *targets, "--collect-only", "-q"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    match = re.search(r"^(\d+) tests collected", result.stdout or "", re.MULTILINE)
    if not match:
        raise Failure(f"{targets} のテスト件数を数えられませんでした。")
    return int(match.group(1))


def load_mutations() -> list[tuple[str, str, str, str]]:
    """`mutate.py` の一覧をそのまま読む。**数を別の場所に写さない。**"""
    spec = importlib.util.spec_from_file_location("mutate_for_check", MUTATE)
    if spec is None or spec.loader is None:
        raise Failure(f"{MUTATE} を読めませんでした。")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return list(module.MUTATIONS)


def publishable_files() -> list[Path]:
    """**これから公開されうるファイル。** 検査の対象をここから取る。

    ``git ls-files`` だけでは足りない。それは「すでに追跡しているもの」で、
    **一番危ないのは「まだ追跡していないが、無視もされていない」ファイル**である。
    その状態のファイルは ``git add -A`` ひとつで公開に入るのに、
    狭い走査では1度も読まれない。**読んでいないものについて、無いとは言えない。**
    """
    result = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0:
        raise Failure("git ls-files を実行できませんでした。")
    return [ROOT / line for line in result.stdout.splitlines() if line.strip()]


def read_env() -> dict[str, str]:
    """本物の資格情報を ``.env`` から読む。**読めなければ落とす。**

    環境変数から取ると、シェルによって空になり、**0 件を「問題なし」として表示する**。
    検査の入力が消えたことと、問題が無いことは別である。
    """
    from common import env_file

    try:
        return env_file.load(ROOT / ".env")
    except Exception as error:  # noqa: BLE001
        raise Failure(f".env を読めませんでした: {error}") from error


# ------------------------------------------------------------------ 自己申告


def read_stated_count(readme: str) -> int | None:
    """README が名乗っている照合項目数。無ければ ``None``。

    **無いのを 0 と読まない。** 0 は「1件も検査していない」という正当な値で、
    「書いていない」とは別の意味になる。
    """
    match = re.search(SELF_COUNT_PATTERN, readme)
    return int(match.group(1)) if match else None


def self_count_check(readme: str, checks_so_far: int) -> tuple[bool, str]:
    """**この検査自身を 1 つ足す。** 最後に積まれるので自分が入っていない。

    +1 を忘れると README には常に1つ少ない数を書くことになり、
    しかも**その状態で一致してしまう**——ずれた物差しどうしが噛み合う。
    """
    actual = checks_so_far + 1
    stated = read_stated_count(readme)
    if stated is None:
        return False, (
            f"README が照合項目数を名乗っていない（実測={actual}）。"
            "「照合 **N** 項目」の形で書く"
        )
    return stated == actual, f"照合項目数: README={stated} 実測={actual}"


def check(results: list[tuple[bool, str]], ok: bool, label: str) -> None:
    results.append((ok, label))


# ------------------------------------------------------------------ 本体


def main() -> int:
    try:
        readme = README.read_text(encoding="utf-8")
        design = DESIGN.read_text(encoding="utf-8")
        env_example = ENV_EXAMPLE.read_text(encoding="utf-8")
        gitignore = GITIGNORE.read_text(encoding="utf-8")
        register = REGISTER.read_text(encoding="utf-8-sig")
        env = read_env()
        mutations = load_mutations()
    except (OSError, Failure) as error:
        print(f"検査を始められません: {error}", file=sys.stderr)
        return 2

    results: list[tuple[bool, str]] = []

    # ---------------------------------------------------------- 1. 件数
    stated_task = re.search(r"課題2のテストは \*\*(\d+) 件\*\*", readme)
    actual_task = collect_count(TASK_TESTS)
    check(
        results,
        bool(stated_task) and int(stated_task.group(1)) == actual_task,
        f"課題2のテスト件数: README={stated_task.group(1) if stated_task else '無し'} "
        f"実測={actual_task}",
    )

    stated_total = re.search(r"合計は (\d+) 件", readme)
    actual_total = collect_count(*TOTAL_TESTS)
    check(
        results,
        bool(stated_total) and int(stated_total.group(1)) == actual_total,
        f"共有部品を含む合計: README={stated_total.group(1) if stated_total else '無し'} "
        f"実測={actual_total}",
    )

    stated_repo = re.search(r"リポジトリ全体では \*\*(\d+) 件\*\*", readme)
    actual_repo = collect_count()
    check(
        results,
        bool(stated_repo) and int(stated_repo.group(1)) == actual_repo,
        f"リポジトリ全体: README={stated_repo.group(1) if stated_repo else '無し'} "
        f"実測={actual_repo}",
    )

    stated_mutations = re.search(r"わざと壊す検査は \*\*(\d+) か所", readme)
    check(
        results,
        bool(stated_mutations) and int(stated_mutations.group(1)) == len(mutations),
        f"わざと壊す検査の総数: "
        f"README={stated_mutations.group(1) if stated_mutations else '無し'} "
        f"実測={len(mutations)}",
    )

    # ---------------------------------------------------------- 2. 層ごと
    rows = re.findall(
        r"\| (?:実装|共有) `([^`]+)` \| ✅ テスト \*\*(\d+) 件\*\*・"
        r"わざと壊す検査 \*\*(\d+) か所",
        readme,
    )
    listed = {name for name, _, _ in rows}
    check(
        results,
        listed == set(MODULE_TESTS),
        f"状態表に並ぶモジュール: README={len(listed)} 種 / 検査の対象={len(MODULE_TESTS)} 種"
        + (f"（差={sorted(listed ^ set(MODULE_TESTS))}）" if listed != set(MODULE_TESTS) else ""),
    )

    test_gaps = []
    for name, tests, _ in rows:
        target = MODULE_TESTS.get(name)
        if target is None:
            continue
        actual = collect_count(target)
        if int(tests) != actual:
            test_gaps.append(f"{name}: README={tests} 実測={actual}")
    check(
        results,
        not test_gaps,
        "層ごとのテスト件数" + ("：" + " / ".join(test_gaps) if test_gaps else "（全部一致）"),
    )

    counted = {target: 0 for target in MODULE_TARGETS.values()}
    for target, _, _, _ in mutations:
        counted[target] = counted.get(target, 0) + 1
    mutation_gaps = []
    for name, _, kills in rows:
        target = MODULE_TARGETS.get(name)
        if target is None:
            continue
        if int(kills) != counted.get(target, 0):
            mutation_gaps.append(f"{name}: README={kills} 実測={counted.get(target, 0)}")
    check(
        results,
        not mutation_gaps,
        "層ごとのわざと壊す検査"
        + ("：" + " / ".join(mutation_gaps) if mutation_gaps else "（全部一致）"),
    )

    # ---------------------------------------------------------- 3-4. 資格情報
    secrets = {
        key: value
        for key, value in env.items()
        if key not in NOT_SECRET and len(value) >= MIN_SECRET_LENGTH
    }
    if not secrets:
        print("検査を始められません: .env から秘密の値を1つも読めませんでした。", file=sys.stderr)
        return 2

    files = publishable_files()
    leaked = []
    for path in files:
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for key, value in secrets.items():
            if value in text:
                leaked.append(f"{path.relative_to(ROOT)} に {key}")
    check(
        results,
        not leaked,
        f"公開されうる {len(files)} ファイルの資格情報"
        + ("：" + " / ".join(leaked) if leaked else "（漏れなし）"),
    )

    key_name = env.get("GOOGLE_SERVICE_ACCOUNT_FILE", "service-account.json")
    key_published = [
        path.relative_to(ROOT) for path in files if path.name == Path(key_name).name
    ]
    check(
        results,
        not key_published,
        f"サービスアカウントの鍵（{key_name}）"
        + (f"：{key_published} が公開の対象に入っている" if key_published else "（対象外）"),
    )

    # ---------------------------------------------------------- 5. .env.example
    needed = {
        "RAKUTEN_APPLICATION_ID",
        "RAKUTEN_ACCESS_KEY",
        "GOOGLE_SHEET_ID",
        "GOOGLE_SERVICE_ACCOUNT_FILE",
        "LINE_CHANNEL_ACCESS_TOKEN",
        "LINE_USER_ID",
    }
    missing = sorted(key for key in needed if key not in env_example)
    check(
        results,
        not missing,
        ".env.example の変数名" + (f"：{missing} が無い" if missing else "（実装と一致）"),
    )

    # ---------------------------------------------------------- 6. 名前を出すファイル
    named = set()
    for match in re.finditer(r"`([\w./\\-]+\.(?:py|ps1|json|jsonl|md))`", readme):
        named.add(match.group(1).replace("\\", "/"))
    absent = sorted(
        name
        for name in named
        if name not in NOT_A_REFERENCE
        and not any(
            (base / name).exists()
            for base in (ROOT, TASK, TASK / "tools", TASK / "logs")
        )
    )
    check(
        results,
        not absent,
        f"README が名前を出す {len(named)} ファイル"
        + (f"：{absent} が実在しない" if absent else "（全部ある）"),
    )

    # ---------------------------------------------------------- 7. コマンド
    commands = re.findall(r"^\.venv\\Scripts\\[\w.]+ (?:-m pytest )?([\w\\.]+\.py)", readme, re.M)
    broken = sorted(
        name for name in set(commands) if not (ROOT / name.replace("\\", "/")).exists()
    )
    tabbed = [line for line in readme.splitlines() if "\t" in line]
    check(
        results,
        not broken,
        f"README のコマンドが指す {len(set(commands))} ファイル"
        + (f"：{broken} が実在しない" if broken else "（全部ある）"),
    )
    check(
        results,
        not tabbed,
        "README にタブが混ざっていない" + (f"：{len(tabbed)} 行" if tabbed else ""),
    )

    # ---------------------------------------------------------- 8. 終了コード
    stated_codes = {int(m) for m in re.findall(r"^\| ([0-4]) \| ", readme, re.M)}
    actual_codes = {int(m) for m in re.findall(r"^    ([0-4])   ", run_daily.__doc__ or "", re.M)}
    check(
        results,
        stated_codes == actual_codes == {0, 1, 2, 3, 4},
        f"終了コード: README={sorted(stated_codes)} 実装={sorted(actual_codes)}",
    )

    # ---------------------------------------------------------- 9. 定期実行の設定
    task_name = re.search(r'\$Name = "([^"]+)"', register)
    check(
        results,
        bool(task_name) and f"`{task_name.group(1)}`" in readme,
        f"タスク名: 登録スクリプト={task_name.group(1) if task_name else '無し'}"
        f"（README に{'ある' if task_name and f'`{task_name.group(1)}`' in readme else '無い'}）",
    )

    at = re.search(r'\[string\]\$At = "([^"]+)"', register)
    check(
        results,
        bool(at) and f"毎日 {at.group(1)}" in readme,
        f"実行時刻: 登録スクリプト={at.group(1) if at else '無し'}"
        f"（README に{'ある' if at and f'毎日 {at.group(1)}' in readme else '無い'}）",
    )

    log_rel = (run_daily.DEFAULT_LOG_DIR / run_daily.LOG_NAME).relative_to(ROOT)
    log_posix = log_rel.as_posix()
    check(
        results,
        log_posix in readme or str(log_rel) in readme,
        f"記録の場所: 実装={log_posix}（README に{'ある' if log_posix in readme or str(log_rel) in readme else '無い'}）",
    )

    check(
        results,
        f"**{run_daily.HEARTBEAT_DAYS}日ごと**" in readme,
        f"生存通知の間隔: 実装={run_daily.HEARTBEAT_DAYS} 日"
        f"（README に{'ある' if f'**{run_daily.HEARTBEAT_DAYS}日ごと**' in readme else '無い'}）",
    )

    check(
        results,
        len(transform.COLUMNS) == len(set(transform.COLUMNS)),
        f"列の名前が重複していない（{len(transform.COLUMNS)} 列）",
    )

    # ---------------------------------------------------------- 10. .gitignore
    check(
        results,
        "task2/logs/" in gitignore,
        "実行の記録が公開の対象から外れている"
        + ("" if "task2/logs/" in gitignore else "：.gitignore に task2/logs/ が無い"),
    )

    # ---------------------------------------------------------- 11. DESIGN の穴
    holes = sorted(set(re.findall(r"^\| (V|W|X|Y|Z|A[A-C]) \| ", design, re.M)))
    blocked = re.search(r"\| \*\*塞ぐ\*\* \| ([^|]+)\|", design)
    unblocked = (
        [hole for hole in holes if f"5-{hole}" not in blocked.group(1)] if blocked else holes
    )
    check(
        results,
        bool(blocked) and not unblocked and len(holes) == 8,
        f"定期実行の穴 {len(holes)} 件が「塞ぐ」欄にある"
        + (f"：{unblocked} が無い" if unblocked else ""),
    )

    # ---------------------------------------------------------- ホームのパス
    # **スクリーンショットにも、リポジトリにも、ユーザー名を残さない。**
    # 秘密ではないが、残す必要が無いものは残さない（課題1でワークスペース名を伏せた形）。
    home = Path.home()
    if not home.name:
        print("検査を始められません: ホームの名前を取れませんでした。", file=sys.stderr)
        return 2
    home_forms = (str(home), home.as_posix())
    exposed = []
    for path in files:
        if path.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp", ".gif"}:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if any(form in text for form in home_forms):
            exposed.append(str(path.relative_to(ROOT)))
    check(
        results,
        not exposed,
        f"公開されうるファイルにホームのパスが無い（{home_forms[0]}）"
        + ("：" + " / ".join(exposed) if exposed else ""),
    )

    # ---------------------------------------------------------- 12. 自己申告
    ok, label = self_count_check(readme, len(results))
    check(results, ok, label)

    # ---------------------------------------------------------- 出す
    failed = [label for ok, label in results if not ok]
    for ok, label in results:
        print(f"  {'OK  ' if ok else 'NG  '}{label}")
    print(f"\n照合 {len(results)} 項目・NG {len(failed)} 件")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
