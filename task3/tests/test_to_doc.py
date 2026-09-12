"""task3/to_doc のテスト。**実装より先に書いた。**

この層が引き受けるのは「**同じ会議を二度書く**」失敗（5-H）と、
「**空のドキュメントだけが残る**」失敗である。どちらも例外にならない。

============ ====================================================================
DESIGN の項目 ここで守ること
============ ====================================================================
5-H          同じ内容を二度書かない。台帳に残して、次に来たら止める
3.2          空の本文で API を呼ばない。呼ぶと空のドキュメントだけが残る
============ ====================================================================

**ドキュメントは会議1本につき1つ新規作成する**（DESIGN 2章）。追記にすると
5-H が「既存の内容を壊す」形で出る。だから重複の判定は**内容のハッシュ**で行う。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "task3"))

import to_doc  # noqa: E402
from common import docs_client  # noqa: E402

NL = chr(10)
BODY = "週次定例" + NL + "1. 決定事項" + NL + "  1. 初回発注は1800個"


class FakeDocuments:
    def __init__(self, create_raises=None, batch_raises=None):
        self.create_calls: list[dict] = []
        self.batch_calls: list[dict] = []
        self.create_raises = create_raises
        self.batch_raises = batch_raises

    def create(self, **kw):
        self.create_calls.append(kw)
        return _Req({"documentId": "DOC_ID", "title": kw["body"]["title"]}, self.create_raises)

    def batchUpdate(self, **kw):  # noqa: N802  Google のメソッド名に合わせる
        self.batch_calls.append(kw)
        return _Req({"replies": [{}]}, self.batch_raises)


class _Req:
    def __init__(self, result, raises):
        self._result, self._raises = result, raises

    def execute(self):
        if self._raises:
            raise self._raises
        return self._result


class FakeService:
    def __init__(self, documents):
        self._documents = documents

    def documents(self):
        return self._documents


@pytest.fixture
def documents():
    return FakeDocuments()


@pytest.fixture
def service(documents):
    return FakeService(documents)


# ------------------------------------------------------------------ ハッシュ

def test_同じ本文は同じハッシュ():
    assert to_doc.content_hash(BODY) == to_doc.content_hash(BODY)


def test_1文字違えば別のハッシュ():
    assert to_doc.content_hash(BODY) != to_doc.content_hash(BODY + "。")


def test_ハッシュは短く読める形():
    """スクリーンショットに載るので、**行が折り返さない長さ**にする。"""
    assert len(to_doc.content_hash(BODY)) == 16


# ------------------------------------------------------------------ 台帳

def test_台帳が無ければ空(tmp_path):
    assert to_doc.load_ledger(tmp_path / "none.json") == {}


def test_台帳を書いて読み戻せる(tmp_path):
    path = tmp_path / "posted.json"
    to_doc.save_ledger(path, {"abc": {"documentId": "D1"}})
    assert to_doc.load_ledger(path)["abc"]["documentId"] == "D1"


def test_壊れた台帳は空として扱う(tmp_path):
    """**読めない台帳で止まらない。** 止めると、直すまで議事録が1本も出せない。

    ただし**空として扱うので、重複の検出は効かなくなる**。黙って続けない。
    """
    path = tmp_path / "posted.json"
    path.write_text("{壊れている", encoding="utf-8")
    assert to_doc.load_ledger(path) == {}


def test_辞書でない台帳も空として扱う(tmp_path):
    """**JSON として読めることと、台帳として使えることは別。**

    配列や文字列が入っていると `digest in ledger` が別の意味になる
    （文字列なら部分一致、配列なら要素の一致）——*壊れた台帳が、
    もっともらしく「もうある」と言い出す。*
    """
    for junk in ("[1, 2, 3]", '"文字列"', "42", "null"):
        path = tmp_path / "p.json"
        path.write_text(junk, encoding="utf-8")
        assert to_doc.load_ledger(path) == {}, junk


# ------------------------------------------------------------------ 投稿

def test_作成して挿入する(service, documents, tmp_path):
    got = to_doc.post(service, title="議事録", body=BODY, ledger_path=tmp_path / "p.json")
    assert documents.create_calls[0]["body"] == {"title": "議事録"}
    assert documents.batch_calls[0]["documentId"] == "DOC_ID"
    assert got["documentId"] == "DOC_ID"
    assert "DOC_ID" in got["url"]


def test_台帳に残す(service, tmp_path):
    path = tmp_path / "p.json"
    to_doc.post(service, title="議事録", body=BODY, ledger_path=path)
    ledger = to_doc.load_ledger(path)
    assert to_doc.content_hash(BODY) in ledger


def test_同じ本文を二度書かない(service, documents, tmp_path):
    """**5-H。** 追記ではなく新規作成なので、二度目は「別の議事録」に見える。"""
    path = tmp_path / "p.json"
    to_doc.post(service, title="議事録", body=BODY, ledger_path=path)
    with pytest.raises(to_doc.AlreadyPosted) as caught:
        to_doc.post(service, title="議事録", body=BODY, ledger_path=path)
    assert "DOC_ID" in str(caught.value)
    assert len(documents.create_calls) == 1


def test_明示すれば二度目も書く(service, documents, tmp_path):
    path = tmp_path / "p.json"
    to_doc.post(service, title="議事録", body=BODY, ledger_path=path)
    to_doc.post(service, title="議事録", body=BODY, ledger_path=path, force=True)
    assert len(documents.create_calls) == 2


def test_本文が違えば書く(service, documents, tmp_path):
    path = tmp_path / "p.json"
    to_doc.post(service, title="議事録", body=BODY, ledger_path=path)
    to_doc.post(service, title="議事録", body=BODY + NL + "追記", ledger_path=path)
    assert len(documents.create_calls) == 2


def test_空の本文では呼ばない(service, documents, tmp_path):
    """**空のドキュメントだけがドライブに残るのを防ぐ。**"""
    with pytest.raises(docs_client.DocError):
        to_doc.post(service, title="議事録", body="", ledger_path=tmp_path / "p.json")
    assert not documents.create_calls


def test_挿入で落ちたら台帳に残さない(tmp_path):
    """**書けていないものを「書いた」と記録しない。**

    残すと、次に同じ内容を投げたときに「もうある」と言って止まり、
    *実際にはどこにも無い議事録を、あると信じ続ける*。
    """
    from googleapiclient.errors import HttpError

    class Resp:
        status = 400
        reason = "Bad Request"

    boom = HttpError(Resp(), b'{"error": {"message": "bad"}}')
    documents = FakeDocuments(batch_raises=boom)
    path = tmp_path / "p.json"
    with pytest.raises(docs_client.DocError):
        to_doc.post(FakeService(documents), title="議事録", body=BODY, ledger_path=path)
    assert to_doc.load_ledger(path) == {}


# ------------------------------------------------------------------ タイトル

def test_タイトルに会議名と日付を入れる():
    assert to_doc.build_title("週次定例", "2026-09-12 10:00") == "議事録 週次定例 2026-09-12 10:00"


def test_日時が不明でもタイトルは作れる():
    assert to_doc.build_title("週次定例", "不明") == "議事録 週次定例 不明"


def test_会議名が空なら例外():
    """**無題のドキュメントを量産しない。**"""
    with pytest.raises(ValueError):
        to_doc.build_title("   ", "2026-09-12")
