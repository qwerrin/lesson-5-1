"""common/docs_client の読み取り側のテスト。**移植元からそのまま移した。**

`lesson-4-3-2/task2/tests/test_verify_doc.py` から `fetch_document` の分だけ。

**偽物を書き込み側と共有しない。** ここの `FakeDocuments` は `get` しか持たず、
*それで通ることが「読むだけで書き換えない」の証拠*になっている。
`batchUpdate` を持つ偽物と一緒にすると、同じテストが何も証明しなくなる。
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
    def __init__(self, get_result=None, get_raises=None) -> None:
        self.get_result = get_result if get_result is not None else {}
        self.get_raises = get_raises
        self.get_calls: list[dict] = []

    def get(self, **kwargs):
        self.get_calls.append(kwargs)
        return FakeRequest(self.get_result, self.get_raises)


class FakeService:
    def __init__(self, documents: FakeDocuments) -> None:
        self._documents = documents

    def documents(self):
        return self._documents


def paragraph(text: str) -> dict:
    return {"paragraph": {"elements": [{"textRun": {"content": text}}]}}


def make_document(document_text: str, *, title: str = "T", document_id: str = "DOC") -> dict:
    """Docs API が返す形を組み立てる。

    document_text は「ドキュメント全体の文字列」で、必ず改行で終わる。
    Docs は末尾の改行を消せないため、空のドキュメントでも "\\n" が1つ残る。
    段落は改行ごとに切れる。
    """
    assert document_text.endswith("\n"), "ドキュメントの本文は必ず改行で終わる"
    parts = document_text.split("\n")
    content: list[dict] = [{"sectionBreak": {"sectionStyle": {}}}]
    content.extend(paragraph(p + "\n") for p in parts[:-1])
    return {"documentId": document_id, "title": title, "body": {"content": content}}


def document_with(inserted_text: str, *, title: str = "T", document_id: str = "DOC") -> dict:
    """`inserted_text` を挿入した直後のドキュメント。末尾に Docs の改行が1つ足される。"""
    return make_document(inserted_text + "\n", title=title, document_id=document_id)


class TestFetchDocument:
    def test_documentsのgetを呼ぶ(self):
        documents = FakeDocuments(get_result=document_with("本文"))
        verify_doc.fetch_document(FakeService(documents), "DOC")
        assert len(documents.get_calls) == 1

    def test_documentIdを渡す(self):
        documents = FakeDocuments(get_result=document_with("本文"))
        verify_doc.fetch_document(FakeService(documents), "DOC")
        assert documents.get_calls[0]["documentId"] == "DOC"

    def test_404はドキュメントが見つからないと伝える(self):
        documents = FakeDocuments(get_raises=make_http_error(404, "not found"))
        with pytest.raises(verify_doc.VerifyError) as caught:
            verify_doc.fetch_document(FakeService(documents), "DOC")
        assert "DOC" in str(caught.value)

    def test_403も失敗として伝える(self):
        documents = FakeDocuments(get_raises=make_http_error(403, "denied"))
        with pytest.raises(verify_doc.VerifyError):
            verify_doc.fetch_document(FakeService(documents), "DOC")

    def test_読むだけで書き換えない(self):
        # get 以外のメソッドを持たない偽物で通ることが、書き込まない証拠になる。
        documents = FakeDocuments(get_result=document_with("本文"))
        verify_doc.fetch_document(FakeService(documents), "DOC")
        assert not hasattr(documents, "batch_calls")
