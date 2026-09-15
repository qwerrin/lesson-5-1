"""task3/notify のテスト。**実装より先に書いた。**

発展機能（決定事項だけを LINE に流す）。**本体で見つけた穴を、そのまま当て直す。**

============ ====================================================================
DESIGN の項目 ここで守ること
============ ====================================================================
5-O          **0件**と**読めていない**を分ける。0 と 0 を比べれば必ず一致する
5-H          同じ通知を二度送らない。無料プランの通数は月200通しかない
5-P          決定の根拠（時刻と逐語）を本文に載せる。要約だけ配らない
============ ====================================================================

**LINE は送ったものを読み返せない**（`common/line_send.py` の冒頭に、公式の
OpenAPI 定義まで当たった経緯がある）。だから「送れた」の証拠は間接材料しかない。
ここでは**通数の増分が記録に残ること**まで見る。

**リンクは手で渡させない。** `--doc-url` のような引数を作ると、
*書き出したのとは別のドキュメントを指す通知*が送れてしまう。どちらも成功するので
見た目では気づけない（4-3-2 課題1 で実際に踏んだ形）。`to_doc.py` が書いた台帳を
**議事録本文のハッシュで引く**ことで、通知と実物を機械的に結ぶ。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "task3"))

import notify  # noqa: E402

NL = chr(10)

#: `to_doc.py` が実機で作った台帳と同じ形。ID は図版 02 に写っているものと同じ。
DOC_ID = "1cTNgXBz1sZRFb2GyQq9A7E_JFEkGUWeeFxOpSIiynXU"
DOC_URL = "https://docs.google.com/document/d/" + DOC_ID + "/edit"
DOC_TITLE = "議事録 週次定例（EC運営） 2026-09-12 10:00"

BODY_TEXT = "週次定例（EC運営）" + NL + "1. 決定事項" + NL + "  1. 初回発注は1800個に変更する"

MESSAGE_ID = "500000000000"

TWO_DECISIONS = {
    "decisions": [
        {
            "text": "初回発注は1800個に変更する",
            "at": "02:36",
            "quote": "では初回は1800個に変更でお願いいします",
            "source": "audio",
        },
        {
            "text": "大和物産に切り替える",
            "at": "03:35",
            "quote": "大和物産に切り替えます",
            "source": "audio",
        },
    ],
    "todos": [{"text": "契約書の準備を手配する"}],
    "open_issues": [{"text": "値上げのタイミング"}],
}


# ------------------------------------------------------------------ 偽物


class FakeResponse:
    def __init__(self, *, status_code=200, payload=None, headers=None):
        self.status_code = status_code
        self._payload = payload
        self.text = ""
        self.headers = headers if headers is not None else {}

    def json(self):
        if self._payload is None:
            raise ValueError("JSON ではありません")
        return self._payload


class FakeLineSession:
    """push と通数の読み取りを持つ。**課題1 のテストの偽物と同じ形。**

    通数は**送信のたびに増える**ようにしてある。固定値にすると
    「増えたことを確かめた」というテストが、増えていなくても通ってしまう。
    """

    def __init__(self, *, push_fails=False, usage=40):
        self.pushes: list = []
        self.push_fails = push_fails
        self.usage = usage
        self.usage_calls = 0
        self.headers: dict = {}

    def post(self, url, **kwargs):
        self.pushes.append((url, kwargs))
        if self.push_fails:
            return FakeResponse(status_code=500, payload={"message": "boom"})
        self.usage += 1
        return FakeResponse(
            payload={"sentMessages": [{"id": MESSAGE_ID, "quoteToken": "q"}]},
            headers={"x-line-request-id": "req-1"},
        )

    def get(self, url, **kwargs):
        self.usage_calls += 1
        return FakeResponse(payload={"totalUsage": self.usage})


@pytest.fixture
def meeting(tmp_path):
    """実機と同じ並びの一式を作る。minutes.json / minutes.txt / posted.json。"""
    folder = tmp_path / "meeting"
    folder.mkdir()
    (folder / "minutes.json").write_text(
        json.dumps(TWO_DECISIONS, ensure_ascii=False), encoding="utf-8"
    )
    (folder / "minutes.txt").write_text(BODY_TEXT, encoding="utf-8")
    import to_doc

    (folder / "posted.json").write_text(
        json.dumps(
            {
                to_doc.content_hash(BODY_TEXT): {
                    "documentId": DOC_ID,
                    "title": DOC_TITLE,
                    "url": DOC_URL,
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return folder


# ------------------------------------------------------------------ 5-O 読めていないことと、0件を分ける


def test_決定事項のキーが無ければ送らない():
    """**読めていない**ことを「決定0件」に倒さない。

    倒すと、議事録の形が変わった日に「何も決まらなかった会議」として
    静かに配られる。*空と、読めていないは別の話*。
    """
    with pytest.raises(notify.NotifyError):
        notify.build_body({}, doc_title=DOC_TITLE, doc_url=DOC_URL)


def test_決定事項がリストでなければ送らない():
    with pytest.raises(notify.NotifyError):
        notify.build_body(
            {"decisions": "初回発注は1800個"}, doc_title=DOC_TITLE, doc_url=DOC_URL
        )


def test_決定が0件なら0件と書いて送る():
    """**決まらなかった会議も情報**なので送る。ただし本文に0件と書く。"""
    body = notify.build_body({"decisions": []}, doc_title=DOC_TITLE, doc_url=DOC_URL)
    assert "0 件" in body
    assert "決定事項なし" in body


# ------------------------------------------------------------------ 5-P 根拠を載せる


def test_決定の本文に時刻と逐語が入る():
    body = notify.build_body(TWO_DECISIONS, doc_title=DOC_TITLE, doc_url=DOC_URL)
    assert "初回発注は1800個に変更する" in body
    assert "02:36" in body
    assert "では初回は1800個に変更でお願いいします" in body


def test_逐語が空なら逐語なしと明記する():
    """**黙って落とさない。** 根拠が無いことは、根拠があることと同じ見た目にしない。"""
    body = notify.build_body(
        {"decisions": [{"text": "切り替える", "at": "03:35", "quote": ""}]},
        doc_title=DOC_TITLE,
        doc_url=DOC_URL,
    )
    assert "切り替える" in body
    assert "逐語なし" in body


def test_時刻が無ければ不明と書く():
    body = notify.build_body(
        {"decisions": [{"text": "切り替える", "quote": "切り替えます"}]},
        doc_title=DOC_TITLE,
        doc_url=DOC_URL,
    )
    assert "不明" in body


def test_本文が空の決定も見える形で残す():
    """要約が空の項目を作ることがある。**数から消さない。**"""
    body = notify.build_body(
        {"decisions": [{"text": "", "at": "01:00", "quote": "あー"}]},
        doc_title=DOC_TITLE,
        doc_url=DOC_URL,
    )
    assert "1 件" in body
    assert "本文なし" in body


def test_件数は決定の数と一致する():
    body = notify.build_body(TWO_DECISIONS, doc_title=DOC_TITLE, doc_url=DOC_URL)
    assert "2 件" in body


# ------------------------------------------------------------------ 抜くと落ちる文脈


def test_全文のリンクを本文に入れる():
    """決定だけ抜くと文脈が落ちる。**開ける場所を必ず添える。**"""
    body = notify.build_body(TWO_DECISIONS, doc_title=DOC_TITLE, doc_url=DOC_URL)
    assert DOC_URL in body


def test_この通知が含まないものを本文に書く():
    """DESIGN 6章の方針（承知で残した穴を出口に書く）を、この通知にも当てる。"""
    body = notify.build_body(TWO_DECISIONS, doc_title=DOC_TITLE, doc_url=DOC_URL)
    assert "TODO" in body
    assert "画面共有" in body


def test_会議の名前が本文に入る():
    body = notify.build_body(TWO_DECISIONS, doc_title=DOC_TITLE, doc_url=DOC_URL)
    assert DOC_TITLE in body


# ------------------------------------------------------------------ リンクは台帳から引く


def test_書き出していない議事録は通知しない(meeting, capsys):
    """台帳に無い＝**まだ Google ドキュメントに無い**。

    読み手が全文を開けない通知を配らない。しかも「どの文書の決定か」を
    こちらも言えていない状態である。

    **「台帳が無い」とは別の失敗**なので、出る文言も分ける。同じ文言にすると、
    片方のガードを外しても検査が気づかない（どちらも 1 で止まるため）。
    """
    (meeting / "minutes.txt").write_text("別の議事録", encoding="utf-8")
    session = FakeLineSession()
    code = notify.main(
        [str(meeting / "minutes.json")], connect=lambda: (session, "U0", ())
    )
    assert code == 1
    assert session.pushes == []
    assert "ハッシュ" in capsys.readouterr().err


def test_リンクは台帳から引く(meeting):
    session = FakeLineSession()
    code = notify.main(
        [str(meeting / "minutes.json")], connect=lambda: (session, "U0", ())
    )
    assert code == 0
    sent_text = session.pushes[0][1]["json"]["messages"][0]["text"]
    assert DOC_URL in sent_text


# ------------------------------------------------------------------ 5-H 二度送らない


def test_同じ通知は二度送らない(meeting):
    """2回目は**通数を1通も使わない**。"""
    session = FakeLineSession()
    connect = lambda: (session, "U0", ())  # noqa: E731
    assert notify.main([str(meeting / "minutes.json")], connect=connect) == 0
    assert notify.main([str(meeting / "minutes.json")], connect=connect) == 3
    assert len(session.pushes) == 1


def test_forceなら二度目も送る(meeting):
    session = FakeLineSession()
    connect = lambda: (session, "U0", ())  # noqa: E731
    notify.main([str(meeting / "minutes.json")], connect=connect)
    assert notify.main([str(meeting / "minutes.json"), "--force"], connect=connect) == 0
    assert len(session.pushes) == 2


def test_送信に失敗したら台帳に残さない(meeting):
    """**台帳に残すのは成功したあと。** 先に残すと、送れていない通知を
    「送った」と信じ続け、再実行しても二度と送らない。"""
    session = FakeLineSession(push_fails=True)
    code = notify.main(
        [str(meeting / "minutes.json")], connect=lambda: (session, "U0", ())
    )
    assert code == 1
    assert not (meeting / "notified.json").exists()


# ------------------------------------------------------------------ 記録に残す材料


def test_通数の増分を記録に残す(meeting):
    """LINE は読み返せない。**増分は「別のエンドポイントが認めた」という材料**。"""
    session = FakeLineSession(usage=40)
    notify.main([str(meeting / "minutes.json")], connect=lambda: (session, "U0", ()))
    ledger = json.loads((meeting / "notified.json").read_text(encoding="utf-8"))
    entry = list(ledger.values())[0]
    assert entry["usageBefore"] == 40
    assert entry["usageAfter"] == 41


def test_メッセージIDを記録に残す(meeting):
    session = FakeLineSession()
    notify.main([str(meeting / "minutes.json")], connect=lambda: (session, "U0", ()))
    ledger = json.loads((meeting / "notified.json").read_text(encoding="utf-8"))
    assert list(ledger.values())[0]["messageId"] == MESSAGE_ID


def test_宛先は伏せて記録する(meeting):
    """記録は public リポジトリの中に置かないが、**置かれても困らない形**にする。"""
    session = FakeLineSession()
    notify.main(
        [str(meeting / "minutes.json")],
        connect=lambda: (session, "U1234567890abcdef", ()),
    )
    raw = (meeting / "notified.json").read_text(encoding="utf-8")
    assert "U1234567890abcdef" not in raw
    assert "…" in raw


def test_決定の件数を記録に残す(meeting):
    session = FakeLineSession()
    notify.main([str(meeting / "minutes.json")], connect=lambda: (session, "U0", ()))
    ledger = json.loads((meeting / "notified.json").read_text(encoding="utf-8"))
    assert list(ledger.values())[0]["decisions"] == 2


# ------------------------------------------------------------------ dry-run


def test_dry_runは送らない(meeting):
    session = FakeLineSession()
    code = notify.main(
        [str(meeting / "minutes.json"), "--dry-run"],
        connect=lambda: (session, "U0", ()),
    )
    assert code == 0
    assert session.pushes == []


def test_dry_runは台帳を書かない(meeting):
    session = FakeLineSession()
    notify.main(
        [str(meeting / "minutes.json"), "--dry-run"],
        connect=lambda: (session, "U0", ()),
    )
    assert not (meeting / "notified.json").exists()


def test_dry_runは接続もしない(meeting):
    """**送信だけをしない、ではなく繋ぎもしない。** 資格情報が無い環境でも
    本文を確かめられるようにする（「dry-run」という語は何をしないかまで言っていない）。"""

    def explode():
        raise AssertionError("dry-run で接続してはいけない")

    assert (
        notify.main([str(meeting / "minutes.json"), "--dry-run"], connect=explode) == 0
    )


def test_dry_runでも本文を画面に出す(meeting, capsys):
    notify.main(
        [str(meeting / "minutes.json"), "--dry-run"],
        connect=lambda: (session_unused(), "U0", ()),
    )
    out = capsys.readouterr().out
    assert "初回発注は1800個に変更する" in out


def session_unused():
    raise AssertionError("dry-run で接続してはいけない")


# ------------------------------------------------------------------ 入口の守り


def test_議事録が無ければ1を返す(tmp_path):
    session = FakeLineSession()
    code = notify.main(
        [str(tmp_path / "ない.json")], connect=lambda: (session, "U0", ())
    )
    assert code == 1
    assert session.pushes == []


def test_議事録がJSONでなければ送らない(meeting):
    (meeting / "minutes.json").write_text("これは JSON ではない", encoding="utf-8")
    session = FakeLineSession()
    code = notify.main(
        [str(meeting / "minutes.json")], connect=lambda: (session, "U0", ())
    )
    assert code == 1
    assert session.pushes == []


def test_書き出し台帳が無ければ送らない(meeting, capsys):
    """**台帳が無い**と、**台帳にこの議事録が無い**は別の失敗。文言で分ける。"""
    (meeting / "posted.json").unlink()
    session = FakeLineSession()
    code = notify.main(
        [str(meeting / "minutes.json")], connect=lambda: (session, "U0", ())
    )
    assert code == 1
    assert session.pushes == []
    assert "台帳がありません" in capsys.readouterr().err


def test_本文ファイルが無ければ送らない(meeting, capsys):
    """**リンクを引く鍵が本文のハッシュ**なので、本文が無ければ何も言えない。"""
    (meeting / "minutes.txt").unlink()
    session = FakeLineSession()
    code = notify.main(
        [str(meeting / "minutes.json")], connect=lambda: (session, "U0", ())
    )
    assert code == 1
    assert session.pushes == []
    assert "本文が見つかりません" in capsys.readouterr().err


def test_通知台帳が壊れていても送るが警告する(meeting, capsys):
    """止めると、台帳が壊れた日から1通も送れなくなる。
    ただし**そのあいだ二重送信の検査は効いていない**ので、黙らせない。"""
    (meeting / "notified.json").write_text("壊れている", encoding="utf-8")
    session = FakeLineSession()
    code = notify.main(
        [str(meeting / "minutes.json")], connect=lambda: (session, "U0", ())
    )
    assert code == 0
    assert "重複" in capsys.readouterr().err
