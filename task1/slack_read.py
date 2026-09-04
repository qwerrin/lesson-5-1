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
    #: スレッドの一部なら親の ``ts``。**返信は親と等しくない**（公式の見分け方）。
    thread_ts: str = ""
    #: 親が抱えている返信の数。0 なら見張る必要が無い。
    reply_count: int = 0


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


@dataclass(frozen=True)
class RepliesFetched:
    """スレッドの返信の読み取り結果。**欠けたことが分かる形で返す。**"""

    messages: list[SlackMessage] = field(default_factory=list)
    #: 除外した件数（subtype 別）。
    skipped: dict[str, int] = field(default_factory=dict)
    #: 親ごとの「ここまで読んだ」。**取れなかった親は入れない**（進めない）。
    latest_by_parent: dict[str, str] = field(default_factory=dict)
    #: 読み取りに失敗した親の ``ts``。**静かにしない**。
    failed: tuple[str, ...] = ()
    #: どれか1本でも上限で打ち切ったか。
    truncated: bool = False


def _as_message(item: dict, *, subtype: str) -> SlackMessage:
    return SlackMessage(
        ts=str(item.get("ts") or ""),
        # bot の投稿は user を持たず bot_id を持つ。どちらも無い形もある。
        user=str(item.get("user") or item.get("bot_id") or ""),
        text=str(item.get("text") or ""),
        subtype=subtype,
        thread_ts=str(item.get("thread_ts") or ""),
        reply_count=int(item.get("reply_count") or 0),
    )


def _ts_key(ts: str) -> tuple[int, int] | None:
    """``ts`` を数として比べるための鍵。**float にしない。**

    ``1503435956.000247`` を float にすると倍精度で表しきれず、末尾が変わって
    別のメッセージを指す（課題7の実測）。文字列比較でも駄目で、桁数が違うと
    ``"9" > "10"`` が真になり、境界の1件が静かに落ちる。

    読めなければ ``None`` を返す。**呼ぶ側は「残す側」に倒す**——
    知らないものを黙って捨てる向きは、課題の目的に反する。
    """
    head, _, tail = str(ts).partition(".")
    if not head.isdigit() or (tail and not tail.isdigit()):
        return None
    return (int(head), int(tail.ljust(6, "0")[:6]) if tail else 0)


def _is_after(ts: str, boundary: str) -> bool:
    """``boundary`` より後か。**読めない値は後ろ扱い**（＝残す）。"""
    if not boundary:
        return True
    left, right = _ts_key(ts), _ts_key(boundary)
    if left is None or right is None:
        return True
    return left > right


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


def _walk(call, params: dict, *, page_limit: int, max_pages: int):
    """cursor が尽きるまで辿る。**チャンネル本文と返信で同じ歩き方をする。**

    以前はこの形が2箇所にコピーされていた。壊す道具から見ると
    「置換先が2件見つかる」として現れ、**1箇所を狙った検査が両方に当たって
    どちらも壊せない**状態になっていた。コピーを直すのではなく消した。

    **終わりを決めてよいのは ``next_cursor`` が空になったときだけ。**
    ``limit`` は best-effort で、非 Marketplace アプリでは最大値も既定値も
    15 に下がる（公式リファレンス）。件数で終わりを決めると、
    相手が少なく返した回だけ静かに取りこぼす。
    """
    raw: list[dict] = []
    cursor = ""
    pages = 0
    truncated = False
    seen_cursors: set[str] = set()

    while True:
        request = {**params, "limit": page_limit}
        if cursor:
            request["cursor"] = cursor

        response = call(**request)
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

    return raw, pages, truncated


def _in_order(raw: list[dict]) -> list[dict]:
    """``ts`` を**数として**並べる。読めないものは後ろへ（＝残す側）。

    以前は文字列で並べていた。本物の ``ts`` は桁が揃っているので実害は
    出ていなかったが、桁が違えば ``"10" < "2"`` になり、並びだけでなく
    **次回の起点まで狂う**。発展（スレッドの返信）のテストが掘り当てた。
    """
    return sorted(
        raw,
        key=lambda item: (
            _ts_key(str(item.get("ts") or "")) is None,
            _ts_key(str(item.get("ts") or "")) or (0, 0),
        ),
    )


def _classify(raw: list[dict], skipped: dict) -> list[SlackMessage]:
    """除外を**数えながら** SlackMessage に変換する。

    捨てた件数を種類別に残すのは、件数が合わないときに「読めなかった」のか
    「捨てた」のかを区別できるようにするため。
    """
    messages: list[SlackMessage] = []
    for item in raw:
        subtype = str(item.get("subtype") or "")
        if subtype in EXCLUDED_SUBTYPES:
            skipped[subtype] = skipped.get(subtype, 0) + 1
            continue
        messages.append(_as_message(item, subtype=subtype))
    return messages


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
    params: dict = {"channel": channel}
    if oldest:
        # 空文字は渡さない。渡すと「指定した」と解釈される余地がある。
        params["oldest"] = oldest

    raw, pages, truncated = _walk(
        client.conversations_history,
        params,
        page_limit=page_limit,
        max_pages=max_pages,
    )

    # 並び順を信用しない。相手が新しい順で返すのは今そうというだけ。
    raw = _in_order(raw)

    # **位置は除外したものも含めて進める。** 読んだことは事実で、
    # ``channel_join`` しか無かった回に位置を止めると同じものを読み直す。
    latest_ts = str(raw[-1].get("ts") or "") if raw else None

    skipped: dict[str, int] = {}
    messages = _classify(raw, skipped)

    return Fetched(
        messages=messages,
        skipped=skipped,
        pages=pages,
        truncated=truncated,
        latest_ts=latest_ts,
    )


def fetch_replies(
    client,
    *,
    channel: str,
    watch: dict,
    page_limit: int = DEFAULT_PAGE_LIMIT,
    max_pages: int = DEFAULT_MAX_PAGES,
) -> RepliesFetched:
    """見張っている親について、前回より後の返信を読む。

    なぜ要るのか（2026-09-04 実測）
    --------------------------------------------------------------

    ``conversations.history`` は**スレッドの返信を1件も返さない**。
    親1件・返信3件を投稿したとき、history の増分は **+1 件**だった。
    返信は例外を出さずに落ちる——件数が減るだけなので、受け取る側からは
    「議論が無かった」と見分けが付かない。**課題の目的語そのもの**である。

    公式リファレンスはこれを明言していない（「返信は
    ``conversations.replies`` を使え」と書いてあるだけ）。だから実測で閉じた。

    ``watch`` の形
    --------------------------------------------------------------

    ``{親の ts: 前回読んだ最後の返信の ts}``。値が空文字なら「まだ1件も
    読んでいない」。**位置を親ごとに持つ**のは、チャンネル本文の位置
    （``cursors``）で代用できないからである——返信の ts は親より後なので、
    返信で本文の位置を進めると**親より後のチャンネル投稿を飛ばす**。

    ``conversations.replies`` は**親を先頭に含めて返す**（2026-09-04 実測）。
    そのまま足すとチャンネル本文と重複するので、親を落とす。

    **1本失敗しても他を止めない。** ただし静かにしない——失敗した親は
    ``failed`` に入れ、**位置も進めない**（進めると二度と読まれない）。
    """
    messages: list[SlackMessage] = []
    skipped: dict[str, int] = {}
    latest_by_parent: dict[str, str] = {}
    failed: list[str] = []
    truncated = False

    for parent_ts, after_ts in watch.items():
        try:
            raw, _pages, hit_limit = _walk(
                client.conversations_replies,
                {"channel": channel, "ts": parent_ts},
                page_limit=page_limit,
                max_pages=max_pages,
            )
        except Exception:  # noqa: BLE001 - 1本の失敗で他のスレッドを止めない
            failed.append(parent_ts)
            continue

        truncated = truncated or hit_limit

        # 親を落とす。**足すとチャンネル本文と重複する。**
        items = _in_order(
            [item for item in raw if str(item.get("ts") or "") != parent_ts]
        )

        # **位置は除外したものも含めて進める。** 読んだことは事実で、
        # ``channel_join`` しか無かった回に止めると同じものを読み直す。
        readable = [item for item in items if _ts_key(str(item.get("ts") or ""))]
        if readable:
            latest_by_parent[parent_ts] = str(readable[-1].get("ts") or "")

        boundary = str(after_ts or "")
        fresh = [
            item for item in items if _is_after(str(item.get("ts") or ""), boundary)
        ]
        messages.extend(_classify(fresh, skipped))

    return RepliesFetched(
        messages=messages,
        skipped=skipped,
        latest_by_parent=latest_by_parent,
        failed=tuple(failed),
        truncated=truncated,
    )


def in_time_order(messages: list[SlackMessage]) -> list[SlackMessage]:
    """時系列に並べた**新しい**リストを返す。

    チャンネル本文と返信は別々の口から来るので、混ぜたら並べ直す必要がある。
    読めない ``ts`` は後ろへ回す（``_is_after`` と同じ「残す側」の向き）。
    """
    return sorted(
        messages,
        key=lambda m: (_ts_key(m.ts) is None, _ts_key(m.ts) or (0, 0)),
    )

