""".env ファイルを読む。**課題9だけこの渡し方を使う。**

課題4〜8 は PowerShell の環境変数（``$env:DISCORD_BOT_TOKEN = "..."``）で
資格情報を渡していた。課題9では ``.env`` ファイルに変える。同じやり方を9回
繰り返しても新しく分かることが無いのと、**このやり方には前8課題に無い
失敗の形がある**ため。

使う側::

    from pathlib import Path
    from common import env_file, line_auth

    ROOT = Path(__file__).resolve().parents[1]
    env = env_file.load(ROOT / env_file.ENV_FILENAME)
    token = line_auth.read_channel_access_token(env)

このモジュールが引き受ける仕事は2つだけである。

**1. 沈黙を例外に変える。**

``dotenv.dotenv_values()`` は存在しないファイルを渡されても
**例外も警告も出さず空の辞書を返す**（python-dotenv 1.2.3 で実測）::

    >>> dotenv_values("does-not-exist.env")
    OrderedDict()

空が返ると、後段の ``read_channel_access_token()`` が
「トークンが設定されていません」と言う。利用者は環境変数の設定を疑い始めるが、
**本当の原因は .env を読めていないこと**で、その情報がどこにも出てこない。
「エラーにならない失敗」の教科書的な形なので、ここで止める。

**2. os.environ と混ぜない。**

``load_dotenv()`` ではなく ``dotenv_values()`` を使う。``load_dotenv()`` は
既定が ``override=False`` で、**プロセスに残っている古い環境変数のほうが勝つ**。
課題4〜8で ``$env:`` に設定した値が同じシェルに残っていると、``.env`` を直しても
効かない、という無言の事故が起きる。

さらに ``dotenv_values()`` の既定 ``interpolate=True`` は ``${VAR}`` を
**os.environ から**展開する（実測）。切らないと「.env だけを見れば何が渡るか
分かる」性質が壊れるので、``interpolate=False`` を固定する。

**引き受けない仕事**

- パースそのもの（クォート剥がし・コメント・``export`` 前置）は python-dotenv に任せる。
  自前で書くと、ライブラリとは違う壊れ方をする箇所が増えるだけで得るものが無い。
- **キーの重複検査はしない。** ``dotenv_values`` は辞書を返すので、同じキーが
  2回書かれていても後勝ちで畳まれ、こちらからは見えない。行を自前で数えれば
  検出できるが、複数行にまたがるクォート値の中身を代入行と誤認する余地がある。
  **誤検知で正しい .env を拒むほうが害が大きい**ので、仕様として受け入れる
  （課題7・課題8で「外しても結果が変わらないガード」を消したのと同じ判断）。
"""

from __future__ import annotations

from pathlib import Path

from dotenv import dotenv_values

#: 既定のファイル名。呼び出し側に文字列を散らさない。
ENV_FILENAME = ".env"

#: 見本ファイルの名前。エラーメッセージから案内する。
EXAMPLE_FILENAME = ".env.example"


class EnvFileError(Exception):
    """.env を読めなかった。利用者にそのまま見せられる。"""


def load(path: str | Path) -> dict[str, str]:
    """``.env`` を読んで辞書で返す。**os.environ は読み書きしない。**

    値が無いキー（``KEY`` や ``KEY=``）は**空文字**にして残す。キーごと消すと
    「書いていない」と「書いたが空」の区別が失われる。空文字は下流の
    ``read_channel_access_token()`` が「設定されていません」で捕まえる。

    :raises EnvFileError: ファイルが無い / ディレクトリ / 1件も読めなかった
    """
    target = Path(path)

    if target.is_dir():
        raise EnvFileError(
            f"{target} はディレクトリです。{ENV_FILENAME} はファイルとして作成してください。"
        )

    if not target.is_file():
        raise EnvFileError(
            f"{ENV_FILENAME} が見つかりません: {target}\n"
            f"{EXAMPLE_FILENAME} をコピーして値を埋めてください。\n"
            f"  copy {EXAMPLE_FILENAME} {ENV_FILENAME}\n"
            "（このファイルは .gitignore で追跡対象から外れています）"
        )

    # interpolate=False は必須。既定の True は ${VAR} を os.environ から展開する。
    raw = dotenv_values(target, encoding="utf-8", interpolate=False)

    # None（``KEY`` と書いて ``=`` が無い行）を空文字に寄せて、欠け方を1種類にする。
    values = {key: ("" if value is None else value) for key, value in raw.items()}

    if not values:
        # 中身は載せない。この文言は実行画面のスクリーンショットとして記事に載る。
        raise EnvFileError(
            f"{target} から設定を1件も読めませんでした。\n"
            f"空か、コメント行だけになっています。{EXAMPLE_FILENAME} を参照してください。"
        )

    return values
