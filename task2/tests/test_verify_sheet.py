"""task2/verify_sheet のテスト。**実装より先に書いた。**

守らせる対象は `task2/DESIGN.md` の **5-R 〜 5-U**——照合層の穴。
4つとも「**照合が緑になったまま成立する**」形をしている。

============ ====================================================================
DESIGN       ここで守ること
============ ====================================================================
5-R          0件を「一致」と言わない。**比べた行数とセル数を必ず出す**
5-S          読み直しは**読み取り専用のスコープ**で別に認証する
5-T          照合器そのものを壊す検査に入れる（`tools/mutate.py` 側）
5-U          追記の前後で全体の行数を数え、**上書きされていないか**を見る
============ ====================================================================

**この検査ファイルが無いと、照合器は誰にも検査されない。**
課題1で実際に踏んだ形——「素通り0」が「守られている」ではなく
「そこを見ていない」を意味していた。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import transform  # noqa: E402
import verify_sheet  # noqa: E402


SHEET_ID = "1AbCdEfGhIjKlMnOpQrStUvWxYz0123456789ABCDEF"
SHEET_NAME = "シート1"
AT = "2026-09-09T21:00:00+09:00"
CODE = "shop:1"


class FakeExecutable:
    def __init__(self, result):
        self._result = result

    def execute(self):
        return self._result


class FakeValues:
    def __init__(self, rows):
        self._rows = rows
        self.get_calls: list[dict] = []

    def get(self, **kwargs):
        self.get_calls.append(kwargs)
        return FakeExecutable({"values": self._rows} if self._rows else {})


class FakeService:
    def __init__(self, rows):
        self._values = FakeValues(rows)

    def spreadsheets(self):
        return self

    def values(self):
        return self._values


def row(code=CODE, at=AT, price=12000, name="テスト商品"):
    return transform.to_row(
        {
            "itemCode": code,
            "itemName": name,
            "itemPrice": price,
            "pointRate": 1,
            "availability": 1,
        },
        at,
    )


def sheet(*data_rows):
    """見出し＋データ。**末尾の空セルは落として返す**（実物と同じ形）。"""
    header = list(transform.COLUMNS)
    return [header, *[list(r[: _last_filled(r)]) for r in data_rows]]


def _last_filled(values):
    """末尾の空セルを落とした長さ。Google の返し方に合わせる。"""
    length = len(values)
    while length > 0 and values[length - 1] == "":
        length -= 1
    return length


# ============================================================ 5-S 別経路


class Test読み取り専用で読む:
    def test_読み取り専用のスコープで認証する(self):
        """**書く側の資格情報を使い回さない**（5-S）。

        同じもので書いて読むと、「書けたつもり」を「書けた」と確かめる経路が
        1本になる。その1本が壊れたとき、確認も一緒に壊れる。
        """
        seen = {}
        verify_sheet.read_only_service(
            "service-account.json",
            loader=lambda path, scopes=None: seen.update(scopes=scopes) or "cred",
            builder=lambda *a, **k: "service",
        )
        assert seen["scopes"] == ["https://www.googleapis.com/auth/spreadsheets.readonly"]

    def test_書き込みスコープを渡さない(self):
        seen = {}
        verify_sheet.read_only_service(
            "service-account.json",
            loader=lambda path, scopes=None: seen.update(scopes=scopes) or "cred",
            builder=lambda *a, **k: "service",
        )
        assert "https://www.googleapis.com/auth/spreadsheets" not in seen["scopes"]


# ============================================================ 5-R 比べた数を出す


class Test照合:
    def test_一致すれば通る(self):
        written = row()
        result = verify_sheet.verify_appended(
            FakeService(sheet(written)), SHEET_ID, SHEET_NAME, [written]
        )
        assert result.ok is True

    def test_比べた行数を出す(self):
        written = row()
        result = verify_sheet.verify_appended(
            FakeService(sheet(written)), SHEET_ID, SHEET_NAME, [written]
        )
        assert result.compared_rows == 1

    def test_比べたセル数を出す(self):
        written = row()
        result = verify_sheet.verify_appended(
            FakeService(sheet(written)), SHEET_ID, SHEET_NAME, [written]
        )
        assert result.compared_cells == len(transform.COLUMNS)

    def test_0件は一致にしない(self):
        """**5-R。いちばん静かで、いちばん起きやすい。**

        0行と0行を比べれば必ず一致する。範囲の指定を1文字間違えるだけで
        こうなり、照合は「一致しました」と言う。
        """
        result = verify_sheet.verify_appended(
            FakeService(sheet()), SHEET_ID, SHEET_NAME, []
        )
        assert result.ok is False
        assert result.compared_rows == 0

    def test_0件のときは理由が出る(self):
        result = verify_sheet.verify_appended(
            FakeService(sheet()), SHEET_ID, SHEET_NAME, []
        )
        assert any("0" in note for note in result.notes)

    def test_報告に比べた数が必ず出る(self):
        # 「一致しました」だけでは、**比べていないのと見分けが付かない**。
        written = row()
        result = verify_sheet.verify_appended(
            FakeService(sheet(written)), SHEET_ID, SHEET_NAME, [written]
        )
        lines = verify_sheet.format_report(result)
        # **部分一致にしない。** 報告には「不一致 0 セル」という別の行もあるので、
        # 「セル」を含むかどうかでは、比べた数が消えても気づけない
        # （2026-09-09 のミューテーションで実際に素通りした）。
        assert lines[0] == f"照合           1 行 × {len(transform.COLUMNS)} セルを比べました"


class Test比べていないなら通さない:
    """`ok` は**型の約束**。どの経路から作られても、比べていなければ通さない。

    `verify_appended` の中では「0件」に必ず理由が付くので、経路をたどる
    テストだけでは `compared_rows` の判定が消えても気づけない
    （2026-09-09 のミューテーションで実際に素通りした）。
    """

    def test_0行なら通さない(self):
        result = verify_sheet.VerifyResult(
            compared_rows=0, compared_cells=0, mismatches=[], rows_after=1
        )
        assert result.ok is False

    def test_1行でも比べていれば通る(self):
        result = verify_sheet.VerifyResult(
            compared_rows=1, compared_cells=15, mismatches=[], rows_after=2
        )
        assert result.ok is True


class Test不一致:
    def test_値が違えば見つかる(self):
        written = row(price=12000)
        stored = row(price=9999)
        result = verify_sheet.verify_appended(
            FakeService(sheet(stored)), SHEET_ID, SHEET_NAME, [written]
        )
        assert result.ok is False and result.mismatches

    def test_不一致に列名が出る(self):
        written = row(price=12000)
        stored = row(price=9999)
        result = verify_sheet.verify_appended(
            FakeService(sheet(stored)), SHEET_ID, SHEET_NAME, [written]
        )
        assert any(m.column == "本体価格" for m in result.mismatches)

    def test_不一致に行番号が出る(self):
        # 直すために「シートの何行目か」が要る。**見出しを1行目として数える。**
        written = row(price=12000)
        result = verify_sheet.verify_appended(
            FakeService(sheet(row(price=9999))), SHEET_ID, SHEET_NAME, [written]
        )
        assert result.mismatches[0].row == 2

    def test_型が変わったら見つかる(self):
        """**5-N が実際に起きたときに気づくのはここ。**

        `FORMATTED_VALUE` で読むと数値が文字列で返る。値は同じに見えるが、
        `diff` は int しか価格と認めないので、比較が全部できなくなる。
        """
        written = row(price=12000)
        stored = list(written)
        stored[transform.COLUMNS.index("本体価格")] = "12000"
        result = verify_sheet.verify_appended(
            FakeService([list(transform.COLUMNS), stored]), SHEET_ID, SHEET_NAME, [written]
        )
        assert result.ok is False

    def test_末尾が落ちていても一致とみなす(self):
        """空の末尾セルは返ってこない（5-Q）。**それは不一致ではない。**

        ここを不一致と数えると、正常な回のたびに照合が赤くなり、
        **本物の不一致が埋もれる**。
        """
        written = row()
        result = verify_sheet.verify_appended(
            FakeService(sheet(written)), SHEET_ID, SHEET_NAME, [written]
        )
        assert result.mismatches == []

    def test_行が足りなければ失敗(self):
        result = verify_sheet.verify_appended(
            FakeService(sheet(row())), SHEET_ID, SHEET_NAME, [row(), row()]
        )
        assert result.ok is False
        # **見出しをデータとして数えていない証拠。** 数えると行数が足りてしまい、
        # 見出しと商品行を突き合わせて「不一致」として報告する
        # ——原因が「足りない」ことだと分からなくなる。
        assert any("足りません" in note for note in result.notes)
        assert result.mismatches == []

    def test_最後に書いた行だけを見る(self):
        # 履歴には過去の行が積まれている。**今回の分だけを照合する。**
        old = row(at="2026-09-01T21:00:00+09:00", price=5000)
        written = row()
        result = verify_sheet.verify_appended(
            FakeService(sheet(old, written)), SHEET_ID, SHEET_NAME, [written]
        )
        assert result.ok is True and result.compared_rows == 1


# ============================================================ 5-U 上書きの検出


class Test行数の帳尻:
    def test_前後の数が合えば通る(self):
        written = row()
        result = verify_sheet.verify_appended(
            FakeService(sheet(row(), written)), SHEET_ID, SHEET_NAME, [written], rows_before=2
        )
        assert result.ok is True

    def test_合わなければ失敗(self):
        """**追記が既存の行を上書きしていないか**（5-U）。

        上書きされた古い行は、**追記した行の検査を全部通る**。
        今回の行だけ見ていても永久に気づけない。
        """
        written = row()
        result = verify_sheet.verify_appended(
            FakeService(sheet(written)), SHEET_ID, SHEET_NAME, [written], rows_before=5
        )
        assert result.ok is False

    def test_合わないときは理由が出る(self):
        result = verify_sheet.verify_appended(
            FakeService(sheet(row())), SHEET_ID, SHEET_NAME, [row()], rows_before=5
        )
        assert any("行数" in note for note in result.notes)

    def test_前の数を知らなければ確かめない(self):
        # **知らないことを、知っているふりで通さない。** 単独で走らせたときは
        # 帳尻を確かめられないので、確かめたとも言わない。
        written = row()
        result = verify_sheet.verify_appended(
            FakeService(sheet(written)), SHEET_ID, SHEET_NAME, [written]
        )
        assert result.rows_before is None and result.ok is True


# ============================================================ シート全体の点検


class Test点検:
    def test_見出しが違えば落ちる(self):
        service = FakeService([["日付", "商品"], row()])
        assert verify_sheet.audit(service, SHEET_ID, SHEET_NAME).ok is False

    def test_行が1行も無ければ通さない(self):
        # **0件を「異常なし」と言わない**（5-R）。
        assert verify_sheet.audit(FakeService([]), SHEET_ID, SHEET_NAME).ok is False

    def test_見出しだけでも通さない(self):
        service = FakeService([list(transform.COLUMNS)])
        assert verify_sheet.audit(service, SHEET_ID, SHEET_NAME).ok is False

    def test_点検した行数を出す(self):
        service = FakeService(sheet(row(), row()))
        assert verify_sheet.audit(service, SHEET_ID, SHEET_NAME).checked_rows == 2

    def test_時刻が読めない行を数える(self):
        broken = row(at="2026-09-09 21:00")
        service = FakeService(sheet(row(), broken))
        result = verify_sheet.audit(service, SHEET_ID, SHEET_NAME)
        assert result.bad_time == 1 and result.ok is False

    def test_状態が知らない語の行を数える(self):
        odd = row()
        odd[transform.COLUMNS.index("状態")] = "たぶん取得"
        service = FakeService(sheet(odd))
        assert verify_sheet.audit(service, SHEET_ID, SHEET_NAME).bad_status == 1

    def test_取得なのに価格が数値でない行を数える(self):
        """**`FORMATTED_VALUE` で読んでしまった日に、ここが鳴る。**"""
        odd = row()
        odd[transform.COLUMNS.index("本体価格")] = "12,000"
        service = FakeService(sheet(odd))
        assert verify_sheet.audit(service, SHEET_ID, SHEET_NAME).bad_price == 1

    def test_失敗行の空の価格は数えない(self):
        # 失敗行は価格が空なのが正しい。ここを数えると毎回鳴る。
        failed = transform.failure_row(CODE, AT, "見つからない")
        service = FakeService(sheet(failed))
        assert verify_sheet.audit(service, SHEET_ID, SHEET_NAME).bad_price == 0

    def test_末尾が落ちた行を数える(self):
        """**この数は「異常」ではなく「実際に起きていること」の記録**（5-Q）。

        取得できた行は末尾（`理由`）が空なので、Google はその列を返さない。
        補ってから数えると**必ず0**になり、鳴らない検査になる
        ——2026-09-09 に実物で「列が短い 0 行」と出て気づいた。
        """
        service = FakeService(sheet(row(), row()))
        assert verify_sheet.audit(service, SHEET_ID, SHEET_NAME).short_rows == 2

    def test_満杯の行は数えない(self):
        # 失敗行は末尾の `理由` が埋まるので、落ちずに返る。
        failed = transform.failure_row(CODE, AT, "見つからない")
        service = FakeService(sheet(failed))
        assert verify_sheet.audit(service, SHEET_ID, SHEET_NAME).short_rows == 0

    def test_短い行があっても異常にはしない(self):
        # **正常な回のたびに起きる。** 異常にすると本物の異常が埋もれる。
        service = FakeService(sheet(row()))
        assert verify_sheet.audit(service, SHEET_ID, SHEET_NAME).ok is True

    def test_異常が無ければ通る(self):
        service = FakeService(sheet(row(), row(price=900)))
        assert verify_sheet.audit(FakeService(sheet(row())), SHEET_ID, SHEET_NAME).ok is True
        assert verify_sheet.audit(service, SHEET_ID, SHEET_NAME).ok is True

    def test_点検の報告に件数が出る(self):
        service = FakeService(sheet(row()))
        text = "\n".join(verify_sheet.format_audit(verify_sheet.audit(service, SHEET_ID, SHEET_NAME)))
        assert "1" in text


class Test読み取りの範囲:
    def test_列数から範囲を作る(self):
        service = FakeService(sheet(row()))
        verify_sheet.audit(service, SHEET_ID, SHEET_NAME)
        expected = f"{SHEET_NAME}!A:{chr(ord('A') + len(transform.COLUMNS) - 1)}"
        assert service.values().get_calls[0]["range"] == expected

    def test_型を変えずに読む(self):
        # 照合する側が FORMATTED_VALUE で読んだら、**書き込み側の異常を
        # 照合側の異常が打ち消してしまう**。
        service = FakeService(sheet(row()))
        verify_sheet.audit(service, SHEET_ID, SHEET_NAME)
        assert service.values().get_calls[0]["valueRenderOption"] == "UNFORMATTED_VALUE"


class TestCLI:
    def test_シート名を指定できる(self):
        assert verify_sheet.build_parser().parse_args(["--sheet-name", "x"]).sheet_name == "x"

    def test_envの場所を指定できる(self):
        assert verify_sheet.build_parser().parse_args(["--env", "y"]).env == "y"
