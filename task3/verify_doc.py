"""書いたドキュメントを**読み返して**、送ったはずの内容と突き合わせる。

**これは出力側の検査である。** 「書いたものが、書いたとおりに入っているか」を見る。
ソース側（台本と合っているか）は `verify_source.py` の担当で、**別ファイルにしてある**
——課題2 では `verify_sheet.py` があるだけで「照合した」と思い込み、
*出力を2回読んだだけ*だった（`DESIGN.md` 11章）。

**照合ロジックは `lesson-4-3-2/task2/verify_doc.py` からそのまま移した。**
合格した課題2 で実機を通したコードで、`task3/tools/check_port.py` が
文字単位で照合している。あの実装が既に踏んでいること:

- Docs は**本文の最後の改行を消せない**。空のドキュメントでも改行が1つ残る
- 応答に `body` が無いのは「空のドキュメント」ではなく「**読み取れていない**」
- **空の照合結果を真にしない**（何も比べていないのに「全部一致」と言わせない・5-O）
- 値が返らなかった項目を OK にしない。*照合できなかったことと一致したことは別*

この課題で承知のうえ残していること（`DESIGN.md` 5-N）
------------------------------------------------------------------

**読み戻しは書き込みと同じ資格情報を使う。** 権限の異常は、その1本が壊れたときに
一緒に壊れるので、この検査では見えない。別アカウントで読むには2つ目の OAuth が要り、
*提出物としては筋が悪い*ので採らなかった。**書いた API とは別の API
（`batchUpdate` ではなく `documents().get`）で読む**ところまでで止めている。
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common import docs_client, google_auth  # noqa: E402
from common.docs_client import VerifyError  # noqa: E402

#: 詳細表示で本文を出すときの上限。長文をそのまま出すと画面が流れて読めない。
PREVIEW_LIMIT = 40


@dataclass(frozen=True)
class Check:
    label: str
    ok: bool
    detail: str = ""


def _content(document: dict) -> list:
    """本文の構造要素を取り出す。

    無いときに空リストを返さない。「本文が空のドキュメント」と
    「本文を読めなかった」は別の話で、混ぜると読めなかったほうが一致に化ける。
    """
    body = document.get("body")
    if body is None:
        raise VerifyError("応答に body がありません。ドキュメントを読み取れていません")
    content = body.get("content")
    if content is None:
        raise VerifyError("応答に body.content がありません。ドキュメントを読み取れていません")
    return content


def extract_text(document: dict) -> str:
    """ドキュメント全体の本文を1つの文字列にする。

    段落の textRun だけを拾う。sectionBreak は本文を持たず、画像などの
    inlineObjectElement にも textRun が無い。

    表の中は辿らない。このプログラムは段落しか作らないので、表が入っていたら
    「本文が一致」が NG になる。素通りするより気づけるほうを選ぶ。
    """
    parts: list[str] = []
    for element in _content(document):
        paragraph = element.get("paragraph")
        if paragraph is None:
            continue
        for run in paragraph.get("elements", []):
            text_run = run.get("textRun")
            if text_run is None:
                continue
            # content が無い textRun は中身が空。ここは既定値で正しい。
            parts.append(text_run.get("content", ""))
    return "".join(parts)


def count_paragraphs(document: dict) -> int:
    """段落の数を数える。

    本文を平らな文字列にして比べるのとは別の角度。改行が「文字として入った」だけで
    段落に分かれていない、という壊れ方はこちらでしか見えない。
    """
    return sum(1 for element in _content(document) if "paragraph" in element)


def strip_document_trailing_newline(text: str) -> str | None:
    """ドキュメント末尾の改行を1つだけ外す。

    Docs は本文の最後の改行を消せない。空のドキュメントでも "\\n" が1つ残るので、
    送った文字列と比べるにはこれを外す。

    改行で終わっていなければ None を返す。あり得ない応答なので、
    「たまたま一致」に倒さず NG にする。
    """
    if not text.endswith("\n"):
        return None
    return text[:-1]


def _preview(text: str) -> str:
    shortened = text if len(text) <= PREVIEW_LIMIT else text[:PREVIEW_LIMIT] + "…"
    return shortened.replace("\n", "\\n")


def compare_with_expected(
    document: dict,
    *,
    expected_text: str,
    expected_title: str,
    expected_document_id: str,
) -> list[Check]:
    """読み返したドキュメントと、送ったはずの内容を項目ごとに突き合わせる。

    値が返ってこなかった項目は OK にしない。照合できなかったことと
    一致したことを同じ扱いにすると、確かめた気になるだけになる。
    """
    checks: list[Check] = []

    actual_id = document.get("documentId")
    checks.append(
        Check(
            "ドキュメントIDが一致",
            actual_id == expected_document_id,
            f"{actual_id or '(返らなかった)'} / {expected_document_id}",
        )
    )

    actual_title = document.get("title")
    checks.append(
        Check(
            "タイトルが一致",
            actual_title == expected_title,
            f"{actual_title or '(返らなかった)'} / {expected_title}",
        )
    )

    body_text = strip_document_trailing_newline(extract_text(document))
    checks.append(
        Check(
            "本文が一致",
            body_text is not None and body_text == expected_text,
            f"{_preview(body_text) if body_text is not None else '(末尾の改行が無く読めなかった)'}"
            f" / {_preview(expected_text)}",
        )
    )
    checks.append(
        Check(
            "文字数が一致",
            body_text is not None and len(body_text) == len(expected_text),
            f"{len(body_text) if body_text is not None else '(読めなかった)'} / {len(expected_text)}",
        )
    )

    actual_paragraphs = count_paragraphs(document)
    expected_paragraphs = expected_text.count("\n") + 1
    checks.append(
        Check(
            "段落数が一致",
            actual_paragraphs == expected_paragraphs,
            f"{actual_paragraphs} / {expected_paragraphs}",
        )
    )

    return checks


def all_ok(checks: Sequence[Check]) -> bool:
    # 空を真にしない。何も照合していないのに「全部一致」と言わせないため。
    return bool(checks) and all(check.ok for check in checks)


def format_checks(checks: Sequence[Check]) -> str:
    return "\n".join(
        f"{'OK ' if c.ok else 'NG '} {c.label}{('  ' + c.detail) if c.detail else ''}"
        for c in checks
    )


# ------------------------------------------------------------------ 入口


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="書いたドキュメントを読み返して、送った内容と突き合わせる（読むだけ）"
    )
    parser.add_argument("document_id", help="確かめるドキュメントの ID")
    parser.add_argument("--body", type=Path, required=True, help="送ったはずの本文")
    parser.add_argument("--title", required=True, help="送ったはずのタイトル")
    parser.add_argument("--credentials", default="credentials.json")
    parser.add_argument("--token", default="token.json")
    return parser


def _default_service_factory(args: argparse.Namespace):
    credentials = google_auth.load_credentials(
        args.credentials, args.token, docs_client.DEFAULT_SCOPES
    )
    return docs_client.build_service(credentials)


def main(argv: Sequence[str] | None = None, *, service_factory: Callable | None = None) -> int:
    args = build_parser().parse_args(argv)
    factory = service_factory or _default_service_factory

    if not args.body.exists():
        print("本文が見つかりません: {}".format(args.body), file=sys.stderr)
        return 1
    expected = docs_client.ensure_insertable(args.body.read_text(encoding="utf-8"))

    try:
        service = factory(args)
        document = docs_client.fetch_document(service, args.document_id)
    except (VerifyError, docs_client.DocError, google_auth.AuthError) as error:
        print(error, file=sys.stderr)
        return 1

    checks = compare_with_expected(
        document,
        expected_text=expected,
        expected_title=args.title,
        expected_document_id=args.document_id,
    )
    print(format_checks(checks))
    print("")
    print("照合: {} 項目中 {} 項目が一致".format(
        len(checks), sum(1 for c in checks if c.ok)))
    if all_ok(checks):
        print("**ただし、これは出力側の検査である。** 台本と合っているかは verify_source.py")
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
