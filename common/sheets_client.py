"""Google スプレッドシートの読みと追記。**課題2の書き込み層のコア。**

`service` を受け取るだけで、自分では組み立てない。テストが
「どの引数で呼んだか」まで検査できるようにするため——
**この層の失敗は、引数の選び方でしか起きない。**

公式ドキュメントを読まないと出てこない穴が3つある
------------------------------------------------------------------

どれも**書いた日は成功する**。壊れるのは読み戻す日や、隣に何か置いた日である。

``valueInputOption``（DESIGN 5-L）
    既定的に選ばれがちな ``USER_ENTERED`` は、**文字列を数値・日付・数式に
    変換する**。商品名が ``=`` で始まれば数式になり、``2026-09-08T21:00:00+09:00``
    は日付に化ける。化けると ``diff.parse_time`` が全行で ``None`` を返し、
    **全商品が「取得時刻が読めない」になる**。だから ``RAW``。

    > RAW — The values the user has entered will not be parsed and will be stored as-is.
    > USER_ENTERED — ... strings may be converted to numbers, dates, etc.
    > — <https://developers.google.com/workspace/sheets/api/reference/rest/v4/ValueInputOption>（2026-09-08）

``insertDataOption``（DESIGN 5-M）
    既定の ``OVERWRITE`` は「書き込む領域の既存データを上書きする」。

    > OVERWRITE — The new data overwrites existing data in the areas it is written.
    > INSERT_ROWS — Rows are inserted for the new data.
    > — <https://developers.google.com/workspace/sheets/api/reference/rest/v4/spreadsheets.values/append>（2026-09-08）

``valueRenderOption``（DESIGN 5-N）
    ``values.get`` の既定は ``FORMATTED_VALUE``。公式の例では数値 ``1.23`` が
    **``"$1.23"`` という文字列**で返る。``diff`` は int しか価格と認めないので、
    ここを外すと**全比較が「価格が無い」になり、値下がり通知が永久に鳴らない**。
    エラーは1つも出ない。

    > UNFORMATTED_VALUE — ... would return the number 1.23.
    > — <https://developers.google.com/workspace/sheets/api/reference/rest/v4/ValueRenderOption>（2026-09-08）

**書く側と読む側は対になっている。** 片方だけ直しても、もう片方で型が変わる。
「書けたか」ではなく「**書いて読み戻したものが同じか**」で確かめること。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

from googleapiclient import discovery
from google.oauth2 import service_account

#: 追記するので読み取り専用では足りない。
SCOPE_WRITE = "https://www.googleapis.com/auth/spreadsheets"
#: 読み直して照合する側（`verify_sheet.py`）はこちらで足りる。
#: **書く資格情報で読み直すと、「書けたつもり」を「書けた」と誤認する経路が1本になる。**
SCOPE_READ = "https://www.googleapis.com/auth/spreadsheets.readonly"

#: 解釈させない（5-L）。
VALUE_INPUT_OPTION = "RAW"
#: 既存データを上書きしない（5-M）。
INSERT_DATA_OPTION = "INSERT_ROWS"
#: 書いた型のまま読み戻す（5-N）。
VALUE_RENDER_OPTION = "UNFORMATTED_VALUE"


class SheetError(Exception):
    """利用者にそのまま見せられる失敗。**中身（鍵）は載せない。**"""


@dataclass(frozen=True)
class AppendResult:
    """送った行数と、相手が「書いた」と言った行数。

    **成功コードだけで信じない**（DESIGN 5-A）。2行送って1行しか入っていなくても
    例外は出ない。
    """

    sent: int
    updated: int

    @property
    def ok(self) -> bool:
        return self.sent == self.updated


def column_letter(number: int) -> str:
    """1 から A1 記法の列記号を作る（1→A・27→AA）。

    見出しの範囲を**列数から作る**ために要る。``A1:O1`` のような文字列を
    手で書くと、列を1本足した日に**古い範囲のまま読んで**「見出しが一致した」
    と誤判定する。
    """
    if number < 1:
        raise ValueError(f"列番号は1以上です: {number}")
    letters = ""
    while number > 0:
        number, remainder = divmod(number - 1, 26)
        letters = chr(ord("A") + remainder) + letters
    return letters


def load_credentials(
    key_path: str | Path,
    scopes: Iterable[str],
    *,
    loader: Callable[..., Any] = service_account.Credentials.from_service_account_file,
) -> Any:
    """サービスアカウントの鍵から資格情報を作る。

    **OAuth ではない。** 公開ステータスが「テスト」の OAuth は
    リフレッシュトークンが7日で失効する（`task2/README.md`）。
    課題2は定期実行がゴールなので、7日目に止まって誰も気づかない。

    ``scopes`` に既定値を持たせない。読み取り専用で書こうとすると
    **認証は通るのに書き込みだけ落ちる**ので、呼ぶ側に必ず書かせる。
    """
    key_path = Path(key_path)
    wanted = list(scopes)
    if not wanted:
        raise SheetError("要求するスコープが空です。呼び出し側で指定してください")
    if not key_path.is_file():
        raise SheetError(
            f"サービスアカウントの鍵が見つかりません: {key_path}\n"
            "Google Cloud コンソールでサービスアカウントを作り、JSON の鍵を"
            "このパスに置いてください。手順は task2/README.md を参照。"
        )
    return loader(str(key_path), scopes=wanted)


def build_service(credentials: Any, *, builder: Callable[..., Any] = discovery.build) -> Any:
    """Sheets v4 のクライアントを組む。

    ``cache_discovery=False`` を明示する。既定の ``True`` は ``oauth2client`` の
    ``file_cache`` を探して警告を出す——**その警告は実行画面のスクリーンショットに写る**。
    """
    return builder("sheets", "v4", credentials=credentials, cache_discovery=False)


def read_client_email(key_path: str | Path) -> str:
    """鍵ファイルから共有先のアドレスだけを取り出す。読めなければ空。

    シートの「共有」に貼る宛先で、**403 が返ったときに案内するために使う**
    （権限＝API の有効化 と 所属＝シートの共有 は別物）。

    **取り出すのはこの1項目だけ。** この関数の出力は画面に出る＝
    記事のスクリーンショットに載る。
    """
    try:
        data = json.loads(Path(key_path).read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return ""
    email = data.get("client_email") if isinstance(data, dict) else None
    return email if isinstance(email, str) else ""


def read_rows(
    service: Any, spreadsheet_id: str, range_: str, *, width: int | None = None
) -> list[list[Any]]:
    """範囲を読む。**書いた型のまま返す**（5-N）。

    空のシートは ``values`` キーごと返ってこない。``None`` を返すと呼ぶ側が落ちる。

    ``width`` を渡すと、**末尾に落ちた空セルを補って**その幅にそろえる。
    公式にこう書いてある（2026-09-08 確認）:

    > For output, empty trailing rows and columns will not be included.
    > — <https://developers.google.com/workspace/sheets/api/reference/rest/v4/spreadsheets.values>

    つまり **15列で書いた行が14列で返る**（末尾の ``理由`` が空のとき）。
    書き込みは成功しているので、**読み戻して初めて分かる**——2026-09-08 に
    実物のシートで踏んだ。

    **長い行は切らない。** 人が列を足していれば、それは ``ensure_header`` が
    弾くべき異常であって、読む側が黙って隠してよいものではない。
    """
    response = (
        service.spreadsheets()
        .values()
        .get(
            spreadsheetId=spreadsheet_id,
            range=range_,
            valueRenderOption=VALUE_RENDER_OPTION,
        )
        .execute()
    )
    values = response.get("values") if isinstance(response, dict) else None
    rows = values if isinstance(values, list) else []
    if width is None:
        return rows
    return [list(row) + [""] * max(0, width - len(row)) for row in rows]


def append_rows(
    service: Any,
    spreadsheet_id: str,
    range_: str,
    rows: Sequence[Sequence[Any]],
    *,
    expected_width: int | None = None,
) -> AppendResult:
    """行を追記する。**送る前に形を確かめる。**

    長さの揃っていない行は送らない。送ってしまうとシートは受け取るので、
    **後から「ズレている」と気づく手段が無い**（DESIGN 5-C）。
    """
    rows = [list(row) for row in rows]
    if not rows:
        # 空の書き込みは相手にとって意味が無い。呼ばない。
        return AppendResult(sent=0, updated=0)

    widths = {len(row) for row in rows}
    if len(widths) != 1:
        raise SheetError(f"行の長さがそろっていません: {sorted(widths)}")
    if expected_width is not None and widths != {expected_width}:
        raise SheetError(
            f"列数が想定と違います: 想定 {expected_width} / 実際 {widths.pop()}"
        )

    response = (
        service.spreadsheets()
        .values()
        .append(
            spreadsheetId=spreadsheet_id,
            range=range_,
            valueInputOption=VALUE_INPUT_OPTION,
            insertDataOption=INSERT_DATA_OPTION,
            body={"values": rows},
        )
        .execute()
    )

    updates = response.get("updates") if isinstance(response, dict) else None
    updated = updates.get("updatedRows") if isinstance(updates, dict) else None
    return AppendResult(sent=len(rows), updated=updated if isinstance(updated, int) else 0)


def ensure_header(
    service: Any,
    spreadsheet_id: str,
    sheet_name: str,
    columns: Sequence[str],
    *,
    create: bool = True,
) -> str:
    """1行目を確かめる。空なら見出しを書き、違えば**書かずに落とす**。

    **追記は見出しを見ない。** 人が列を並べ替えても名前を変えても、
    ``append`` は成功する——列の意味だけが全部ズレる（DESIGN 5-K）。
    ズレたことは、履歴を後から見ても分からない。

    :param create: ``False`` なら空でも見出しを書かず ``"空"`` を返す。
        **書かない指定でも、ズレの検査だけは通す**——書かないことは
        見逃してよい理由にならない。
    :returns: ``"一致"`` / ``"書いた"`` / ``"空"``
    :raises SheetError: 1行目が ``columns`` と完全一致でないとき
    """
    wanted = list(columns)
    header_range = f"{sheet_name}!A1:{column_letter(len(wanted))}1"
    rows = read_rows(service, spreadsheet_id, header_range)
    current = [str(cell) for cell in rows[0]] if rows else []

    if not current:
        if not create:
            # 「1行も書かない」指定のときは見出しも書かない。
            return "空"
        append_rows(service, spreadsheet_id, header_range, [wanted])
        return "書いた"
    if current == wanted:
        return "一致"

    # **何が入っているかを見せる。** 「不一致」だけでは直せない。
    raise SheetError(
        "シートの見出しが想定と違います。列がズレるので書き込みを中止しました。\n"
        f"  想定: {wanted}\n"
        f"  実際: {current}\n"
        "見出しを直すか、別のシートを指定してください。"
    )
