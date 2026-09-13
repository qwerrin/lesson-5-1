"""task3/verify_doc のテスト。**移植元からそのまま移した。**

`lesson-4-3-2/task2/tests/test_verify_doc.py` から、照合ロジックの分だけ。
**本体もヘルパも1文字変えていない**——`task3/tools/check_port.py` が照合する。

移植元は `verify_doc` という名前で参照している。**その名前を下で別名として作る。**
参照名を書き換えると本体が「そのまま」でなくなり、照合器が落ちる。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "task3"))

import verify_doc as _verify_doc  # noqa: E402

#: 移植元での名前。本体を1文字も変えないための別名。
verify_doc = _verify_doc


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


class TestExtractText:
    def test_段落のテキストを取り出す(self):
        assert verify_doc.extract_text(make_document("こんにちは\n")) == "こんにちは\n"

    def test_複数段落を順番どおりに連結する(self):
        assert verify_doc.extract_text(make_document("1行目\n2行目\n")) == "1行目\n2行目\n"

    def test_同じ段落の複数のtextRunを連結する(self):
        # Docs は書式の切れ目で textRun を分ける。分かれても本文は変わらない。
        document = {
            "body": {
                "content": [
                    {"sectionBreak": {}},
                    {
                        "paragraph": {
                            "elements": [
                                {"textRun": {"content": "こんに"}},
                                {"textRun": {"content": "ちは\n"}},
                            ]
                        }
                    },
                ]
            }
        }
        assert verify_doc.extract_text(document) == "こんにちは\n"

    def test_sectionBreakは無視する(self):
        # sectionBreak は本文を持たない。数えると先頭がずれる。
        assert verify_doc.extract_text(make_document("あ\n")) == "あ\n"

    def test_textRunの無い要素は飛ばす(self):
        # 画像などの inlineObjectElement には textRun が無い。
        document = {
            "body": {
                "content": [
                    {
                        "paragraph": {
                            "elements": [
                                {"inlineObjectElement": {"inlineObjectId": "kix.1"}},
                                {"textRun": {"content": "文字\n"}},
                            ]
                        }
                    }
                ]
            }
        }
        assert verify_doc.extract_text(document) == "文字\n"

    def test_表の中身は取り出さない(self):
        # この課題は段落しか作らない。表を拾わないので、もし表が入っていたら
        # 「本文が一致」が NG になる。素通りするより気づけるほうを選ぶ。
        document = {
            "body": {
                "content": [
                    {"table": {"tableRows": [{"tableCells": [{"content": [paragraph("表\n")]}]}]}},
                    paragraph("本文\n"),
                ]
            }
        }
        assert verify_doc.extract_text(document) == "本文\n"

    def test_bodyが無ければ失敗する(self):
        # 空文字を既定値にすると「本文が空のドキュメント」と区別できなくなる。
        with pytest.raises(verify_doc.VerifyError):
            verify_doc.extract_text({"documentId": "DOC", "title": "T"})

    def test_contentが無ければ失敗する(self):
        with pytest.raises(verify_doc.VerifyError):
            verify_doc.extract_text({"body": {}})

    def test_contentが空なら空文字(self):
        assert verify_doc.extract_text({"body": {"content": []}}) == ""


class TestCountParagraphs:
    def test_段落を数える(self):
        assert verify_doc.count_paragraphs(make_document("1\n2\n3\n")) == 3

    def test_sectionBreakは数えない(self):
        assert verify_doc.count_paragraphs(make_document("1\n")) == 1

    def test_bodyが無ければ失敗する(self):
        with pytest.raises(verify_doc.VerifyError):
            verify_doc.count_paragraphs({"title": "T"})


class TestStripDocumentTrailingNewline:
    def test_末尾の改行を1つ外す(self):
        assert verify_doc.strip_document_trailing_newline("本文\n") == "本文"

    def test_改行が2つなら1つ残る(self):
        assert verify_doc.strip_document_trailing_newline("本文\n\n") == "本文\n"

    def test_改行だけなら空文字になる(self):
        assert verify_doc.strip_document_trailing_newline("\n") == ""

    def test_末尾に改行が無ければNone(self):
        # Docs は末尾の改行を必ず残す。無いなら読み違えている。
        # 「たまたま一致した」に倒さないため、None を返して NG にする。
        assert verify_doc.strip_document_trailing_newline("本文") is None


class TestCompareWithExpected:
    def _ok_checks(self, text="こんにちは", title="T", document_id="DOC"):
        return verify_doc.compare_with_expected(
            document_with(text, title=title, document_id=document_id),
            expected_text=text,
            expected_title=title,
            expected_document_id=document_id,
        )

    def _labels(self, checks, ok: bool):
        return [c.label for c in checks if c.ok is ok]

    def test_一致していれば全部OK(self):
        assert verify_doc.all_ok(self._ok_checks())

    def test_日本語と改行を含む本文でも一致する(self):
        assert verify_doc.all_ok(self._ok_checks(text="1行目\n2行目\nおわり"))

    def test_本文が違えばNGになる(self):
        checks = verify_doc.compare_with_expected(
            document_with("こんばんは"),
            expected_text="こんにちは",
            expected_title="T",
            expected_document_id="DOC",
        )
        assert not verify_doc.all_ok(checks)

    def test_末尾の自動改行は差分にしない(self):
        # ドキュメント側は "こんにちは\n"、送ったのは "こんにちは"。
        checks = self._ok_checks(text="こんにちは")
        assert "本文が一致" in self._labels(checks, ok=True)

    def test_末尾に改行が無い応答は一致にしない(self):
        document = make_document("こんにちは\n")
        # 末尾の改行を削った、あり得ない応答にする。
        document["body"]["content"][-1]["paragraph"]["elements"][0]["textRun"]["content"] = "こんにちは"
        checks = verify_doc.compare_with_expected(
            document,
            expected_text="こんにちは",
            expected_title="T",
            expected_document_id="DOC",
        )
        assert not verify_doc.all_ok(checks)

    def test_タイトルが違えばNGになる(self):
        checks = verify_doc.compare_with_expected(
            document_with("本文", title="ちがうタイトル"),
            expected_text="本文",
            expected_title="T",
            expected_document_id="DOC",
        )
        assert not verify_doc.all_ok(checks)

    def test_タイトルが返らなければNGになる(self):
        # 既定値を入れると、返ってこなかったことが一致に化ける。
        document = document_with("本文")
        del document["title"]
        checks = verify_doc.compare_with_expected(
            document,
            expected_text="本文",
            expected_title="T",
            expected_document_id="DOC",
        )
        assert not verify_doc.all_ok(checks)

    def test_ドキュメントIDが違えばNGになる(self):
        checks = verify_doc.compare_with_expected(
            document_with("本文", document_id="OTHER"),
            expected_text="本文",
            expected_title="T",
            expected_document_id="DOC",
        )
        assert not verify_doc.all_ok(checks)

    def test_ドキュメントIDが返らなければNGになる(self):
        document = document_with("本文")
        del document["documentId"]
        checks = verify_doc.compare_with_expected(
            document,
            expected_text="本文",
            expected_title="T",
            expected_document_id="DOC",
        )
        assert not verify_doc.all_ok(checks)

    def test_文字数を照合する(self):
        assert "文字数が一致" in self._labels(self._ok_checks(), ok=True)

    def test_文字数が違えばNGになる(self):
        checks = verify_doc.compare_with_expected(
            document_with("ながい本文です"),
            expected_text="みじかい",
            expected_title="T",
            expected_document_id="DOC",
        )
        assert "文字数が一致" in self._labels(checks, ok=False)

    def test_段落数を照合する(self):
        checks = self._ok_checks(text="1行目\n2行目")
        assert "段落数が一致" in self._labels(checks, ok=True)

    def test_段落数が違えばNGになる(self):
        # 改行が文字として入っただけで段落に分かれていない、という壊れ方を見る。
        checks = verify_doc.compare_with_expected(
            document_with("1行目2行目"),
            expected_text="1行目\n2行目",
            expected_title="T",
            expected_document_id="DOC",
        )
        assert "段落数が一致" in self._labels(checks, ok=False)

    def test_照合項目が5つある(self):
        assert len(self._ok_checks()) == 5


class TestAllOk:
    def test_全部OKならTrue(self):
        checks = [verify_doc.Check("a", True), verify_doc.Check("b", True)]
        assert verify_doc.all_ok(checks)

    def test_1つでもNGならFalse(self):
        checks = [verify_doc.Check("a", True), verify_doc.Check("b", False)]
        assert not verify_doc.all_ok(checks)

    def test_空なら真にしない(self):
        # 何も照合していないのに「全部一致」と言わせない。
        assert not verify_doc.all_ok([])


class TestFormatChecks:
    def test_OKを印字する(self):
        assert "OK" in verify_doc.format_checks([verify_doc.Check("項目", True)])

    def test_NGを印字する(self):
        assert "NG" in verify_doc.format_checks([verify_doc.Check("項目", False)])

    def test_NGをOKと印字しない(self):
        output = verify_doc.format_checks([verify_doc.Check("項目", False)])
        assert "OK" not in output

    def test_項目名を印字する(self):
        assert "本文が一致" in verify_doc.format_checks([verify_doc.Check("本文が一致", True)])

    def test_詳細を印字する(self):
        assert "くわしく" in verify_doc.format_checks(
            [verify_doc.Check("項目", True, "くわしく")]
        )
