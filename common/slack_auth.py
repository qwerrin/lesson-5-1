"""Slack の Bot Token 認証。課題をまたいで使う。

common/ の他の3つとは別モジュールにしてある。4つとも「認証」だが手順が違う。

===================== ================================================
何を使うか             どういう相手か
===================== ================================================
google_auth（OAuth）  **本人のデータ**を触る。同意画面 → token.json → リフレッシュ
zoom_auth（S2S）      アカウントの権限で動く。同意画面なし・毎回取り直し
youtube_auth（キー）  **公開データ**を読むだけ。認可する相手がいない
slack_auth（Bot）     **アプリ自身**が動く。インストール時に1回発行、期限なし
===================== ================================================

使う側::

    from common import slack_auth

    SCOPES = ("chat:write",)
    token = slack_auth.read_bot_token(os.environ)
    client = slack_auth.build_client(token)
    identity = slack_auth.fetch_identity(client)
    check = slack_auth.check_scopes(identity, SCOPES)

スコープに既定値は持たせない（zoom_auth・google_auth と同じ）。課題ごとに違う値で、
間違えると「権限が足りないまま動いているように見える」状態になるため、呼ぶ側に書かせる。

この課題に固有の事情が2つある。
------------------------------------------------------------------

**1. トークンは Authorization ヘッダで送る。URL には載らない。**

課題6（YouTube）の「API キーが URI に載るので、例外を印字した時点で漏れる」は
ここでは起きない。ただし *その漏れかたが* 起きないだけで、安全になったわけではない。
Slack SDK の例外は応答オブジェクトを抱えており、そこにヘッダが入る。
表に出す文字列は redact() を通す方針を変えない。

**2. 付与されたスコープが応答本文に入らない。**

zoom_auth は access_token と一緒に scope が返るので require_scopes() で確定できた。
Slack は HTTP ヘッダ ``x-oauth-scopes`` に入るとされるが、**公式のメソッド
リファレンスには記載がない**（2026-08-16 に chat.postMessage / conversations.history /
auth.test の3ページを読んで確認）。つまり「取れるかどうか自体が不確実」である。

そこで require_scopes()（足りなければ例外）ではなく check_scopes()（結果を返す）
にした。**読めなかったことを「足りている」に倒さない**のが要点で、
ScopeCheck.missing は読めなかったときに *要求したスコープを全部* 入れて返す。
呼び出し側が ``if check.missing:`` と書いても安全側に落ちる。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Iterable, Mapping

from slack_sdk import WebClient

BOT_TOKEN_ENV = "SLACK_BOT_TOKEN"

# Bot User OAuth Token の接頭辞。User Token は xoxp- で、**投稿自体は通ってしまう**。
# 通ってしまうからこそ、取り違えを検出できないと「アプリが投稿した」つもりで
# 本人名義の投稿ができあがる。
BOT_TOKEN_PREFIX = "xoxb-"

# 伏せたことが分かる印。空文字にすると「元から無かった」と区別がつかない。
REDACTED = "***"

# 付与済みスコープが載るとされる応答ヘッダ。HTTP のヘッダ名は大小を区別しないので、
# 読むときは小文字に畳んでから比べる。
SCOPE_HEADER = "x-oauth-scopes"


class AuthError(Exception):
    """利用者にそのまま見せられる認証まわりの失敗。"""


@dataclass(frozen=True)
class Identity:
    """auth.test が答えた「あなたは誰か」。

    user_id は **Bot 自身のユーザーID**で、投稿したメッセージの ``user`` と
    突き合わせる物差しになる。投稿の応答だけを見て「投稿できた」と判断すると、
    同じ応答の中で値を比べるトートロジーになる（課題4・課題6と同じ形）。
    別のエンドポイントから取ったこの値と比べて初めて閉じる。

    scopes は **None が「読めなかった」** で、空タプルは「0個だった」。
    区別できないと、後段の照合で「足りないものは無い」に化ける。
    """

    team: str
    team_id: str
    user_id: str
    bot_id: str
    scopes: tuple[str, ...] | None = None


@dataclass(frozen=True)
class ScopeCheck:
    """スコープが足りているかの判定結果。

    known が False のとき missing には *要求した全部* が入る。
    「確認できなかった」を「足りている」に倒さないための既定値で、
    呼び出し側が missing だけを見ても安全側に落ちる。
    理由の説明を分けたいときに known を見る。
    """

    known: bool
    missing: tuple[str, ...]
    granted: tuple[str, ...] | None = None


def read_bot_token(env: Mapping[str, str]) -> str:
    """環境変数から Bot Token を読む。

    空文字・空白だけは「未設定」と同じ扱いにする。``$env:SLACK_BOT_TOKEN = ""``
    と書いただけでも変数としては存在するので、有無だけ見ると素通りして、
    後段の API が invalid_auth を返し、原因がここだと分からなくなる。
    """
    value = (env.get(BOT_TOKEN_ENV) or "").strip()
    if not value:
        raise AuthError(
            f"Slack の Bot Token が設定されていません: {BOT_TOKEN_ENV}\n"
            "https://api.slack.com/apps でアプリの OAuth & Permissions を開き、"
            f"Bot User OAuth Token（{BOT_TOKEN_PREFIX}...）を環境変数に設定してください。"
            "手順は README を参照。\n"
            f'PowerShell: $env:{BOT_TOKEN_ENV} = "{BOT_TOKEN_PREFIX}..."'
        )

    if not value.startswith(BOT_TOKEN_PREFIX):
        # 値そのものは絶対に載せない（壊れた値であっても）。
        # このメッセージは公開されるスクリーンショットに写る。
        raise AuthError(
            f"{BOT_TOKEN_ENV} が Bot Token ではありません。"
            f"{BOT_TOKEN_PREFIX} で始まる値を設定してください。\n"
            "OAuth & Permissions の「Bot User OAuth Token」を使います"
            "（User OAuth Token（xoxp-）ではありません）。"
        )

    return value


def redact(text: str, token: str | None) -> str:
    """文字列からトークンを伏せる。

    トークンが空や None のときは何もしない。``str.replace("", x)`` は
    **全部の文字の間に x を挿し込む**ので、素通りさせると文章が壊れる。
    """
    if not token:
        return text
    return text.replace(token, REDACTED)


def build_client(token: str, *, factory: Callable = WebClient):
    """Slack Web API のクライアントを組む。"""
    value = (token or "").strip()
    if not value:
        # 空のまま組むと、実行時に invalid_auth が返って原因がここだと分からなくなる。
        raise AuthError(f"Bot Token が空です。{BOT_TOKEN_ENV} を設定してください")

    return factory(token=value)


def _read_scopes(headers) -> tuple[str, ...] | None:
    """応答ヘッダから付与済みスコープを読む。読めなければ None。

    **空タプルを返さない。** 「0個だった」と「ヘッダが無かった」は別のことで、
    混ぜると check_scopes が「足りないものは無い」と答えてしまう。

    最初に ``if not headers: return None`` というガードを置いていたが、
    **消しても動作が変わらない**ことがミューテーションで分かったので外した
    （空の dict ならループが回らず、下の return None に落ちる。None なら
    ``.items()`` が AttributeError を出して同じところへ落ちる）。
    テストを何件足しても kill できない種類の指摘で、**「そのガードを外しても
    結果が変わらない」＝ガードが仕事をしていない**は実行して初めて見える。
    """
    try:
        items = headers.items()
    except AttributeError:
        return None

    for key, value in items:
        if str(key).lower() != SCOPE_HEADER:
            continue
        return tuple(part.strip() for part in str(value).split(",") if part.strip())

    return None


def fetch_identity(client) -> Identity:
    """auth.test で「このトークンが誰なのか」を確かめる。

    auth.test は **スコープを要求しない**ので、権限の設定が済んでいなくても
    トークンの有効性だけは確認できる（2026-08-16 に公式リファレンスで確認）。
    最初の1回をここに置くと、invalid_auth と missing_scope を分けて報告できる。
    """
    response = client.auth_test()

    if not response.get("ok"):
        detail = response.get("error") or "(理由不明)"
        raise AuthError(
            f"Slack の認証に失敗しました: {detail}\n"
            f"{BOT_TOKEN_ENV} の値が正しいか、アプリがワークスペースに"
            "インストールされているかを確認してください。"
        )

    # **プレフィックス検査は形式しか見ていない。** ここが性質の検査になる。
    # User Token でも auth.test は成功するが、bot_id は返らない。
    bot_id = str(response.get("bot_id") or "").strip()
    if not bot_id:
        raise AuthError(
            "このトークンは Bot Token ではありません（auth.test が bot_id を返しませんでした）。\n"
            "OAuth & Permissions の「Bot User OAuth Token」を使ってください。"
        )

    # 投稿者の照合に使う物差し。空のまま進むと照合が素通りする。
    user_id = str(response.get("user_id") or "").strip()
    if not user_id:
        raise AuthError(
            "auth.test が user_id を返しませんでした。投稿者の照合ができないため中断します。"
        )

    return Identity(
        team=str(response.get("team") or ""),
        team_id=str(response.get("team_id") or ""),
        user_id=user_id,
        bot_id=bot_id,
        scopes=_read_scopes(getattr(response, "headers", None)),
    )


def check_scopes(identity: Identity, wanted: Iterable[str]) -> ScopeCheck:
    """権限が足りているかを判定する。足りない分だけを名指しして返す。"""
    wanted = tuple(wanted)
    if not wanted:
        raise AuthError("確認するスコープが空です。呼び出し側で指定してください")

    if identity.scopes is None:
        # 読めなかった。**「足りている」に倒さない。**
        return ScopeCheck(known=False, missing=wanted, granted=None)

    granted = set(identity.scopes)
    # 完全一致で見る。Slack には chat:write と **chat:write.public** の両方が実在し、
    # 前方一致で通すと「足りている」と報告したうえで呼び出しが missing_scope になる。
    # zoom_auth の meeting:read:meeting と同じ形。
    missing = tuple(scope for scope in wanted if scope not in granted)
    return ScopeCheck(known=True, missing=missing, granted=tuple(identity.scopes))
