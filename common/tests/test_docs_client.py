"""common/docs_client の書き込み側のテスト。**移植元からそのまま移した。**

`lesson-4-3-2/task2/tests/test_create_doc.py` から、API 層に関わるものだけを取っている。
**テストの本体も偽物も1文字変えていない**——`task3/tools/check_port.py` が照合する。

**読み取り側は別ファイル**（`test_docs_client_read.py`）。移植元でファイルが
分かれており、*偽物の作りが証拠そのものになっている*ため——読み取りの
「読むだけで書き換えない」は、**`get` しか持たない偽物で通ること**が根拠で、
`batchUpdate` を持つ偽物と一緒にすると意味が消える（2026-09-13 に実際に踏んだ）。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from common import docs_client  # noqa: E402

#: 移植元での名前。**本体を1文字も変えないための別名。**
create_doc = docs_client
verify_doc = docs_client


class FakeResponse:
    """HttpError が読む最小限のレスポンス。status と reason しか見られない。"""

    def __init__(self, status: int, reason: str = "") -> None:
        self.status = status
        self.reason = reason


def make_http_error(status: int, message: str):
    from googleapiclient.errors import HttpError

    content = json.dumps({"error": {"code": status, "message": message}}).encode("utf-8")
    return HttpError(FakeResponse(status, "Error"), content, uri="https://example.invalid")


class FakeRequest:
    def __init__(self, result, raises) -> None:
        self._result = result
        self._raises = raises

    def execute(self):
        if self._raises is not None:
            raise self._raises
        return self._result


class FakeDocuments:
    """documents().create / batchUpdate / get の呼ばれ方を記録するだけの偽物。"""

    def __init__(
        self,
        create_result=None,
        batch_result=None,
        get_result=None,
        create_raises=None,
        batch_raises=None,
        get_raises=None,
    ) -> None:
        self.create_result = (
            create_result if create_result is not None else {"documentId": "DOC_ID", "title": "T"}
        )
        self.batch_result = batch_result if batch_result is not None else {"replies": [{}]}
        self.get_result = get_result if get_result is not None else {}
        self.create_raises = create_raises
        self.batch_raises = batch_raises
        self.get_raises = get_raises
        self.create_calls: list[dict] = []
        self.batch_calls: list[dict] = []
        self.get_calls: list[dict] = []

    def create(self, **kwargs):
        self.create_calls.append(kwargs)
        return FakeRequest(self.create_result, self.create_raises)

    def batchUpdate(self, **kwargs):  # noqa: N802  Google のメソッド名に合わせる
        self.batch_calls.append(kwargs)
        return FakeRequest(self.batch_result, self.batch_raises)

    def get(self, **kwargs):
        self.get_calls.append(kwargs)
        return FakeRequest(self.get_result, self.get_raises)


class FakeService:
    def __init__(self, documents: FakeDocuments) -> None:
        self._documents = documents

    def documents(self):
        return self._documents


@pytest.fixture
def documents() -> FakeDocuments:
    return FakeDocuments()


@pytest.fixture
def service(documents: FakeDocuments) -> FakeService:
    return FakeService(documents)


class TestNormalizeNewlines:
    def test_CRLFをLFにする(self):
        assert create_doc.normalize_newlines("a\r\nb") == "a\nb"

    def test_CRをLFにする(self):
        assert create_doc.normalize_newlines("a\rb") == "a\nb"

    def test_LFは変えない(self):
        assert create_doc.normalize_newlines("a\nb") == "a\nb"

    def test_改行が無ければ変えない(self):
        assert create_doc.normalize_newlines("あいうえお") == "あいうえお"


class TestBuildInsertRequests:
    def test_insertTextを1つ作る(self):
        requests = create_doc.build_insert_requests("本文")
        assert len(requests) == 1
        assert "insertText" in requests[0]

    def test_挿入位置は1(self):
        # 0 は本文の外（sectionBreak の位置）。段落の中でないと挿入できず 400 になる。
        requests = create_doc.build_insert_requests("本文")
        assert requests[0]["insertText"]["location"]["index"] == 1

    def test_テキストをそのまま入れる(self):
        requests = create_doc.build_insert_requests("あいう\nえお")
        assert requests[0]["insertText"]["text"] == "あいう\nえお"

    def test_endOfSegmentLocationは使わない(self):
        # 末尾追記ではなく先頭挿入で固定する。位置がぶれると照合できない。
        requests = create_doc.build_insert_requests("本文")
        assert "endOfSegmentLocation" not in requests[0]["insertText"]

    def test_本文開始インデックスの定数が1(self):
        assert create_doc.BODY_START_INDEX == 1


class TestCreateDocument:
    def test_documentsのcreateを呼ぶ(self, service, documents):
        create_doc.create_document(service, "タイトル")
        assert len(documents.create_calls) == 1

    def test_bodyにタイトルを入れる(self, service, documents):
        create_doc.create_document(service, "タイトル")
        assert documents.create_calls[0]["body"]["title"] == "タイトル"

    def test_作成時に本文を入れない(self, service, documents):
        # Docs API の documents.create は title 以外を無視する。
        # 入れると「送ったのに反映されない」形の勘違いが起きる。
        create_doc.create_document(service, "タイトル")
        assert "body" not in documents.create_calls[0]["body"]

    def test_作成結果を返す(self, service, documents):
        documents.create_result = {"documentId": "ABC", "title": "タイトル"}
        assert create_doc.create_document(service, "タイトル")["documentId"] == "ABC"

    def test_documentIdが返らなければ失敗にする(self, service, documents):
        documents.create_result = {"title": "タイトル"}
        with pytest.raises(create_doc.DocError):
            create_doc.create_document(service, "タイトル")

    def test_documentIdが空文字なら失敗にする(self, service, documents):
        documents.create_result = {"documentId": "", "title": "タイトル"}
        with pytest.raises(create_doc.DocError):
            create_doc.create_document(service, "タイトル")


class TestInsertText:
    def test_batchUpdateを呼ぶ(self, service, documents):
        create_doc.insert_text(service, "DOC", "本文")
        assert len(documents.batch_calls) == 1

    def test_documentIdを渡す(self, service, documents):
        create_doc.insert_text(service, "DOC", "本文")
        assert documents.batch_calls[0]["documentId"] == "DOC"

    def test_requestsにinsertTextを渡す(self, service, documents):
        create_doc.insert_text(service, "DOC", "本文")
        requests = documents.batch_calls[0]["body"]["requests"]
        assert requests == create_doc.build_insert_requests("本文")

    def test_挿入結果を返す(self, service, documents):
        documents.batch_result = {"documentId": "DOC", "replies": [{}]}
        assert create_doc.insert_text(service, "DOC", "本文")["documentId"] == "DOC"


class TestCreateDocumentWithText:
    def test_作成してから挿入する(self, service, documents):
        create_doc.create_document_with_text(service, "タイトル", "本文")
        assert len(documents.create_calls) == 1
        assert len(documents.batch_calls) == 1

    def test_作成で得たIDに挿入する(self, service, documents):
        documents.create_result = {"documentId": "REAL_ID", "title": "タイトル"}
        create_doc.create_document_with_text(service, "タイトル", "本文")
        assert documents.batch_calls[0]["documentId"] == "REAL_ID"

    def test_作成に失敗したら挿入しない(self, service, documents):
        documents.create_raises = make_http_error(403, "denied")
        with pytest.raises(create_doc.DocError):
            create_doc.create_document_with_text(service, "タイトル", "本文")
        assert documents.batch_calls == []

    def test_挿入に失敗したら残ったドキュメントのIDを伝える(self, service, documents):
        # 作成は通って挿入だけ落ちると、空のドキュメントがドライブに残る。
        # ID を出さないと、どれを消せばいいか分からない。
        documents.create_result = {"documentId": "LEFT_BEHIND", "title": "T"}
        documents.batch_raises = make_http_error(403, "denied")
        with pytest.raises(create_doc.DocError) as caught:
            create_doc.create_document_with_text(service, "タイトル", "本文")
        assert "LEFT_BEHIND" in str(caught.value)

    def test_挿入に失敗しても元のエラー内容を残す(self, service, documents):
        documents.batch_raises = make_http_error(403, "denied")
        with pytest.raises(create_doc.DocError) as caught:
            create_doc.create_document_with_text(service, "タイトル", "本文")
        assert "403" in str(caught.value)

    def test_戻り値にドキュメントIDが入る(self, service, documents):
        documents.create_result = {"documentId": "REAL_ID", "title": "タイトル"}
        result = create_doc.create_document_with_text(service, "タイトル", "本文")
        assert result["documentId"] == "REAL_ID"

    def test_戻り値にタイトルが入る(self, service, documents):
        documents.create_result = {"documentId": "REAL_ID", "title": "タイトル"}
        result = create_doc.create_document_with_text(service, "タイトル", "本文")
        assert result["title"] == "タイトル"

    def test_戻り値にリンクが入る(self, service, documents):
        documents.create_result = {"documentId": "REAL_ID", "title": "タイトル"}
        result = create_doc.create_document_with_text(service, "タイトル", "本文")
        assert result["url"] == create_doc.document_url("REAL_ID")


class TestDocumentUrl:
    def test_ドキュメントIDからリンクを組む(self):
        assert create_doc.document_url("ABC") == "https://docs.google.com/document/d/ABC/edit"


class TestErrors:
    def test_403は権限とAPI有効化を案内する(self, service, documents):
        documents.create_raises = make_http_error(403, "The caller does not have permission")
        with pytest.raises(create_doc.DocError) as caught:
            create_doc.create_document(service, "タイトル")
        assert "Docs API" in str(caught.value)

    def test_APIが無効なら有効化の手順を案内する(self, service, documents):
        # 「権限が足りない」と同じ文面にしない。原因が別なので、探す場所も別になる。
        documents.create_raises = make_http_error(
            403, "Google Docs API has not been used in project 123 before or it is disabled"
        )
        with pytest.raises(create_doc.DocError) as caught:
            create_doc.create_document(service, "タイトル")
        assert "ライブラリ" in str(caught.value)

    def test_権限不足に有効化の手順を混ぜない(self, service, documents):
        documents.create_raises = make_http_error(403, "The caller does not have permission")
        with pytest.raises(create_doc.DocError) as caught:
            create_doc.create_document(service, "タイトル")
        assert "ライブラリ" not in str(caught.value)

    def test_404はドキュメントが見つからないと伝える(self, service, documents):
        documents.batch_raises = make_http_error(404, "Requested entity was not found.")
        with pytest.raises(create_doc.DocError) as caught:
            create_doc.insert_text(service, "DOC", "本文")
        assert "見つかりません" in str(caught.value)

    def test_ステータスコードを残す(self, service, documents):
        documents.create_raises = make_http_error(400, "Invalid requests[0].insertText")
        with pytest.raises(create_doc.DocError) as caught:
            create_doc.create_document(service, "タイトル")
        assert "400" in str(caught.value)

    def test_APIの説明文を残す(self, service, documents):
        documents.create_raises = make_http_error(500, "Internal error encountered.")
        with pytest.raises(create_doc.DocError) as caught:
            create_doc.create_document(service, "タイトル")
        assert "Internal error encountered." in str(caught.value)

    def test_知らないステータスでも落ちない(self, service, documents):
        documents.create_raises = make_http_error(429, "Quota exceeded")
        with pytest.raises(create_doc.DocError):
            create_doc.create_document(service, "タイトル")

    def test_知らないステータスでもコードと説明文を残す(self, service, documents):
        # 分岐に無いステータスこそ、手がかりが本文しかない。
        documents.create_raises = make_http_error(429, "Quota exceeded")
        with pytest.raises(create_doc.DocError) as caught:
            create_doc.create_document(service, "タイトル")
        assert "429" in str(caught.value)
        assert "Quota exceeded" in str(caught.value)


class TestEnsureInsertable:
    """**この課題で足した検査。** 移植元では CLI 側にあり、素通りできた。"""

    def test_改行をLFにそろえる(self):
        cr, lf = chr(13), chr(10)
        got = docs_client.ensure_insertable("a" + cr + lf + "b" + cr + "c")
        assert got == "a" + lf + "b" + lf + "c"

    def test_空なら送る前に止める(self):
        with pytest.raises(docs_client.DocError):
            docs_client.ensure_insertable("")

    def test_Noneも空として扱う(self):
        with pytest.raises(docs_client.DocError):
            docs_client.ensure_insertable(None)

    def test_空白だけは通す(self):
        """**意味のある空白かもしれない。** 落とすのは空だけにする。"""
        assert docs_client.ensure_insertable("   ") == "   "

    def test_検査を通してから挿入する(self, service, documents):
        docs_client.insert_text_checked(service, "DOC", "本文")
        assert documents.batch_calls

    def test_返ったrepliesの数を確かめる(self, service, documents):
        """**batchUpdate は部分的に成功しうる**（5-L）。成功コードだけで信じない。"""
        documents.batch_result = {"replies": []}
        with pytest.raises(docs_client.DocError) as caught:
            docs_client.insert_text_checked(service, "DOC", "本文")
        assert "replies" in str(caught.value)

    def test_数が合っていれば通す(self, service, documents):
        documents.batch_result = {"replies": [{}]}
        assert docs_client.insert_text_checked(service, "DOC", "本文")["replies"]

    def test_repliesが無くても数えられる(self, service, documents):
        """**鍵が無いのを 0 件として扱う。** None で落ちると原因が遠くなる。"""
        documents.batch_result = {}
        with pytest.raises(docs_client.DocError):
            docs_client.insert_text_checked(service, "DOC", "本文")

    def test_空ならAPIを呼ばない(self, service, documents):
        with pytest.raises(docs_client.DocError):
            docs_client.insert_text_checked(service, "DOC", "")
        assert not documents.batch_calls
