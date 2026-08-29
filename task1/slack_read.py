"""Slack のチャンネルから、前回の続きを読む。

課題1（Slack → 要約 → LINE）の入力側。**この層は要約も送信もしない。**
守るのは1つだけ——**静かに減らないこと**。

なぜそこだけを守るのか
------------------------------------------------------------------

課題の目的は「重要な情報を LINE で受け取り、**見逃しを防ぐ**」である。
見逃しは例外を出さない。件数が減るだけで、受け取る側からは
「投稿が無かった日」と見分けがつかない。だから**減る経路を先に塞ぐ**。

公式リファレンスで確認した罠（2026-08-29）
------------------------------------------------------------------

============================ ==================================================
仕様                          効き方
============================ ==================================================
``oldest`` は**排他**         「Only messages **after** this Unix timestamp」。
                              前回の最新 ts を渡せば、その1件は重複しない
``inclusive`` の既定は false  境界を含めるかの切り替え。**使わない**（下記）
``limit`` は **best-effort**  「Fewer than the requested number of items may be
                              returned, **even if the end of the conversation
                              history hasn't been reached**」
ページング                     ``response_metadata.next_cursor``
============================ ==================================================

**``limit`` の一文がいちばん危ない。** ``len(messages) < limit`` を
「終わりまで読んだ」と解釈する実装は、相手が少なく返した回だけ静かに取りこぼす。
終わりを決めてよいのは ``next_cursor`` が空になったときだけである。

``inclusive`` を使わない理由
------------------------------------------------------------------

包含にすると**毎回1件重複**し、要約に同じ発言が繰り返し混ざる。
一方、排他でも取りこぼしは起きない——前回の最新 ts **より後**を取るので、
その ts の発言はすでに読んでいる。

取りこぼし対策は別の場所で作る。**位置の更新を LINE 送信が成功した後に置く。**
途中で落ちた回は位置が進まないので、次回また同じ範囲を読む
（重複はするが、取りこぼさない＝at-least-once）。

``subtype`` は「残す側」に倒す
------------------------------------------------------------------

除外するのは ``channel_join`` / ``channel_leave`` だけを名指しする。
「subtype があるものは全部捨てる」と書くと ``bot_message`` や将来増える種類まで
落ちるが、**知らないものを黙って捨てる向きは「見逃しを防ぐ」という目的に反する**。

そのうえで、**捨てた件数は種類別に数えて残す**。件数が合わないときに
「読めなかった」のか「捨てた」のかを区別できるようにするため。
"""

from __future__ import annotations

from dataclasses import dataclass, field

#: 要約の材料にしない subtype。**名指しで並べる**（上の方針）。
EXCLUDED_SUBTYPES = frozenset({"channel_join", "channel_leave"})

#: 1ページあたりの要求件数。相手はこれより少なく返してよい（best-effort）。
DEFAULT_PAGE_LIMIT = 200

#: 何ページまで辿るか。**上限そのものより、上限に当たったと言うことが大事。**
DEFAULT_MAX_PAGES = 10


@dataclass(frozen=True)
class SlackMessage:
    """要約に渡せる形まで還元した1件。

    ``ts`` は**文字列のまま**持つ。``1503435956.000247`` を float にすると
    倍精度で表しきれず、末尾が変わって別のメッセージを指す（課題7の実測）。
    """

    ts: str
    user: str
    text: str
    subtype: str = ""


@dataclass(frozen=True)
class Fetched:
    """読み取りの結果。**欠けたことが分かる形で返す。**"""

    messages: list[SlackMessage]
    #: 除外した件数（subtype 別）。捨てたものを数えて残す。
    skipped: dict[str, int] = field(default_factory=dict)
    #: 実際に辿ったページ数。
    pages: int = 0
    #: 上限で打ち切ったか。**真なら「全部読んだ」とは言えない。**
    truncated: bool = False
    #: 次回の起点。**0件のときは None**（進めない）。
    latest_ts: str | None = None


def _messages_of(response) -> list[dict]:
    """応答から messages を取り出す。

    ``response.get(...)`` だけで読む。実物の ``SlackResponse`` は Mapping なので、
    この読み方なら本物と偽物で経路が変わらない。属性アクセスを使うと
    偽物にだけ生えている属性を実装が頼りはじめ、実機で初めて落ちる
    （課題10 Discord で ``identity.id`` が実在せず、実機で落ちた）。
    """
    messages = response.get("messages")
    return messages if isinstance(messages, list) else []


def _next_cursor_of(response) -> str:
    metadata = response.get("response_metadata")
    if not isinstance(metadata, dict):
        return ""
    return str(metadata.get("next_cursor") or "").strip()


def fetch_permalink(client, *, channel: str, ts: str) -> str:
    """そのメッセージへのリンクを取る。**取れなければ空を返す。**

    要約の下に置く「原文への入口」に使う。要約は入力に無いことを言い出す
    （2026-08-29 の実機で、``Drive & Notion`` が「Google DriveとNotion」になり、
    別人の発言が1人に畳まれた。プロンプトで禁じても起きた）。
    目的は「見逃しを防ぐ」ことであって「要約を信じさせる」ことではないので、
    **原文へ辿れる道を残す**。

    ``chat.getPermalink`` は**スコープを要求しない**（課題7で公式リファレンスに
    当たって確認済み）。権限を増やさずに足せる。

    **リンクが取れないことで送信を止めない。** リンクは「あると良いもの」で、
    無くても通知の目的は果たせる。ただし**静かにしない**——空を返したことは、
    呼ぶ側が画面に出す。
    """
    try:
        response = client.chat_getPermalink(channel=channel, message_ts=ts)
    except Exception:  # noqa: BLE001 - リンクの失敗で本題を止めない
        return ""

    if not response.get("ok"):
        return ""

    return str(response.get("permalink") or "").strip()


def fetch_since(
    client,
    *,
    channel: str,
    oldest: str | None = None,
    page_limit: int = DEFAULT_PAGE_LIMIT,
    max_pages: int = DEFAULT_MAX_PAGES,
) -> Fetched:
    """``oldest`` より後のメッセージを、cursor が尽きるまで読む。

    **0件は正常値。** 「投稿が無かった」は伝えるべき情報であって失敗ではない。
    """
    raw: list[dict] = []
    cursor = ""
    pages = 0
    truncated = False
    seen_cursors: set[str] = set()

    while True:
        params: dict = {"channel": channel, "limit": page_limit}
        if oldest:
            # 空文字は渡さない。渡すと「指定した」と解釈される余地がある。
            params["oldest"] = oldest
        if cursor:
            params["cursor"] = cursor

        response = client.conversations_history(**params)
        pages += 1
        raw.extend(_messages_of(response))

        cursor = _next_cursor_of(response)
        if not cursor:
            # **終わりを決めてよいのはここだけ。** 件数では決めない。
            break

        if cursor in seen_cursors:
            # 同じ cursor が返り続ける形。回り続けるとレート制限を静かに焼く。
            truncated = True
            break
        seen_cursors.add(cursor)

        if pages >= max_pages:
            truncated = True
            break

    # 並び順を信用しない。相手が新しい順で返すのは今そうというだけ。
    raw.sort(key=lambda item: str(item.get("ts") or ""))

    # **位置は除外したものも含めて進める。** 読んだことは事実で、
    # ``channel_join`` しか無かった回に位置を止めると同じものを読み直す。
    latest_ts = str(raw[-1].get("ts") or "") if raw else None

    skipped: dict[str, int] = {}
    messages: list[SlackMessage] = []
    for item in raw:
        subtype = str(item.get("subtype") or "")
        if subtype in EXCLUDED_SUBTYPES:
            skipped[subtype] = skipped.get(subtype, 0) + 1
            continue
        messages.append(
            SlackMessage(
                ts=str(item.get("ts") or ""),
                # bot の投稿は user を持たず bot_id を持つ。どちらも無い形もある。
                user=str(item.get("user") or item.get("bot_id") or ""),
                text=str(item.get("text") or ""),
                subtype=subtype,
            )
        )

    return Fetched(
        messages=messages,
        skipped=skipped,
        pages=pages,
        truncated=truncated,
        latest_ts=latest_ts,
    )
