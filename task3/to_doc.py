"""議事録を Google ドキュメントへ書き出す。

`minutes.py` が書いた本文を受け取り、**会議1本につき1つのドキュメントを新規作成**
して挿入する（`DESIGN.md` 2章）。追記にしないのは、二重処理（5-H）が
**既存の内容を壊す形**で出るためである。

============ ====================================================================
DESIGN       ここで引き受ける穴
============ ====================================================================
5-H          同じ内容を二度書かない。**内容のハッシュ**を台帳に残す
3.2          空の本文で API を呼ばない（空のドキュメントだけが残る）
3.2          作成と挿入は別（`documents().create` は本文を無視して成功する）
============ ====================================================================

**API を叩く部分は `common/docs_client.py`**。あれは合格済みの実装を
1文字も変えずに移したもので、`task3/tools/check_port.py` が照合している。
ここに書くのは、**この課題に固有の判断だけ**——重複の見分け方と、台帳の扱い。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Callable, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common import docs_client, env_file, google_auth  # noqa: E402

#: 台帳の既定の置き場。議事録と同じ場所に置く。
DEFAULT_LEDGER = "posted.json"

#: ハッシュの長さ。**スクリーンショットに載るので折り返さない長さ**にする。
HASH_LENGTH = 16


class AlreadyPosted(Exception):
    """同じ内容が既に書き出されている。**失敗ではないが、黙って進めない。**"""


def content_hash(body: str) -> str:
    """本文から重複判定の鍵を作る。

    **音声ではなく議事録の本文で見る。** 音声が同じでも、要約は実行ごとに
    揺れる（実測で決定事項が 2件／3件 と変わった）。*同じ音声から出た違う議事録*は
    別物として扱ってよく、止めたいのは**まったく同じものを二度書く**ことだけである。
    """
    return hashlib.sha256(body.encode("utf-8")).hexdigest()[:HASH_LENGTH]


def build_title(meeting: str, held_at: str) -> str:
    """ドキュメントのタイトル。**無題のドキュメントを量産しない。**"""
    name = (meeting or "").strip()
    if not name:
        raise ValueError("会議名が空です。タイトルを付けられません")
    return "議事録 {} {}".format(name, (held_at or "不明").strip())


# ------------------------------------------------------------------ 台帳


def load_ledger(path: str | Path) -> dict:
    """書き出し済みの記録を読む。**読めなければ空として続ける。**

    止めると、台帳が壊れた日から議事録が1本も出せなくなる。ただし
    **空として扱えば重複の検出は効かない**ので、呼び手には黙らせない
    （`main` が警告を出す）。
    """
    p = Path(path)
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def save_ledger(path: str | Path, data: dict) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + chr(10),
        encoding="utf-8",
        newline=chr(10),
    )


# ------------------------------------------------------------------ 書き出し


def post(
    service,
    *,
    title: str,
    body: str,
    ledger_path: str | Path,
    force: bool = False,
) -> dict:
    """ドキュメントを作って本文を入れ、台帳に残す。

    **台帳に残すのは成功したあと。** 先に残すと、挿入で落ちたときに
    *どこにも無い議事録を「ある」と信じ続ける*ことになる。
    """
    text = docs_client.ensure_insertable(body)  # 空なら API を呼ぶ前に落ちる
    digest = content_hash(text)

    ledger = load_ledger(ledger_path)
    if not force and digest in ledger:
        known = ledger[digest]
        raise AlreadyPosted(
            "同じ内容が既に書き出されています（{}）。"
            "作り直すなら --force を付けてください: {}".format(
                known.get("documentId", "?"), known.get("url", "")
            )
        )

    created = docs_client.create_document_with_text(service, title, text)

    ledger[digest] = {
        "documentId": created["documentId"],
        "title": created["title"],
        "url": created["url"],
    }
    save_ledger(ledger_path, ledger)
    return created


# ------------------------------------------------------------------ 入口


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="議事録を Google ドキュメントへ書き出す（会議1本につき1つ新規作成）"
    )
    parser.add_argument("minutes", type=Path, help="minutes.py が書いた議事録")
    parser.add_argument("--meeting", default="会議", help="タイトルに入れる会議名")
    parser.add_argument("--held-at", default="不明", help="タイトルに入れる日時")
    parser.add_argument("--ledger", type=Path, default=None,
                        help="書き出し済みの台帳（既定: 議事録と同じ場所の posted.json）")
    parser.add_argument("--force", action="store_true",
                        help="同じ内容でも作り直す（5-H の検査を外す）")
    # 既定は相対パス。**公開するスクリーンショットにホームのパスを写さない。**
    parser.add_argument("--credentials", default="credentials.json")
    parser.add_argument("--token", default="token.json")
    return parser


def _default_service_factory(args: argparse.Namespace):
    credentials = google_auth.load_credentials(
        args.credentials, args.token, docs_client.DEFAULT_SCOPES
    )
    return docs_client.build_service(credentials)


def main(argv: Sequence[str] | None = None, *, service_factory: Callable | None = None) -> int:
    args = build_parser().parse_args(argv)
    factory = service_factory or _default_service_factory

    if not args.minutes.exists():
        print("議事録が見つかりません: {}".format(args.minutes), file=sys.stderr)
        return 1
    body = args.minutes.read_text(encoding="utf-8")
    ledger_path = args.ledger or args.minutes.with_name(DEFAULT_LEDGER)

    # **読めない台帳を黙って通さない。** 空として続けるが、
    # そのあいだ重複の検査は効いていない。
    if Path(ledger_path).exists() and not load_ledger(ledger_path):
        print(
            "台帳が読めないので空として扱います（この回は重複を検出できません）: {}".format(
                ledger_path
            ),
            file=sys.stderr,
        )

    try:
        title = build_title(args.meeting, args.held_at)
        service = factory(args)
        created = post(
            service, title=title, body=body, ledger_path=ledger_path, force=args.force
        )
    except AlreadyPosted as error:
        print(error, file=sys.stderr)
        return 3
    except (ValueError, docs_client.DocError, google_auth.AuthError) as error:
        print(error, file=sys.stderr)
        return 1

    print("書き出しました")
    print("  タイトル      : {}".format(created["title"]))
    print("  ドキュメントID: {}".format(created["documentId"]))
    print("  挿入した文字数: {:,}".format(created["insertedLength"]))
    print("  内容のハッシュ: {}".format(content_hash(body)))
    print("  リンク        : {}".format(created["url"]))
    print("  台帳          : {}".format(ledger_path))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
