"""前回どこまで読んだかを覚える。

課題の目的は「**見逃しを防ぐ**」である。見逃さないためには、前回の続きから
読む必要がある。その「続き」がここに入る。

分からなくなったら止まる
------------------------------------------------------------------

この層の失敗は**静かではなく、高くつく**。位置を見失った実行は、
チャンネルの全履歴を読み直して巨大な要約を作り、LINE へ送る。
無料プランは月200通、Gemini は呼ぶたびに金額が動く。

だから**壊れたファイルを「初回」と解釈しない**。読めなかったことを
読めなかったと言って止め、人に直させる。「エラーにならない失敗」を
自分から作らない（課題6で踏んだ「空が正常値の欄はバグが静かな側に倒れる」の
裏返しで、ここでは**危険な側に倒れる**）。

なぜチャンネルごとに持つのか
------------------------------------------------------------------

1本の値を使い回すと、見るチャンネルを変えた瞬間に前のチャンネルの位置を使う。
新しいチャンネルの ts と噛み合わず、**大量に読み直すか、何も読まないか**の
どちらかになる。どちらも「見逃しを防ぐ」に反する。

なぜ差し替えで書くのか
------------------------------------------------------------------

上書きの途中で落ちると、半端な JSON が残る。次回は ``StateError`` で止まるので
危険はないが、**人が直すまで動かない**。一時ファイルに書いてから差し替えれば、
落ちても元の内容が残って動き続ける。

**位置を進めるのは、LINE への送信が成功した後。** ここは値を持つだけで、
いつ進めるかは呼ぶ側が決める。送信前に進めると、送信に失敗した回の内容を
次回も読まない＝取りこぼす。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, replace
from pathlib import Path


class StateError(Exception):
    """状態ファイルを信用できない。利用者にそのまま見せられる。"""


@dataclass(frozen=True)
class State:
    """チャンネルごとの「ここまで読んだ」。

    ``ts`` は**文字列**で持つ。float にすると倍精度で表しきれず、
    末尾が変わって別のメッセージを指す（課題7の実測）。
    """

    cursors: dict = field(default_factory=dict)
    #: チャンネル → {親の ts: 最後に読んだ返信の ts}。**空文字は「まだ0件」**。
    threads: dict = field(default_factory=dict)


#: 見張る親の上限。**上限そのものより、超えたと言うことが大事。**
#:
#: 1実行あたり ``conversations.replies`` をこの回数だけ呼ぶ。Slack の Tier 3 は
#: 毎分 50+ なので 20 は余裕がある。増やすほど古い議論を拾えるが、
#: そのぶん毎回叩く。
WATCH_LIMIT = 20


def empty() -> State:
    return State(cursors={}, threads={})


def cursor_for(current: State, channel: str) -> str | None:
    """そのチャンネルの前回位置。無ければ None（初回）。"""
    value = current.cursors.get(channel)
    return value if isinstance(value, str) and value else None


def advanced(current: State, channel: str, ts: str) -> State:
    """位置を進めた**新しい** State を返す。

    **元を書き換えない。** 呼ぶ側は「送信に成功したら進める」順で使うので、
    元が変わる実装だと、失敗した経路でも進んだ値が残って取りこぼす。
    """
    value = (ts or "").strip()
    if not value:
        raise ValueError("空の ts では位置を進められません")

    return replace(current, cursors={**current.cursors, channel: value})


def watch_for(current: State, channel: str) -> dict:
    """そのチャンネルで見張っている親。``{親の ts: 最後に読んだ返信の ts}``。"""
    value = current.threads.get(channel)
    return dict(value) if isinstance(value, dict) else {}


def watching(current: State, channel: str, parents) -> tuple[State, tuple[str, ...]]:
    """見張る親を足した**新しい** State と、**窓から落ちた親**を返す。

    既に読んだ位置は**上書きしない**。上書きすると同じ返信を読み直して、
    要約に同じ発言が重複して混ざる。

    見張る親は増え続けるので ``WATCH_LIMIT`` で切る。**切ったことを返り値に
    出す**——落ちた親に後から付いた返信は二度と読まれないので、
    呼ぶ側が画面に出せないと「静かに減る」ことになる。
    """
    known = watch_for(current, channel)

    for parent in parents:
        key = str(parent or "").strip()
        if not key:
            continue
        known.setdefault(key, "")

    dropped: tuple[str, ...] = ()
    if len(known) > WATCH_LIMIT:
        # 挿入順＝古い順。古いほうから落とす。
        overflow = len(known) - WATCH_LIMIT
        keys = list(known)
        dropped = tuple(keys[:overflow])
        known = {key: known[key] for key in keys[overflow:]}

    return replace(current, threads={**current.threads, channel: known}), dropped


def replies_advanced(current: State, channel: str, latest_by_parent: dict) -> State:
    """スレッドごとの位置を進めた**新しい** State を返す。

    **見張っていない親は足さない。** 足すと窓の意味が消えて、
    ``threads`` が無限に伸びる。
    """
    known = watch_for(current, channel)

    for parent, ts in latest_by_parent.items():
        key = str(parent or "")
        value = str(ts or "").strip()
        if key in known and value:
            known[key] = value

    return replace(current, threads={**current.threads, channel: known})


def load(path: str | Path) -> State:
    """状態を読む。**無いのは正常、壊れているのは異常。**"""
    target = Path(path)

    if not target.exists():
        return empty()

    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise StateError(
            f"状態ファイルを読めませんでした: {target}\n"
            f"{error}\n"
            "**初回として続行しません。** 続行すると全履歴を読み直して、"
            "巨大な要約を1通送ることになります。\n"
            "中身を確認して直すか、意図的にやり直すならファイルを削除してください。"
        ) from error

    if not isinstance(raw, dict):
        raise StateError(
            f"状態ファイルの形が想定と違います（辞書ではありません）: {target}"
        )

    cursors = raw.get("cursors", {})
    if not isinstance(cursors, dict):
        raise StateError(f"状態ファイルの cursors が辞書ではありません: {target}")

    for channel, value in cursors.items():
        if not isinstance(value, str):
            # 数値で入っていたら黙って文字列化しない。float 経由でズレた値を
            # そのまま位置として使うことになる。
            raise StateError(
                f"状態ファイルの位置が文字列ではありません: {channel} -> {type(value).__name__}\n"
                "ts は識別子なので、数値として保存すると別のメッセージを指します。"
            )

    # **この欄は後から足した。** 持たない古いファイルは正常として読む——
    # ここで止めると、位置を見失った扱いで全履歴を読み直すことになる。
    threads = raw.get("threads", {})
    if not isinstance(threads, dict):
        raise StateError(f"状態ファイルの threads が辞書ではありません: {target}")

    for channel, per_channel in threads.items():
        if not isinstance(per_channel, dict):
            raise StateError(
                f"状態ファイルの threads の中身が辞書ではありません: {channel} -> "
                f"{type(per_channel).__name__}"
            )
        for parent, value in per_channel.items():
            if not isinstance(value, str):
                # cursors と同じ判断。数値で入っていたら黙って文字列化しない。
                raise StateError(
                    f"状態ファイルの返信の位置が文字列ではありません: "
                    f"{channel}/{parent} -> {type(value).__name__}\n"
                    "ts は識別子なので、数値として保存すると別のメッセージを指します。"
                )

    return State(
        cursors=dict(cursors),
        threads={key: dict(value) for key, value in threads.items()},
    )


def save(path: str | Path, current: State) -> None:
    """状態を書く。**一時ファイルに書いてから差し替える。**"""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)

    temporary = target.with_name(target.name + ".tmp")
    try:
        # json.dumps を先に済ませる。書けない値をファイルへ流し始めてから
        # 落ちると、一時ファイルが半端に残る。
        body = json.dumps(
            {"cursors": current.cursors, "threads": current.threads},
            ensure_ascii=False,
            indent=2,
        )
        temporary.write_text(body + "\n", encoding="utf-8")
        os.replace(temporary, target)
    finally:
        # 失敗しても一時ファイルを残さない。残ると次回の書き込みで
        # 「前回落ちた形跡」と区別が付かなくなる。
        if temporary.exists():
            temporary.unlink()
