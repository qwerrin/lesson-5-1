"""書いたものを、**別の経路で読み直して照合する**。

課題1で評価された型をそのまま持ってきている——「送った API の戻り値ではなく、
着地した先を読んで確かめる」。ここでは**読み取り専用のスコープで別に認証**して読む。

なぜ別の資格情報で読むのか（DESIGN 5-S）
------------------------------------------------------------------

書いたのと同じもので読み直すと、「書けたつもり」を「書けた」と確かめる経路が
**1本になる**。その1本が壊れたとき、確認も一緒に壊れて何も気づけない。

「一致」は「比べた」の証拠にならない（DESIGN 5-R）
------------------------------------------------------------------

**0行と0行を比べれば必ず一致する。** 範囲の指定を1文字間違えるだけでそうなり、
照合は堂々と「一致しました」と言う。だからこの実装は、
**比べた行数とセル数を必ず返し、0件を成功にしない**。
報告にも「N 行 × M セルを比べて一致」と書く——数字が無い「一致」は信じない。

照合器は誰が検査するのか（DESIGN 5-T）
------------------------------------------------------------------

課題1で実際に踏んだ。照合器そのものがミューテーションの対象に入っておらず、
「素通り0」が「守られている」ではなく「**そこを見ていない**」を意味していた。
このファイルは `task2/tools/mutate.py` の対象に入れてある。

追記が既存の行を壊していないか（DESIGN 5-U）
------------------------------------------------------------------

**上書きされた古い行は、今回追記した行の検査を全部通る。**
だから追記の前後で全体の行数を数え、``前 + 送った数 = 後`` を確かめる。
前の数を知らないとき（単独で走らせたとき）は、**確かめたとも言わない**。
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Sequence

import diff
import transform

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common import env_file, sheets_client  # noqa: E402

#: 状態の列に入ってよい語。**これ以外は「知らない語」として数える。**
KNOWN_STATUSES = frozenset({transform.STATUS_OK, transform.STATUS_FAILED})


@dataclass(frozen=True)
class Mismatch:
    """1セルの食い違い。**直せる形で持つ。**"""

    #: シート上の行番号（見出しを1行目として数える）。
    row: int
    column: str
    expected: Any
    actual: Any


@dataclass(frozen=True)
class VerifyResult:
    compared_rows: int
    compared_cells: int
    mismatches: list[Mismatch]
    rows_after: int
    rows_before: int | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """**比べていなければ通さない。** 0件の「一致」は一致ではない。"""
        return bool(self.compared_rows) and not self.mismatches and not self.notes


@dataclass(frozen=True)
class AuditResult:
    """シート全体の点検。照合とは別に、**形が崩れていないか**を見る。"""

    checked_rows: int
    header_ok: bool
    bad_time: int
    bad_status: int
    bad_price: int
    short_rows: int

    @property
    def ok(self) -> bool:
        return (
            self.header_ok
            and bool(self.checked_rows)
            and not (self.bad_time or self.bad_status or self.bad_price)
        )


def read_only_service(
    key_path: str | Path,
    *,
    loader: Callable[..., Any] | None = None,
    builder: Callable[..., Any] | None = None,
) -> Any:
    """**読み取り専用のスコープ**でサービスを組む（DESIGN 5-S）。

    書く側（`to_sheet.py`）は ``spreadsheets``、こちらは ``spreadsheets.readonly``。
    別々に認証することで、確認の経路が書き込みの経路と分かれる。
    """
    kwargs: dict[str, Any] = {}
    if loader is not None:
        kwargs["loader"] = loader
    credentials = sheets_client.load_credentials(
        key_path, [sheets_client.SCOPE_READ], **kwargs
    )
    if builder is not None:
        return sheets_client.build_service(credentials, builder=builder)
    return sheets_client.build_service(credentials)


def _data_range(sheet_name: str) -> str:
    return f"{sheet_name}!A:{sheets_client.column_letter(len(transform.COLUMNS))}"


def _read_all(service: Any, spreadsheet_id: str, sheet_name: str) -> list[list[Any]]:
    """**書いた型のまま**読む。

    照合する側が ``FORMATTED_VALUE`` で読んだら、書き込み側の異常を
    照合側の異常が打ち消してしまう。
    """
    return sheets_client.read_rows(
        service, spreadsheet_id, _data_range(sheet_name), width=len(transform.COLUMNS)
    )


def verify_appended(
    service: Any,
    spreadsheet_id: str,
    sheet_name: str,
    expected_rows: Sequence[Sequence[Any]],
    *,
    rows_before: int | None = None,
) -> VerifyResult:
    """いま書いた行が、そのままの形で着地しているかを確かめる。

    末尾の空セルが落ちて返るのは**不一致ではない**（DESIGN 5-Q）。
    ここを不一致と数えると正常な回のたびに赤くなり、**本物の不一致が埋もれる**。
    """
    rows = _read_all(service, spreadsheet_id, sheet_name)
    notes: list[str] = []
    expected = [list(row) for row in expected_rows]

    if not expected:
        # **0件を成功にしない**（DESIGN 5-R）。
        notes.append("照合する行が0件でした。書いた行が渡っていません。")
        return VerifyResult(
            compared_rows=0, compared_cells=0, mismatches=[],
            rows_after=len(rows), rows_before=rows_before, notes=notes,
        )

    data = rows[1:]  # 見出しは照合の対象にしない
    if len(data) < len(expected):
        notes.append(
            f"シートの行が足りません: 期待 {len(expected)} 行 / 実際 {len(data)} 行"
        )
        return VerifyResult(
            compared_rows=0, compared_cells=0, mismatches=[],
            rows_after=len(rows), rows_before=rows_before, notes=notes,
        )

    if rows_before is not None and rows_before + len(expected) != len(rows):
        # **上書きされた古い行は、今回の行の検査を全部通る**（DESIGN 5-U）。
        notes.append(
            f"全体の行数が合いません: 前 {rows_before} + 送った {len(expected)} "
            f"≠ 後 {len(rows)}。既存の行が上書きされた可能性があります。"
        )

    tail = data[-len(expected):]
    mismatches: list[Mismatch] = []
    compared_cells = 0
    for offset, (want, got) in enumerate(zip(expected, tail)):
        # シート上の行番号。見出しが1行目なので、末尾から数え直す。
        sheet_row = len(rows) - len(expected) + offset + 1
        for index, column in enumerate(transform.COLUMNS):
            compared_cells += 1
            if want[index] != got[index]:
                mismatches.append(
                    Mismatch(row=sheet_row, column=column,
                             expected=want[index], actual=got[index])
                )

    return VerifyResult(
        compared_rows=len(expected),
        compared_cells=compared_cells,
        mismatches=mismatches,
        rows_after=len(rows),
        rows_before=rows_before,
        notes=notes,
    )


def audit(service: Any, spreadsheet_id: str, sheet_name: str) -> AuditResult:
    """シート全体の形を点検する。**期待する値を持たずに走れる。**

    `verify_appended` が「いま書いたもの」を見るのに対し、こちらは
    **積み上がった履歴が読める形のままか**を見る。定期実行の途中で
    設定が変わっても、ここが鳴る。
    """
    # **補う前に数える。** `_read_all` は幅をそろえて返すので、
    # そのあとで「短い行」を数えると**必ず0**になる——鳴らない検査になる。
    # 2026-09-09 に実物で「列が短い 0 行」と出て気づいた。
    raw = sheets_client.read_rows(service, spreadsheet_id, _data_range(sheet_name))
    short_rows = sum(1 for row in raw[1:] if len(row) < len(transform.COLUMNS))

    rows = [
        list(row) + [""] * max(0, len(transform.COLUMNS) - len(row)) for row in raw
    ]
    header = [str(cell) for cell in rows[0]] if rows else []
    header_ok = header == list(transform.COLUMNS)

    bad_time = bad_status = bad_price = 0
    data = rows[1:]
    for row in data:
        if diff.parse_time(row[transform.COLUMNS.index("取得時刻")]) is None:
            bad_time += 1

        status = row[transform.COLUMNS.index("状態")]
        if status not in KNOWN_STATUSES:
            bad_status += 1
        elif status == transform.STATUS_OK:
            # 失敗行は価格が空なのが正しい。**そこを数えると毎回鳴る。**
            price = row[transform.COLUMNS.index("本体価格")]
            if not isinstance(price, int) or isinstance(price, bool):
                bad_price += 1

    return AuditResult(
        checked_rows=len(data),
        header_ok=header_ok,
        bad_time=bad_time,
        bad_status=bad_status,
        bad_price=bad_price,
        short_rows=short_rows,
    )


def format_report(result: VerifyResult) -> list[str]:
    """**数字の無い「一致」を出さない**（DESIGN 5-R）。"""
    lines = [
        f"照合           {result.compared_rows} 行 × "
        f"{result.compared_cells} セルを比べました"
    ]
    if result.rows_before is not None:
        lines.append(
            f"行数           前 {result.rows_before} → 後 {result.rows_after}"
        )
    else:
        lines.append(f"行数           後 {result.rows_after}（前の数は不明・確かめていません）")

    for note in result.notes:
        lines.append(f"⚠ {note}")
    if result.mismatches:
        lines.append(f"不一致         {len(result.mismatches)} セル")
        for mismatch in result.mismatches[:10]:
            lines.append(
                f"  {mismatch.row} 行目 {mismatch.column}: "
                f"書いた {mismatch.expected!r} / 入っている {mismatch.actual!r}"
            )
    elif result.compared_rows:
        lines.append("不一致         0 セル")
    return lines


def format_audit(result: AuditResult) -> list[str]:
    return [
        f"点検           {result.checked_rows} 行",
        f"見出し         {'一致' if result.header_ok else '不一致'}",
        f"時刻が読めない {result.bad_time} 行",
        f"知らない状態   {result.bad_status} 行",
        f"価格が数値でない {result.bad_price} 行",
        f"末尾が落ちた行 {result.short_rows} 行（空欄は返ってこない。異常ではない）",
    ]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="verify_sheet.py",
        description=(
            "シートを読み取り専用の資格情報で読み直し、形が崩れていないか点検します。"
        ),
    )
    parser.add_argument("--env", default=".env", help="資格情報を読む .env の場所")
    parser.add_argument("--sheet-name", default="シート1", help="読むタブの名前")
    return parser


def main(argv: Sequence[str] | None = None, *, out: Callable[[str], None] = print) -> int:
    args = build_parser().parse_args(argv)
    try:
        env = env_file.load(args.env)
        service = read_only_service(env["GOOGLE_SERVICE_ACCOUNT_FILE"])
        result = audit(service, env["GOOGLE_SHEET_ID"], args.sheet_name)
    except (sheets_client.SheetError, env_file.EnvFileError, KeyError) as error:
        out(str(error))
        return 1

    for line in format_audit(result):
        out(line)
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
