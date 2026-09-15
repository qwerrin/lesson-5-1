"""議事録の**決定事項だけ**を LINE に流す（発展機能）。

会議に出られなかった人が、全文 1,080 字を開かなくても「何が決まったか」を
受け取れるようにする。`DESIGN.md` 9章の候補のうち、実装したのはこれ。

============ ====================================================================
DESIGN       ここで引き受ける穴
============ ====================================================================
5-O          **0件**と**読めていない**を分ける。0 と 0 を比べれば必ず一致する
5-H          同じ通知を二度送らない。無料プランの通数は月200通しかない
5-P          決定の根拠（時刻と逐語）を本文に載せる。要約だけ配らない
============ ====================================================================

**リンクを手で渡させない**
------------------------------------------------------------------

``--doc-url`` のような引数を作ると、*書き出したのとは別のドキュメントを指す
通知*が送れてしまう。**どちらも成功する**ので、見た目では気づけない
（4-3-2 課題1 で「実行画面と照合画面が別のファイルを指した」のと同じ形）。

そこで ``to_doc.py`` が書いた台帳 ``posted.json`` を、**議事録本文のハッシュ**で
引く。台帳に無ければ**送らない**——まだ Google ドキュメントに無い議事録の
決定事項を配ると、読み手が全文を開けない。

**台帳の読み書きは ``to_doc`` のものをそのまま使う**
------------------------------------------------------------------

同じ形の台帳が2つになる。書き直すと、片方だけ直った日に静かにずれる
（`docs_client.py` を「1文字も変えずに移す」と決めたのと同じ理由）。

**送ったものは読み返せない**
------------------------------------------------------------------

LINE には bot が送ったテキストを読み返す API が無い（`common/line_send.py`
の冒頭に、公式の OpenAPI 定義まで当たった経緯がある）。だから記録に
**messageId と通数の増分**を残す。増分は「*別のエンドポイントが*通数の増加を
認めた」という間接材料で、何を送ったかは言わない。言わないことを承知で残す。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Callable, Sequence

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import to_doc  # noqa: E402
from common import env_file, line_auth, line_send  # noqa: E402

NL = chr(10)

#: 通知した記録の置き場。``posted.json``（書き出しの台帳）とは**別物**。
#: 書き出しは済んでいて通知だけ落ちた、という状態が普通に起きる。
DEFAULT_LEDGER = "notified.json"

#: ``to_doc.py`` が書いた書き出しの台帳。ここからリンクを引く。
POSTED_LEDGER = "posted.json"


class NotifyError(Exception):
    """利用者にそのまま見せられる失敗。"""


class AlreadyNotified(Exception):
    """同じ内容を既に送っている。**失敗ではないが、黙って送り直さない。**"""


# ------------------------------------------------------------------ 本文を組む


def _one(index: int, item) -> str:
    """決定1件を2行にする。

    **欠けている項目を黙って落とさない。** 落とすと、根拠が無い決定と
    根拠がある決定が同じ見た目で並ぶ。読む側は区別できない。
    """
    if not isinstance(item, dict):
        return "{}. （読み取れない項目）".format(index)

    text = str(item.get("text") or "").strip() or "（本文なし）"
    at = str(item.get("at") or "").strip() or "時刻不明"
    quote = str(item.get("quote") or "").strip()
    evidence = "「{}」".format(quote) if quote else "（逐語なし）"
    return "{}. {}{}   {} {}".format(index, text, NL, at, evidence)


def build_body(minutes, *, doc_title: str, doc_url: str) -> str:
    """決定事項だけの本文を組む。

    **「決定が0件」と「decisions が読めていない」を分ける。** 倒すと、
    議事録の形が変わった日に「何も決まらなかった会議」として静かに配られる。
    """
    if not isinstance(minutes, dict) or "decisions" not in minutes:
        raise NotifyError(
            "議事録に decisions がありません。0件ではなく**読めていない**ので送りません。"
        )
    decisions = minutes["decisions"]
    if not isinstance(decisions, list):
        raise NotifyError(
            "decisions が一覧ではありません（{}）。送りません。".format(
                type(decisions).__name__
            )
        )

    lines = [
        "【決定事項】",
        doc_title,
        "",
        "決定 {} 件".format(len(decisions)),
        "",
    ]
    if not decisions:
        # 決まらなかった会議も情報である。**送らないほうが誤解を生む。**
        lines.append("決定事項なし（この会議では何も決まりませんでした）")
    else:
        lines.extend(_one(i, item) for i, item in enumerate(decisions, 1))

    lines.extend(
        [
            "",
            "全文: {}".format(doc_url),
            "",
            "※この通知は決定事項だけです。TODO・論点は全文にあります。",
            "※画面共有の中身・無言の合意・議題に上がらなかったことは、"
            "元の録音に入っていません。",
        ]
    )
    return NL.join(lines)


# ------------------------------------------------------------------ リンクを引く


def find_document(posted: dict, body_text: str) -> dict:
    """書き出しの台帳から、この議事録のドキュメントを引く。

    **鍵は議事録本文のハッシュ。** ``to_doc.py`` が同じ関数で作ったものなので、
    人が打ち間違える余地が無い。
    """
    digest = to_doc.content_hash(body_text)
    entry = posted.get(digest)
    if not isinstance(entry, dict) or not str(entry.get("url") or "").strip():
        raise NotifyError(
            "この議事録はまだ Google ドキュメントに書き出されていません"
            "（ハッシュ {}）。".format(digest)
            + NL
            + "先に to_doc.py を実行してください。"
            "全文を開けない通知は送りません。"
        )
    return entry


# ------------------------------------------------------------------ 送る


def notify(
    session,
    *,
    to: str,
    body: str,
    ledger_path: str | Path,
    force: bool = False,
    secrets: tuple = (),
    decisions: int = 0,
) -> dict:
    """LINE へ送り、証跡を台帳に残す。

    **台帳に残すのは成功したあと。** 先に残すと、送れていない通知を
    「送った」と信じ続け、再実行しても二度と送らない。
    """
    digest = to_doc.content_hash(body)

    ledger = to_doc.load_ledger(ledger_path)
    if not force and digest in ledger:
        known = ledger[digest]
        raise AlreadyNotified(
            "同じ決定事項を既に送っています（messageId {}）。"
            "送り直すなら --force を付けてください。".format(
                known.get("messageId", "?")
            )
        )

    # 送る前に数える。**取れなければ送らない**——増分を材料にすると決めた以上、
    # 片側が無い記録は「送れた」を言えない。
    usage_before = line_send.fetch_usage(session, secrets=secrets)

    response = line_send.push(
        session, line_send.build_payload(to=to, text=body), secrets=secrets
    )
    sent = line_send.read_send_result(response)

    # 送ったあとに落ちても、送信は取り消せない。**記録は残す。**
    try:
        usage_after: int | None = line_send.fetch_usage(session, secrets=secrets)
    except (line_send.SendError, line_auth.LineError):
        usage_after = None

    record = {
        "messageId": sent.message_id,
        "requestId": sent.request_id,
        "to_masked": line_send.mask_destination(to),
        "usageBefore": usage_before,
        "usageAfter": usage_after,
        "decisions": decisions,
    }
    ledger[digest] = record
    to_doc.save_ledger(ledger_path, ledger)
    return record


# ------------------------------------------------------------------ 入口


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="議事録の決定事項だけを LINE に送る（全文へのリンクを添える）"
    )
    parser.add_argument("minutes", type=Path, help="minutes.py が書いた minutes.json")
    parser.add_argument(
        "--body",
        type=Path,
        default=None,
        help="ドキュメントに入れた本文（既定: minutes.json と同じ名前の .txt）",
    )
    parser.add_argument(
        "--posted",
        type=Path,
        default=None,
        help="to_doc.py の書き出し台帳（既定: 同じ場所の posted.json）",
    )
    parser.add_argument(
        "--ledger",
        type=Path,
        default=None,
        help="送信済みの台帳（既定: 同じ場所の notified.json）",
    )
    parser.add_argument(
        "--force", action="store_true", help="同じ内容でも送り直す（5-H の検査を外す）"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="本文を組んで表示するだけ。**送信も接続もしない**（台帳も書かない）",
    )
    return parser


def _default_connect() -> tuple:
    env = env_file.load(_REPO_ROOT / env_file.ENV_FILENAME)
    token = line_auth.read_channel_access_token(env)
    to = line_auth.read_user_id(env)
    session = line_auth.build_session(token)
    return session, to, (token,)


def main(argv: Sequence[str] | None = None, *, connect: Callable | None = None) -> int:
    args = build_parser().parse_args(argv)

    if not args.minutes.exists():
        print("議事録が見つかりません: {}".format(args.minutes), file=sys.stderr)
        return 1
    try:
        data = json.loads(args.minutes.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        print("議事録を JSON として読めません: {}".format(error), file=sys.stderr)
        return 1

    body_path = args.body or args.minutes.with_suffix(".txt")
    if not body_path.exists():
        print(
            "ドキュメントに入れた本文が見つかりません: {}".format(body_path),
            file=sys.stderr,
        )
        return 1
    posted_path = args.posted or args.minutes.with_name(POSTED_LEDGER)
    if not posted_path.exists():
        print(
            "書き出しの台帳がありません: {}".format(posted_path) + NL
            + "先に to_doc.py を実行してください。",
            file=sys.stderr,
        )
        return 1

    try:
        entry = find_document(to_doc.load_ledger(posted_path), body_path.read_text(encoding="utf-8"))
        text = build_body(
            data, doc_title=str(entry.get("title") or ""), doc_url=str(entry["url"])
        )
    except NotifyError as error:
        print(error, file=sys.stderr)
        return 1

    ledger_path = args.ledger or args.minutes.with_name(DEFAULT_LEDGER)
    # **読めない台帳を黙って通さない。** 空として続けるが、
    # そのあいだ重複の検査は効いていない。
    if Path(ledger_path).exists() and not to_doc.load_ledger(ledger_path):
        print(
            "台帳が読めないので空として扱います（この回は重複を検出できません）: "
            "{}".format(ledger_path),
            file=sys.stderr,
        )

    print("送る本文")
    print("-" * 60)
    print(text)
    print("-" * 60)

    if args.dry_run:
        print("--dry-run なので送信していません（接続もしていません）")
        return 0

    try:
        session, to, secrets = (connect or _default_connect)()
        record = notify(
            session,
            to=to,
            body=text,
            ledger_path=ledger_path,
            force=args.force,
            secrets=secrets,
            decisions=len(data["decisions"]),
        )
    except AlreadyNotified as error:
        print(error, file=sys.stderr)
        return 3
    except (NotifyError, line_send.SendError, line_auth.LineError) as error:
        print(error, file=sys.stderr)
        return 1

    print("送信しました")
    print("  決定          : {} 件".format(record["decisions"]))
    print("  メッセージID  : {}".format(record["messageId"]))
    print(
        "  今月の通数    : {} -> {}".format(
            record["usageBefore"],
            "読めず" if record["usageAfter"] is None else record["usageAfter"],
        )
    )
    print("  宛先          : {}".format(record["to_masked"]))
    print("  台帳          : {}".format(ledger_path))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
