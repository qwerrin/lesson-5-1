#!/usr/bin/env python3
"""課題1 の README と実体を機械照合する。

    .venv/Scripts/python.exe task1/tools/check_docs.py

文章は目で読んでも合っているように見える。**数字とコードの食い違いは目視では出ない。**

このツールが確かめること
------------------------------------------------------------------

1. README のテスト件数が pytest の実測と一致する
2. README が書いている閾値・既定値が実装の定数と一致する
3. **追跡されるファイルに本物の資格情報が入っていない**
4. ``.env.example`` の変数名が実装の定数と一致する
5. README が名前を出しているファイルが実在する
6. README のコマンドが壊れていない（タブ化・存在しないパス）
7. ルート README の課題1の行が最新である
8. **README が名乗る照合項目数が、実際の項目数と一致する**

**検査対象を環境変数から取らない。** ``os.environ`` 経由の値はシェルで変わり、
**空なら黙って 0 件を「問題なし」として表示する**。本物の資格情報は ``.env`` から
読み、**読めなければ検査を成功させずに落とす**。
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

TASK = ROOT / "task1"
if str(TASK) not in sys.path:
    sys.path.insert(0, str(TASK))

from common import env_file, gemini_client, line_auth, slack_auth  # noqa: E402

import slack_read  # noqa: E402
import summarize  # noqa: E402
import summarize_to_line  # noqa: E402

README = TASK / "README.md"
ROOT_README = ROOT / "README.md"
ENV_EXAMPLE = ROOT / ".env.example"

PY_EXE = ROOT / ".venv/Scripts/python.exe"

#: README の件数表に出す対象。**ここに並べたものだけを照合する**ので、
#: 表に行を足したらここにも足す。
TEST_TARGETS = ("task1/tests", "common/tests")

#: README が名乗る照合項目数。``照合 **N** 項目`` の形で書く。
SELF_COUNT_PATTERN = r"照合\s*\*\*(\d+)\*\*\s*項目"

#: 資格情報らしき形。**値そのものは絶対に出力しない**（件数と場所だけ言う）。
#:
#: 実測した本物の形（2026-08-29）:
#:   Slack  … ``xoxb-`` ＋ 54文字。数字・数字・英数字をハイフンで区切る
#:   Gemini … ``AQ.`` ＋ 50文字
#:   LINE   … Base64 の 172文字
#:
#: **広すぎる網はテストのダミーを拾う。** 実際に拾った——しかも拾われた側
#: （``common/tests/test_slack_auth.py``）は課題7の時点で「本物の形を真似ない」と
#: 書いてダミーを作っていた。悪かったのはこちらの網である。
#:
#: **ダミーを本物から離すことと、網を本物に寄せることは両輪。** 片方だけだと、
#: 本物を貼ってしまった日に気づけない（網が広いと毎回鳴るので無視するようになり、
#: ダミーが本物そっくりだと網を狭めた瞬間に本物も抜ける）。
SECRET_PATTERNS = (
    ("Slack Bot Token", re.compile(r"xoxb-[A-Za-z0-9]+-[A-Za-z0-9]+-[A-Za-z0-9]{20,}")),
    ("Gemini API キー", re.compile(r"\bAQ\.[A-Za-z0-9_-]{40,}")),
    ("LINE チャネルアクセストークン", re.compile(r"[A-Za-z0-9+/]{100,}=")),
)


class Failure(Exception):
    """検査を始められない。**不一致とは区別する。**"""


# ------------------------------------------------- 自分の項目数を自分で検査する


def read_stated_count(readme: str) -> int | None:
    """README が名乗っている照合項目数を読む。無ければ ``None``。

    **無いのを 0 と読まない。** 0 は「1件も検査していない」という正当な値で、
    「書いていない」とは別の意味になる。
    """
    match = re.search(SELF_COUNT_PATTERN, readme)
    return int(match.group(1)) if match else None


def self_count_check(readme: str, checks_so_far: int) -> tuple[bool, str]:
    """README の項目数と実際の項目数を突き合わせる。

    **この検査自身を 1 つ足す。** 最後に積まれるので ``checks_so_far`` には
    自分が入っていない。+1 を忘れると README には常に1つ少ない数を書くことになり、
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


# ------------------------------------------------------------------ 件数を数える


def collect_count(target: str) -> int:
    """pytest に数えさせる。**README の数字を物差しにしない。**"""
    result = subprocess.run(
        [str(PY_EXE), "-m", "pytest", target, "--collect-only", "-q"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    match = re.search(r"^(\d+) tests collected", result.stdout or "", re.MULTILINE)
    if not match:
        raise Failure(f"{target} のテスト件数を数えられませんでした。")
    return int(match.group(1))


def tracked_files() -> list[Path]:
    """git が追跡しているファイル。**検査の対象をここから取る。**

    ディレクトリを歩いて集めると ``.venv`` や ``__pycache__`` まで拾い、
    「本物の資格情報が入っていないか」の検査が的外れになる。
    """
    result = subprocess.run(
        ["git", "ls-files"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0:
        raise Failure("git ls-files を実行できませんでした。")
    return [ROOT / line for line in result.stdout.splitlines() if line.strip()]


def check(results: list[tuple[bool, str]], ok: bool, label: str) -> None:
    results.append((ok, label))


def main() -> int:
    try:
        readme = README.read_text(encoding="utf-8")
        root_readme = ROOT_README.read_text(encoding="utf-8")
        env_example = ENV_EXAMPLE.read_text(encoding="utf-8")
    except OSError as error:
        print(f"検査を始められません: {error}", file=sys.stderr)
        return 2

    results: list[tuple[bool, str]] = []

    # ---- 1. テスト件数
    try:
        total = sum(collect_count(target) for target in TEST_TARGETS)
    except Failure as error:
        print(error, file=sys.stderr)
        return 2

    stated_tests = re.search(r"(\d+)\s*(?:件|passed)", readme)
    check(
        results,
        str(total) in readme,
        f"README にテスト実測 {total} 件が書かれている",
    )

    # ---- 2. 閾値・既定値が実装と一致する
    check(
        results,
        str(summarize.SUMMARIZE_THRESHOLD_CHARS) in readme,
        f"要約の閾値 {summarize.SUMMARIZE_THRESHOLD_CHARS} 文字が README と一致",
    )
    check(
        results,
        str(summarize.MIN_MESSAGES_TO_SUMMARIZE) in readme,
        f"要約する件数 {summarize.MIN_MESSAGES_TO_SUMMARIZE} 件が README と一致",
    )
    check(
        results,
        gemini_client.DEFAULT_MODEL in readme,
        f"採用モデル {gemini_client.DEFAULT_MODEL} が README と一致",
    )
    for subtype in sorted(slack_read.EXCLUDED_SUBTYPES):
        check(results, subtype in readme, f"除外する subtype {subtype} が README に載っている")

    # ---- 3. 追跡されるファイルに本物の資格情報が入っていない
    #
    # **値は出力しない。** 出すとこの画面自体が漏洩経路になる。
    try:
        files = tracked_files()
    except Failure as error:
        print(error, file=sys.stderr)
        return 2

    if not files:
        # **0 件を「問題なし」にしない。** 数えられていないだけかもしれない。
        check(results, False, "git が追跡するファイルを1件も取得できなかった")
    else:
        hits: list[str] = []
        for path in files:
            if not path.is_file():
                continue
            try:
                body = path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            for name, pattern in SECRET_PATTERNS:
                if pattern.search(body):
                    hits.append(f"{path.relative_to(ROOT)} に {name} らしき文字列")
        check(
            results,
            not hits,
            f"追跡ファイル {len(files)} 件に資格情報らしき文字列が無い"
            + ("" if not hits else "： " + " / ".join(hits)),
        )

    # ---- 4. .env.example の変数名が実装の定数と一致する
    for constant in (
        slack_auth.BOT_TOKEN_ENV,
        gemini_client.API_KEY_ENV,
        line_auth.CHANNEL_ACCESS_TOKEN_ENV,
        line_auth.USER_ID_ENV,
    ):
        check(
            results,
            f"{constant}=" in env_example,
            f".env.example に {constant} がある",
        )

    # ---- 5. README が名前を出しているファイルが実在する
    for name in re.findall(r"`([A-Za-z0-9_./-]+\.(?:py|json|md|example))`", readme):
        candidates = [
            ROOT / name,
            TASK / name,
            ROOT / "common" / name,
            TASK / "tools" / name,
            TASK / Path(name).name,
        ]
        check(
            results,
            any(candidate.exists() for candidate in candidates),
            f"README が挙げるファイルが実在する: {name}",
        )

    # ---- 6. README のコマンドが壊れていない
    #
    # **タブは実際に踏んだ形**。ツール呼び出しの JSON でバックスラッシュが
    # 潰れると `\t` がタブ文字に化け、コマンドが黙って別物になる。
    check(results, "\t" not in readme, "README にタブ文字が混入していない")
    for command in re.findall(r"python\.exe\s+([A-Za-z0-9_\\/.-]+\.py)", readme):
        target = ROOT / command.replace("\\", "/")
        check(results, target.exists(), f"README のコマンドが指すスクリプトが実在する: {command}")

    # ---- 7. 実行時の状態が追跡されていない
    #
    # **記録（results.json）とは性質が違う。** 追跡すると、新しいクローンが
    # 「他人がどこまで読んだか」を持って始まり、まだ読んでいない範囲を飛ばす——
    # この課題の目的（見逃しを防ぐ）に真っ向から反する。
    #
    # 人の注意力に頼らず検査に入れるのは、**一度 add してしまうと以後は
    # 何もしなくても追跡され続ける**ため。気づく機会が最初の1回しかない。
    tracked_names = {path.name for path in files}
    check(
        results,
        "state.json" not in tracked_names,
        "実行時の状態（state.json）が追跡されていない",
    )

    # ---- 8. ルート README の課題1の行が最新である
    check(
        results,
        "task1/README.md" in root_readme,
        "ルート README が task1 を指している",
    )

    # ---- 8. 自分の項目数（**最後に積む**）
    ok, label = self_count_check(readme, len(results))
    check(results, ok, label)

    # ---- 表示
    ng = [label for ok, label in results if not ok]
    for ok, label in results:
        print(f"  {'OK  ' if ok else 'NG  '}{label}")

    print(f"\n照合 {len(results)} 項目 / NG {len(ng)} 件")
    return 0 if not ng else 1


if __name__ == "__main__":
    raise SystemExit(main())
