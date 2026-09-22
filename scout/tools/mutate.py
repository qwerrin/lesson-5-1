#!/usr/bin/env python3
"""scout を1か所ずつ壊して、テストが落ちることを確かめる。

**テストが通っていることは、守られていることの証拠にならない。**

使い方::

    .venv\\Scripts\\python.exe scout\\tools\\mutate.py

仕組みは `figset/tools/mutate.py` と同じ。リポジトリを一時ディレクトリへ写し、
**写した側だけ**を壊す。数え方も同じで、**pytest の終了コード 1 だけを kill と数え、
2〜5 は「測定不能」として混ぜない**。バイトコードのキャッシュも止める
——*置換前後が同じ長さだと `.pyc` が生き残り、以降の全件が偽の kill に化ける*
（2026-09-20 に `figset` で実際に踏んだ。教訓
`restoring-a-file-does-not-restore-its-code`）。

`fetch` で狙う失敗の形
------------------------------------------------------------------

この段の仕事は「取れなかったことを、無かったことにしない」だけなので、
狙うのは **「静かに0件になること」** と **「静かに成功に見えること」**。

================================== ==============================================
壊すと何が起きるか                  なぜ静かなのか
================================== ==============================================
0件を成功にする                     **取れていないのに順調に見える**
例外を0件として握る                 繋がっていないのに「今日は無かった」になる
非200 を通す                        エラー本文を記事として読もうとする
配列でない応答を通す                 Qiita のエラーオブジェクトが0件に化ける
1つ死んでも全体を成功にする          **他が生きていれば見た目は変わらない**
時差を捨てる                        9時間ずれた日付が、そのまま台帳に入る
本文の空文字を本文として通す         中身の無い本文で要約が作られる
総数が無いのを0にする                「まだ先がある」が永久に出なくなる
期間に時刻を入れる                   Qiita が受け付けず、**絞り込みが効かない**
上限を外す                          100 を超える要求が黙って切られる
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

FETCH = "scout/fetch.py"
DEDUPE = "scout/dedupe.py"

IGNORE = shutil.ignore_patterns(
    ".venv", ".git", "__pycache__", ".pytest_cache", ".pytest_tmp",
    "docs", "*.png", "*.wav", "_parts", "node_modules",
)

#: **範囲を広げ忘れると、壊したのにテストが1件も走らず「素通り」に見える。**
TEST_PATHS = ("scout/tests",)

#: pytest の終了コード。**「テストが落ちた」は 1 だけ。**
PYTEST_FAILED = 1

# (対象ファイル, 壊した内容, 置換前, 置換後)
MUTATIONS: list[tuple[str, str, str, str]] = [
    # ------------------------------------------------------------ 取得元の指定
    (
        FETCH,
        "取得元が空でも回す（何も取らずに異常なしと答える）",
        "    if not sources:",
        "    if False:",
    ),
    (
        FETCH,
        "取得元の順を名前順に並べ替える（どれが落ちたか追いにくくなる）",
        "    results = [_one(source, get, since=since) for source in sources]",
        "    results = [_one(source, get, since=since) for source in sorted(sources, key=lambda s: s.name)]",
    ),
    (
        FETCH,
        "最初の取得元の記事しか返さない",
        "        return tuple(a for result in self.results for a in result.articles)",
        "        return tuple(a for result in self.results[:1] for a in result.articles)",
    ),
    # ------------------------------------------------------------ 3値の判定
    (
        FETCH,
        "0件を成功にする（取れていないのに順調に見える）",
        "        status=OK if articles else EMPTY,",
        "        status=OK,",
    ),
    (
        FETCH,
        "非200 を通す（エラー本文を記事として読もうとする）",
        "    if reply.status != 200:",
        "    if False:",
    ),
    (
        FETCH,
        "レート制限を成功扱いにする",
        "    if reply.status != 200:",
        "    if reply.status not in (200, 429):",
    ),
    (
        FETCH,
        "繋がらなかったことを0件として握る",
        '        return _failed(source, f"{type(error).__name__}: {error}")',
        '        return SourceResult(source.name, EMPTY, (), "", None, False)',
    ),
    (
        FETCH,
        "読めなかった応答を0件として握る",
        '        return _failed(source, f"応答を読めない（{type(error).__name__}: {error}）")',
        '        return SourceResult(source.name, EMPTY, (), "", None, False)',
    ),
    (
        FETCH,
        "配列でない応答を通す（Qiita のエラーオブジェクトが0件に化ける）",
        "    if not isinstance(payload, list):",
        "    if False:",
    ),
    (
        FETCH,
        "1つ死んでも全体を成功にする（他が生きていれば見た目は変わらない）",
        "        if any(result.status == FAILED for result in self.results):",
        "        if False:",
    ),
    (
        FETCH,
        "全部空でも成功にする",
        "        if all(result.status == EMPTY for result in self.results):",
        "        if False:",
    ),
    (
        FETCH,
        "1件でも空なら全体を空にする",
        "        if all(result.status == EMPTY for result in self.results):",
        "        if any(result.status == EMPTY for result in self.results):",
    ),
    # ---------------------------------------------------------------- 日時
    (
        FETCH,
        "時差を捨てる（9時間ずれた日付が台帳に入る）",
        "    return value.astimezone().replace(tzinfo=None)",
        "    return value.replace(tzinfo=None)",
    ),
    (
        FETCH,
        "揃えずにそのまま返す（時差つきと素の日時が混ざる）",
        "    if value.tzinfo is None:\n        return value",
        "    if True:\n        return value",
    ),
    (
        FETCH,
        "公開日に更新日を入れる（更新で古い記事が新着に化ける）",
        '            published_at=_local(datetime.fromisoformat(item["created_at"])),',
        '            published_at=_local(datetime.fromisoformat(item["updated_at"])),',
    ),
    # ---------------------------------------------------------------- 本文
    (
        FETCH,
        "本文の空文字を本文として通す（中身の無い本文で要約が作られる）",
        "        return bool(self.body)",
        "        return self.body is not None",
    ),
    (
        FETCH,
        "Qiita の本文を読まない",
        '            body=item.get("body"),',
        "            body=None,",
    ),
    (
        FETCH,
        "Zenn の打ち切られた説明を本文として通す",
        "                body=None,",
        '                body=_text(item, "description"),',
    ),
    # ---------------------------------------------------------------- URL
    (
        FETCH,
        "期間の指定を無視する（毎回ぜんぶ取りに行く）",
        "        if since is not None:",
        "        if False:",
    ),
    (
        FETCH,
        "期間に時刻まで入れる（Qiita が受け付けず絞り込みが効かない）",
        'query = f"{query} created:>={since:%Y-%m-%d}"',
        'query = f"{query} created:>={since:%Y-%m-%dT%H:%M}"',
    ),
    (
        FETCH,
        "件数の上限を外す（100 を超える要求が黙って切られる）",
        "        per_page = min(source.limit, QIITA_MAX_PER_PAGE)",
        "        per_page = source.limit",
    ),
    (
        FETCH,
        "Zenn のトピックを外した URL を叩く",
        'return f"https://zenn.dev/topics/{quote(source.query)}/feed"',
        'return f"https://zenn.dev/{quote(source.query)}/feed"',
    ),
    # ------------------------------------------------------------ 総数・続き
    (
        FETCH,
        "総数が分からないのを0にする（まだ先があるが永久に出ない）",
        "    total = int(raw_total) if raw_total is not None else None",
        "    total = int(raw_total) if raw_total is not None else 0",
    ),
    (
        FETCH,
        "まだ先があることを言わない",
        "        more=total is not None and total > len(articles),",
        "        more=False,",
    ),
    (
        FETCH,
        "いつでも先があると言う",
        "        more=total is not None and total > len(articles),",
        "        more=True,",
    ),
    # ---------------------------------------------------------------- 項目
    (
        FETCH,
        "タグを取らない",
        '            tags=tuple(tag["name"] for tag in item.get("tags") or ()),',
        "            tags=(),",
    ),
    (
        FETCH,
        "いいね数を取らない",
        '                "likes": item.get("likes_count", 0),',
        '                "likes": 0,',
    ),
    # ================================================================= dedupe
    # **ここで狙うのは「効きすぎ」と「効かなすぎ」の両方。**
    # 効かなければ重複が1行増えるだけだが、**効きすぎるとその記事は二度と来ない**。
    # ------------------------------------------------------------ 正規化
    (
        DEDUPE,
        "追跡パラメータを落とさない（`?utm_source=` 違いが別物として重複する）",
        "        if key.lower() not in TRACKING_PARAMS",
        "        if True",
    ),
    (
        DEDUPE,
        "クエリを全部落とす（**効きすぎ**。別の記事が同じものになる）",
        "        if key.lower() not in TRACKING_PARAMS",
        "        if False",
    ),
    (
        DEDUPE,
        "追跡パラメータを大小で取りこぼす（`UTM_SOURCE` が残って重複する）",
        "        if key.lower() not in TRACKING_PARAMS",
        "        if key not in TRACKING_PARAMS",
    ),
    (
        DEDUPE,
        "クエリを並べ替えない（並びだけ違うものが別物になる）",
        'query = "&".join(f"{key}={value}" for key, value in sorted(kept))',
        'query = "&".join(f"{key}={value}" for key, value in kept)',
    ),
    (
        DEDUPE,
        "フラグメントを残す（同じページの中の位置が別の記事になる）",
        '            "",  # フラグメントは同じページの中の位置であって、別の記事ではない',
        "            parts.fragment,",
    ),
    (
        DEDUPE,
        "末尾のスラッシュを落とさない",
        '    if len(path) > 1 and path.endswith("/"):',
        "    if False:",
    ),
    (
        DEDUPE,
        "根のスラッシュまで落とす（別物に見えるだけで得が無い）",
        '    if len(path) > 1 and path.endswith("/"):',
        '    if path.endswith("/"):',
    ),
    (
        DEDUPE,
        "パスの大小を揃える（**効きすぎ**。大小が意味を持つ URL が潰れる）",
        "            path,  # **パスの大小は揃えない**（大小が意味を持つ URL がある）",
        "            path.lower(),",
    ),
    (
        DEDUPE,
        "ホストの大小を揃えない",
        "            parts.netloc.lower(),",
        "            parts.netloc,",
    ),
    (
        DEDUPE,
        "www を落とす（**踏み込みすぎ**。落として困る場合が理論上ある）",
        "            parts.netloc.lower(),",
        '            parts.netloc.lower().removeprefix("www."),',
    ),
    # **スキームの `.lower()` は仕込まない。** `urlsplit` が既に小文字にしているので、
    # 重ねても観測できるものが1つも変わらない（2026-09-23 に素通りして分かり、実装から消した）。
    (
        DEDUPE,
        "正規化できないものを鍵にする（台帳の空行で、URL 無しの記事が全部消える）",
        "        if key:\n            keys.add(key)",
        "        if True:\n            keys.add(key)",
    ),
    # ------------------------------------------------------------ 突き合わせ
    (
        DEDUPE,
        "台帳を見ない（毎日同じ記事が積み上がる）",
        "    if key in before:",
        "    if False:",
    ),
    (
        DEDUPE,
        "vault を見ない（既に持っている記事をまた持ってくる）",
        "    if key in in_vault:",
        "    if False:",
    ),
    (
        DEDUPE,
        "同じ取り込みの中の重複を見ない",
        "    if key in batch:",
        "    if False:",
    ),
    (
        DEDUPE,
        "理由の順番を入れ替える（直しやすいほうを先に出さない）",
        "    if key in before:\n        return SEEN_BEFORE\n    if key in in_vault:\n        return IN_VAULT",
        "    if key in in_vault:\n        return IN_VAULT\n    if key in before:\n        return SEEN_BEFORE",
    ),
    (
        DEDUPE,
        "残したものを取り込みに覚えさせない（同じ回の重複が素通りする）",
        "            batch.add(key)",
        "            pass",
    ),
    (
        DEDUPE,
        "捨てたものを返さない（**H8：何を捨てたか残らない**）",
        "            dropped.append(Dropped(article=article, key=key, reason=reason))",
        "            pass",
    ),
    (
        DEDUPE,
        "渡された順を逆にする",
        "    for article in articles:\n        key = normalize(article.url)",
        "    for article in reversed(articles):\n        key = normalize(article.url)",
    ),
    # ---------------------------------------------------------------- 件数
    (
        DEDUPE,
        "見た件数を残した件数と同じにする（何件落としたか分からない）",
        "        return len(self.kept) + len(self.dropped)",
        "        return len(self.kept)",
    ),
    (
        DEDUPE,
        "理由の内訳を出さない",
        "        return dict(Counter(d.reason for d in self.dropped))",
        "        return {}",
    ),
    (
        DEDUPE,
        "件数を出さずに「重複を除きました」とだけ言う",
        '            f"{self.total} 件中 {len(self.kept)} 件を残した"',
        '            "重複を除きました"',
    ),
    (
        DEDUPE,
        "内訳を空にする（落とした理由が伝わらない）",
        'breakdown = "・".join(f"{r} {n}" for r, n in sorted(self.reasons.items()))',
        'breakdown = ""',
    ),
]


def run_tests(work: Path) -> tuple[int, str]:
    """写した側でテストを回して、終了コードと出力の末尾を返す。

    **`!= 0` を kill と数えない。** テストの失敗は 1 だけで、
    2〜5（中断・内部エラー・使い方の誤り・1件も集まらなかった）は測定不能。

    **バイトコードのキャッシュを止める。** `.pyc` は (mtime の秒, サイズ) で
    有効性を判断するので、置換前後が同じ長さだと壊れたまま生き残る。
    """
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    proc = subprocess.run(
        [str(PYTHON), "-m", "pytest", *TEST_PATHS, "-x", "-q", "--no-header",
         "-p", "no:cacheprovider", "--basetemp", str(work / ".pytest_tmp")],
        cwd=work, capture_output=True, text=True, encoding="utf-8", errors="replace", env=env,
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
                errored.append(f"{index:3}. {label}（exit {code}）\n      {tail}")
            path.write_text(original, encoding="utf-8", newline="")

        print(f"壊した箇所: {len(MUTATIONS)}")
        print(f"  kill（テストが落ちた）: {len(killed)}")
        print(f"  素通り: {len(survived)}")
        print(f"  置換先なし: {len(not_found)}")
        print(f"  測定不能: {len(errored)}")

        for title, rows in (
            ("素通りしたもの（テストが守っていない）", survived),
            ("置換先が見つからなかったもの（壊しかたが古い）", not_found),
            ("測定不能だったもの（テストの失敗ではない理由で終了した）", errored),
        ):
            if rows:
                print(f"\n{title}:")
                for row in rows:
                    print(f"  {row}")

    # **置換先なしも測定不能も、成功にしない。**
    return 0 if not (survived or not_found or errored) else 1


if __name__ == "__main__":
    raise SystemExit(main())
