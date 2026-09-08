"""watchlist の商品を取りに行き、行にして、スプレッドシートへ追記する CLI。

課題2の**皮**。判断は下の層（`fetch_items` / `transform` / `diff` /
`common.sheets_client`）が持っていて、ここがやるのは順序と突き合わせだけである。

順序に意味がある
------------------------------------------------------------------

1. **見出しを確かめる。** 違えば1件も取りに行かない——書けないと分かっているのに
   楽天へ投げても、QPS を捨てるだけで成果は1行も残らない（DESIGN 5-K）
2. **履歴を読む。** 追記より**前**に読む。後に読むと今回の行が履歴に入り、
   自分自身と比べて何が起きても「変化なし」になる（`diff` 側でも弾いているが、
   順序を固定しておけば二重に守れる）
3. 取りに行く → 行にする（**失敗も1行**・DESIGN 5-B）
4. 比べる → 追記する

突き合わせるもの（DESIGN 5-C）
------------------------------------------------------------------

**watchlist の件数 = 作った行数 = 送った行数 = 入った行数。**
1つでも欠けたら、それは「その商品の価格が動かなかった日」ではなく
「**記録できなかった日**」である。終了コードで区別する::

    0   全部取れて、全部書けた
    1   書き込みが足りない（送った行数と入った行数が違う）
    2   取れなかった商品がある（書き込みは足りている）

**0 以外を返すことが目的ではない。** 定期実行では誰も画面を見ていないので、
「何が起きたか」を**数字の形で**残すのが目的である。

既知の割り切り
------------------------------------------------------------------

履歴は毎回**全部**読む。行が増えれば読む量も増える。
シートのセル上限（DESIGN 5-D）と合わせて、**まだ実測していない**。
確かめるまで「大丈夫」と書かない。
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Sequence

import diff
import fetch_items
import transform

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common import env_file, sheets_client  # noqa: E402

#: watchlist の既定の場所。`task2/` の中に置く。
DEFAULT_WATCHLIST = Path(__file__).resolve().parent / "watchlist.json"
#: 書き込み先のタブ名。スプレッドシートの名前ではない。
DEFAULT_SHEET_NAME = "シート1"


class ToSheetError(Exception):
    """利用者にそのまま見せられる失敗。"""


@dataclass(frozen=True)
class Watchlist:
    """追う商品。**落とした重複も持って回る。**"""

    codes: list[str]
    duplicates: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class RunResult:
    """その回に何が起きたか。**数字で残す。**"""

    requested: int
    fetched_ok: int
    fetched_failed: int
    reasons: dict[str, int]
    rows_built: int
    sent: int
    written: int
    wrote: bool
    drops: list[diff.Comparison]
    incomparable: dict[str, int]
    name_changed: list[str]
    stock_changed: list[str]
    duplicates: list[str]
    header_state: str

    @property
    def ok(self) -> bool:
        """**送った行が全部入ったか。** 取得の失敗はここに含めない。"""
        return self.sent == self.written

    @property
    def exit_code(self) -> int:
        if self.wrote and not self.ok:
            return 1
        if self.fetched_failed:
            return 2
        return 0


def load_watchlist(path: str | Path) -> Watchlist:
    """追う商品を読む。**空も、壊れているのも、エラーにする。**

    0件で正常終了すると「今日は動いた」と読める（DESIGN 5-P）。
    重複は落とすが、**落としたことは持って回る**——黙って落とすと
    「入れたのに行が無い」と混同される（DESIGN 5-O）。
    """
    path = Path(path)
    if not path.is_file():
        raise ToSheetError(
            f"watchlist が見つかりません: {path}\n"
            '{"items": [{"itemCode": "ショップコード:商品番号", "memo": "覚え書き"}]} '
            "の形で作ってください。"
        )
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, UnicodeDecodeError) as error:
        raise ToSheetError(f"watchlist が JSON として読めません: {path}\n  {error}") from None

    items = data.get("items") if isinstance(data, dict) else None
    if not isinstance(items, list):
        raise ToSheetError(f'watchlist に "items" の配列がありません: {path}')

    codes: list[str] = []
    duplicates: list[str] = []
    for index, item in enumerate(items, start=1):
        code = item.get("itemCode") if isinstance(item, dict) else None
        if not isinstance(code, str) or not code.strip():
            # **黙って飛ばさない。** 追っているつもりの商品が消える。
            raise ToSheetError(
                f"watchlist の {index} 件目に itemCode がありません: {path}"
            )
        code = code.strip()
        (duplicates if code in codes else codes).append(code)

    if not codes:
        raise ToSheetError(
            f"watchlist が空です: {path}\n"
            "0件で正常終了すると「動いた」と読めてしまうので、ここで止めます。"
        )
    return Watchlist(codes=codes, duplicates=duplicates)


def build_rows(report: fetch_items.FetchReport, fetched_at: str) -> list[list[Any]]:
    """取得の結果を、そのまま行にする。**失敗も1行**（DESIGN 5-B）。"""
    return [
        transform.to_row(result.item, fetched_at)
        if result.ok
        else transform.failure_row(result.item_code, fetched_at, result.reason)
        for result in report.results
    ]


def run(
    *,
    http: Any,
    service: Any,
    item_codes: Sequence[str],
    application_id: str,
    access_key: str,
    spreadsheet_id: str,
    sheet_name: str = DEFAULT_SHEET_NAME,
    fetched_at: str | None = None,
    threshold: int = 0,
    write: bool = True,
    duplicates: Sequence[str] = (),
    **fetch_kwargs: Any,
) -> RunResult:
    """1回ぶんを通す。**外部への接続は引数で受け取る**ので、テストで再現できる。"""
    fetched_at = fetched_at or transform.now_iso()
    data_range = f"{sheet_name}!A:{sheets_client.column_letter(len(transform.COLUMNS))}"

    # 1. 見出し。違えば**ここで止まる**——取りに行く前に。
    header_state = sheets_client.ensure_header(
        service, spreadsheet_id, sheet_name, transform.COLUMNS, create=write
    )

    # 2. 履歴。**追記より前に読む。**
    history = sheets_client.read_rows(
        service, spreadsheet_id, data_range, width=len(transform.COLUMNS)
    )

    # 3. 取りに行く。
    report = fetch_items.fetch_all(
        http,
        item_codes=item_codes,
        application_id=application_id,
        access_key=access_key,
        **fetch_kwargs,
    )
    rows = build_rows(report, fetched_at)

    # 4. 比べる。
    comparisons = diff.compare_all(rows, history)
    drops = diff.drops(comparisons, threshold=threshold)

    # 5. 追記する。
    appended = sheets_client.AppendResult(sent=0, updated=0)
    if write:
        appended = sheets_client.append_rows(
            service,
            spreadsheet_id,
            data_range,
            rows,
            expected_width=len(transform.COLUMNS),
        )

    return RunResult(
        requested=report.requested,
        fetched_ok=report.ok,
        fetched_failed=report.failed,
        reasons=dict(Counter(r.reason for r in report.results if not r.ok)),
        rows_built=len(rows),
        sent=appended.sent,
        written=appended.updated,
        wrote=write,
        drops=drops,
        incomparable=dict(Counter(c.note for c in comparisons if not c.comparable)),
        name_changed=[c.item_code for c in comparisons if c.name_changed],
        stock_changed=[c.item_code for c in comparisons if c.stock_changed],
        duplicates=list(duplicates),
        header_state=header_state,
    )


def format_report(result: RunResult) -> list[str]:
    """画面に出す行。**資格情報は1文字も載せない**（記事のスクショに写る）。"""
    lines = [
        f"対象           {result.requested} 件"
        + (f"（重複 {len(result.duplicates)} 件を除外）" if result.duplicates else ""),
        f"見出し         {result.header_state}",
        f"取得           {result.fetched_ok} 件 / 失敗 {result.fetched_failed} 件"
        + (
            "（" + " ".join(f"{k} {v}" for k, v in result.reasons.items()) + "）"
            if result.reasons
            else ""
        ),
        f"作った行       {result.rows_built} 行",
    ]

    if result.wrote:
        mark = "" if result.ok else "  ← 足りない"
        lines.append(f"書き込み       送った {result.sent} 行 / 入った {result.written} 行{mark}")
    else:
        # **「何をしないか」だけでなく「何をしたか」も言う。**
        lines.append("書き込み       シートには1行も書いていません（--dry-run）")
        lines.append("               ※ 楽天 API には投げています（QPS を消費しました）")

    lines.append(f"値下がり       {len(result.drops)} 件")
    for comparison in result.drops:
        lines.append(
            f"  ↓ {comparison.item_code} "
            f"{comparison.previous_price} → {comparison.current_price} 円"
            f"（{comparison.delta:+} 円・実質）"
        )

    if result.incomparable:
        detail = " ".join(f"{k} {v}" for k, v in result.incomparable.items())
        lines.append(f"比較できず     {sum(result.incomparable.values())} 件（{detail}）")
    for code in result.name_changed:
        lines.append(f"⚠ 商品名が変わりました: {code}（同じ商品か確かめてください）")
    for code in result.stock_changed:
        lines.append(f"⚠ 在庫の状態が変わりました: {code}")
    return lines


def permission_hint(key_path: str | Path, error: Exception) -> str:
    """403 / 404 のときだけ、**何をすれば直るか**を返す。他は空。

    **権限（API の有効化）と所属（シートの共有）は別物。**
    サービスアカウントは認証に成功しても、シートに共有されていなければ
    書き込みだけ 403 で落ちる。課題1で Slack の
    「スコープはあるがチャンネルに居ない」で踏んだのと同じ形である。

    エラー本文だけ見せても、**何をすれば直るのかが分からない**。
    関係ない失敗にこの案内を出さないのは、**本当の原因から目をそらすため**。

    ``status_code`` と ``resp.status`` の両方を見る。``googleapiclient`` の
    版によって片方しか無いことがあり、片方だけ見ると取りこぼす。
    """
    status = getattr(error, "status_code", None)
    if status is None:
        status = getattr(getattr(error, "resp", None), "status", None)

    if status == 403:
        email = read_client_email_or_placeholder(key_path)
        return (
            "シートへの書き込みが拒否されました（403）。\n"
            "認証は通っているので、足りないのは**シートの共有**です。\n"
            f"  1. 書き込み先のスプレッドシートを開く\n"
            f"  2. 右上の「共有」を押す\n"
            f"  3. 次のアドレスを「編集者」として追加する:\n"
            f"       {email}\n"
            "（Google Sheets API の有効化とは別の設定です）"
        )
    if status == 404:
        return (
            "スプレッドシートが見つかりません（404）。\n"
            "「.env」の GOOGLE_SHEET_ID を確かめてください。\n"
            "  https://docs.google.com/spreadsheets/d/<ここがID>/edit"
        )
    return ""


def read_client_email_or_placeholder(key_path: str | Path) -> str:
    """共有先のアドレス。読めなければ**案内は出したまま**、場所だけ示す。

    アドレスが取れないことと、案内を出さないことは別。
    """
    return (
        sheets_client.read_client_email(key_path)
        or "（鍵ファイルの client_email を見てください）"
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="to_sheet.py",
        description="watchlist の商品を楽天 API で取得し、スプレッドシートへ追記します。",
    )
    parser.add_argument("--watchlist", default=str(DEFAULT_WATCHLIST),
                        help="追う商品の一覧（既定: task2/watchlist.json）")
    parser.add_argument("--env", default=".env", help="資格情報を読む .env の場所")
    parser.add_argument("--sheet-name", default=DEFAULT_SHEET_NAME,
                        help=f"書き込み先のタブ名（既定: {DEFAULT_SHEET_NAME}）")
    parser.add_argument("--threshold", type=int, default=0,
                        help="値下がりを知らせる下げ幅（円）。既定 0 は1円でも知らせる")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "シートに1行も書きません（見出しも作りません）。"
            "ただし楽天 API には投げます＝QPS を消費します。"
            "「何もしない」ではありません。"
        ),
    )
    return parser


def main(argv: Sequence[str] | None = None, *, out: Callable[[str], None] = print) -> int:
    args = build_parser().parse_args(argv)
    # **先に空で束縛しておく。** 下の except は env を読むので、
    # .env の読み込み自体が想定外の失敗をすると NameError で握りつぶされる。
    env: dict[str, str] = {}
    try:
        env = env_file.load(args.env)
        watchlist = load_watchlist(args.watchlist)

        missing = [
            key
            for key in (
                "RAKUTEN_APPLICATION_ID",
                "RAKUTEN_ACCESS_KEY",
                "GOOGLE_SHEET_ID",
                "GOOGLE_SERVICE_ACCOUNT_FILE",
            )
            if not env.get(key)
        ]
        if missing:
            raise ToSheetError(
                "「.env」に足りない項目があります: " + " / ".join(missing) + "\n"
                ".env.example に値の取り方を書いてあります。"
            )

        key_path = env["GOOGLE_SERVICE_ACCOUNT_FILE"]
        credentials = sheets_client.load_credentials(key_path, [sheets_client.SCOPE_WRITE])
        service = sheets_client.build_service(credentials)

        import requests  # 遅延 import。**資格情報の検査より後に読む**（失敗を早く見せる）

        with requests.Session() as session:
            result = run(
                http=session,
                service=service,
                item_codes=watchlist.codes,
                duplicates=watchlist.duplicates,
                application_id=env["RAKUTEN_APPLICATION_ID"],
                access_key=env["RAKUTEN_ACCESS_KEY"],
                spreadsheet_id=env["GOOGLE_SHEET_ID"],
                sheet_name=args.sheet_name,
                threshold=args.threshold,
                write=not args.dry_run,
            )
    except (ToSheetError, sheets_client.SheetError, env_file.EnvFileError) as error:
        out(str(error))
        return 1
    except Exception as error:  # noqa: BLE001
        # **握って握りつぶすのではなく、握って案内を足してから投げ直す。**
        # 403 は「認証は通っているのに書けない」形で、本文だけでは直せない。
        hint = permission_hint(env.get("GOOGLE_SERVICE_ACCOUNT_FILE", ""), error)
        if not hint:
            raise
        out(hint)
        return 1

    for line in format_report(result):
        out(line)
    return result.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
