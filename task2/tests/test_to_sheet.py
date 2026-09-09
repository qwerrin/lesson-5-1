"""task2/to_sheet のテスト。**実装より先に書いた。**

楽天にも Google にも1回も繋がない。`http` と `service` を差し替える。

守らせる対象は `task2/DESIGN.md` の **5-C / 5-O / 5-P** と、
「**取れなかったものも1行にする**」（5-B）が最後まで通っているかどうか。

============ ====================================================================
DESIGN       ここで守ること
============ ====================================================================
5-B          失敗した商品も**シートに送る行になる**（層をまたいで消えない）
5-C          watchlist の件数 = 送った行数 = 入った行数 を突き合わせる
5-O          watchlist の重複は落とし、**落とした件数を報告する**
5-P          空の watchlist は**エラー**。0件を正常終了にしない
============ ====================================================================
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import to_sheet  # noqa: E402
import transform  # noqa: E402


SHEET_ID = "1AbCdEfGhIjKlMnOpQrStUvWxYz0123456789ABCDEF"
SHEET_NAME = "シート1"
AT = "2026-09-08T21:00:00+09:00"
YESTERDAY = "2026-09-07T21:00:00+09:00"
CODE_A = "shop-a:1"
CODE_B = "shop-b:2"


# ============================================================ 偽物


class FakeResponse:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload
        self.headers: dict[str, str] = {}

    def json(self):
        return self._payload


class FakeHttp:
    def __init__(self, by_code):
        """`itemCode` ごとに返す応答を決める。順番に依存しない偽物にする。"""
        self._by_code = by_code
        self.calls: list[dict] = []

    def get(self, url, params=None, timeout=None, **kwargs):
        self.calls.append({"url": url, "params": params, "timeout": timeout})
        return self._by_code[params["itemCode"]]


class FakeExecutable:
    def __init__(self, result):
        self._result = result

    def execute(self):
        return self._result


class FakeValues:
    def __init__(self, header, history, updated=None, corrupt=False):
        self._header = header
        self._history = history
        self._updated = updated
        #: **書き込みは成功を返すのに、着地した中身が違う**状況を作る。
        #: 相手が「書いた」と言うことと、そのとおりに入っていることは別。
        self._corrupt = corrupt
        self.get_calls: list[dict] = []
        self.append_calls: list[dict] = []

    def get(self, **kwargs):
        self.get_calls.append(kwargs)
        rows = self._header if "A1:" in kwargs["range"] else self._history
        return FakeExecutable({"values": rows} if rows else {})

    def append(self, **kwargs):
        self.append_calls.append(kwargs)
        sent = len(kwargs["body"]["values"])
        updated = sent if self._updated is None else self._updated
        # **書いたものが読めるようにする。** 読み直しの検査に要る——
        # 書いても読めない偽物だと、照合が「行が足りない」で必ず落ちる。
        stored = [list(r) for r in kwargs["body"]["values"]]
        if self._corrupt:
            for r in stored:
                r[transform.COLUMNS.index("商品名")] = "別の商品に化けた"
        self._history.extend(stored)
        return FakeExecutable({"updates": {"updatedRows": updated}})


class FakeService:
    def __init__(self, values):
        self._values = values

    def spreadsheets(self):
        return self

    def values(self):
        return self._values


def ok_response(code, price=12000):
    return FakeResponse(
        200,
        {
            "count": 1,
            "Items": [
                {
                    "Item": {
                        "itemCode": code,
                        "itemName": "テスト商品",
                        "itemPrice": price,
                        "pointRate": 1,
                        "availability": 1,
                    }
                }
            ],
        },
    )


def empty_response():
    """**実在ショップ＋存在しない商品番号**（実測）。エラーは出ない。"""
    return FakeResponse(200, {"count": 0, "Items": []})


def history_row(code, at=YESTERDAY, price=12000):
    return transform.to_row(
        {
            "itemCode": code,
            "itemName": "テスト商品",
            "itemPrice": price,
            "pointRate": 1,
            "availability": 1,
        },
        at,
    )


def run(http, values, codes=(CODE_A,), **kwargs):
    kwargs.setdefault("fetched_at", AT)
    return to_sheet.run(
        http=http,
        service=FakeService(values),
        item_codes=list(codes),
        application_id="app",
        access_key="key",
        spreadsheet_id=SHEET_ID,
        sheet_name=SHEET_NAME,
        min_interval=0.0,
        sleep=lambda _s: None,
        monotonic=lambda: 0.0,
        **kwargs,
    )


def values_with(history=(), header=None, updated=None, corrupt=False):
    header_rows = [list(transform.COLUMNS)] if header is None else header
    return FakeValues(
        header_rows, [list(transform.COLUMNS), *history], updated=updated, corrupt=corrupt
    )


# ============================================================ 5-O / 5-P watchlist


class Testwatchlist:
    def write(self, tmp_path, payload):
        path = tmp_path / "watchlist.json"
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return path

    def test_商品コードが読める(self, tmp_path):
        path = self.write(tmp_path, {"items": [{"itemCode": CODE_A, "memo": "コーヒー"}]})
        assert to_sheet.load_watchlist(path).codes == [CODE_A]

    def test_重複は落とす(self, tmp_path):
        # 残すと**その商品だけ2行入り**、件数の突き合わせが狂う。API も2回叩く。
        path = self.write(tmp_path, {"items": [{"itemCode": CODE_A}, {"itemCode": CODE_A}]})
        assert to_sheet.load_watchlist(path).codes == [CODE_A]

    def test_落とした重複を報告する(self, tmp_path):
        # **黙って落とさない。** 「入れたのに行が無い」と混同される。
        path = self.write(tmp_path, {"items": [{"itemCode": CODE_A}, {"itemCode": CODE_A}]})
        assert to_sheet.load_watchlist(path).duplicates == [CODE_A]

    def test_並び順は保つ(self, tmp_path):
        path = self.write(tmp_path, {"items": [{"itemCode": CODE_B}, {"itemCode": CODE_A}]})
        assert to_sheet.load_watchlist(path).codes == [CODE_B, CODE_A]

    def test_空ならエラー(self, tmp_path):
        """**0件を正常終了にしない**（5-P）。

        何も書かずに成功で終わると、「今日は動いた」と読める。
        """
        path = self.write(tmp_path, {"items": []})
        with pytest.raises(to_sheet.ToSheetError):
            to_sheet.load_watchlist(path)

    def test_ファイルが無ければエラー(self, tmp_path):
        with pytest.raises(to_sheet.ToSheetError):
            to_sheet.load_watchlist(tmp_path / "no.json")

    def test_JSONが壊れていればエラー(self, tmp_path):
        path = tmp_path / "watchlist.json"
        path.write_text("{壊れている", encoding="utf-8")
        with pytest.raises(to_sheet.ToSheetError):
            to_sheet.load_watchlist(path)

    def test_itemCodeが無い項目はエラー(self, tmp_path):
        # **黙って飛ばさない。** 追っているつもりの商品が消える。
        path = self.write(tmp_path, {"items": [{"memo": "コーヒー"}]})
        with pytest.raises(to_sheet.ToSheetError):
            to_sheet.load_watchlist(path)

    def test_エラーにファイル名が載る(self, tmp_path):
        with pytest.raises(to_sheet.ToSheetError) as caught:
            to_sheet.load_watchlist(tmp_path / "watchlist.json")
        assert "watchlist.json" in str(caught.value)


# ============================================================ 5-B / 5-C 層をまたぐ


class Test取得から書き込みまで:
    def test_取れた商品が1行になる(self):
        values = values_with()
        result = run(FakeHttp({CODE_A: ok_response(CODE_A)}), values)
        assert result.sent == 1 and result.written == 1

    def test_取れなかった商品も1行になる(self):
        """**5-B が層をまたいで生きているか。**

        `fetch_items` が失敗を結果として返しても、ここで捨てたら同じこと。
        """
        values = values_with()
        result = run(FakeHttp({CODE_A: empty_response()}), values)
        assert result.sent == 1
        row = values.append_calls[0]["body"]["values"][0]
        assert row[transform.COLUMNS.index("状態")] == "失敗"
        assert row[transform.COLUMNS.index("理由")] == "見つからない"

    def test_失敗と成功が混ざっても件数が合う(self):
        values = values_with()
        http = FakeHttp({CODE_A: ok_response(CODE_A), CODE_B: empty_response()})
        result = run(http, values, codes=(CODE_A, CODE_B))
        assert result.requested == 2 and result.sent == 2
        assert result.fetched_ok == 1 and result.fetched_failed == 1

    def test_送った行数と入った行数を突き合わせる(self):
        # **成功コードだけで信じない**（5-A）。2行送って1行しか入らないことがある。
        values = values_with(updated=1)
        http = FakeHttp({CODE_A: ok_response(CODE_A), CODE_B: ok_response(CODE_B)})
        result = run(http, values, codes=(CODE_A, CODE_B))
        assert result.sent == 2 and result.written == 1
        assert result.ok is False

    def test_列数を指定して送る(self, monkeypatch):
        """ズレた行はシートが受け取ってしまう。**送る前に弾く指定を渡す。**

        行の長さを見るだけでは足りない——`transform` が作る行はいつも揃うので、
        **指定を外しても検査が緑のまま**だった（2026-09-08 のミューテーションで素通り）。
        「どの引数で呼んだか」まで見る。
        """
        seen = {}
        real = to_sheet.sheets_client.append_rows

        def spy(*args, **kwargs):
            seen.update(kwargs)
            return real(*args, **kwargs)

        monkeypatch.setattr(to_sheet.sheets_client, "append_rows", spy)
        values = values_with()
        run(FakeHttp({CODE_A: ok_response(CODE_A)}), values)
        assert seen.get("expected_width") == len(transform.COLUMNS)

    def test_行の長さが列名とそろっている(self):
        values = values_with()
        run(FakeHttp({CODE_A: ok_response(CODE_A)}), values)
        assert len(values.append_calls[0]["body"]["values"][0]) == len(transform.COLUMNS)

    def test_落とした重複が結果まで届く(self):
        """**読み込みで報告しても、結果に載せなければ画面に出ない。**

        `load_watchlist` の検査だけでは、途中で捨てても気づけなかった
        （2026-09-08 のミューテーションで素通り）。
        """
        values = values_with()
        result = run(FakeHttp({CODE_A: ok_response(CODE_A)}), values, duplicates=[CODE_B])
        assert result.duplicates == [CODE_B]
        assert any("重複" in line for line in to_sheet.format_report(result))

    def test_全行の長さがそろっている(self):
        values = values_with()
        http = FakeHttp({CODE_A: ok_response(CODE_A), CODE_B: empty_response()})
        run(http, values, codes=(CODE_A, CODE_B))
        widths = {len(r) for r in values.append_calls[0]["body"]["values"]}
        assert widths == {len(transform.COLUMNS)}


class Test見出し:
    def test_見出しが違えば1行も書かない(self):
        # **列の意味が全部ズレる。追記は成功してしまう**（5-K）。
        values = values_with(header=[["日付", "商品", "値段"]])
        with pytest.raises(Exception):
            run(FakeHttp({CODE_A: ok_response(CODE_A)}), values)
        assert values.append_calls == []

    def test_見出しが違えば楽天にも投げない(self):
        """**書けないと分かっているのに取りに行かない。**

        QPS を捨てるだけで、成果は1行も残らない。
        """
        values = values_with(header=[["日付", "商品", "値段"]])
        http = FakeHttp({CODE_A: ok_response(CODE_A)})
        with pytest.raises(Exception):
            run(http, values)
        assert http.calls == []


# ============================================================ 値下がり


class Test値下がり:
    def test_前日より安ければ拾う(self):
        values = values_with(history=[history_row(CODE_A, price=12000)])
        result = run(FakeHttp({CODE_A: ok_response(CODE_A, price=11000)}), values)
        assert len(result.drops) == 1

    def test_値上がりは拾わない(self):
        values = values_with(history=[history_row(CODE_A, price=12000)])
        result = run(FakeHttp({CODE_A: ok_response(CODE_A, price=13000)}), values)
        assert result.drops == []

    def test_初回は拾わない(self):
        values = values_with()
        result = run(FakeHttp({CODE_A: ok_response(CODE_A)}), values)
        assert result.drops == []

    def test_比較できなかった理由が数えられる(self):
        # **「変化なし」と「比較できない」を混ぜない**——数で見えるようにする。
        values = values_with()
        result = run(FakeHttp({CODE_A: ok_response(CODE_A)}), values)
        assert result.incomparable["初回"] == 1

    def test_閾値を渡せる(self):
        values = values_with(history=[history_row(CODE_A, price=12000)])
        http = FakeHttp({CODE_A: ok_response(CODE_A, price=11900)})
        assert run(http, values, threshold=500).drops == []

    def test_履歴は書く前に読む(self):
        """**追記してから読むと、今回の行が履歴に入る。**

        `diff` 側でも自分自身を弾いているが、ここで順序を固定しておけば
        二重に守れる。
        """
        values = values_with(history=[history_row(CODE_A)])
        run(FakeHttp({CODE_A: ok_response(CODE_A)}), values)
        assert len(values.get_calls) >= 2  # 見出し → 履歴 → （append）


# ============================================================ 書かない指定


class Test書かない指定:
    def test_書かないと1行も送らない(self):
        values = values_with()
        result = run(FakeHttp({CODE_A: ok_response(CODE_A)}), values, write=False)
        assert values.append_calls == []
        assert result.written == 0

    def test_書かなくても取得はする(self):
        """**オプション名は「何をしないか」までしか言っていない。**

        楽天には投げる＝QPS は消費する。ここを黙っていると、
        「何もしない」と読んだ人が繰り返し実行する。
        """
        values = values_with()
        http = FakeHttp({CODE_A: ok_response(CODE_A)})
        result = run(http, values, write=False)
        assert len(http.calls) == 1 and result.fetched_ok == 1

    def test_書かないときは見出しを作らない(self):
        values = values_with(header=[])
        run(FakeHttp({CODE_A: ok_response(CODE_A)}), values, write=False)
        assert values.append_calls == []


# ============================================================ 報告と終了コード


class Test書いたあとの読み直し:
    """**別の資格情報で読み直して照合する**（DESIGN 5-S）。

    `to_sheet` から呼べないと、人が思い出したときにしか走らない。
    """

    def test_照合まで走る(self):
        values = values_with()
        result = run(
            FakeHttp({CODE_A: ok_response(CODE_A)}), values,
            verify_service=FakeService(values),
        )
        assert result.verification is not None
        assert result.verification.compared_rows == 1

    def test_照合を渡さなければ走らない(self):
        # **走っていないことを、走ったふりで隠さない。**
        values = values_with()
        result = run(FakeHttp({CODE_A: ok_response(CODE_A)}), values)
        assert result.verification is None

    def test_書かないときは照合しない(self):
        values = values_with()
        result = run(
            FakeHttp({CODE_A: ok_response(CODE_A)}), values,
            write=False, verify_service=FakeService(values),
        )
        assert result.verification is None

    def test_行数の帳尻を渡す(self):
        # 追記が既存の行を上書きしていないか（5-U）は、前の数を渡さないと見られない。
        values = values_with(history=[history_row(CODE_A)])
        result = run(
            FakeHttp({CODE_A: ok_response(CODE_A)}), values,
            verify_service=FakeService(values),
        )
        assert result.verification.rows_before == 2  # 見出し + 履歴1行

    def test_照合が失敗したら終了コードが1(self):
        """**書き込みは成功を返している。** それでも着地が違えば失敗にする。

        送った行数と入った行数が一致していると、書き込み側の検査は通る。
        照合を終了コードに出さないと、**この回は成功として記録される**
        （2026-09-09 のミューテーションで実際に素通りした）。
        """
        values = values_with(corrupt=True)
        result = run(
            FakeHttp({CODE_A: ok_response(CODE_A)}), values,
            verify_service=FakeService(values),
        )
        assert result.ok is True            # 送った数 = 入った数
        assert result.verification.ok is False
        assert result.exit_code == 1

    def test_報告に照合の行が出る(self):
        values = values_with()
        result = run(
            FakeHttp({CODE_A: ok_response(CODE_A)}), values,
            verify_service=FakeService(values),
        )
        text = chr(10).join(to_sheet.format_report(result))
        assert "照合" in text and "セル" in text


class Test報告:
    def test_件数がそろっていれば成功(self):
        values = values_with()
        assert run(FakeHttp({CODE_A: ok_response(CODE_A)}), values).exit_code == 0

    def test_取得に失敗があれば2(self):
        # **0 にしない。** 定期実行では、誰も画面を見ていない。
        values = values_with()
        assert run(FakeHttp({CODE_A: empty_response()}), values).exit_code == 2

    def test_書けた行が足りなければ1(self):
        values = values_with(updated=0)
        assert run(FakeHttp({CODE_A: ok_response(CODE_A)}), values).exit_code == 1

    def test_報告に件数が出る(self):
        values = values_with()
        http = FakeHttp({CODE_A: ok_response(CODE_A), CODE_B: empty_response()})
        text = "\n".join(to_sheet.format_report(run(http, values, codes=(CODE_A, CODE_B))))
        assert "2" in text and "見つからない" in text

    def test_報告に資格情報が出ない(self):
        # この出力は**記事のスクリーンショットに載る**。
        values = values_with()
        result = run(FakeHttp({CODE_A: ok_response(CODE_A)}), values)
        text = "\n".join(to_sheet.format_report(result))
        assert "app" not in text.split() and "key" not in text.split()

    def test_書かなかったことが報告に出る(self):
        values = values_with()
        result = run(FakeHttp({CODE_A: ok_response(CODE_A)}), values, write=False)
        assert any("書いて" in line for line in to_sheet.format_report(result))


class Test権限の案内:
    """**権限（API の有効化）と所属（シートの共有）は別物。**

    課題1で Slack の「スコープはあるがチャンネルに居ない」で踏んだのと同じ形で、
    ここでは「認証は通るのに書き込みだけ 403」になる。
    エラー本文だけ見せても、**何をすれば直るのかが分からない。**
    """

    class FakeHttpError(Exception):
        def __init__(self, status):
            super().__init__(f"HTTP {status}")
            self.status_code = status

    def key_file(self, tmp_path):
        path = tmp_path / "service-account.json"
        path.write_text(
            json.dumps({"client_email": "sheet-writer@example.iam.gserviceaccount.com"}),
            encoding="utf-8",
        )
        return path

    def test_403なら共有先のアドレスを出す(self, tmp_path):
        hint = to_sheet.permission_hint(self.key_file(tmp_path), self.FakeHttpError(403))
        assert "sheet-writer@example.iam.gserviceaccount.com" in hint

    def test_403の案内に共有の手順が入る(self, tmp_path):
        hint = to_sheet.permission_hint(self.key_file(tmp_path), self.FakeHttpError(403))
        assert "共有" in hint and "編集者" in hint

    def test_404ならシートIDを疑う案内(self, tmp_path):
        hint = to_sheet.permission_hint(self.key_file(tmp_path), self.FakeHttpError(404))
        assert "GOOGLE_SHEET_ID" in hint

    def test_他の状態では案内しない(self, tmp_path):
        # 関係ない失敗に共有の話を出すと、**本当の原因から目をそらす。**
        assert to_sheet.permission_hint(self.key_file(tmp_path), self.FakeHttpError(500)) == ""

    def test_古い形の例外でも状態を読む(self, tmp_path):
        # googleapiclient は resp.status にも入れる。片方だけ見ると取りこぼす。
        class OldStyle(Exception):
            def __init__(self):
                super().__init__("403")
                self.resp = type("R", (), {"status": 403})()

        assert "共有" in to_sheet.permission_hint(self.key_file(tmp_path), OldStyle())

    def test_鍵が読めなくても案内は出る(self, tmp_path):
        # アドレスが取れないことと、案内を出さないことは別。
        hint = to_sheet.permission_hint(tmp_path / "no.json", self.FakeHttpError(403))
        assert "共有" in hint

    def test_案内に秘密鍵が入らない(self, tmp_path):
        path = tmp_path / "service-account.json"
        path.write_text(
            json.dumps({"client_email": "a@b.iam.gserviceaccount.com",
                        "private_key": "BEGIN PRIVATE KEY SECRET"}),
            encoding="utf-8",
        )
        assert "SECRET" not in to_sheet.permission_hint(path, self.FakeHttpError(403))


class TestCLI:
    def test_dryrunの説明が両方を言う(self):
        """**「何をするか」も書く。** 「書かない」だけだと、

        楽天に投げること（QPS を消費すること）が伝わらない。
        """
        help_text = to_sheet.build_parser().format_help()
        assert "--dry-run" in help_text
        index = help_text.index("--dry-run")
        section = help_text[index:index + 400]
        assert "書き" in section and "楽天" in section

    def test_watchlistの場所を指定できる(self):
        args = to_sheet.build_parser().parse_args(["--watchlist", "x.json"])
        assert args.watchlist == "x.json"

    def test_閾値を指定できる(self):
        assert to_sheet.build_parser().parse_args(["--threshold", "500"]).threshold == 500

    def test_既定で照合する(self):
        # **確かめないほうを既定にしない。** 確かめないなら、そう言って選ばせる。
        assert to_sheet.build_parser().parse_args([]).no_verify is False

    def test_照合を切れる(self):
        assert to_sheet.build_parser().parse_args(["--no-verify"]).no_verify is True

    def test_照合の説明が別の資格情報だと言う(self):
        # usage 行にもオプション名が出るので、**説明が並ぶ側**を見る。
        help_text = to_sheet.build_parser().format_help()
        index = help_text.rindex("--no-verify")
        assert "読み取り専用" in help_text[index:index + 300]

    def test_既定では書く(self):
        assert to_sheet.build_parser().parse_args([]).dry_run is False


# ============================================================ 実行の切り出し


class Test実行の切り出し:
    """`main` は `execute` の**薄い皮**。画面へ出す責任だけを持つ。

    定期実行のラッパ（`run_daily.py`）は報告の**文字列を読まない**。
    読めば「値下がり 0 件」と「値下がり 10 件」を部分一致で見分けることになり、
    README に自分で書いた「部分一致は弱い」を、照合器に続いてもう一度踏む。
    だから `execute` は `RunResult` を**そのまま**返す。
    """

    #: 鍵の場所は**実在しないパス**にする。パッチ済みなので中身は要らないが、
    #: 実在パスを書くと 403 の案内が**本物の client_email を読んで**しまう。
    ENV = {
        "RAKUTEN_APPLICATION_ID": "app-id",
        "RAKUTEN_ACCESS_KEY": "access-key",
        "GOOGLE_SHEET_ID": SHEET_ID,
        "GOOGLE_SERVICE_ACCOUNT_FILE": "no-such-key.json",
    }

    @classmethod
    def _patch(cls, monkeypatch, *, result=None, error=None, calls=None):
        monkeypatch.setattr(to_sheet.env_file, "load", lambda path: dict(cls.ENV))
        monkeypatch.setattr(
            to_sheet.sheets_client, "load_credentials", lambda *a, **k: object()
        )
        monkeypatch.setattr(
            to_sheet.sheets_client, "build_service", lambda credentials: object()
        )
        monkeypatch.setattr(
            to_sheet.verify_sheet, "read_only_service", lambda path: object()
        )

        def fake_run(**kwargs):
            if calls is not None:
                calls.append(kwargs)
            if error is not None:
                raise error
            return result

        monkeypatch.setattr(to_sheet, "run", fake_run)

    @staticmethod
    def _result(**over):
        base = dict(
            requested=2, fetched_ok=2, fetched_failed=0, reasons={},
            rows_built=2, sent=2, written=2, wrote=True, drops=[],
            incomparable={}, name_changed=[], stock_changed=[],
            duplicates=[], header_state="そろっています",
        )
        base.update(over)
        return to_sheet.RunResult(**base)

    def test_画面に何も出さない(self, monkeypatch, capsys):
        # **どこへ出すかは呼ぶ側が決める。** ここで print すると、
        # ラッパが捕まえられない場所へ報告が漏れる（DESIGN 5-V）。
        self._patch(monkeypatch, result=self._result())
        to_sheet.execute([])
        assert capsys.readouterr().out == ""

    def test_結果をそのまま返す(self, monkeypatch):
        result = self._result()
        self._patch(monkeypatch, result=result)
        assert to_sheet.execute([]).result is result

    def test_報告はformat_reportと同じ(self, monkeypatch):
        result = self._result()
        self._patch(monkeypatch, result=result)
        assert to_sheet.execute([]).lines == to_sheet.format_report(result)

    def test_終了コードは結果のもの(self, monkeypatch):
        self._patch(monkeypatch, result=self._result(fetched_failed=1, reasons={"不明": 1}))
        assert to_sheet.execute([]).exit_code == 2

    def test_mainは報告を全部流す(self, monkeypatch):
        result = self._result()
        self._patch(monkeypatch, result=result)
        seen: list[str] = []
        to_sheet.main([], out=seen.append)
        assert seen == to_sheet.format_report(result)

    def test_mainは終了コードを返す(self, monkeypatch):
        self._patch(monkeypatch, result=self._result(sent=2, written=1))
        assert to_sheet.main([], out=lambda line: None) == 1

    def test_失敗したら結果はNone(self, monkeypatch):
        # **「走らなかった」を「結果0件」で表さない。**
        self._patch(monkeypatch, error=to_sheet.ToSheetError("こわれました"))
        outcome = to_sheet.execute([])
        assert outcome.result is None
        assert outcome.exit_code == 1
        assert outcome.lines == ["こわれました"]

    def test_権限の案内も報告に入る(self, monkeypatch):
        self._patch(monkeypatch, error=Test権限の案内.FakeHttpError(403))
        outcome = to_sheet.execute([])
        assert outcome.result is None
        assert outcome.exit_code == 1
        assert any("共有" in line for line in outcome.lines)

    def test_案内が作れない例外はそのまま投げる(self, monkeypatch):
        # **握って握りつぶさない。** 案内を足せない失敗は、隠すと原因が消える。
        self._patch(monkeypatch, error=RuntimeError("なにか"))
        with pytest.raises(RuntimeError):
            to_sheet.execute([])

    def test_mainも同じ失敗の出し方をする(self, monkeypatch):
        self._patch(monkeypatch, error=to_sheet.ToSheetError("こわれました"))
        seen: list[str] = []
        assert to_sheet.main([], out=seen.append) == 1
        assert seen == ["こわれました"]
