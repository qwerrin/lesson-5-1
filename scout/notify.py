"""1回の実行の結果を LINE に**1通**送る段。**0件でも、途中で失敗しても送る**（M7）。

無音は2つの意味を持つ——「今日は何も無かった」と「収集が死んだ」が同じ見た目になる
（教訓 `silence-has-two-meanings`）。毎回1通送れば、届かないこと自体が異常の印になる。

状態は自分で判定する
--------------------------------------------------------------------------

呼び出し側から「正常」を受け取らない。**各段の結果を見て**決める。
判定を外に置くと、呼び出し側のバグがそのまま「正常」として届く。

再送キー（2026-10-03 に公式で確認）
--------------------------------------------------------------------------

LINE Developers「Retry failed API requests」:

- 同じ `X-Line-Retry-Key` の2回目は **409** で、`x-line-accepted-request-id` と、
  push なら**最初と同じ `sentMessages.id`** が返る
- キーは**最初のリクエストから24時間**有効
- **同じキーで中身や宛先を変えない**こと

だからキーは **run-id と本文**から決める。同じ回を同じ中身で送り直せば1通にまとまり、
中身が変わった送り直し（2回目は `emit` が「使用済み」で止まる、など）は別の1通になる。

`common/line_send.push` は使わない——409 を例外にするので「送ってあった」と区別できない。
合格済みの共有部品は書き換えず、**本文の形・応答の読み方・通数・宛先の伏せ方**だけ借りる。

接続は持たない
--------------------------------------------------------------------------

`session` は差し込み。`.env` もここでは読まない（`cli` が組み立てる）。
**テストが本番の宛先へ配達しない**ことを、構造で保証する。
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime

import requests

from common import line_auth, line_send
from emit import Emitted
from fetch import EMPTY, FAILED, OK, Harvest
from summarize import Digest
from verify_source import CONFIRMED, Audit

NORMAL = "normal"
ATTENTION = "attention"
ABNORMAL = "abnormal"
LABELS = {NORMAL: "正常", ATTENTION: "注意", ABNORMAL: "異常"}

#: 再送キーの名前空間。**決まった値**にしておく——変えると、同じ回でも別のキーになる。
RETRY_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_URL, "https://github.com/qwerrin/lesson-5-1/scout/notify")


@dataclass(frozen=True)
class Health:
    level: str
    #: **異常の理由を先に**並べる。
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class Delivered:
    message_id: str
    request_id: str
    #: 409 で「同じ中身を既に受け付けた」と言われた。
    duplicate: bool
    accepted_request_id: str
    to_masked: str
    #: **読めなかったことを 0 と混ぜない。**
    usage_before: int | None
    usage_after: int | None

    @property
    def summary(self) -> str:
        how = (
            f"送ってあった（同じ内容・messageId {self.message_id}・最初の受付 {self.accepted_request_id}）"
            if self.duplicate
            else f"送った（messageId {self.message_id}）"
        )
        return f"{how}／今月の通数 {_count(self.usage_before)} -> {_count(self.usage_after)}／宛先 {self.to_masked}"


def judge(
    *,
    harvest: Harvest,
    digest: Digest,
    audit: Audit,
    emitted: Emitted | None,
    emit_error: str | None,
) -> Health:
    """**各段の結果から**状態を決める。0件は異常ではない。"""
    if (emitted is None) == (emit_error is None):
        raise ValueError("emitted と emit_error は、どちらか一方だけを渡す")

    abnormal: list[str] = []
    attention: list[str] = []
    if not harvest.results:
        # **1件も処理しなかったのに成功**にしない（M9）。
        abnormal.append("取得元が1つも無い（何も取りに行っていない）")
    for result in harvest.results:
        if result.status == FAILED:
            abnormal.append(f"取得元 {result.source} が取れなかった（{_line(result.detail)}）")
        elif result.status not in (OK, EMPTY):
            # **知らない状態を正常に倒さない。** `fetch` が状態を足した日に黙って通る。
            abnormal.append(f"取得元 {result.source} の状態が分からない（{_line(result.status)}）")
        elif result.more:
            attention.append(f"取得元 {result.source} にまだ先がある（取りこぼし）")
    if digest.failed:
        abnormal.append(f"要約できなかった {len(digest.failed)} 件")
    if len(audit.checks) != len(digest.done):
        # 照合が要約を覆っていない。**0件の照合は「不一致0件」に見える。**
        abnormal.append(f"照合 {len(audit.checks)} 件が要約 {len(digest.done)} 件と合わない")
    if emitted is None:
        abnormal.append("Inbox に書けなかった")
    elif not emitted.ok:
        abnormal.append(f"Inbox の読み戻しに問題 {len(emitted.problems)} 件")
    unconfirmed = sum(1 for c in audit.checks if c.verdict != CONFIRMED)
    if unconfirmed:
        attention.append(f"照合できなかった要約 {unconfirmed} 件")

    level = ABNORMAL if abnormal else ATTENTION if attention else NORMAL
    return Health(level=level, reasons=(*abnormal, *attention))


def compose(
    *,
    at: datetime,
    run_id: str,
    health: Health,
    stages: Sequence[str],
    emitted: Emitted | None,
    emit_error: str | None,
) -> str:
    """通知の本文。**記事のタイトルは載せない**——通知は入口で、中身は Inbox にある。"""
    lines = [f"【scout】{LABELS[health.level]}｜{at:%Y-%m-%d %H:%M}", f"run {run_id}"]
    if health.reasons:
        lines.append("")
        lines.extend(f"・{_line(r)}" for r in health.reasons)
    lines.append("")
    lines.extend(f"- {_line(s)}" for s in stages)
    lines.append("")
    if emitted is not None:
        lines.append(_line(emitted.summary))
    else:
        lines.append(f"Inbox に書けなかった: {_line(emit_error or '')}")
    return "\n".join(lines)


def retry_key(run_id: str, body: str) -> str:
    """**同じ回・同じ中身なら同じキー。**

    区切りは JSON の配列にする。改行で区切ると、`emit` が弾いた（＝改行を含みうる）run-id で
    `("a\\nb", "c")` と `("a", "b\\nc")` がぶつかる。
    """
    return str(uuid.uuid5(RETRY_NAMESPACE, json.dumps([run_id, body], ensure_ascii=False)))


def send(
    session,
    *,
    to: str,
    body: str,
    run_id: str,
    secrets: tuple = (),
    base: str = line_auth.API_BASE,
) -> Delivered:
    """1通送る。**送ったら、後で数えられなくても送ったことを返す。**"""
    # 本文は各段の文言の寄せ集めで、例外の文字列も入る。**送る直前の1か所で伏せる。**
    body = line_auth.redact(body, *secrets)
    # 空の本文は**通数を使う前に**止める（`build_payload` が投げる）。
    payload = line_send.build_payload(to=to, text=body)

    # **数えられなくても送る。** ここで守りたいのは証拠より、無音にしないこと。
    usage_before = _usage(session, base=base, secrets=secrets)

    try:
        response = session.post(
            base + line_send.PUSH_PATH,
            json=payload,
            headers={"X-Line-Retry-Key": retry_key(run_id, body)},
        )
    except requests.RequestException as e:
        # 応答が返らない失敗。**素の例外のまま出さない**——呼び出し側は LineError だけを待っている。
        raise line_auth.ApiError(
            line_auth.redact(f"LINE へ送れなかった（通信）: {type(e).__name__}: {e}", *secrets)
        ) from e
    accepted = _header(response, "x-line-accepted-request-id")
    # **受け付け済みの ID が付いた 409 だけ**を「送ってあった」にする。他の 409 は失敗。
    duplicate = response.status_code == 409 and bool(accepted)
    if not duplicate:
        line_auth.raise_for_line_error(response, *secrets)
    # 409 でも、何を送ってあったかの ID が無ければ失敗（`read_send_result` が投げる）。
    sent = line_send.read_send_result(response)

    usage_after = _usage(session, base=base, secrets=secrets)
    return Delivered(
        message_id=sent.message_id,
        request_id=sent.request_id,
        duplicate=duplicate,
        accepted_request_id=accepted,
        to_masked=line_send.mask_destination(to),
        usage_before=usage_before,
        usage_after=usage_after,
    )


def _usage(session, *, base: str, secrets: tuple) -> int | None:
    try:
        return line_send.fetch_usage(session, base=base, secrets=secrets)
    except (line_send.SendError, line_auth.LineError, requests.RequestException):
        # **HTTP の失敗だけでなく、応答が返らない失敗も。** 数えられないことで送信を止めない。
        return None


def _header(response, name: str) -> str:
    """**ヘッダ名は大小を区別しない**（`line_auth.request_id_of` と同じ理由）。"""
    headers: Mapping[str, str] = getattr(response, "headers", None) or {}
    for key, value in headers.items():
        if str(key).lower() == name:
            return str(value)
    return ""


def _count(value: int | None) -> str:
    return "読めず" if value is None else str(value)


def _line(text: str) -> str:
    return " ".join(text.split())
