"""OAuth 2.0 の同意・トークン保存・リフレッシュ。課題をまたいで使う。

**Section 4-3 の lesson-4-3-2 から、コードを1行も変えずに移した。**

なぜコピーなのか
------------------------------------------------------------------

Section 4-3 では課題1（Drive）と課題2（Docs）で同じ内容を2回書き、
呼ぶ API が変わってもここは変わらなかったので課題3から共有部品にした。
そこで**合格した課題3・5・6・10 が実際に通した**コードである。

Section 5-1 はリポジトリが分かれるので import では持ってこられない。
``common/line_send.py`` と同じ判断で、**中身を変えずにコピーする**。
書き直すと「新しい実装から期待値を作る」形になり、実機で1度通った知識が失われる。

**テストも一緒に移した**（``common/tests/test_google_auth.py``・14件）。
共有モジュールに自前のテストが無いと、壊しても誰も気づかない。

移植にあたって変えたのはこの docstring だけ。実装は
``diff lesson-4-3-2/common/google_auth.py lesson-5-1/common/google_auth.py``
の docstring 部分以外が空になることで確認している。

課題2（EC → スプレッドシート）では Sheets のスコープを渡して使う。
**スコープに既定値を持たせていないので、呼ぶ側が必ず書く**（下記）。

使う側は必要なスコープを渡す::

    from common import google_auth

    SCOPES = ("https://www.googleapis.com/auth/meetings.space.created",)
    credentials = google_auth.load_credentials("credentials.json", "token.json", SCOPES)

保存済みトークンの権限が足りなければ、同意画面を開き直す。
手で token.json を消す必要はない。
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Iterable

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow


class AuthError(Exception):
    """利用者にそのまま見せられる認証まわりの失敗。"""


def _default_refresher(credentials: Credentials) -> None:
    credentials.refresh(Request())


def save_token(token_path: str | Path, credentials: Credentials) -> None:
    token_path = Path(token_path)
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(credentials.to_json(), encoding="utf-8")


def read_token(token_path: str | Path) -> Credentials | None:
    """保存済みトークンを読む。読めなければ捨てる（取り直せばいいので落とさない）。"""
    token_path = Path(token_path)
    if not token_path.exists():
        return None
    try:
        # scopes を渡さないこと。渡すとファイルに書かれた実際の権限が
        # 引数で上書きされ、権限不足を検出できなくなる。
        return Credentials.from_authorized_user_file(str(token_path))
    except (ValueError, UnicodeDecodeError):
        return None


def load_credentials(
    credentials_path: str | Path,
    token_path: str | Path,
    scopes: Iterable[str],
    *,
    flow_factory: Callable = InstalledAppFlow.from_client_secrets_file,
    refresher: Callable[[Credentials], None] = _default_refresher,
) -> Credentials:
    """使える資格情報を返す。足りなければ同意画面を開いて取り直す。

    scopes は既定値を持たせない。課題ごとに違う値で、間違えると
    「権限が足りないまま動いているように見える」状態になるため、
    呼ぶ側に必ず書かせる。
    """
    credentials_path = Path(credentials_path)
    token_path = Path(token_path)
    wanted = list(scopes)
    if not wanted:
        raise AuthError("要求するスコープが空です。呼び出し側で指定してください")

    credentials = read_token(token_path)
    if credentials is not None and not credentials.has_scopes(wanted):
        # 前の課題の token.json は別の権限しか持っていない。捨てて取り直す。
        credentials = None

    if credentials is not None:
        if credentials.valid:
            return credentials
        if credentials.expired and credentials.refresh_token:
            refresher(credentials)
            save_token(token_path, credentials)
            return credentials

    if not credentials_path.exists():
        raise AuthError(
            f"credentials.json が見つかりません: {credentials_path}\n"
            "Google Cloud コンソールで OAuth 2.0 クライアント ID（デスクトップアプリ）を作り、"
            "ダウンロードした JSON をこのパスに置いてください。手順は README を参照。"
        )

    flow = flow_factory(str(credentials_path), wanted)
    credentials = flow.run_local_server(port=0)
    save_token(token_path, credentials)
    return credentials
