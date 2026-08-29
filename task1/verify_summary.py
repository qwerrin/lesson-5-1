#!/usr/bin/env python3
"""送った記録を、**別の経路から**読み直して突き合わせる。読むだけ。

    .venv\\Scripts\\python.exe task1\\verify_summary.py --results task1\\results.json

この照合には、原理的に届かない場所がある
------------------------------------------------------------------

LINE には bot が送ったテキストを読み返す API が無い（課題9で公式 OpenAPI
定義に当たって確認）。**「送った本文が届いたか」は最後まで機械では言えない。**

さらにこの課題では、もう1つ言えないことが増えた。**要約が正しいかは
検証できない。** 2026-08-29 の実機で、要約は原文に無い語（「Google」）を足し、
別人の発言を1人に畳んだ。プロンプトで禁じても起きる。

だからこの道具は**言えることだけ**を照合し、言えないことは
「確認できない」と**必ず表示する**。合格したときこそ表示する——
全部 OK の画面に注記が無ければ、読んだ人は「全部確かめた」と受け取る。

何を、どこから突き合わせるか
------------------------------------------------------------------

============================ ==================================================
言えること                    どこから取るか
============================ ==================================================
通数が1つ増えた               ``quota/consumption``（**押した経路とは別の口**）
意図したチャネルへ送った       ``/v2/bot/info`` の ``basicId``
読んだ件数が再現する           Slack を**記録した ``oldest`` から数え直す**
============================ ==================================================

**数え直しは記録の ``oldest`` を使う。** 現在の状態ファイルは次の実行のために
先へ進んでいるので、そちらを使うと再現できない。物差しは**記録の側**から取る
（課題7で「応答の中だけで値を突き合わせるとトートロジーになる」と決めたのと
同じ形で、比べる相手をこちらの記録に固定する）。
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import slack_read  # noqa: E402
from common import env_file, line_auth, slack_auth  # noqa: E402

DEFAULT_RESULTS = str(_HERE / "results.json")


class VerifyError(Exception):
    """照合できなかった。利用者にそのまま見せられる。"""


@dataclass(frozen=True)
class Check:
    """1項目の照合結果。**期待と実際の両方を持つ。**

    片方だけだと、食い違ったときに何を直せばよいか分からない。
    """

    label: str
    expected: object
    actual: object
    ok: bool


def _compare(label: str, expected, actual) -> Check:
    return Check(label=label, expected=expected, actual=actual, ok=expected == actual)


def load_results(path: str | Path) -> dict:
    target = Path(path)
    if not target.is_file():
        raise VerifyError(
            f"記録が見つかりません: {target}\n"
            "先に summarize_to_line.py を --json-out 付きで実行してください。"
        )
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise VerifyError(f"記録を読めませんでした: {target}\n{error}") from error

    if not isinstance(payload, dict):
        raise VerifyError(f"記録の形が想定と違います（辞書ではありません）: {target}")
    return payload


# ------------------------------------------------------------------ 記録の中だけで言えること


def build_local_checks(payload: dict) -> list[Check]:
    """記録の中で閉じる検査。**ここだけで合格にしない。**

    同じ記録の中で値を比べるのはトートロジーになりうるので、
    ここでは「形として成立しているか」だけを見る。
    """
    checks: list[Check] = []

    message_id = str(payload.get("message_id") or "")
    checks.append(
        Check(
            label="message ID がある",
            expected="空でない",
            actual=message_id or "(空)",
            ok=bool(message_id),
        )
    )

    before = payload.get("usage_before")
    after = payload.get("usage_after")
    if "usage_after" not in payload or "usage_before" not in payload:
        # **「キーが無い」と「読めなかった」は別の事実。**
        # 前者の直しかたは撮り直し、後者は API の調査。同じ NG に畳むと、
        # 正反対の対処を1つの表示に混ぜることになる（2026-08-29 に実際に踏んだ）。
        checks.append(
            Check(
                label="通数が1つ増えた",
                expected="1",
                actual="(記録が古い形式です。通数が入っていません)",
                ok=False,
            )
        )
    elif after is None:
        # **読めなかったことを「一致」に倒さない。**
        checks.append(
            Check(
                label="通数が1つ増えた",
                expected="1",
                actual="(送信後の通数を読めなかった)",
                ok=False,
            )
        )
    else:
        delta = after - before if isinstance(before, int) else None
        checks.append(_compare("通数が1つ増えた", 1, delta))

    masked = str(payload.get("to_masked") or "")
    # 記録は public リポジトリに入る。書く側だけで守ると、書き方を変えた日に
    # 気づけないので、**照合側でも見る**。
    checks.append(
        Check(
            label="宛先が伏せられている",
            expected="伏せ字を含む",
            actual=masked,
            ok="…" in masked,
        )
    )

    return checks


# ------------------------------------------------------------------ 別経路と突き合わせ


def build_remote_checks(
    payload: dict, info: line_auth.BotInfo, slack_count: int | None
) -> list[Check]:
    """**別のエンドポイント**が答えた値と突き合わせる。"""
    checks = [_compare("チャネル（basicId）", payload.get("basic_id"), info.basic_id)]

    if slack_count is None:
        # 数え直せなかったことを「一致」にしない（課題8「0 件は不一致」と同じ）。
        checks.append(
            Check(
                label="読んだ件数",
                expected=payload.get("read_count"),
                actual="(Slack から数え直せなかった)",
                ok=False,
            )
        )
    else:
        checks.append(_compare("読んだ件数", payload.get("read_count"), slack_count))

    return checks


def recount(client, payload: dict) -> int | None:
    """Slack を**記録した ``oldest`` から**数え直す。

    **読み取りと同じ規則で数える。** 別の規則で数えると、実装が正しくても
    件数が食い違う。だから ``slack_read.fetch_since`` をそのまま使う。

    **``oldest`` のキーが無い記録では数え直さない。** 無いのを
    「初回（範囲指定なし）」と読むとチャンネルの全件を数えることになり、
    「期待 1 / 実際 10」という**照合器のほうが間違っている NG** が出る
    （2026-08-29 に実際に踏んだ）。数え直せないなら、数え直せないと言う。
    """
    if "oldest" not in payload:
        return None

    fetched = slack_read.fetch_since(
        client, channel=str(payload.get("channel") or ""), oldest=payload.get("oldest")
    )
    return len(fetched.messages)


# ------------------------------------------------------------------ 表示


def all_ok(checks: Sequence[Check]) -> bool:
    return all(check.ok for check in checks)


def format_checks(checks: Sequence[Check]) -> tuple[str, ...]:
    """**期待と実際の両方**を出す。片方では直せない。"""
    lines = []
    for check in checks:
        mark = "OK  " if check.ok else "NG  "
        lines.append(f"  {mark}{check.label}: 期待 {check.expected!r} / 実際 {check.actual!r}")
    return tuple(lines)


def format_unverifiable() -> tuple[str, ...]:
    """**確認できないことを、合格したときこそ出す。**"""
    return (
        "  - 送った本文が届いたか: LINE に bot の送信を読み返す API が無いため確認できません（目視）",
        "  - 要約が原文に忠実か: 検証できません。2026-08-29 の実機で、要約は原文に無い語を足し、"
        "別人の発言を1人に畳みました。本文には原文へのリンクを入れてあります",
    )


# ------------------------------------------------------------------ CLI


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="送信の記録を別経路から読み直して突き合わせます（読むだけ）。"
    )
    parser.add_argument("--results", default=DEFAULT_RESULTS, help="記録のパス")
    return parser.parse_args(argv)


def _default_dependencies():
    env = env_file.load(_REPO_ROOT / env_file.ENV_FILENAME)

    slack_client = slack_auth.build_client(slack_auth.read_bot_token(env))

    line_token = line_auth.read_channel_access_token(env)
    session = line_auth.build_session(line_token)
    info = line_auth.fetch_bot_info(session, secrets=(line_token,))

    return {"slack_client": slack_client, "bot_info": info}


def main(argv: Sequence[str] | None = None, *, factory: Callable | None = None) -> int:
    args = parse_args(argv)
    build = factory or _default_dependencies

    try:
        payload = load_results(args.results)
        deps = build()
    except (VerifyError, line_auth.LineError, slack_auth.AuthError,
            env_file.EnvFileError) as error:
        print(error, file=sys.stderr)
        return 1

    print(f"記録: {args.results}")
    print(f"チャンネル: {payload.get('channel')} / 起点: {payload.get('oldest')}")

    local = build_local_checks(payload)
    print("\n記録の中で言えること:")
    for line in format_checks(local):
        print(line)

    try:
        slack_count = recount(deps["slack_client"], payload)
    except Exception as error:  # noqa: BLE001 - 数え直せないことも結果のうち
        print(f"\nSlack から数え直せませんでした: {error}", file=sys.stderr)
        slack_count = None

    remote = build_remote_checks(payload, deps["bot_info"], slack_count)
    print("\n別のエンドポイントと突き合わせた結果:")
    for line in format_checks(remote):
        print(line)

    print("\nこの照合では確認できないこと:")
    for line in format_unverifiable():
        print(line)

    checks = list(local) + list(remote)
    ok = all_ok(checks)
    print(f"\n照合: {len(checks)} 項目 / " + ("すべて一致" if ok else "不一致あり"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
