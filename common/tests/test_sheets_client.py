"""common/sheets_client のテスト。**実装より先に書いた。**

Google には1回も繋がない。`service` を差し替えるので、
「どの引数で呼んだか」まで検査できる。

守らせる対象は `task2/DESIGN.md` の **5-K 〜 5-N**——
書き込み層の穴で、**公式ドキュメントを読まないと1つも出てこない**もの。

============ ====================================================================
DESIGN       ここで守ること
============ ====================================================================
5-K          見出しが違うシートには**書かない**
5-L          `valueInputOption=RAW`。文字列を数値・日付・数式に変換させない
5-M          `insertDataOption=INSERT_ROWS`。既存データを上書きしない
5-N          `valueRenderOption=UNFORMATTED_VALUE`。**読み戻しで型を変えない**
5-A          送った行数と `updates.updatedRows` を突き合わせる
============ ====================================================================
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from common import sheets_client  # noqa: E402


SHEET_ID = "1AbCdEfGhIjKlMnOpQrStUvWxYz0123456789ABCDEF"
COLUMNS = ("取得時刻", "itemCode", "商品名", "価格")


# ============================================================ 偽物


class FakeExecutable:
    def __init__(self, result=None, error=None):
        self._result = result
        self._error = error

    def execute(self):
        if self._error is not None:
            raise self._error
        return self._result if self._result is not None else {}


class FakeValues:
    """`service.spreadsheets().values()` の形だけを持つ。

    **`**kwargs` で受けて記録する**ので、引数名を1文字でも間違えれば
    検査で分かる（本物は知らない引数名で TypeError を出す）。
    """

    def __init__(self, get_result=None, append_result=None):
        self._get_result = get_result if get_result is not None else {}
        self._append_result = append_result if append_result is not None else {}
        self.get_calls: list[dict] = []
        self.append_calls: list[dict] = []

    def get(self, **kwargs):
        self.get_calls.append(kwargs)
        return FakeExecutable(self._get_result)

    def append(self, **kwargs):
        self.append_calls.append(kwargs)
        return FakeExecutable(self._append_result)


class FakeService:
    def __init__(self, values):
        self._values = values

    def spreadsheets(self):
        return self

    def values(self):
        return self._values


def service_with(**kwargs):
    values = FakeValues(**kwargs)
    return FakeService(values), values


def appended(rows):
    """`append` の正常応答（公式の `AppendValuesResponse` の形）。"""
    return {
        "spreadsheetId": SHEET_ID,
        "tableRange": "シート1!A1:D1",
        "updates": {"updatedRows": rows, "updatedColumns": 4, "updatedCells": rows * 4},
    }


# ============================================================ 5-N 読み戻し


class Test読む:
    def test_型を変えずに読む(self):
        """**ここを外すと、値下がり通知が永久に鳴らない。**

        `values.get` の既定は `FORMATTED_VALUE` で、公式の例では数値 `1.23` が
        `"$1.23"` という**文字列**で返る。`diff` は int しか価格と認めないので、
        全比較が「価格が無い」になる。**エラーは1つも出ない。**
        """
        service, values = service_with(get_result={"values": []})
        sheets_client.read_rows(service, SHEET_ID, "シート1!A:O")
        assert values.get_calls[0]["valueRenderOption"] == "UNFORMATTED_VALUE"

    def test_シートIDと範囲を渡す(self):
        service, values = service_with(get_result={"values": []})
        sheets_client.read_rows(service, SHEET_ID, "シート1!A:O")
        assert values.get_calls[0]["spreadsheetId"] == SHEET_ID
        assert values.get_calls[0]["range"] == "シート1!A:O"

    def test_行が返る(self):
        service, _ = service_with(get_result={"values": [["a", 1], ["b", 2]]})
        assert sheets_client.read_rows(service, SHEET_ID, "x") == [["a", 1], ["b", 2]]

    def test_末尾の空セルを補って返す(self):
        """**書いた行と読み戻す行は、列数が違う。**

        公式にこう書いてある（2026-09-08 確認）:

        > For output, empty trailing rows and columns will not be included.

        末尾が空の列（この課題では `理由`）は落ちて返る。**実測で気づいた**——
        書き込みは成功していて、読み戻して初めて分かる。
        """
        service, _ = service_with(get_result={"values": [["a", "b"], ["c"]]})
        rows = sheets_client.read_rows(service, SHEET_ID, "x", width=4)
        assert rows == [["a", "b", "", ""], ["c", "", "", ""]]

    def test_幅を言わなければ補わない(self):
        # 何列ぶんが正しいのかは、読む側にしか分からない。
        service, _ = service_with(get_result={"values": [["a"]]})
        assert sheets_client.read_rows(service, SHEET_ID, "x") == [["a"]]

    def test_長い行は切らない(self):
        """**人が列を足していることがある。**

        切ると、その事実がここで消える——`ensure_header` が弾くべき異常を、
        読む側が黙って隠してしまう。
        """
        service, _ = service_with(get_result={"values": [["a", "b", "c", "d", "e"]]})
        rows = sheets_client.read_rows(service, SHEET_ID, "x", width=4)
        assert rows == [["a", "b", "c", "d", "e"]]

    def test_空のシートは空のリストになる(self):
        # **`values` キーごと来ない。** None を返すと呼ぶ側が落ちる。
        service, _ = service_with(get_result={})
        assert sheets_client.read_rows(service, SHEET_ID, "x") == []


# ============================================================ 5-L / 5-M 書き方


class Test追記:
    def test_解釈させない(self):
        """`USER_ENTERED` は文字列を数値・日付・数式に変える（公式原文）。

        商品名が `=` で始まれば数式になり、取得時刻は日付に化ける。
        **書き込みは成功する。壊れるのは読み戻す日。**
        """
        service, values = service_with(append_result=appended(1))
        sheets_client.append_rows(service, SHEET_ID, "シート1!A:D", [["a", "b", "c", 1]])
        assert values.append_calls[0]["valueInputOption"] == "RAW"

    def test_既存データを上書きしない(self):
        # 既定の OVERWRITE は「書き込む領域の既存データを上書きする」（公式原文）。
        service, values = service_with(append_result=appended(1))
        sheets_client.append_rows(service, SHEET_ID, "シート1!A:D", [["a", "b", "c", 1]])
        assert values.append_calls[0]["insertDataOption"] == "INSERT_ROWS"

    def test_本文の形(self):
        service, values = service_with(append_result=appended(1))
        rows = [["a", "b", "c", 1]]
        sheets_client.append_rows(service, SHEET_ID, "シート1!A:D", rows)
        assert values.append_calls[0]["body"] == {"values": rows}

    def test_送った行数が残る(self):
        service, _ = service_with(append_result=appended(2))
        result = sheets_client.append_rows(
            service, SHEET_ID, "x", [["a", "b", "c", 1], ["d", "e", "f", 2]]
        )
        assert result.sent == 2

    def test_書けた行数を返り値から取る(self):
        service, _ = service_with(append_result=appended(2))
        result = sheets_client.append_rows(
            service, SHEET_ID, "x", [["a", "b", "c", 1], ["d", "e", "f", 2]]
        )
        assert result.updated == 2 and result.ok is True

    def test_部分的にしか書けていないと分かる(self):
        """**5-A。** 成功コードだけで信じない。

        2行送って1行しか入っていないとき、例外は出ない。
        """
        service, _ = service_with(append_result=appended(1))
        result = sheets_client.append_rows(
            service, SHEET_ID, "x", [["a", "b", "c", 1], ["d", "e", "f", 2]]
        )
        assert result.ok is False

    def test_更新情報が無い応答でも落ちない(self):
        service, _ = service_with(append_result={"spreadsheetId": SHEET_ID})
        result = sheets_client.append_rows(service, SHEET_ID, "x", [["a", "b", "c", 1]])
        assert result.updated == 0 and result.ok is False

    def test_0行なら呼ばない(self):
        # 空の書き込みは相手にとって意味が無い。**QPS と課金だけ減る。**
        service, values = service_with()
        result = sheets_client.append_rows(service, SHEET_ID, "x", [])
        assert values.append_calls == []
        assert result.sent == 0 and result.updated == 0

    def test_長さがそろわない行は送らない(self):
        # **送ってから気づけない。** シートは受け取ってしまう。
        service, values = service_with(append_result=appended(2))
        with pytest.raises(sheets_client.SheetError):
            sheets_client.append_rows(service, SHEET_ID, "x", [["a", "b"], ["c"]])
        assert values.append_calls == []

    def test_列数が想定と違えば送らない(self):
        service, values = service_with(append_result=appended(1))
        with pytest.raises(sheets_client.SheetError):
            sheets_client.append_rows(
                service, SHEET_ID, "x", [["a", "b"]], expected_width=4
            )
        assert values.append_calls == []


# ============================================================ 5-K 見出し


class Test見出し:
    def test_一致していれば書かない(self):
        service, values = service_with(get_result={"values": [list(COLUMNS)]})
        state = sheets_client.ensure_header(service, SHEET_ID, "シート1", COLUMNS)
        assert state == "一致"
        assert values.append_calls == []

    def test_空なら見出しを書く(self):
        service, values = service_with(get_result={}, append_result=appended(1))
        state = sheets_client.ensure_header(service, SHEET_ID, "シート1", COLUMNS)
        assert state == "書いた"
        assert values.append_calls[0]["body"] == {"values": [list(COLUMNS)]}

    def test_違う見出しには書かない(self):
        """**追記は成功してしまう。** 列の意味だけが全部ズレる。

        人はシートの列を並べ替えられるし、名前も変えられる。
        """
        service, values = service_with(get_result={"values": [["日付", "商品", "値段", "x"]]})
        with pytest.raises(sheets_client.SheetError):
            sheets_client.ensure_header(service, SHEET_ID, "シート1", COLUMNS)
        assert values.append_calls == []

    def test_並べ替えられていても弾く(self):
        reordered = [COLUMNS[1], COLUMNS[0], COLUMNS[2], COLUMNS[3]]
        service, _ = service_with(get_result={"values": [reordered]})
        with pytest.raises(sheets_client.SheetError):
            sheets_client.ensure_header(service, SHEET_ID, "シート1", COLUMNS)

    def test_列が1つ多くても弾く(self):
        service, _ = service_with(get_result={"values": [list(COLUMNS) + ["メモ"]]})
        with pytest.raises(sheets_client.SheetError):
            sheets_client.ensure_header(service, SHEET_ID, "シート1", COLUMNS)

    def test_作らない指定なら空でも書かない(self):
        # `--dry-run` は「1行も書かない」。見出しも書かない。
        service, values = service_with(get_result={})
        state = sheets_client.ensure_header(service, SHEET_ID, "シート1", COLUMNS, create=False)
        assert state == "空"
        assert values.append_calls == []

    def test_作らない指定でも不一致は弾く(self):
        # 書かないからといって、ズレを見逃す理由にはならない。
        service, _ = service_with(get_result={"values": [["日付", "商品", "値段", "x"]]})
        with pytest.raises(sheets_client.SheetError):
            sheets_client.ensure_header(service, SHEET_ID, "シート1", COLUMNS, create=False)

    def test_見出しの範囲は列数から作る(self):
        service, values = service_with(get_result={"values": [list(COLUMNS)]})
        sheets_client.ensure_header(service, SHEET_ID, "シート1", COLUMNS)
        assert values.get_calls[0]["range"] == "シート1!A1:D1"

    @pytest.mark.parametrize("width,last", [(5, "E"), (15, "O"), (27, "AA")])
    def test_列数が変われば範囲も変わる(self, width, last):
        """**列数を固定文字列で書くと、列を足した日に古い範囲を読む。**

        4列ぶんだけ読んで「一致した」と判定し、5列目のズレを見逃す。
        1つの列数でしか試さないと、この間違いは検査を素通りする
        （2026-09-08 のミューテーションで実際に素通りした）。
        """
        columns = tuple(f"列{i}" for i in range(1, width + 1))
        service, values = service_with(get_result={"values": [list(columns)]})
        sheets_client.ensure_header(service, SHEET_ID, "シート1", columns)
        assert values.get_calls[0]["range"] == f"シート1!A1:{last}1"

    def test_エラーに実際の見出しが載る(self):
        # 直すために**何が入っているか**が要る。「不一致」だけでは動けない。
        service, _ = service_with(get_result={"values": [["日付", "商品", "値段", "x"]]})
        with pytest.raises(sheets_client.SheetError) as caught:
            sheets_client.ensure_header(service, SHEET_ID, "シート1", COLUMNS)
        assert "日付" in str(caught.value)


class Test列記号:
    @pytest.mark.parametrize("number,letter", [
        (1, "A"), (4, "D"), (15, "O"), (26, "Z"), (27, "AA"), (52, "AZ"), (53, "BA"),
    ])
    def test_列番号から記号を作る(self, number, letter):
        assert sheets_client.column_letter(number) == letter

    @pytest.mark.parametrize("bad", [0, -1])
    def test_0以下は作れない(self, bad):
        with pytest.raises(ValueError):
            sheets_client.column_letter(bad)


# ============================================================ 資格情報


class Testサービスの組み立て:
    def test_SheetsのV4を組む(self):
        seen = {}

        def builder(name, version, **kwargs):
            seen.update({"name": name, "version": version, **kwargs})
            return "service"

        sheets_client.build_service("cred", builder=builder)
        assert seen["name"] == "sheets" and seen["version"] == "v4"
        assert seen["credentials"] == "cred"

    def test_ディスカバリのキャッシュを使わない(self):
        # 既定の True は oauth2client の file_cache を探して警告を出す。
        # **その警告は実行画面のスクリーンショットに写る。**
        seen = {}
        sheets_client.build_service(
            "cred", builder=lambda n, v, **kw: seen.update(kw) or "service"
        )
        assert seen["cache_discovery"] is False


class Test資格情報:
    def test_鍵ファイルが無ければ理由が出る(self, tmp_path):
        missing = tmp_path / "service-account.json"
        with pytest.raises(sheets_client.SheetError) as caught:
            sheets_client.load_credentials(missing, [sheets_client.SCOPE_WRITE])
        assert "service-account.json" in str(caught.value)

    def test_スコープが空なら拒む(self, tmp_path):
        key = tmp_path / "service-account.json"
        key.write_text("{}", encoding="utf-8")
        with pytest.raises(sheets_client.SheetError):
            sheets_client.load_credentials(key, [], loader=lambda *a, **k: "cred")

    def test_スコープをそのまま渡す(self, tmp_path):
        # **既定値を持たせない。** 読み取り専用で書こうとすると、
        # 認証は通るのに書き込みだけ落ちる。
        key = tmp_path / "service-account.json"
        key.write_text("{}", encoding="utf-8")
        seen = {}

        def loader(path, scopes=None):
            seen["path"], seen["scopes"] = path, scopes
            return "cred"

        sheets_client.load_credentials(key, [sheets_client.SCOPE_WRITE], loader=loader)
        assert seen["scopes"] == [sheets_client.SCOPE_WRITE]

    def test_共有先のアドレスを取り出せる(self, tmp_path):
        # シートの「共有」に貼る宛先。403 のときに案内するために使う。
        key = tmp_path / "service-account.json"
        key.write_text(
            json.dumps({"client_email": "sheet-writer@example.iam.gserviceaccount.com",
                        "private_key": "-----BEGIN PRIVATE KEY-----\nSECRET\n"}),
            encoding="utf-8",
        )
        assert sheets_client.read_client_email(key).endswith(".iam.gserviceaccount.com")

    def test_秘密鍵は取り出さない(self):
        """**この関数の出力は画面に出る＝記事のスクショに載る。**"""
        source = Path(sheets_client.__file__).read_text(encoding="utf-8")
        assert "private_key" not in source

    def test_読めないファイルでも落ちない(self, tmp_path):
        broken = tmp_path / "service-account.json"
        broken.write_text("これは JSON ではない", encoding="utf-8")
        assert sheets_client.read_client_email(broken) == ""

    def test_ファイルが無ければ空になる(self, tmp_path):
        assert sheets_client.read_client_email(tmp_path / "no.json") == ""
