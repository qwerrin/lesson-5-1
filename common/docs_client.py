"""Google ドキュメントの作成・挿入・読み戻し。課題をまたいで使う。

**Section 4-3 の `lesson-4-3-2/task2` から、API 層をそのまま移した。**
`create_doc.py` から作成と挿入、`verify_doc.py` から読み戻しを取っている。
どちらも**合格した課題2で実機を通したコード**である。

なぜコピーなのか
------------------------------------------------------------------

`common/google_auth.py` と同じ判断。Section 5-1 はリポジトリが分かれるので
import では持ってこられない。**書き直すと「新しい実装から期待値を作る」形になり、
実機で1度通った知識が失われる。**

**移植した関数は1文字も変えていない。** `tools/check_port.py` が
移植元のソースと**文字単位で照合**する。目視で「同じはず」と書かない。

移植元が実機で確かめたこと（`task3/DESIGN.md` 3.2）
------------------------------------------------------------------

============================================ ==================================
挙動                                          ここでの扱い
============================================ ==================================
`documents().create` は本文を無視して成功する  作成と挿入を分ける（2段階）
空文字の `insertText` は 400 で弾かれる        `insert_text_checked` が手前で止める
挿入位置 0 は本文の外で 400 になる             `BODY_START_INDEX = 1`
複数回に分けるとインデックスがずれる           1回の `insertText` にまとめる（5-M）
============================================ ==================================

**この課題で足したもの**は `insert_text_checked` の1つだけ。移植した
`insert_text` は残してある——*消すと「移植元と同じ」が言えなくなる*。
"""

from __future__ import annotations

import json

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

#: ドキュメントの作成と編集に必要な権限。読み取り専用（documents.readonly）では
#: documents.create も batchUpdate もできない。
DEFAULT_SCOPES: tuple[str, ...] = ("https://www.googleapis.com/auth/documents",)

#: 本文の先頭。インデックス 0 は本文の外（sectionBreak の位置）で、
#: そこへ挿入しようとすると「段落の中ではない」として 400 で弾かれる。
BODY_START_INDEX = 1

DOCUMENT_URL_TEMPLATE = "https://docs.google.com/document/d/{document_id}/edit"


class DocError(Exception):
    """利用者にそのまま見せられる失敗。想定外の例外とは区別する。"""

# ---------------------------------------------------------------- 送る前に決めること

def normalize_newlines(text: str) -> str:
    """改行を LF に揃える。

    Docs の改行は LF。CRLF のまま送ると本文に CR が余分な文字として残り、
    読み返したときに「送った文字列と違う」ことになる。
    """
    return text.replace("\r\n", "\n").replace("\r", "\n")

def build_insert_requests(text: str) -> list[dict]:
    """batchUpdate に渡すリクエストを組み立てる。

    本文の先頭に1回だけ挿入する。複数回に分けるとインデックスが挿入のたびに
    ずれて、書いた順と並ぶ順が食い違う。
    """
    return [{"insertText": {"location": {"index": BODY_START_INDEX}, "text": text}}]

def document_url(document_id: str) -> str:
    return DOCUMENT_URL_TEMPLATE.format(document_id=document_id)

# ---------------------------------------------------------------- API を呼ぶ

def _api_message(error: HttpError) -> str:
    """HttpError の本文から Google が返した説明文だけを取り出す。"""
    try:
        payload = json.loads(error.content.decode("utf-8"))
        return str(payload["error"]["message"])
    except (ValueError, KeyError, AttributeError, UnicodeDecodeError):
        return str(error)

def _looks_like_api_disabled(detail: str) -> bool:
    lowered = detail.lower()
    return "has not been used in project" in lowered or "it is disabled" in lowered

def _translate_http_error(error: HttpError, document_id: str | None = None) -> DocError:
    status = error.resp.status
    detail = _api_message(error)

    if status == 403 and _looks_like_api_disabled(detail):
        return DocError(
            f"[{status}] Google Docs API がこのプロジェクトで有効になっていません。"
            "Google Cloud コンソールの「API とサービス」→「ライブラリ」で "
            "Google Docs API を有効にし、数分おいてから実行し直してください。"
            f" / API の応答: {detail}"
        )

    if status == 403:
        return DocError(
            f"[{status}] 権限が足りません。"
            "同意した権限に Docs API の documents スコープが含まれているか確認してください。"
            "token.json を消して同意を取り直すと直ることがあります。"
            f" / API の応答: {detail}"
        )

    if status == 404:
        target = f"（ID: {document_id}）" if document_id else ""
        return DocError(f"[{status}] ドキュメントが見つかりません{target} / API の応答: {detail}")

    if status == 400:
        return DocError(
            f"[{status}] リクエストの組み立てが正しくありません。"
            "挿入位置（インデックス）や本文の中身を確認してください。"
            f" / API の応答: {detail}"
        )

    return DocError(f"[{status}] Docs API の呼び出しに失敗しました / API の応答: {detail}")

def create_document(service, title: str) -> dict:
    """空のドキュメントを作り、API が返したドキュメント情報を返す。

    body には title しか入れない。documents.create は title 以外を無視する仕様で、
    本文を一緒に送っても反映されないまま成功が返る。挿入は batchUpdate で行う。
    """
    try:
        created = service.documents().create(body={"title": title}).execute()
    except HttpError as error:
        raise _translate_http_error(error) from error

    if not created.get("documentId"):
        raise DocError(
            "ドキュメントは作られたようですが、応答に documentId がありません。"
            f"応答: {created}"
        )
    return created

def insert_text(service, document_id: str, text: str) -> dict:
    """作成済みのドキュメントの先頭に本文を挿入する。"""
    body = {"requests": build_insert_requests(text)}
    try:
        return service.documents().batchUpdate(documentId=document_id, body=body).execute()
    except HttpError as error:
        raise _translate_http_error(error, document_id) from error

def create_document_with_text(service, title: str, text: str) -> dict:
    """作成 → 挿入をまとめて行い、画面に出す材料を返す。"""
    created = create_document(service, title)
    document_id = created["documentId"]

    try:
        insert_text(service, document_id, text)
    except DocError as error:
        # 作成だけ通って挿入で落ちると、空のドキュメントがドライブに残る。
        # ID を出さないと、どれを消せばいいか分からない。
        raise DocError(
            f"{error}\n"
            f"※ 空のドキュメントが作られたまま残っています（ID: {document_id}）。"
            f"不要なら削除してください: {document_url(document_id)}"
        ) from error

    return {
        "documentId": document_id,
        "title": created.get("title") or title,
        "url": document_url(document_id),
        # Python の文字数。Docs API のインデックスは UTF-16 単位なので、
        # 絵文字などサロゲートペアを含む場合はこの数と一致しない。表示用。
        "insertedLength": len(text),
    }

class VerifyError(Exception):
    """利用者にそのまま見せられる失敗。"""


def fetch_document(service, document_id: str) -> dict:
    """ドキュメントを読む。

    fields は指定しない。既定の応答に body も title も含まれる。
    タブ機能を使う場合は includeTabsContent が要るが、既定では先頭タブの内容が
    そのまま body に入るので、このプログラムの用途では触らない。
    """
    try:
        return service.documents().get(documentId=document_id).execute()
    except HttpError as error:
        status = error.resp.status
        raise VerifyError(
            f"[{status}] ドキュメントを読み取れませんでした（ID: {document_id}）"
        ) from error

def build_service(credentials: Credentials):
    return build("docs", "v1", credentials=credentials)


def ensure_insertable(text: str) -> str:
    """挿入できる形に整えて返す。**空なら送る前に止める。**

    移植元ではこの検査が CLI 側（`resolve_text`）にあり、**ライブラリを直接
    呼べば素通りできた**。空文字の `insertText` は Docs API が 400 で弾くが、
    *その 400 は「組み立てが悪い」としか言わないので、原因が空文字だと分からない。*

    改行を LF に揃えるのも移植元と同じ理由——CRLF のまま送ると本文に CR が
    残り、読み返したときに「送った文字列と違う」ことになる。
    """
    normalized = normalize_newlines(text or "")
    if not normalized:
        raise DocError("挿入するテキストが空です")
    return normalized


def insert_text_checked(service, document_id: str, text: str) -> dict:
    """`insert_text` の前に `ensure_insertable` を通す。

    **移植した `insert_text` はそのまま残す。** 中身を変えると
    「移植元と1文字も違わない」が言えなくなり、`tools/check_port.py` が落ちる。
    """
    return insert_text(service, document_id, ensure_insertable(text))
