#!/usr/bin/env python3
"""実装を1か所ずつ壊して、テストが落ちることを確かめる。

**テストが通っていることは、守られていることの証拠にならない。**

使い方::

    .venv\\Scripts\\python.exe task1\\tools\\mutate.py

リポジトリを一時ディレクトリへ写し、**写した側だけ**を壊す。
成果物には触らないので、途中で強制終了しても壊れたまま残らない。

**置換先が見つからない（NOT FOUND）は素通りと同じ扱いにする。**
実装を直して壊しかたを直し忘れると、何も壊さずに全部通って
「穴ゼロ」と出てしまうため。

この課題で狙う失敗の形
------------------------------------------------------------------

守りたいのは「**エラーにならず、静かに減る／静かに進む**」失敗である。

============================== ================================================
壊すと何が起きるか              なぜ静かなのか
============================== ================================================
件数で読み終わりを決める         ``limit`` は best-effort。少なく返った回だけ減る
位置を送信の前に進める           送信に失敗した範囲が二度と読まれない
0件で位置を進める               次回、まだ読んでいない範囲を飛ばす
除外した件数を数えない           件数が合わない理由を追えなくなる
unescape の順番を入れ替える      **書いていない文字が現れる**
壊れた状態を初回として続行       全履歴を読み直して巨大な要約を1通送る
``None`` の残通数を 0 と読む     **無制限のアカウントで送信を止める**
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

READ = "task1/slack_read.py"
SUM = "task1/summarize.py"
STATE = "task1/state.py"
TOOL = "task1/summarize_to_line.py"
GEMINI = "common/gemini_client.py"
SEND = "common/line_send.py"
VERIFY = "task1/verify_summary.py"

IGNORE = shutil.ignore_patterns(
    ".venv", ".git", "__pycache__", ".pytest_cache", "docs", "*.png", "node_modules"
)

#: common/ を壊すので **common のテストも一緒に回す**。
TEST_PATHS = ("task1/tests", "common/tests")

# (対象ファイル, 壊した内容, 置換前, 置換後)
MUTATIONS: list[tuple[str, str, str, str]] = [
    (
        READ,
        '件数で読み終わりを決める（limit は best-effort なのに）',
        '        cursor = _next_cursor_of(response)\n        if not cursor:',
        '        cursor = _next_cursor_of(response)\n        if not cursor or len(_messages_of(response)) < page_limit:',
    ),
    (
        READ,
        '並び順を相手任せにする（ソートを消す）',
        '    raw = _in_order(raw)',
        '    pass',
    ),
    (
        READ,
        'ts を文字列で並べる（桁が違うと "10" < "2" になり位置まで狂う）',
        '            _ts_key(str(item.get("ts") or "")) is None,\n            _ts_key(str(item.get("ts") or "")) or (0, 0),\n        ),\n    )',
        '            False,\n            str(item.get("ts") or ""),\n        ),\n    )',
    ),
    (
        READ,
        '除外した件数を数えない',
        '            skipped[subtype] = skipped.get(subtype, 0) + 1',
        '            pass',
    ),
    (
        READ,
        '未知の subtype も捨てる（残す側に倒す方針を壊す）',
        '        if subtype in EXCLUDED_SUBTYPES:',
        '        if subtype:',
    ),
    (
        READ,
        '打ち切りを黙って隠す',
        '            truncated = True\n            break\n        seen_cursors.add(cursor)',
        '            break\n        seen_cursors.add(cursor)',
    ),
    (
        READ,
        'ts を float にする（別のメッセージを指す）',
        '        ts=str(item.get("ts") or ""),',
        '        ts=str(float(item.get("ts") or 0)),',
    ),
    (
        READ,
        '除外したメッセージを位置に数えない',
        '    latest_ts = str(raw[-1].get("ts") or "") if raw else None',
        '    kept = [m for m in raw if not m.get("subtype")]\n    latest_ts = str(kept[-1].get("ts") or "") if kept else None',
    ),
    (
        READ,
        'oldest を空文字でも渡す',
        '    if oldest:\n        # 空文字は渡さない',
        '    if oldest is not None:\n        # 空文字は渡さない',
    ),
    (
        SUM,
        'unescape の順番を入れ替える（&amp; を最初に戻す）',
        '_UNESCAPES = (("&lt;", "<"), ("&gt;", ">"), ("&amp;", "&"))',
        '_UNESCAPES = (("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"))',
    ),
    (
        SUM,
        'unescape をしない（&amp; のまま要約に渡す）',
        '        f"{\'↳ \' if is_reply(m) else \'\'}{m.user}: {unescape_slack(m.text)}"',
        '        f"{\'↳ \' if is_reply(m) else \'\'}{m.user}: {m.text}"',
    ),
    (
        SUM,
        '他の実体参照まで戻す',
        '_UNESCAPES = (("&lt;", "<"), ("&gt;", ">"), ("&amp;", "&"))',
        '_UNESCAPES = (("&lt;", "<"), ("&gt;", ">"), ("&quot;", \'"\'), ("&amp;", "&"))',
    ),
    (
        SUM,
        '常に要約する（要件の「必要に応じて」を壊す・0件でも Gemini を呼ぶ）',
        '    if len(messages) >= MIN_MESSAGES_TO_SUMMARIZE:\n        return True',
        '    return True\n    if len(messages) >= MIN_MESSAGES_TO_SUMMARIZE:\n        return True',
    ),
    (
        SUM,
        '長さの閾値を無視して常にそのまま送る（要約しなくなる）',
        '    return len(render_transcript(messages)) > SUMMARIZE_THRESHOLD_CHARS',
        '    return False',
    ),
    (
        SUM,
        '件数の閾値を無視する',
        '    if len(messages) >= MIN_MESSAGES_TO_SUMMARIZE:\n        return True',
        '    pass',
    ),
    (
        SUM,
        '要約であることを本文に書かない',
        '        lines.append("―― 要約 ――")',
        '        pass',
    ),
    (
        SUM,
        '除外件数を本文に出さない',
        '        lines.append(f"（{subtype} {number} 件は要約の対象外）")',
        '        pass',
    ),
    (
        SUM,
        '打ち切りを本文に出さない',
        '        lines.append("（取得を上限で打ち切りました。続きが残っています）")',
        '        pass',
    ),
    (
        SUM,
        '原文をそのまま送るときもリンクを付ける',
        '        if permalink.strip():',
        '        if True:',
    ),
    (
        SUM,
        '要約のときにリンクを付けない',
        '        if permalink.strip():',
        '        if False:',
    ),
    (
        SUM,
        '書かれていないことを足すなの指示を消す',
        '        "**ログに書かれていないことは足さないでください。**"',
        '        ""',
    ),
    (
        TOOL,
        '位置を送信の前に進める',
        '    if fetched.latest_ts:\n        advanced = state_module.advanced(',
        '    if False:\n        advanced = state_module.advanced(',
    ),
    (
        TOOL,
        '0件でも位置を進める',
        '    if fetched.latest_ts:\n        advanced = state_module.advanced(',
        '    if True:\n        advanced = state_module.advanced(',
    ),
    (
        TOOL,
        'dry-run でも送る',
        '    if dry_run or not gate.ok:',
        '    if not gate.ok:',
    ),
    (
        TOOL,
        'ガードが止めても送る',
        '    if dry_run or not gate.ok:',
        '    if dry_run:',
    ),
    (
        TOOL,
        '要約に失敗しても続行して原文を送る（月200通を焼く）',
        '        except Exception as error:  # noqa: BLE001 - 失敗しても状態は進めない',
        '        except Exception as error:  # noqa: BLE001\n            summary = None\n        if False:',
    ),
    (
        TOOL,
        '要約しなくてもリンクを取りに行く',
        '    if summarized and fetched.latest_ts:',
        '    if fetched.latest_ts:',
    ),
    (
        TOOL,
        '送信前の通数が読めなくても 0 として送る',
        '            permalink=permalink,\n            replies=replies,\n            dropped_threads=dropped,\n            watching=watching,\n        )\n\n    try:\n        response = line_send.push(',
        '            permalink=permalink,\n        ) if False else None\n        usage_before = 0\n\n    try:\n        response = line_send.push(',
    ),
    (
        TOOL,
        '読めなかった通数を before と同じ値に倒す',
        '    except (line_send.SendError, line_auth.LineError):\n        usage_after = None',
        '    except (line_send.SendError, line_auth.LineError):\n        usage_after = usage_before',
    ),
    (
        TOOL,
        '記録に oldest を残さない',
        '        oldest=oldest,\n        to=to,',
        '        oldest=None,\n        to=to,',
    ),
    (
        TOOL,
        '宛先を伏せずに記録する',
        '        "to_masked": line_send.mask_destination(to),',
        '        "to_masked": to,',
    ),
    (
        TOOL,
        '無制限（None）を 0 として扱う',
        '    if remaining is None:\n        checked.append("残り通数: 無制限")',
        '    if False:\n        checked.append("残り通数: 無制限")',
    ),
    (
        TOOL,
        '確認した値を出さない',
        '    lines = [f"  [確認] {value}" for value in gate.checked]',
        '    lines = []',
    ),
    (
        TOOL,
        '止める理由を1つだけ返す',
        '    return Gate(\n        ok=not blocks,\n        blocks=tuple(blocks),',
        '    return Gate(\n        ok=not blocks,\n        blocks=tuple(blocks[:1]),',
    ),
    (
        TOOL,
        '届かない宛先でも通す',
        '    if reachability.reachable:',
        '    if True:',
    ),
    (
        STATE,
        '壊れた状態ファイルを初回として続行する',
        '    except (OSError, json.JSONDecodeError) as error:\n        raise StateError(',
        '    except (OSError, json.JSONDecodeError) as error:\n        return empty()\n        raise StateError(',
    ),
    (
        STATE,
        '数値の位置を黙って文字列にする',
        '        if not isinstance(value, str):\n            # 数値で入っていたら',
        '        if False:\n            # 数値で入っていたら',
    ),
    (
        STATE,
        '位置を進めるときに元を書き換える',
        '    return replace(current, cursors={**current.cursors, channel: value})',
        '    current.cursors[channel] = value\n    return current',
    ),
    (
        STATE,
        '空の ts でも位置を進める',
        '    if not value:\n        raise ValueError("空の ts では位置を進められません")',
        '    pass',
    ),
    (
        STATE,
        '一時ファイルを使わず直接上書きする',
        '        temporary.write_text(body + "\\n", encoding="utf-8")\n        os.replace(temporary, target)',
        '        target.write_text(body + "\\n", encoding="utf-8")',
    ),
    (
        STATE,
        'チャンネルを区別せず1本の位置を使う',
        '    value = current.cursors.get(channel)',
        '    value = next(iter(current.cursors.values()), None)',
    ),
    (
        GEMINI,
        '一時エラーの判定に本文の文字列を使う',
        '    if isinstance(error, genai_errors.APIError):\n        return error.code in TRANSIENT_STATUS',
        '    if isinstance(error, genai_errors.APIError):\n        return any(str(c) in str(error) for c in TRANSIENT_STATUS)',
    ),
    (
        GEMINI,
        'httpx.TransportError を拾わない',
        '    return isinstance(error, (OSError, httpx.TransportError))',
        '    return isinstance(error, OSError)',
    ),
    (
        GEMINI,
        'AFC を切らない',
        '        automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True)',
        '        automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=False)',
    ),
    (
        GEMINI,
        '空の答えを成功として返す',
        '    if not text:\n        raise ApiError(',
        '    if False:\n        raise ApiError(',
    ),
    (
        GEMINI,
        '空の prompt でも呼ぶ（材料の無い要約＝発明）',
        '    if not (prompt or "").strip():\n        raise ValueError',
        '    if False:\n        raise ValueError',
    ),
    (
        GEMINI,
        'エラー本文を載せない（後継モデルの名指しが消える）',
        '    message = f"{head}\\n詳細: {error}"',
        '    message = head',
    ),
    (
        GEMINI,
        'キーを伏せない',
        '    return ApiError(redact(message, api_key))',
        '    return ApiError(message)',
    ),
    (
        GEMINI,
        '空白だけのキーを通す',
        '    value = (env.get(API_KEY_ENV) or "").strip()',
        '    value = env.get(API_KEY_ENV) or ""',
    ),
    (
        SEND,
        'sentMessages が空でも成功にする',
        '    if not isinstance(sent_messages, list) or not sent_messages:',
        '    if False:',
    ),
    (
        SEND,
        '再送キーを付けない',
        '    headers = {"X-Line-Retry-Key": retry_key or str(uuid.uuid4())}',
        '    headers = {}',
    ),
    (
        SEND,
        '通数が取れないときに 0 を返す',
        '    if not isinstance(payload, dict) or "totalUsage" not in payload:\n        raise SendError',
        '    if False:\n        raise SendError',
    ),
    (
        SEND,
        'bool を通数として通す',
        '    if isinstance(value, bool) or not isinstance(value, int):',
        '    if not isinstance(value, int):',
    ),
    (
        SEND,
        '短い宛先をそのまま出す',
        '    if len(text) <= _MASK_KEEP * 2:\n        return "…" * 3',
        '    if False:\n        return "…" * 3',
    ),
    (
        SEND,
        '本文を strip する（送った文字列と届いた文字列がずれる）',
        '    return {"to": to, "messages": [{"type": "text", "text": text}]}',
        '    return {"to": to, "messages": [{"type": "text", "text": text.strip()}]}',
    ),
    (
        READ,
        '返信から親を落とさない（チャンネル本文と重複する）',
        '            [item for item in raw if str(item.get("ts") or "") != parent_ts]',
        '            list(raw)',
    ),
    (
        READ,
        '返信の位置を無視する（毎回ぜんぶ送り直す）',
        '            item for item in items if _is_after(str(item.get("ts") or ""), boundary)',
        '            item for item in items',
    ),
    (
        READ,
        '読めない ts の返信を捨てる（残す側の方針を壊す）',
        '    if left is None or right is None:\n        return True',
        '    if left is None or right is None:\n        return False',
    ),
    (
        READ,
        '取れなかったスレッドを黙って飲む（次回また試す手がかりが消える）',
        '            failed.append(parent_ts)\n            continue',
        '            continue',
    ),
    (
        STATE,
        '窓から落ちた親を黙って消す（以後の返信が届かないことを言わない）',
        '        dropped = tuple(keys[:overflow])',
        '        dropped = ()',
    ),
    (
        STATE,
        '既に読んだ返信の位置を上書きする（同じ返信を送り直す）',
        '        known.setdefault(key, "")',
        '        known[key] = ""',
    ),
    (
        STATE,
        '見張っていない親まで位置に足す（窓が無限に伸びる）',
        '        if key in known and value:',
        '        if value:',
    ),
    (
        STATE,
        'threads の中身の型を検査しない（数値の ts を黙って受ける）',
        '            if not isinstance(value, str):\n                # cursors と同じ判断。',
        '            if False:\n                # cursors と同じ判断。',
    ),
    (
        TOOL,
        '返信を本文に混ぜない（読んでいるのに届かない）',
        '    messages = slack_read.in_time_order(list(fetched.messages) + list(replies.messages))',
        '    messages = slack_read.in_time_order(list(fetched.messages))',
    ),
    (
        TOOL,
        '送信より先に返信の位置を進める（送信に失敗した返信が二度と読まれない）',
        '            watch=watch,\n        )',
        '            watch=watch,\n        )\n        state_module.save(\n            state_path,\n            state_module.replies_advanced(\n                current, channel, replies.latest_by_parent\n            ),\n        )',
    ),
    (
        TOOL,
        '--include-replies を無視して常に読まない（フラグが黙って効かなくなる）',
        '            include_replies=args.include_replies,',
        '            include_replies=False,',
    ),
    (
        TOOL,
        '返信の件数を本文に出さない（Slack を開いても数が合わない）',
        '        reply_count=len(replies.messages),',
        '        reply_count=0,',
    ),
    (
        SUM,
        '親まで返信として数える（内訳が狂う）',
        '    return bool(message.thread_ts) and message.thread_ts != message.ts',
        '    return bool(message.thread_ts)',
    ),
    (
        TOOL,
        '見張っている本数を数えない（dry-run の画面が 0 本になる）',
        '        watching = len(watch)',
        '        watching = 0',
    ),
    (
        VERIFY,
        '返信を読んでいない実行にも照合項目を作る（確かめていないことが一致に化ける）',
        '    if "reply_watch" not in payload or payload.get("reply_watch") is None:',
        '    if False:',
    ),
    (
        VERIFY,
        '数え直せなかった返信を「一致」として扱う',
        '    if reply_count is None:',
        '    if False:',
    ),
    (
        VERIFY,
        '記録ではなく空の見張りから数え直す（物差しを記録の側から取らない）',
        '    watch = payload.get("reply_watch")',
        '    watch = {}',
    ),
]


def run_tests(work: Path) -> bool:
    """写した側でテストを回す。1件でも落ちたら True。"""
    proc = subprocess.run(
        [str(PYTHON), "-m", "pytest", *TEST_PATHS, "-x", "-q", "--no-header"],
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
            # このリポジトリは core.autocrlf の影響で .py が CRLF になりうる。
            # newline="" のまま `\n` を含むパターンを探すと、**複数行のパターンは
            # 構造的に一度もマッチしない**——そして「置換先なし」は素通りと
            # 同じ扱いなので、壊し方が悪いのか照合器が壊れているのか
            # 区別が付かないまま数字だけ出る（課題9で実際に4件が該当した）。
            haystack = original.replace("\r\n", "\n")

            if haystack.count(before) != 1:
                not_found.append(f"{index:3}. {label}（{target}・{haystack.count(before)}件一致）")
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
