"""common/env_file.py のテスト。

課題9だけ、資格情報の渡し方を変えている。

===================== ================================================
課題                   渡し方
===================== ================================================
課題1〜3（Google）     credentials.json / token.json（ファイル）
課題4（Zoom）          PowerShell の環境変数 ``$env:ZOOM_...``
課題5〜8               同上（``$env:SLACK_...`` / ``$env:DISCORD_...``）
**課題9（LINE）**      **``.env`` ファイル ＋ python-dotenv**
===================== ================================================

変えた理由は「同じやり方を9回繰り返しても新しく分かることが無い」から。
そして実際、**このやり方には前8課題に無い失敗の形があった**。

``dotenv.dotenv_values()`` は存在しないファイルを渡されても
**例外も警告も出さず、空の辞書を返す**（2026-08-19 に python-dotenv 1.2.3 で実測）::

    >>> dotenv.dotenv_values("does-not-exist.env")
    OrderedDict()

これは課題9の記事の主題そのものである。``.env`` の名前を打ち間違えても、
置く場所を間違えても、**読み込みは成功したように見える**。失敗が現れるのは
ずっと後、``read_channel_access_token()`` が「設定されていません」と言うときで、
そこには「``.env`` を読めていない」という本当の原因が出てこない。

**このモジュールの仕事は、その沈黙を例外に変えることだけ。**
パースそのものは python-dotenv に任せる（クォート・コメント・``export`` 前置の
処理を自前で書くと、ライブラリと違う壊れ方をする箇所が増えるだけ）。

**os.environ は触らない。** ``load_dotenv()` を使わず ``dotenv_values()`` を使うのは
このため。前8課題で ``$env:`` に設定した値がプロセスに残っていても、
``.env`` の内容と混ざらない。python-dotenv の ``load_dotenv()`` は既定が
``override=False`` なので、**古い環境変数のほうが勝つ**。``.env`` を直したのに
効かない、という無言の事故はこれで起きる。読み込みと適用を混ぜない。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from common import env_file  # noqa: E402


def write_env(tmp_path: Path, body: str) -> Path:
    path = tmp_path / ".env"
    path.write_text(body, encoding="utf-8")
    return path


# ------------------------------------------------------------------ 沈黙を潰す


def test_missing_file_raises_instead_of_returning_empty(tmp_path):
    """ファイルが無いときに例外を出す。**ここがこのモジュールの存在理由。**

    素の dotenv_values は空の辞書を返して黙る。空を返すと、後段の
    read_channel_access_token() が「トークンが設定されていません」と言い、
    利用者は環境変数の設定を疑い始める。**本当の原因は .env が無いこと**なのに、
    その情報がどこにも出てこない。
    """
    with pytest.raises(env_file.EnvFileError) as error:
        env_file.load(tmp_path / ".env")

    message = str(error.value)
    # **「不在」と名指しできていることまで見る。** ここを ``EnvFileError が出た``
    # だけで済ませると、下の「読めたが0件」の検査に肩代わりされても気づけない。
    # 実際、is_file() の検査を消しても全テストが通った（2026-08-19・ミューテーションで検出）。
    # ファイルが無ければ dotenv_values は空を返すので、0件の側で例外になるため。
    # **落ちてはいるが理由が違う**——課題8で8件踏んだのと同じ形。
    assert "見つかりません" in message
    # 「どうすれば直るか」まで書く。存在しないことだけ言われても次の手が分からない。
    assert ".env.example" in message


def test_missing_file_message_contains_the_path_it_looked_for(tmp_path):
    """探した場所を出す。**「.env が無い」だけでは、どこを探したのか分からない。**

    dotenv の既定はカレントディレクトリからの探索なので、実行した場所が違うと
    「あるのに無いと言われる」ことが起きる。パスを出せば一目で分かる。
    """
    target = tmp_path / "sub" / ".env"

    with pytest.raises(env_file.EnvFileError) as error:
        env_file.load(target)

    assert str(target) in str(error.value)


def test_directory_instead_of_file_raises(tmp_path):
    """ディレクトリを渡されたら例外。

    ``.env`` という名前のフォルダを作ってしまう事故は実在する。
    open() まで進ませると OS 依存のエラーになり、原因が読めない。
    """
    target = tmp_path / ".env"
    target.mkdir()

    with pytest.raises(env_file.EnvFileError) as error:
        env_file.load(target)

    # **「ディレクトリだ」と言えていることまで見る。** この検査を消しても
    # is_file() が False なので「見つかりません」で例外にはなり、
    # ``EnvFileError が出た`` だけの検査では素通りする（2026-08-19・実測）。
    # そして「見つかりません」は**目の前に見えているのに見つからないと言われる**、
    # いちばん人を迷わせる文言になる。
    assert "ディレクトリ" in str(error.value)


def test_file_with_no_keys_raises(tmp_path):
    """読めたが 0 件なら例外。

    コメントだけの .env、空の .env はどちらも「設定したつもり」の状態である。
    0 件を成功として返すと、ファイルが無い場合とまったく同じ失敗の仕方をする
    （＝せっかく例外にした意味が消える）。
    """
    path = write_env(tmp_path, "# 何も設定していない\n\n")

    with pytest.raises(env_file.EnvFileError) as error:
        env_file.load(path)

    # 「不在」とは別の文言であることを固定する。2つの失敗が同じ顔をしていると、
    # 片方の検査を消しても誰も気づかない（上のテストのコメント参照）。
    assert "1件も読めませんでした" in str(error.value)


# ------------------------------------------------------------------ 読めるものは読む


def test_reads_plain_values(tmp_path):
    path = write_env(tmp_path, "LINE_CHANNEL_ACCESS_TOKEN=abc123\nLINE_USER_ID=U0000\n")

    assert env_file.load(path) == {
        "LINE_CHANNEL_ACCESS_TOKEN": "abc123",
        "LINE_USER_ID": "U0000",
    }


def test_quotes_and_comments_and_export_follow_the_library(tmp_path):
    """クォート剥がし・コメント・``export`` 前置は python-dotenv の挙動に従う。

    **自前で実装しない。** ここを自分で書くと、ライブラリとは違う壊れ方をする
    箇所が増えるだけで、得るものが無い。このテストは「ライブラリに任せている」
    ことを固定するために置く（将来ライブラリを差し替えたら、ここが落ちる）。
    """
    path = write_env(
        tmp_path,
        '# コメント行\n'
        'A=plain\n'
        'B="quoted"\n'
        'export C=exported\n',
    )

    assert env_file.load(path) == {"A": "plain", "B": "quoted", "C": "exported"}


def test_surrounding_whitespace_is_stripped_by_the_library(tmp_path):
    """``C=  spaced  `` は ``spaced`` になる（2026-08-19 実測）。

    課題4（Zoom）で「コピペすると末尾に空白や改行が付く」を踏んでいる。
    .env 方式では**ライブラリが先に落としてくれる**ので、同じ事故は起きない。
    起きないことを測っておかないと、下流で二重に strip する無駄なコードが残る。
    """
    path = write_env(tmp_path, "A=  spaced  \n")

    assert env_file.load(path) == {"A": "spaced"}


def test_key_without_value_becomes_empty_string(tmp_path):
    """``KEY``（``=`` なし）は空文字にする。

    python-dotenv はこれを **None** で返す。None のまま下流へ渡すと
    ``(env.get(...) or "").strip()`` は動くが、型が Mapping[str, str] でなくなり、
    「値が無い」と「キーが無い」の2種類の欠け方を下流に押し付けることになる。
    **空文字に寄せて、欠け方を1種類にする。**
    空文字は read_channel_access_token() が「設定されていません」で捕まえる。
    """
    path = write_env(tmp_path, "LINE_USER_ID\nLINE_CHANNEL_ACCESS_TOKEN=abc\n")

    assert env_file.load(path) == {
        "LINE_USER_ID": "",
        "LINE_CHANNEL_ACCESS_TOKEN": "abc",
    }


def test_empty_assignment_is_kept_as_empty_string(tmp_path):
    """``KEY=`` も空文字。**キーごと消さない。**

    消すと「書いていない」と「書いたが空」の区別が失われる。
    課題4・課題8で踏んだ「変数としては存在するので有無だけ見ると素通りする」の
    裏返しで、ここで潰しておくと下流のメッセージを正確にできる。
    """
    path = write_env(tmp_path, "LINE_USER_ID=\nLINE_CHANNEL_ACCESS_TOKEN=abc\n")

    assert env_file.load(path)["LINE_USER_ID"] == ""


def test_dollar_brace_is_not_expanded_from_os_environ(tmp_path, monkeypatch):
    """``${VAR}`` を展開しない。**このモジュールの「os.environ を触らない」を守る要。**

    python-dotenv の既定は ``interpolate=True`` で、**os.environ から値を持ってくる**
    （2026-08-19 実測）::

        os.environ["LEAK_PROBE"] = "FROM_OS_ENVIRON"
        dotenv_values(path)                     -> {'A': 'FROM_OS_ENVIRON/tail'}
        dotenv_values(path, interpolate=False)  -> {'A': '${LEAK_PROBE}/tail'}

    既定のままだと「.env だけを見れば何が渡るか分かる」という性質が壊れる。
    プロセスに残った前の課題の変数が値の一部に混ざっても、**.env を読んでも気づけない**。
    展開を切って、書いたとおりに読む。
    """
    monkeypatch.setenv("LEAK_PROBE", "FROM_OS_ENVIRON")
    path = write_env(tmp_path, "A=${LEAK_PROBE}/tail\n")

    assert env_file.load(path) == {"A": "${LEAK_PROBE}/tail"}


# ------------------------------------------------------------------ 返り値の性質


def test_returns_a_plain_dict_that_callers_may_not_share(tmp_path):
    """呼び出しごとに独立した dict を返す。

    返した辞書を呼び出し側が書き換えても、次の load() に影響してはいけない。
    dotenv_values は OrderedDict を返すので、そのまま返すと型も揺れる。
    """
    path = write_env(tmp_path, "A=1\n")

    first = env_file.load(path)
    first["A"] = "書き換えた"
    first["B"] = "足した"

    assert env_file.load(path) == {"A": "1"}
    assert type(env_file.load(path)) is dict


def test_accepts_a_string_path(tmp_path):
    """str でも Path でも受ける。呼び出し側に変換を強いない。"""
    path = write_env(tmp_path, "A=1\n")

    assert env_file.load(str(path)) == {"A": "1"}


# ------------------------------------------------------------------ 値を漏らさない


def test_error_messages_never_contain_the_file_contents(tmp_path):
    """例外に .env の中身を載せない。

    このメッセージは実行画面のスクリーンショットとして記事に載る。
    「読めたが 0 件」の判定で中身を出すと、**中身がある場合には出さない**という
    非対称な作りになり、いつか出る側に倒れる。最初から一度も載せない。
    """
    path = write_env(tmp_path, "# コメントだけ\n")

    with pytest.raises(env_file.EnvFileError) as error:
        env_file.load(path)

    assert "コメントだけ" not in str(error.value)


def test_default_filename_is_dot_env():
    """既定のファイル名を定数で持つ。呼び出し側に文字列を散らさない。"""
    assert env_file.ENV_FILENAME == ".env"
