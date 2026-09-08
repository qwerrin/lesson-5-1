"""task2/fetch_items のテスト。**実装より先に書いた。**

外部に1回も繋がない。`http` ・ `sleep` ・ `monotonic` を差し替えるので、
429 も切断もタイムアウトも、こちらで作って再現できる。

守らせる対象は `task2/DESIGN.md` の穴分析と、**2026-09-08 の実測**から引いている。

============ ====================================================================
DESIGN の項目 ここで守ること
============ ====================================================================
5-B          取れなかったものが**必ず1件の結果として残る**（黙って消えない）
5-C          要求した件数と返る件数が一致する
4-⑥          「売り切れ／出品終了／打ち間違い」が **200 + 0件** で来る。
             エラーが出ないので、ここを失敗として拾わないと履歴から静かに消える
4-⑧          429 は指数退避する。**諦めたことを attempts に残す**
4-⑨          公式に載っていない状態（403 など）は**生の status と error を残す**
============ ====================================================================

実測（2026-09-08・`.env` の資格情報で3リクエスト）:

============================== =================================================
投げたもの                      返り
============================== =================================================
実在ショップ＋存在しない商品番号  **HTTP 200 ・ count=0 ・ Items=[]**（無言）
書式不正（コロン無し）           HTTP 400 ``wrong_parameter``
実在しないショップ               HTTP 400
正常                            HTTP 200 ・ count=1 ・ ``Items[0]["Item"]``
============================== =================================================
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import fetch_items  # noqa: E402


APP_ID = "test-application-id-0123456789012345"
ACCESS_KEY = "test-access-key-0123456789012345678901234567"
CODE = "f282260-awaji:10001542"


# ============================================================ 偽物


class FakeResponse:
    """`requests` の応答のうち、この実装が触る部分だけを持つ。

    **本物に似せて作るのではなく、本物が返した形をそのまま置く。**
    ラッパ形（``Items[0]["Item"]``）は 2026-09-08 の実測から取った。
    """

    def __init__(self, status_code, payload=None, headers=None, *, broken=False):
        self.status_code = status_code
        self._payload = payload
        self.headers = headers or {}
        self._broken = broken

    def json(self):
        if self._broken:
            raise ValueError("Expecting value: line 1 column 1 (char 0)")
        return self._payload


class FakeHttp:
    """`get` を呼ばれた回数と引数を記録する。応答は渡された順に返す。

    要素が例外なら送出する（タイムアウト・切断の再現）。
    """

    def __init__(self, *responses):
        self._responses = list(responses)
        self.calls: list[dict] = []

    def get(self, url, params=None, timeout=None, **kwargs):
        self.calls.append({"url": url, "params": params, "timeout": timeout, **kwargs})
        if not self._responses:
            raise AssertionError("応答を使い切ったのに、もう1回呼ばれた")
        nxt = self._responses.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt


class FakeClock:
    """眠った時間を記録し、**その分だけ時計を進める**。

    進めないと「間隔が空いたから待たない」経路が一度も通らず、
    待ち時間の検査が「常に待つ」実装でも通ってしまう。
    """

    def __init__(self, start=1000.0):
        self.now = start
        self.sleeps: list[float] = []

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds

    def monotonic(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def ok_payload(code=CODE, **overrides):
    """正常応答。**外側はラッパ形**（実測）。"""
    item = {
        "itemCode": code,
        "itemName": "テスト商品",
        "itemPrice": 12000,
        "pointRate": 1,
        "availability": 1,
    }
    item.update(overrides)
    return {"count": 1, "hits": 1, "page": 1, "pageCount": 1, "Items": [{"Item": item}]}


def empty_payload():
    """**実在ショップ＋存在しない商品番号**。エラーは1つも出ない（実測）。"""
    return {"count": 0, "hits": 0, "page": 1, "pageCount": 0, "Items": []}


def error_payload(error, description="dummy"):
    return {"error": error, "error_description": description}


def fetch(http, clock=None, **kwargs):
    """テストから呼ぶ入口。時計と資格情報を毎回書かないための薄い皮。"""
    clock = clock or FakeClock()
    kwargs.setdefault("item_codes", [CODE])
    return fetch_items.fetch_all(
        http,
        application_id=APP_ID,
        access_key=ACCESS_KEY,
        sleep=clock.sleep,
        monotonic=clock.monotonic,
        **kwargs,
    )


# ============================================================ 正常系


class Test取得できたとき:
    def test_商品の中身が返る(self):
        report = fetch(FakeHttp(FakeResponse(200, ok_payload())))
        assert report.results[0].item["itemCode"] == CODE

    def test_ラッパの中身を取り出す(self):
        # 実測の形は Items[0]["Item"]。ラッパごと渡すと transform が全列を空にする。
        report = fetch(FakeHttp(FakeResponse(200, ok_payload())))
        assert "Item" not in report.results[0].item
        assert report.results[0].item["itemPrice"] == 12000

    def test_理由は空(self):
        report = fetch(FakeHttp(FakeResponse(200, ok_payload())))
        assert report.results[0].reason == ""

    def test_okが真(self):
        report = fetch(FakeHttp(FakeResponse(200, ok_payload())))
        assert report.results[0].ok is True

    def test_1回で済んだら試行回数は1(self):
        report = fetch(FakeHttp(FakeResponse(200, ok_payload())))
        assert report.results[0].attempts == 1

    def test_平坦な形でも拾える(self):
        """**外側の形は公式ドキュメントに書かれていない。**

        書かれていないものは、変わっても告知されない。実測はラッパ形だが、
        平坦（``Items[0]`` が商品そのもの）で来ても拾えるようにしておく。
        ここを固定すると、形が変わった日に**全商品が「見つからない」になる**
        ——エラーは出ないので、シートには何も残らない。
        """
        payload = {"count": 1, "Items": [{"itemCode": CODE, "itemPrice": 900}]}
        report = fetch(FakeHttp(FakeResponse(200, payload)))
        assert report.results[0].item["itemPrice"] == 900


# ============================================================ 4-⑥ 200 だが0件


class Test無言の0件:
    def test_0件は失敗として残る(self):
        # **ここが課題1の講評そのもの。** エラーが出ないまま履歴から消える経路。
        report = fetch(FakeHttp(FakeResponse(200, empty_payload())))
        assert report.results[0].ok is False

    def test_理由は見つからない(self):
        report = fetch(FakeHttp(FakeResponse(200, empty_payload())))
        assert report.results[0].reason == "見つからない"

    def test_0件でも結果は1件返る(self):
        # 結果ごと消すと「その日は書かなかった」と区別が付かない。
        report = fetch(FakeHttp(FakeResponse(200, empty_payload())))
        assert len(report.results) == 1

    def test_0件は再送しない(self):
        # 存在しないものは待っても現れない。QPS を捨てるだけ。
        http = FakeHttp(FakeResponse(200, empty_payload()))
        fetch(http)
        assert len(http.calls) == 1


class Test別商品が返ったとき:
    def test_要求と違うitemCodeは採用しない(self):
        # 採用すると**別商品の価格が同じ行に積まれる**。例外は出ない。
        payload = ok_payload(code="other-shop:99")
        report = fetch(FakeHttp(FakeResponse(200, payload)))
        assert report.results[0].ok is False
        assert report.results[0].reason == "別商品"

    def test_複数返っても一致するものを選ぶ(self):
        payload = ok_payload(code="other-shop:99")
        payload["Items"].append({"Item": {"itemCode": CODE, "itemPrice": 777}})
        report = fetch(FakeHttp(FakeResponse(200, payload)))
        assert report.results[0].item["itemPrice"] == 777


# ============================================================ 公式のエラー分類


class Test公式に載っている状態:
    @pytest.mark.parametrize("status,error,reason", [
        (400, "wrong_parameter", "パラメータ不正"),
        (404, "not_found", "見つからない"),
        (500, "system_error", "サーバエラー"),
        (503, "service_unavailable", "メンテナンス"),
    ])
    def test_文書化された状態は固定語になる(self, status, error, reason):
        # 出どころ: 公式ドキュメントの Error 節（2026-09-08 に生HTMLから確認）。
        responses = [FakeResponse(status, error_payload(error))] * 4
        report = fetch(FakeHttp(*responses))
        assert report.results[0].reason == reason

    def test_429は固定語になる(self):
        responses = [FakeResponse(429, error_payload("too_many_requests"))] * 4
        report = fetch(FakeHttp(*responses))
        assert report.results[0].reason == "429"


class Test公式に載っていない状態:
    def test_403は生のstatusを残す(self):
        """**公式のエラー一覧に 403 が無い**（生HTMLで実測・0件）。

        IP 拒否をここに当てはめたくなるが、**その形を確かめていない**。
        固定語にすると、403 が別の理由で返った日に嘘の履歴が残る。
        """
        report = fetch(FakeHttp(FakeResponse(403, error_payload("some_code"))))
        assert report.results[0].reason == "不明(HTTP 403 some_code)"

    def test_error識別子が無ければstatusだけ残す(self):
        report = fetch(FakeHttp(FakeResponse(418, {})))
        assert report.results[0].reason == "不明(HTTP 418)"

    def test_403は再送しない(self):
        # 拒否は待っても直らない。IP 変更なら人が動くまで直らない。
        http = FakeHttp(FakeResponse(403, error_payload("x")))
        fetch(http)
        assert len(http.calls) == 1


class Test理由に資格情報を混ぜない:
    def test_error_descriptionは理由に入らない(self):
        """**記事にはスクショを載せる。シートも見せる。**

        楽天のエラー本文には URL が入りうる。URL には ``applicationId`` が載る。
        本文をそのまま理由に流すと、**鍵がシートとスクショの両方に残る**。
        """
        leak = f"https://openapi.rakuten.co.jp/x?applicationId={APP_ID}&accessKey={ACCESS_KEY}"
        report = fetch(FakeHttp(FakeResponse(403, error_payload("denied", leak))))
        reason = report.results[0].reason
        assert APP_ID not in reason
        assert ACCESS_KEY not in reason
        assert "applicationId" not in reason

    def test_理由の長さに上限がある(self):
        # 長い文字列を error 欄に入れられても、行が壊れるほど伸びない。
        report = fetch(FakeHttp(FakeResponse(403, error_payload("z" * 500))))
        assert len(report.results[0].reason) <= 80

    def test_理由に改行が入らない(self):
        # 改行はセルを壊す（transform の 5-F と同じ形）。
        report = fetch(FakeHttp(FakeResponse(403, error_payload("a\nb\tc"))))
        assert "\n" not in report.results[0].reason
        assert "\t" not in report.results[0].reason


# ============================================================ 通信の失敗


class Test通信できないとき:
    def test_タイムアウトは固定語になる(self):
        errors = [requests.exceptions.Timeout()] * 4
        report = fetch(FakeHttp(*errors))
        assert report.results[0].reason == "タイムアウト"

    def test_切断は通信失敗になる(self):
        errors = [requests.exceptions.ConnectionError()] * 4
        report = fetch(FakeHttp(*errors))
        assert report.results[0].reason == "通信失敗"

    def test_例外の文字列は理由に入らない(self):
        # requests の例外メッセージは URL を含む＝資格情報が載る。
        leak = f"HTTPSConnectionPool: /x?applicationId={APP_ID}"
        errors = [requests.exceptions.ConnectionError(leak)] * 4
        report = fetch(FakeHttp(*errors))
        assert APP_ID not in report.results[0].reason

    def test_JSONでなければ不正な応答(self):
        # メンテナンス中の HTML が 200 で返ることがある。
        report = fetch(FakeHttp(FakeResponse(200, broken=True)))
        assert report.results[0].reason == "不正な応答"

    def test_Items欄が無ければ不正な応答(self):
        report = fetch(FakeHttp(FakeResponse(200, {"count": 1})))
        assert report.results[0].reason == "不正な応答"

    def test_不正な応答は再送しない(self):
        http = FakeHttp(FakeResponse(200, broken=True))
        fetch(http)
        assert len(http.calls) == 1

    def test_エラー本文がJSONでなくても落ちない(self):
        # 502 の HTML など。**ここで例外を出すと、その商品だけでなく全件が落ちる。**
        # 502 は「やり直す側」なので、応答は試行回数ぶん置く。
        report = fetch(FakeHttp(*[FakeResponse(502, broken=True)] * 4))
        assert report.results[0].reason.startswith("不明(HTTP 502")

    def test_実装のバグは握りつぶさない(self):
        """通信以外の例外は**外へ出す**。

        握って「通信失敗」にすると、こちらのバグがシート一面の
        「通信失敗」に化けて、原因が永久に分からなくなる。
        """
        http = FakeHttp(ZeroDivisionError("実装のバグ"))
        with pytest.raises(ZeroDivisionError):
            fetch(http)


# ============================================================ 4-⑧ 429 と退避


class Test退避:
    def test_429のあと成功したら取得できる(self):
        http = FakeHttp(
            FakeResponse(429, error_payload("too_many_requests")),
            FakeResponse(200, ok_payload()),
        )
        report = fetch(http)
        assert report.results[0].ok is True

    def test_やり直した回数が残る(self):
        # **諦めたのか1回で済んだのかを、後から履歴で見分けられるようにする。**
        http = FakeHttp(
            FakeResponse(429, error_payload("too_many_requests")),
            FakeResponse(200, ok_payload()),
        )
        report = fetch(http)
        assert report.results[0].attempts == 2

    def test_退避は倍々に伸びる(self):
        clock = FakeClock()
        http = FakeHttp(
            FakeResponse(429, error_payload("too_many_requests")),
            FakeResponse(429, error_payload("too_many_requests")),
            FakeResponse(200, ok_payload()),
        )
        fetch(http, clock=clock, min_interval=0.0, backoff_base=2.0)
        assert clock.sleeps == [2.0, 4.0]

    def test_上限で諦めて失敗行になる(self):
        # **無限に粘らない。** 粘ると1商品で1日ぶんの QPS を使い切る。
        responses = [FakeResponse(429, error_payload("too_many_requests"))] * 3
        http = FakeHttp(*responses)
        report = fetch(http, max_attempts=3)
        assert report.results[0].ok is False
        assert report.results[0].attempts == 3
        assert len(http.calls) == 3

    def test_サーバエラーもやり直す(self):
        http = FakeHttp(
            FakeResponse(503, error_payload("service_unavailable")),
            FakeResponse(200, ok_payload()),
        )
        assert fetch(http).results[0].ok is True

    def test_パラメータ不正はやり直さない(self):
        # 何度投げても同じ。**打ち間違いに退避を使うと、その回の全商品が遅れる。**
        http = FakeHttp(FakeResponse(400, error_payload("wrong_parameter")))
        fetch(http)
        assert len(http.calls) == 1

    def test_RetryAfterがあれば従う(self):
        clock = FakeClock()
        http = FakeHttp(
            FakeResponse(429, error_payload("too_many_requests"), {"Retry-After": "7"}),
            FakeResponse(200, ok_payload()),
        )
        fetch(http, clock=clock, min_interval=0.0)
        assert clock.sleeps == [7.0]

    def test_RetryAfterが数値でなければ無視する(self):
        # HTTP-date 形式もありうる。**読めない値を 0 として扱わない**
        # ——0 にすると即座に投げ直して、429 を悪化させる。
        clock = FakeClock()
        http = FakeHttp(
            FakeResponse(429, error_payload("x"), {"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"}),
            FakeResponse(200, ok_payload()),
        )
        fetch(http, clock=clock, min_interval=0.0, backoff_base=2.0)
        assert clock.sleeps == [2.0]

    def test_退避には上限がある(self):
        clock = FakeClock()
        http = FakeHttp(
            FakeResponse(429, error_payload("x"), {"Retry-After": "99999"}),
            FakeResponse(200, ok_payload()),
        )
        fetch(http, clock=clock, min_interval=0.0)
        assert clock.sleeps == [fetch_items.MAX_BACKOFF_SECONDS]


# ============================================================ 間隔制御


class Test送信間隔:
    def test_1件なら待たない(self):
        # 待っても誰も得しない。定期実行の所要時間が伸びるだけ。
        clock = FakeClock()
        fetch(FakeHttp(FakeResponse(200, ok_payload())), clock=clock)
        assert clock.sleeps == []

    def test_2件目の前に待つ(self):
        clock = FakeClock()
        http = FakeHttp(FakeResponse(200, ok_payload("a:1")), FakeResponse(200, ok_payload("b:2")))
        fetch(http, clock=clock, item_codes=["a:1", "b:2"], min_interval=1.0)
        assert clock.sleeps == [1.0]

    def test_時間が経っていれば待たない(self):
        """**「毎回1秒眠る」ではなく「前回から1秒空ける」。**

        退避で30秒待った直後にさらに1秒眠るのは、ただの遅延。
        時計を見ずに眠る実装は、この検査でだけ落ちる。
        """
        clock = FakeClock()

        class SlowHttp(FakeHttp):
            def get(self, *args, **kwargs):
                clock.advance(5.0)  # 応答に5秒かかった
                return super().get(*args, **kwargs)

        http = SlowHttp(FakeResponse(200, ok_payload("a:1")), FakeResponse(200, ok_payload("b:2")))
        fetch(http, clock=clock, item_codes=["a:1", "b:2"], min_interval=1.0)
        assert clock.sleeps == []


# ============================================================ 5-C 件数の突き合わせ


class Test件数:
    def test_要求した数だけ結果が返る(self):
        codes = ["a:1", "b:2", "c:3"]
        http = FakeHttp(*[FakeResponse(200, ok_payload(c)) for c in codes])
        report = fetch(http, item_codes=codes)
        assert len(report.results) == len(codes)

    def test_全部失敗しても数は減らない(self):
        # **ここが 5-B と 5-C の芯。** 失敗を「無かったこと」にしない。
        codes = ["a:1", "b:2", "c:3"]
        http = FakeHttp(*[FakeResponse(200, empty_payload()) for _ in codes])
        report = fetch(http, item_codes=codes)
        assert len(report.results) == 3
        assert report.failed == 3
        assert report.ok == 0

    def test_1件の失敗で他が止まらない(self):
        # 1件の例外が全件の欠落になる経路を塞ぐ。
        codes = ["a:1", "b:2", "c:3"]
        http = FakeHttp(
            FakeResponse(200, ok_payload("a:1")),
            requests.exceptions.ConnectionError("落ちた"),
            requests.exceptions.ConnectionError("落ちた"),
            requests.exceptions.ConnectionError("落ちた"),
            requests.exceptions.ConnectionError("落ちた"),
            FakeResponse(200, ok_payload("c:3")),
        )
        report = fetch(http, item_codes=codes)
        assert [r.ok for r in report.results] == [True, False, True]

    def test_要求件数が記録される(self):
        report = fetch(FakeHttp(FakeResponse(200, ok_payload())))
        assert report.requested == 1

    def test_順序が入れ替わらない(self):
        # 並びが変わると、watchlist との突き合わせが目視でできなくなる。
        codes = ["a:1", "b:2", "c:3"]
        http = FakeHttp(*[FakeResponse(200, ok_payload(c)) for c in codes])
        report = fetch(http, item_codes=codes)
        assert [r.item_code for r in report.results] == codes

    def test_商品コードは結果に必ず残る(self):
        report = fetch(FakeHttp(FakeResponse(200, empty_payload())))
        assert report.results[0].item_code == CODE

    def test_watchlistが空なら1回も投げない(self):
        http = FakeHttp()
        report = fetch(http, item_codes=[])
        assert report.results == [] and report.requested == 0
        assert http.calls == []


# ============================================================ リクエストの中身


class Testリクエスト:
    def test_公式のエンドポイントを叩く(self):
        http = FakeHttp(FakeResponse(200, ok_payload()))
        fetch(http)
        assert http.calls[0]["url"] == (
            "https://openapi.rakuten.co.jp/ichibams/api/IchibaItem/Search/20260701"
        )

    def test_資格情報を2つとも載せる(self):
        # applicationId だけでは通らない（2026年の刷新で accessKey が必須）。
        http = FakeHttp(FakeResponse(200, ok_payload()))
        fetch(http)
        params = http.calls[0]["params"]
        assert params["applicationId"] == APP_ID
        assert params["accessKey"] == ACCESS_KEY

    def test_itemCodeで引く(self):
        # keyword で引くと日によって別商品が混ざり、価格の推移が別物の比較になる。
        http = FakeHttp(FakeResponse(200, ok_payload()))
        fetch(http)
        assert http.calls[0]["params"]["itemCode"] == CODE
        assert "keyword" not in http.calls[0]["params"]

    def test_JSONで受け取る(self):
        http = FakeHttp(FakeResponse(200, ok_payload()))
        fetch(http)
        assert http.calls[0]["params"]["format"] == "json"

    def test_タイムアウトを必ず渡す(self):
        """**渡し忘れると無限に待つ。**

        「欠落」ではなく「終わらない」形の穴。定期実行だと、
        翌日の実行と重なってプロセスが積み上がる。誰も見ていない。
        """
        http = FakeHttp(FakeResponse(200, ok_payload()))
        fetch(http)
        timeout = http.calls[0]["timeout"]
        assert timeout is not None
        assert len(timeout) == 2  # (接続, 読み取り) を別々に持つ
        assert all(t > 0 for t in timeout)
