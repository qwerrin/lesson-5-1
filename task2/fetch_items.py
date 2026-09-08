"""楽天市場の商品検索 API を叩いて、watchlist の1商品につき**必ず1つの結果**を返す。

**変換も書き込みもしない。** ここが引き受けるのは「取りに行く」責任だけで、
行への変換は `transform.py`、シートへの追記は `to_sheet.py` が持つ。
`http` ・ `sleep` ・ `monotonic` を差し替えられるようにしてあるのは、
**外部に1回も繋がずに 429 や切断を再現するため**である。

この層が守ること
------------------------------------------------------------------

**1件も黙って消えないこと。** 取れなかった商品も ``ItemResult`` として返す。
戻り値から落とすと、呼び出し側では「その日は書かなかった」と区別が付かない
（DESIGN 5-B / 5-C）。

**いちばん静かな失敗は 200 で返ってくる**（2026-09-08 実測）
------------------------------------------------------------------

`.env` の資格情報で実際に投げて確かめた。

================================== ================================================
投げたもの                          返り
================================== ================================================
正常                                HTTP 200 ・ ``count=1`` ・ ``Items[0]["Item"]``
**実在ショップ＋存在しない商品番号**  **HTTP 200 ・ ``count=0`` ・ ``Items=[]``**
書式不正（``:`` 無し）               HTTP 400 ``wrong_parameter``
実在しないショップ                   HTTP 400 ``wrong_parameter``
================================== ================================================

**2行目が本体。** 出品終了・売り切れ非公開・商品番号の打ち間違いは、どれも
「エラーを出さずに0件」という同じ形で返る（DESIGN 4-⑥）。ここを失敗として
拾わないと、シートにはその日の行が**ただ無い**——「価格が動かなかった」と読める。

一方、**打ち間違いのうちショップコードが違うものは 400 で分かれる**。
「設定を直せば取れるもの」と「もう存在しないもの」は、この2つで区別できる。

理由の語彙は、公式に載っているものだけ固定語にする
------------------------------------------------------------------

公式ドキュメントの Error 節（2026-09-08 に生 HTML から確認）に載っているのは
**400 / 404 / 429 / 500 / 503 の5つだけ**。**403 は1件も無く、IP 拒否の
エラー形も書かれていない。**

DESIGN 4-⑨ には「拒否を区別して ``理由=IP拒否`` で記録する」と書いたが、
**その形を確かめていない以上、固定語にしない**。403 が別の理由で返った日に、
履歴へ嘘が残るためである。載っていない状態は
``不明(HTTP 403 some_code)`` のように**生の status と error 識別子を残す**。
実際に拒否を踏んだ日、その文字列がシートに残る——そこで初めて語を足せる。

**``error_description`` は理由に入れない。** 楽天のエラー本文には URL が
入りうる。URL には ``applicationId`` が載る。本文をそのまま流すと、
**鍵がシートにもスクショにも残る**。`requests` の例外メッセージも同じ理由で使わない。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable, Iterable

import requests

#: 公式のエンドポイント（version:2026-07-01）。
#: 版はブログに多い ``20220601`` ではない——そちらは HTTP 400 で存在しない（DESIGN 3.2）。
ENDPOINT = "https://openapi.rakuten.co.jp/ichibams/api/IchibaItem/Search/20260701"

#: (接続, 読み取り) の秒数。**必ず渡す。**
#: 渡し忘れると `requests` は無限に待つ。これは「欠落」ではなく「終わらない」形の穴で、
#: 定期実行だと翌日の実行と重なってプロセスが積み上がる。誰も見ていない。
DEFAULT_TIMEOUT: tuple[float, float] = (10.0, 30.0)

#: 送信の最短間隔（秒）。このアプリは QPS=1 で申告している（`.env.example`）。
#: **「毎回1秒眠る」ではなく「前回の送信から1秒空ける」**——退避で30秒待った直後に
#: さらに眠るのは、ただの遅延。
DEFAULT_MIN_INTERVAL = 1.0

#: 1商品あたりの試行回数の上限。**無限に粘らない。**
#: 粘ると1商品で1日ぶんの QPS を使い切り、後ろの商品が全部落ちる。
DEFAULT_MAX_ATTEMPTS = 4

#: 最初の退避の秒数。以後は倍々（2 → 4 → 8）。
DEFAULT_BACKOFF_BASE = 2.0

#: 退避の上限（秒）。``Retry-After`` に極端な値が入っていても、ここで頭を打つ。
MAX_BACKOFF_SECONDS = 60.0

#: 1回の検索で受け取る件数。``itemCode`` 検索は**完全一致で1件**返ることを実測した
#: （2026-09-08・``count=1``）。多く求める理由が無い。
#: 万一そこが変わって別商品が先頭に来ても、下の一致検査が ``別商品`` として弾く
#: ——**間違った価格を積むより、取れなかったと記録するほうが直せる。**
DEFAULT_HITS = 1

#: 理由に載せる ``error`` 識別子の最大長。行が伸びて読めなくなるのを防ぐ。
_ERROR_TOKEN_LIMIT = 40

REASON_NOT_FOUND = "見つからない"
REASON_MISMATCH = "別商品"
REASON_RATE_LIMIT = "429"
REASON_BAD_PARAMETER = "パラメータ不正"
REASON_SERVER_ERROR = "サーバエラー"
REASON_MAINTENANCE = "メンテナンス"
REASON_TIMEOUT = "タイムアウト"
REASON_NETWORK = "通信失敗"
REASON_BAD_RESPONSE = "不正な応答"

#: **公式ドキュメントに載っている状態だけ**を固定語にする（2026-09-08 確認）。
#: ここに無い status は生値で残す（``unknown_reason``）。
DOCUMENTED_STATUS_REASONS: dict[int, str] = {
    400: REASON_BAD_PARAMETER,
    404: REASON_NOT_FOUND,
    429: REASON_RATE_LIMIT,
    500: REASON_SERVER_ERROR,
    503: REASON_MAINTENANCE,
}


@dataclass(frozen=True)
class ItemResult:
    """商品1件ぶんの結果。**取れても取れなくても1つ返る。**"""

    item_code: str
    #: 取れたときだけ中身が入る。ラッパ（``{"Item": {...}}``）は外してある。
    item: dict[str, Any] | None
    #: 取れなかった理由。取れたときは空。
    reason: str
    #: 何回投げたか。**諦めたのか1回で済んだのかを、後から履歴で見分けるため。**
    attempts: int

    @property
    def ok(self) -> bool:
        return self.item is not None


@dataclass(frozen=True)
class FetchReport:
    """その回の全体。**``requested`` と ``len(results)`` は必ず一致する。**

    watchlist に入れたのに一度も行が入らない商品を見つけるための突き合わせ
    （DESIGN 5-C）。件数が合わないことを、件数で言えるようにしておく。
    """

    results: list[ItemResult]
    requested: int

    @property
    def ok(self) -> int:
        return sum(1 for r in self.results if r.ok)

    @property
    def failed(self) -> int:
        return sum(1 for r in self.results if not r.ok)


class _Pacer:
    """前回の送信から一定時間が空くまで待つ。

    眠った回数ではなく**時計**を見る。応答に5秒かかった後に、さらに1秒眠る
    必要は無い——そこを眠る実装は、遅いだけで安全でもない。
    """

    def __init__(self, min_interval: float, sleep: Callable[[float], None],
                 monotonic: Callable[[], float]) -> None:
        self._min_interval = min_interval
        self._sleep = sleep
        self._monotonic = monotonic
        self._last: float | None = None

    def wait(self) -> None:
        now = self._monotonic()
        if self._last is not None:
            remaining = self._min_interval - (now - self._last)
            if remaining > 0:
                self._sleep(remaining)
        self._last = self._monotonic()


def build_params(item_code: str, *, application_id: str, access_key: str) -> dict[str, Any]:
    """1商品ぶんのクエリ。

    **``accessKey`` を必ず載せる。** 2026年の刷新で必須になり、
    ``applicationId`` だけでは通らない。

    ``keyword`` は使わない。日によって別商品が混ざり、
    **価格の推移が別物同士の比較になる**（DESIGN 2）。
    """
    return {
        "applicationId": application_id,
        "accessKey": access_key,
        "itemCode": item_code,
        "format": "json",
        "hits": DEFAULT_HITS,
    }


def unknown_reason(status: int, error: Any = None) -> str:
    """公式に載っていない状態を、**生のまま**理由にする。

    識別子だけを載せる。``error_description`` は載せない——URL が入りうるし、
    URL には資格情報が載る。
    """
    token = _safe_token(error)
    return f"不明(HTTP {status} {token})" if token else f"不明(HTTP {status})"


def _safe_token(value: Any) -> str:
    """``error`` 欄を1行に潰し、長さを切る。

    改行が入るとセルが壊れる（`transform` の 5-F と同じ形）。
    """
    if value is None:
        return ""
    text = " ".join(str(value).split())
    return text[:_ERROR_TOKEN_LIMIT]


def _read_json(response: Any) -> Any | None:
    """本文を読む。読めなければ ``None``。**ここで例外を外へ出さない。**

    出すと、その商品だけでなく**その回の全商品**が落ちる。
    """
    try:
        return response.json()
    except ValueError:
        return None


def _items_of(payload: Any) -> list[dict[str, Any]] | None:
    """``Items`` から商品の本体を取り出す。形が違えば ``None``。

    実測の形は ``Items[0]["Item"]`` のラッパ形。**この外側の形は公式
    ドキュメントに書かれていない**——書かれていないものは、変わっても告知されない。
    平坦な形で来ても拾えるようにしてあるのは、そのため。ここを固定すると、
    形が変わった日に**全商品が「見つからない」になる**（エラーは出ない）。
    """
    if not isinstance(payload, dict):
        return None
    entries = payload.get("Items")
    if not isinstance(entries, list):
        return None

    bodies: list[dict[str, Any]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        inner = entry.get("Item")
        body = inner if isinstance(inner, dict) else entry
        bodies.append(body)
    return bodies


def _retry_after(response: Any) -> float | None:
    """``Retry-After`` を秒として読む。読めなければ ``None``。

    HTTP-date 形式もありうるが、**この API から受け取ったことがまだ無い**ので
    解釈しない。読めない値を 0 として扱わないこと——0 にすると即座に投げ直して
    429 を悪化させる。
    """
    headers = getattr(response, "headers", None) or {}
    try:
        seconds = float(headers.get("Retry-After"))
    except (TypeError, ValueError):
        return None
    return seconds if seconds >= 0 else None


def _backoff_seconds(attempt: int, backoff_base: float, retry_after: float | None) -> float:
    """次の退避。相手が秒数を言ってきたらそちらを優先し、上限で頭を打つ。"""
    seconds = retry_after if retry_after is not None else backoff_base * (2 ** (attempt - 1))
    return min(seconds, MAX_BACKOFF_SECONDS)


def _classify(response: Any, item_code: str) -> tuple[dict[str, Any] | None, str, bool]:
    """応答1つを (商品, 理由, やり直すか) に分ける。"""
    status = getattr(response, "status_code", None)

    if status != 200:
        payload = _read_json(response)
        error = payload.get("error") if isinstance(payload, dict) else None
        reason = DOCUMENTED_STATUS_REASONS.get(status) or unknown_reason(status, error)
        # やり直して直りうるのは混雑と相手側の一時障害だけ。
        # パラメータ不正や拒否は、待っても人が動くまで直らない。
        transient = status == 429 or (isinstance(status, int) and status >= 500)
        return None, reason, transient

    payload = _read_json(response)
    bodies = _items_of(payload)
    if bodies is None:
        # 形が読めない。同じ応答が返り続ける公算が高いので、QPS を捨てずに記録する。
        return None, REASON_BAD_RESPONSE, False
    if not bodies:
        # **エラーが1つも出ない失敗**（実測）。出品終了・売り切れ非公開・打ち間違い。
        return None, REASON_NOT_FOUND, False

    for body in bodies:
        if body.get("itemCode") == item_code:
            return body, "", False
    # 要求したものが入っていない。採用すると**別商品の価格が同じ行に積まれる**。
    return None, REASON_MISMATCH, False


def fetch_one(
    http: Any,
    item_code: str,
    *,
    application_id: str,
    access_key: str,
    pacer: _Pacer | None = None,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
    min_interval: float = DEFAULT_MIN_INTERVAL,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    backoff_base: float = DEFAULT_BACKOFF_BASE,
    timeout: tuple[float, float] = DEFAULT_TIMEOUT,
) -> ItemResult:
    """1商品を取りに行く。**例外を外へ出さない**（通信の失敗に限る）。

    通信以外の例外は**外へ出す**。握って「通信失敗」にすると、こちらのバグが
    シート一面の「通信失敗」に化けて、原因が永久に分からなくなる。
    """
    pacer = pacer or _Pacer(min_interval, sleep, monotonic)
    params = build_params(item_code, application_id=application_id, access_key=access_key)

    reason = REASON_NETWORK
    for attempt in range(1, max_attempts + 1):
        pacer.wait()
        retry_after: float | None = None
        try:
            response = http.get(ENDPOINT, params=params, timeout=timeout)
        except requests.exceptions.Timeout:
            # 例外の文字列は使わない。URL＝資格情報が載る。
            reason, transient = REASON_TIMEOUT, True
        except requests.exceptions.RequestException:
            reason, transient = REASON_NETWORK, True
        else:
            item, reason, transient = _classify(response, item_code)
            if item is not None:
                return ItemResult(item_code=item_code, item=item, reason="", attempts=attempt)
            retry_after = _retry_after(response)

        if not transient or attempt == max_attempts:
            return ItemResult(item_code=item_code, item=None, reason=reason, attempts=attempt)
        sleep(_backoff_seconds(attempt, backoff_base, retry_after))

    # max_attempts が 0 以下でもここへ来る。**黙って結果を返さない経路を作らない。**
    return ItemResult(item_code=item_code, item=None, reason=reason, attempts=0)


def fetch_all(
    http: Any,
    *,
    item_codes: Iterable[str],
    application_id: str,
    access_key: str,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
    min_interval: float = DEFAULT_MIN_INTERVAL,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    backoff_base: float = DEFAULT_BACKOFF_BASE,
    timeout: tuple[float, float] = DEFAULT_TIMEOUT,
) -> FetchReport:
    """watchlist を順に取りに行く。**入れた数だけ結果が返る。**

    間隔制御は**商品をまたいで1つ**の時計で行う。商品ごとに作ると、
    1商品目の直後に2商品目を投げてしまう。
    """
    codes = list(item_codes)
    pacer = _Pacer(min_interval, sleep, monotonic)
    results = [
        fetch_one(
            http,
            code,
            application_id=application_id,
            access_key=access_key,
            pacer=pacer,
            sleep=sleep,
            monotonic=monotonic,
            max_attempts=max_attempts,
            backoff_base=backoff_base,
            timeout=timeout,
        )
        for code in codes
    ]
    return FetchReport(results=results, requested=len(codes))
