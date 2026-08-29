#!/usr/bin/env python3
"""Slack のチャンネルを読み、必要に応じて要約して、LINE に送る。

    .venv\\Scripts\\python.exe task1\\summarize_to_line.py --channel C0XXXXXXXXX --dry-run

要件（2026-08-29 に講座サイトで確認・原文）::

    Slack のチャンネルから情報を取得し、必要に応じて要約し、LINE に送信する
    シンプルなツールを作成する。
    目的: 重要な情報を LINE で受け取り、見逃しを防ぐ仕組みを実現。

**「見逃しを防ぐ」は、順番でできている**
------------------------------------------------------------------

読む → 要約 → 送る → **成功したら位置を進める**。

最後の1手が最後にあることだけが、取りこぼさないことを支えている。
途中で落ちた回は位置が進まないので、次回また同じ範囲を読む。
**重複はするが落とさない**（at-least-once）。逆にすると、送信に失敗した
範囲が二度と読まれない。

この課題で新しいのは要約だけ
------------------------------------------------------------------

============================ ================================================
部品                          どこから来たか
============================ ================================================
Slack の読み取り              課題7（``conversations.history``）
LINE の送信                   課題9 → ``common/line_send.py`` に格上げ
送信前ガード                  課題10（``profile`` の 404 と残通数）
**要約**                      **この課題で新規**（Gemini）
============================ ================================================

``--dry-run`` が何をしないか
------------------------------------------------------------------

**送信だけをしない。** 読み取りは実行し、本文も組み立てて表示する。
状態ファイルも書かない。**Gemini は呼ぶ**（要約が必要な入力なら）ので、
課金は発生する。

「dry-run」という語は何をしないかまで言っていない。実際、vault の
``vault_doctor --dry-run`` は「日報を直さない」だけで NOW.md は毎回書く、
という取り違えを実際に起こした。だから**ここに何をしないかを書く**。
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

from slack_sdk.errors import SlackApiError  # noqa: E402

import slack_read  # noqa: E402
import state as state_module  # noqa: E402
import summarize  # noqa: E402
from common import env_file, gemini_client, line_auth, line_send, slack_auth  # noqa: E402

DEFAULT_STATE = str(_HERE / "state.json")
DEFAULT_RESULTS = str(_HERE / "results.json")

#: 読むのに必要な Slack のスコープ。**使う側に書かせる**（slack_auth の方針）。
SLACK_SCOPES = ("channels:history",)

#: 残りがこれ以下なら注意として伝える（止めはしない）。課題10 と同じ。
LOW_REMAINING = 10


class ToolError(Exception):
    """利用者にそのまま見せられる失敗。"""


@dataclass(frozen=True)
class Gate:
    """送る前の確認の結果。

    ``blocks`` と ``notes`` を分けるのは、**止める理由と、伝えるだけの話を
    混ぜないため**。混ぜると「警告が出たから止まったのか」が読めなくなる。
    """

    ok: bool
    blocks: tuple[str, ...]
    notes: tuple[str, ...]
    #: **確認に使った値そのもの。** 「通過」だけを出すと、何を見て通過に
    #: したのかが残らない。実行画面はこの課題の証拠になるので、見た値を出す。
    checked: tuple[str, ...] = ()


@dataclass(frozen=True)
class Outcome:
    """1回の実行の結果。**送ったかどうかと、なぜ送らなかったかを分けて持つ。**"""

    sent: bool
    body: str
    fetched: slack_read.Fetched
    summarized: bool
    gate: Gate
    record: dict | None = None
    error: str = ""
    #: 要約したときに付ける原文へのリンク。**空なら取れなかった**——
    #: 呼ぶ側が画面に出す（静かにしない）。
    permalink: str = ""


# ------------------------------------------------------------------ 送る前の判定


def judge_send(
    reachability: line_auth.Reachability,
    remaining: int | None,
    *,
    needed: int = 1,
) -> Gate:
    """送ってよいかを決める。**通信をしない純粋な判定**にしてある。

    理由は**全部集める**。1つ返して止めると、直して再実行したら次の理由で
    止まる、を繰り返させることになる。

    ``remaining`` の ``None`` は**無制限**であって 0 ではない。0 は
    「上限はあるが使い切った」で、真逆の意味になる。
    """
    blocks: list[str] = []
    notes: list[str] = []
    checked: list[str] = []

    if reachability.reachable:
        checked.append("宛先: 届く（プロフィールを取得できた）")
    else:
        checked.append("宛先: 届かない（プロフィールが 404）")
        blocks.append(
            reachability.reason or "宛先に届きません（理由が記録されていません）。"
        )

    if remaining is None:
        checked.append("残り通数: 無制限")
        notes.append("今月の送信上限は設定されていません（無制限）。")
    else:
        checked.append(f"残り通数: {remaining} 通")
        if remaining < needed:
            # 負の値もそのまま出す。丸めると「あと 0 通」と「12 通ぶん超過」が
            # 同じ文面になり、直しかたの見当が付かなくなる。
            blocks.append(
                f"今月の残り通数が足りません: 残り {remaining} 通 / 必要 {needed} 通。"
            )
        elif remaining <= LOW_REMAINING:
            notes.append(f"今月の残り通数がわずかです: あと {remaining} 通。")

    return Gate(
        ok=not blocks,
        blocks=tuple(blocks),
        notes=tuple(notes),
        checked=tuple(checked),
    )


def format_gate(gate: Gate) -> tuple[str, ...]:
    """ガードの結果を、画面に出す行の並びにする。

    **1本の文字列にせず行で返す。** 呼ぶ側が好きに出せるうえ、
    「何が出たか」をテストで1行ずつ確かめられる。
    """
    lines = [f"  [確認] {value}" for value in gate.checked]
    lines += [f"  [注意] {note}" for note in gate.notes]
    lines += [f"  [中止] {reason}" for reason in gate.blocks]
    lines.append("  送信前の確認: " + ("通過" if gate.ok else "中止"))
    return tuple(lines)


# ------------------------------------------------------------------ 記録


def build_record(
    *,
    channel: str,
    oldest: str | None,
    to: str,
    info: line_auth.BotInfo,
    fetched: slack_read.Fetched,
    summarized: bool,
    body: str,
    sent: line_send.Sent,
    usage_before: int,
    usage_after: int | None,
) -> dict:
    """あとから照合するための記録を組む。

    LINE には bot が送ったテキストを読み返す API が無い（課題9で確認）。
    だから**何を読んで何に変えたか**を、こちら側に残す。

    ``read_count`` と ``latest_ts`` は Slack 側と突き合わせられる。
    ``basic_id`` は ``/v2/bot/info`` という**別のエンドポイント**が答えた値で、
    意図したチャネルへ送ったことを言える。

    **宛先は伏せる。** 記録は public リポジトリに入る。
    **要約の本文そのものは残さない**——Slack の会話が公開物になるため。
    代わりに長さだけ残す。
    """
    return {
        "channel": channel,
        # **どこから読んだか。** 残さないと、あとから read_count を再現できない
        # （位置は次の実行のために先へ進んでしまう）。
        "oldest": oldest,
        "to_masked": line_send.mask_destination(to),
        "basic_id": info.basic_id,
        "bot_user_id": info.user_id,
        # 通数は**押した経路とは別の口**から取った値。「何を送ったか」は言わないが、
        # 「送信対象として1通数えた」ことは言える。
        # **after が None は「読めなかった」。** 0 や before と同じ値に倒すと、
        # 「送ったのに数えられていない」という別の事実に化ける。
        "usage_before": usage_before,
        "usage_after": usage_after,
        "read_count": len(fetched.messages),
        "skipped": fetched.skipped,
        "pages": fetched.pages,
        "truncated": fetched.truncated,
        "latest_ts": fetched.latest_ts,
        "summarized": summarized,
        "body_chars": len(body),
        "message_id": sent.message_id,
        "request_id": sent.request_id,
    }


def write_record(path: str | Path, record: dict) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


# ------------------------------------------------------------------ 本体


def run(
    *,
    slack_client,
    line_session,
    bot_info: line_auth.BotInfo,
    reachability: line_auth.Reachability,
    remaining: int | None,
    gemini,
    channel: str,
    channel_label: str,
    to: str,
    state_path: str | Path,
    results_path: str | Path | None = None,
    dry_run: bool = False,
    model: str = gemini_client.DEFAULT_MODEL,
    secrets: tuple = (),
) -> Outcome:
    """1回ぶんの処理。**通信する相手は全部引数で受け取る。**

    そうしないと、テストが「本物を呼ばないこと」しか確かめられなくなる。
    """
    current = state_module.load(state_path)
    oldest = state_module.cursor_for(current, channel)

    fetched = slack_read.fetch_since(slack_client, channel=channel, oldest=oldest)

    # ---- 要約するか（要らないなら呼ばない。呼べば課金される）
    summary = None
    summarized = False
    if summarize.needs_summary(fetched.messages):
        transcript = summarize.render_transcript(fetched.messages)
        try:
            summary = gemini_client.generate(
                gemini,
                prompt=summarize.build_prompt(transcript),
                model=model,
            )
        except Exception as error:  # noqa: BLE001 - 失敗しても状態は進めない
            # **代わりに原文を送らない。** 巨大な本文は読めないうえ、
            # 月200通の枠を消費する。送らなければ位置も進まないので、
            # 次回そのまま拾える。
            return Outcome(
                sent=False,
                body="",
                fetched=fetched,
                summarized=False,
                gate=Gate(ok=False, blocks=("要約に失敗しました。",), notes=()),
                error=str(error),
            )
        summarized = True

    # **要約したときだけ原文へのリンクを取る。** 要約しないなら本文が原文
    # そのものなので、リンクは冗長。使わない値を取りに行かない。
    permalink = ""
    if summarized and fetched.latest_ts:
        permalink = slack_read.fetch_permalink(
            slack_client, channel=channel, ts=fetched.latest_ts
        )

    body = summarize.build_message(
        summary=summary,
        messages=fetched.messages,
        channel_label=channel_label,
        skipped=fetched.skipped,
        truncated=fetched.truncated,
        permalink=permalink,
    )

    gate = judge_send(reachability, remaining)

    if dry_run or not gate.ok:
        # 本文は作って返す。**何を送るはずだったかは必ず見せる。**
        return Outcome(
            sent=False,
            body=body,
            fetched=fetched,
            summarized=summarized,
            gate=gate,
            permalink=permalink,
        )

    # **送る前の通数を先に取る。** 取れないなら送らない——増分を出せないと
    # 送った証拠が1つ減る。まだ送っていないのだから、止まるほうが安い。
    try:
        usage_before = line_send.fetch_usage(line_session, secrets=secrets)
    except (line_send.SendError, line_auth.LineError) as error:
        return Outcome(
            sent=False,
            body=body,
            fetched=fetched,
            summarized=summarized,
            gate=gate,
            error=f"送信前の通数を読めませんでした（送信していません）。\n{error}",
            permalink=permalink,
        )

    try:
        response = line_send.push(
            line_session,
            line_send.build_payload(to=to, text=body),
            secrets=secrets,
        )
        sent = line_send.read_send_result(response)
    except (line_send.SendError, line_auth.LineError) as error:
        return Outcome(
            sent=False,
            body=body,
            fetched=fetched,
            summarized=summarized,
            gate=gate,
            error=str(error),
            permalink=permalink,
        )

    # ここから先は**送信済み**。失敗しても「送っていない」ことにはできない。
    try:
        usage_after: int | None = line_send.fetch_usage(line_session, secrets=secrets)
    except (line_send.SendError, line_auth.LineError):
        usage_after = None

    record = build_record(
        channel=channel,
        oldest=oldest,
        to=to,
        info=bot_info,
        fetched=fetched,
        summarized=summarized,
        body=body,
        sent=sent,
        usage_before=usage_before,
        usage_after=usage_after,
    )
    if results_path:
        write_record(results_path, record)

    # ---- **送信が成功した後にだけ位置を進める。**
    if fetched.latest_ts:
        state_module.save(
            state_path, state_module.advanced(current, channel, fetched.latest_ts)
        )

    return Outcome(
        sent=True,
        body=body,
        fetched=fetched,
        summarized=summarized,
        gate=gate,
        record=record,
        permalink=permalink,
    )


# ------------------------------------------------------------------ CLI


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Slack のチャンネルを読み、必要に応じて要約して LINE に送ります。"
        )
    )
    parser.add_argument(
        "--channel", required=True, help="読むチャンネルID（C で始まる値）"
    )
    parser.add_argument(
        "--label", help="本文に出すチャンネルの呼び名（既定はチャンネルID）"
    )
    parser.add_argument("--state", default=DEFAULT_STATE, help="前回位置の保存先")
    parser.add_argument(
        "--json-out", help="結果の記録先。指定したときだけ書く"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "LINE へ送らず、状態も書きません。"
            "**読み取りと要約は実行します**（Gemini の課金は発生します）"
        ),
    )
    return parser.parse_args(argv)


def _default_dependencies(channel: str):
    """本物の相手を組む。**組む順番に意味がある。**

    資格情報の失敗を、送信より手前で1つずつ名指しできるようにする。

    **3つとも ``.env`` から読む。環境変数は使わない。**

    Section 4-3 では課題ごとに渡し方が割れていた（課題4〜8は環境変数、
    課題9だけ ``.env``）。ここで ``.env`` に寄せるのは、**環境変数が
    共有の名前空間だから**である。

    2026-08-17、課題7のために ``SLACK_BOT_TOKEN`` を User スコープの
    環境変数へ置いたところ、同じ PC に常駐していた別のシステム（Hermes）が
    それを「Slack を使う」判定に読み、接続に必要な別のトークンが無いまま
    起動しようとして落ちた。**cron が約2日間まるごと止まった。**
    設定は1文字も触っていない。

    ``.env`` はリポジトリの中で閉じているので、この形の巻き添えが起きない。
    """
    env = env_file.load(_REPO_ROOT / env_file.ENV_FILENAME)

    slack_token = slack_auth.read_bot_token(env)
    slack_client = slack_auth.build_client(slack_token)
    identity = slack_auth.fetch_identity(slack_client)

    line_token = line_auth.read_channel_access_token(env)
    to = line_auth.read_user_id(env)
    session = line_auth.build_session(line_token)
    info = line_auth.fetch_bot_info(session, secrets=(line_token,))

    reachability = line_auth.fetch_profile(session, to, secrets=(line_token,))
    quota = line_auth.fetch_quota(session, secrets=(line_token,))
    consumption = line_auth.fetch_consumption(session, secrets=(line_token,))
    remaining = line_auth.remaining_messages(quota, consumption)

    api_key = gemini_client.read_api_key(env)
    gemini = gemini_client.build_client(api_key)

    return {
        "slack_client": slack_client,
        "slack_identity": identity,
        "slack_token": slack_token,
        "line_session": session,
        "bot_info": info,
        "reachability": reachability,
        "remaining": remaining,
        "to": to,
        "gemini": gemini,
        "secrets": (line_token, api_key),
    }


def main(argv: Sequence[str] | None = None, *, factory: Callable | None = None) -> int:
    args = parse_args(argv)
    build = factory or _default_dependencies

    try:
        deps = build(args.channel)
    except (slack_auth.AuthError, line_auth.LineError, gemini_client.GeminiError,
            env_file.EnvFileError) as error:
        print(error, file=sys.stderr)
        return 1
    except SlackApiError as error:
        print(f"Slack API がエラーを返しました: {error}", file=sys.stderr)
        return 1

    identity = deps.get("slack_identity")
    if identity is not None:
        print(f"Slack ワークスペース: {identity.team}")
        check = slack_auth.check_scopes(identity, SLACK_SCOPES)
        if check.known and check.missing:
            print(
                "読み取りのスコープが足りません: " + " / ".join(check.missing),
                file=sys.stderr,
            )
            return 1

    print(f"LINE チャネル: {deps['bot_info'].basic_id}")

    try:
        outcome = run(
            slack_client=deps["slack_client"],
            line_session=deps["line_session"],
            bot_info=deps["bot_info"],
            reachability=deps["reachability"],
            remaining=deps["remaining"],
            gemini=deps["gemini"],
            channel=args.channel,
            channel_label=args.label or args.channel,
            to=deps["to"],
            state_path=args.state,
            results_path=args.json_out,
            dry_run=args.dry_run,
            secrets=deps.get("secrets", ()),
        )
    except (state_module.StateError, ToolError) as error:
        print(error, file=sys.stderr)
        return 1
    except SlackApiError as error:
        print(f"Slack API がエラーを返しました: {error}", file=sys.stderr)
        return 1

    fetched = outcome.fetched
    print(f"\n読んだ件数: {len(fetched.messages)} 件（{fetched.pages} ページ）")
    for subtype, number in sorted(fetched.skipped.items()):
        print(f"  除外: {subtype} {number} 件")
    if fetched.truncated:
        print("  取得を上限で打ち切りました。続きが残っています")
    print(f"要約: {'した' if outcome.summarized else 'していない（そのまま送る長さ）'}")
    if outcome.summarized and not outcome.permalink:
        # **静かにしない。** リンクが無いことで送信は止めないが、
        # 「要約だけが届いて原文へ辿れない」状態は伝える必要がある。
        print("  原文へのリンクを取得できませんでした（本文には入りません）")

    print("\n送る本文:")
    for line in outcome.body.splitlines():
        print(f"  | {line}")

    print()
    for line in format_gate(outcome.gate):
        print(line)

    if outcome.error:
        print(f"\n{outcome.error}", file=sys.stderr)
        return 1

    if not outcome.sent:
        if args.dry_run:
            print("\n--dry-run のため送信しませんでした（状態も書いていません）。")
            return 0
        return 1

    print(f"\n送信しました。message_id: {outcome.record['message_id']}")
    if fetched.latest_ts:
        print(f"次回はこの ts より後を読みます: {fetched.latest_ts}")
    if args.json_out:
        print(f"結果を書き出しました: {args.json_out}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
