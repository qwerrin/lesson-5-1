"""Gemini API の呼び出し。要約に使う LLM をここに閉じ込める。

課題1（Slack → 要約 → LINE）で**唯一新しく増える相手**がこれ。Slack と LINE は
Section 4-3 で作った認証をそのまま使えるが、要約だけは新規になる。

他の認証モジュールとの違い
------------------------------------------------------------------

===================== ================================================
何を使うか             どういう相手か
===================== ================================================
slack_auth（Bot）     **アプリ自身**が動く。インストール時に1回発行、期限なし
line_auth（長期）     アプリ自身が動く。無期限で、チャネルに1本しか無い
gemini_client（キー） **認可する相手がいない**。キー1本がそのまま権限
===================== ================================================

``youtube_auth``（課題6）と同じ「キーだけ」の形だが、**課金が乗っている**点が違う。
YouTube のキーは無料枠の読み取りだったのに対し、こちらは呼ぶたびに金額が動く。
だから「静かに何度も呼ぶ」経路を作らないことが、この課題の安全側になる。

このモジュールに固有の事情が4つある
------------------------------------------------------------------

**1. クライアントを一時オブジェクトにしてはいけない。**

``genai.Client(...).models.generate_content(...)`` と書くと、**リクエストが飛ぶ前に**
``RuntimeError: Cannot send a request, as the client has been closed.`` になる。
参照が消えた時点で内部の httpx が閉じるため。``build_client()`` が返した値を
必ず変数で受けて使い回す。

**2. 一時エラーの判定に本文の文字列を使わない。**

``str(error)`` に ``429`` が含まれるかで判定すると、
``token count 429 exceeds limit``（**恒久エラー**）を投げ直し続ける。
判定に使ってよいのは ``APIError.code``（整数）だけである。

**3. 接続断は ``OSError`` だけでは拾えない。**

``httpx.TransportError`` の継承は ``RequestError → HTTPError → Exception`` で、
**``OSError`` の仲間ではない**（2026-08-29 に実測）。「接続断＝OSError」で
済ませると、転送タイムアウトが恒久エラーに化けて1回で諦める。

**4. 一覧に載ることは、呼べることの証拠にならない。**

2026-08-29 の実測で、``models.list()`` に載っている ``gemini-2.5-flash-lite`` が
404 を返した。本文は「no longer available to new users」で後継を名指ししていた。
**モデル名を一覧から選ぶ実装にしない**——既定を1つ決め、変えるときは呼んで確かめる。
"""

from __future__ import annotations

from typing import Any, Callable, Mapping

import httpx
from google import genai
from google.genai import errors as genai_errors
from google.genai import types

#: キーを渡す環境変数名。
API_KEY_ENV = "GEMINI_API_KEY"

#: 既定のモデル。**2026-08-29 に実際に呼んで通ることを確認した値。**
#: 一覧から動的に選ばない（上の事情4）。変えるときは呼んで確かめてから変える。
DEFAULT_MODEL = "gemini-3.5-flash-lite"

#: 伏せたことが分かる印。空文字にすると「元から無かった」と区別がつかない。
REDACTED = "***"

#: 投げ直してよい HTTP ステータス。**本文ではなくこれで判定する**（上の事情2）。
TRANSIENT_STATUS = frozenset({429, 500, 502, 503, 504})


class GeminiError(Exception):
    """このモジュールが出す失敗の共通の親。利用者にそのまま見せられる。"""


class AuthError(GeminiError):
    """資格情報まわりの失敗。"""


class ApiError(GeminiError):
    """API が失敗を返した、または答えが使えなかった。"""


# ------------------------------------------------------------------ 資格情報


def read_api_key(env: Mapping[str, str]) -> str:
    """API キーを読む。

    空文字・空白だけは「未設定」と同じ扱いにする。``GEMINI_API_KEY=`` と
    書いただけでもキーとしては存在するので、有無だけ見ると素通りして、
    後段の API が 400 を返し、原因がここだと分からなくなる。

    **形は検査しない。** 現行のキーは ``AQ.`` で始まる（2026-08-29 実測）が、
    それを仕様として明記した文書は確認していない。確かめていない形を検査に
    使うと、提供側が形を変えた日に**正しい値を拒む**側で壊れる
    （``line_auth.read_user_id`` で長さを検査しないと決めたのと同じ）。

    **値そのものは絶対にメッセージへ載せない**（壊れた値であっても）。
    打ち間違いなら本物がそのまま入っているし、この文言は公開する
    スクリーンショットに写る。
    """
    value = (env.get(API_KEY_ENV) or "").strip()

    if not value:
        raise AuthError(
            f"Gemini の API キーが設定されていません: {API_KEY_ENV}\n"
            "Google AI Studio (https://aistudio.google.com/apikey) で発行し、"
            "環境変数か .env に設定してください。"
        )

    return value


def redact(text: str, *secrets: str | None) -> str:
    """文字列から資格情報を伏せる。

    空や None の秘密は素通りさせる。``str.replace("", x)`` は**全部の文字の
    間に x を挿し込む**ので、素通りさせないと文章が壊れる。
    """
    for secret in secrets:
        if secret:
            text = text.replace(secret, REDACTED)
    return text


def build_client(api_key: str, *, factory: Callable = genai.Client):
    """クライアントを組む。**返り値は必ず変数で受けて使い回す**（上の事情1）。"""
    value = (api_key or "").strip()
    if not value:
        raise AuthError(f"API キーが空です。{API_KEY_ENV} を設定してください")
    return factory(api_key=value)


# ------------------------------------------------------------------ 呼び出しの設定


def build_config() -> types.GenerateContentConfig:
    """生成の設定を組む。

    **自動関数呼び出し（AFC）を切る。** ツールを1つも渡していないのに、
    SDK が stderr へ「Chat.send_message を使え」という勧告を出す
    （2026-08-29 実測）。要約にツールは使わないので、意味の無い行を
    実行画面に残さない。**実行画面は記事のスクリーンショットになる。**
    """
    return types.GenerateContentConfig(
        automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True)
    )


# ------------------------------------------------------------------ エラーの扱い


def is_transient(error: BaseException) -> bool:
    """投げ直してよい失敗かを答える。

    判定に使うのは **``code``（整数）と例外の型だけ**。本文の文字列は見ない
    （上の事情2）。``httpx.TransportError`` は ``OSError`` の仲間ではないので
    別に並べる（上の事情3）。
    """
    if isinstance(error, genai_errors.APIError):
        return error.code in TRANSIENT_STATUS
    return isinstance(error, (OSError, httpx.TransportError))


def translate_error(error: BaseException, api_key: str | None = None) -> ApiError:
    """SDK の例外を、利用者に見せられる文言に置き換える。

    **エラーコードは必ず出す。** 日本語の説明だけにすると公式資料を引けない。
    本文もそのまま載せる——404 のときに SDK が後継モデルを名指しすることがあり
    （2026-08-29 実測）、**こちらで要約するとその情報が消える**。

    ただし必ず ``redact()`` を通す。確認用に書いた短いコードほど素の例外を
    画面に出すので（課題6で実際に鍵を出した）、本番の経路の側で伏せる。
    """
    if isinstance(error, genai_errors.APIError):
        head = f"Gemini API がエラーを返しました: HTTP {error.code}"
    else:
        head = f"Gemini API の呼び出しに失敗しました: {type(error).__name__}"

    message = f"{head}\n詳細: {error}"

    if is_transient(error):
        message += "\n（一時的な失敗です。時間をおいて実行し直してください）"

    return ApiError(redact(message, api_key))


# ------------------------------------------------------------------ 生成


def generate(
    client,
    *,
    prompt: str,
    model: str = DEFAULT_MODEL,
    api_key: str | None = None,
) -> str:
    """本文を生成して返す。

    **入力が空なら呼ばない。** 空のまま投げても相手は何かを返すが、それは
    材料の無いところから作った文章＝発明である。呼ぶ前に止めれば、
    課金も発生しない。

    **空の答えを「できた」にしない。** 安全フィルタや打ち切りで本文が空に
    なることがある。空文字を返すと、呼ぶ側は「要約が空だった」ではなく
    「空という要約」を送ってしまう（課題6で踏んだ「空が正常値の欄は
    バグが静かな側に倒れる」と同じ形）。
    """
    if not (prompt or "").strip():
        raise ValueError("prompt が空です。要約する材料がありません")

    try:
        response = client.models.generate_content(
            model=model, contents=prompt, config=build_config()
        )
    except Exception as error:  # noqa: BLE001 - 訳して投げ直す
        raise translate_error(error, api_key) from error

    text = _text_of(response)
    if not text:
        raise ApiError(
            "Gemini が本文を返しませんでした。"
            "安全フィルタで止まったか、出力が打ち切られた可能性があります。"
        )

    return text


def _text_of(response: Any) -> str:
    """応答から本文を取り出す。None も空白だけも「無し」に寄せる。"""
    return (getattr(response, "text", None) or "").strip()
