"""読んだメッセージを、LINE に送る本文にする。

要件の原文は「Slack のチャンネルから情報を取得し、**必要に応じて**要約し、
LINE に送信する」（2026-08-29 に講座サイトで確認）。
**「必要に応じて」を実装に落とすのがこのモジュールの主題である。**

なぜ「常に要約」にしないのか
------------------------------------------------------------------

要約は**情報を落とす操作**である。1件の「19時から会議です」を要約しても、
短くならないうえに、元の言い回しが失われる。落とす必要が無いときに落とすのは、
ただの劣化になる。

だから分岐を持つ。

============================== ================================================
入力                            出す本文
============================== ================================================
0件                            「投稿はありませんでした」。**それでも送る**
少なく・短い                    **原文をそのまま**（要約しない）
多い or 長い                    要約。**要約だと明記して**送る
============================== ================================================

**0件でも送る。** 送らないと、通知が来ない日が「投稿が無かった」のか
「動いていない」のかを受け取る側から区別できない（課題10 ``notify_schedule``
と同じ判断）。

閾値はこちらの都合の数字である
------------------------------------------------------------------

``SUMMARIZE_THRESHOLD_CHARS`` は**LINE の仕様ではない**。相手の上限を定数に
置くと、相手が変えた日に正しい送信を拒む側で壊れる（``line_send`` が長さ検査を
持たないのと同じ理由）。ここは「電話の画面で読み下せる長さ」という**こちらの
基準**なので、決めてよい。決めた以上、根拠を書く。

Slack の変換を戻す（課題7の逆問題）
------------------------------------------------------------------

課題7では「送る前に Slack の変換を再現する」ことで読み返しの照合を成立させた。
今回は**読む側**なので逆をやる。やらないと、要約する相手に ``&amp;`` という
文字列をそのまま見せることになり、要約にも ``&amp;`` が現れる。

**表示名には解決しない。** 表示名を引くには ``users:read`` が要るが、この
アプリのスコープは ``chat:write`` と ``channels:history`` だけである
（2026-08-29 実測）。引けないものを引けたように見せず、ID のまま出す。
"""

from __future__ import annotations

from slack_read import SlackMessage

#: これを超える長さなら要約する。**LINE の上限ではなく、読みやすさの線引き。**
#: 電話の画面で数スクロールに収まる量として決めた。
SUMMARIZE_THRESHOLD_CHARS = 600

#: これ以上の件数なら、短くても要約する。会話は件数が増えるほど筋が追いにくい。
MIN_MESSAGES_TO_SUMMARIZE = 6

#: Slack が本文の保存時に変換する3文字。**戻す順番に意味がある**（下記）。
_UNESCAPES = (("&lt;", "<"), ("&gt;", ">"), ("&amp;", "&"))


def unescape_slack(text: str) -> str:
    """Slack の保存時変換を戻す。

    **``&amp;`` を最後に戻す。** 先に戻すと、本文として書かれた ``&amp;lt;`` が
    ``&lt;`` になり、続く段で ``<`` まで進んで**書いていない文字**が現れる。
    課題7の ``escape_for_slack`` が ``&`` を最初に置換したのと裏返しの理由。

    **この3つ以外は戻さない。** Slack が変換するのはこの3文字だけで、
    ``&quot;`` のような他の実体参照は本文としてそう書かれたものである。
    """
    for entity, char in _UNESCAPES:
        text = text.replace(entity, char)
    return text


def render_transcript(messages: list[SlackMessage]) -> str:
    """要約に渡す／そのまま送る形に整える。

    ユーザーは ID のまま出す（上記のとおり表示名は引けない）。

    **スレッドの返信には印を付ける。** 返信は ``conversations.history`` に
    出ないので（2026-09-04 実測）、受け取った人が Slack を開いても
    チャンネル本文には並んでいない。印が無いと、どれがチャンネルの発言で
    どれがスレッドの中の発言かを**復元できない**。

    返信かどうかは ``thread_ts`` と ``ts`` の比較で決める（公式の見分け方）。
    **等しければ親**であって返信ではない。
    """
    return "\n".join(
        f"{'↳ ' if is_reply(m) else ''}{m.user}: {unescape_slack(m.text)}"
        for m in messages
    )


def is_reply(message: SlackMessage) -> bool:
    """スレッドの返信か。**親を返信と数えない。**

    公式の見分け方は「``thread_ts`` と ``ts`` が等しければ親、違えば返信」。
    ``thread_ts`` の有無だけで決めると、**親まで返信として数えて**
    件数の内訳が狂う。
    """
    return bool(message.thread_ts) and message.thread_ts != message.ts


def needs_summary(messages: list[SlackMessage]) -> bool:
    """要約すべきかを答える。**0件と1件の短文は要約しない。**

    **0件の早期 return は置いていない。** 一見あったほうが親切だが、
    0件なら下の2つがどちらも偽になる（``len([]) >= 6`` は偽、
    空の transcript の長さは 0）ので、**外しても結果が変わらないガード**になる。

    ミューテーションで実際に素通りした（2026-08-29）。外しても落ちない行は、
    読む人に「ここで何かを守っている」と誤解させるだけなので消す
    （課題7・8で同じ判断をしている）。
    """
    if len(messages) >= MIN_MESSAGES_TO_SUMMARIZE:
        return True
    return len(render_transcript(messages)) > SUMMARIZE_THRESHOLD_CHARS


def build_prompt(transcript: str) -> str:
    """要約の指示を組む。

    **「書かれていないことを足すな」を明示する。** 要約は入力に無いことを
    言い出す失敗をする。プロンプトで完全には防げないが、書かないよりは効く。
    そのうえで、**この指示だけを根拠に「正しい」と主張しない**——
    検証は別（本文には常に件数と、要約であることを併記する）。
    """
    if not (transcript or "").strip():
        raise ValueError("要約する材料がありません")

    return (
        "次は Slack チャンネルの発言ログです。"
        "要点を日本語で3〜5行にまとめてください。"
        "決まったこと・保留になっていることが分かるように書いてください。"
        "**ログに書かれていないことは足さないでください。**"
        "発言者は Slack のユーザーIDのままで構いません。\n\n"
        f"{transcript}"
    )


def build_message(
    *,
    summary: str | None,
    messages: list[SlackMessage],
    channel_label: str,
    skipped: dict[str, int] | None = None,
    truncated: bool = False,
    permalink: str = "",
    reply_count: int = 0,
) -> str:
    """LINE に送る本文を組む。

    **件数は必ず出す。** 受け取った側が Slack を開いて数と突き合わせられる。
    **要約なら要約と書く。** 原文と要約が同じ見た目で届くと「これで全部」と読まれる。
    情報を落としたことは、落とした側が言う。

    **要約したときだけ原文へのリンクを置く。** 2026-08-29 の実機で、要約が
    原文に無い語を足し、別人の発言を1人に畳んだ。プロンプトで禁じても起きる。
    要約は**入口**であって正本ではない、という関係を本文の形で表す。

    原文をそのまま送るときは付けない——落としていないものへの
    「原文はこちら」は冗長で、リンクの意味を薄める。
    """
    count = len(messages)
    # **返信の件数は内訳として出す。** 受け取った人が Slack を開いても、
    # 返信はチャンネル本文に並んでいない（``conversations.history`` に
    # 出ないため・2026-09-04 実測）。合計だけ出すと「5件と書いてあるのに
    # 2件しか見えない」になり、**この本文が数えた根拠を追えなくなる**。
    headline = f"【{channel_label}】新着 {count} 件"
    if reply_count:
        headline += f"（うちスレッド返信 {reply_count} 件）"
    lines = [headline]

    if count == 0:
        lines.append("投稿はありませんでした。")
    elif summary is not None:
        lines.append("")
        lines.append("―― 要約 ――")
        lines.append(summary.strip())
        if permalink.strip():
            lines.append("")
            lines.append(f"▼ 原文（要約は入口です）: {permalink.strip()}")
    else:
        lines.append("")
        lines.append(render_transcript(messages))

    for subtype, number in sorted((skipped or {}).items()):
        # 「3件」と書いてあるのに Slack には5件ある、という食い違いを
        # 受け取る側が自力で解けるようにする。
        lines.append(f"（{subtype} {number} 件は要約の対象外）")

    if truncated:
        # 黙って切ると、欠けた要約が完全な要約として届く。
        lines.append("（取得を上限で打ち切りました。続きが残っています）")

    return "\n".join(lines)
