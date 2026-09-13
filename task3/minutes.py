"""文字起こしから議事録を作る。**もっともらしさを検査する層。**

文字起こしの層（`transcribe.py`）が見たのは「**欠ける**」失敗だった。
この層で見るのは逆で、「**足してしまう**」失敗である。
*足された文はいちばん自然に読める*ので、読んでも気づけない。

============ ====================================================================
DESIGN       ここで引き受ける穴
============ ====================================================================
5-F          引用が会議の終盤まで届いているか。**丸めても例外は出ない**
5-K          決定の**逐語引用が文字起こしに実在する**ことを機械で照合する
5-P          根拠（MM:SS）を必ず持たせる。無い主張は原文と結び付けられない
5-G          `finish_reason` を見る。**打ち切られた JSON は途中で切れる**
4-②          チャットログを入力として受け取れる形にする（要件が指している）
4-⑤          日時は**人が渡す**。音声の中に入っていないので推測しない
============ ====================================================================

**自由文にしない。** 型で受ければ「空だった」ことが分かる。自由文だと
*何も決まらなかった会議と、決定を拾い損ねた出力が同じ見た目*になる。

**照合の前に正規化する。** 2026-09-12 の実機で「十月三日」が
「10 月 3 日」（分かち書きの空白つき）になっていた。
素朴な部分一致は、**正しい引用まで「原文に無い」と言う**。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common import env_file, gemini_client  # noqa: E402
from common.gemini_client import Reply  # noqa: E402
from transcribe import Utterance  # noqa: E402

#: 引用の場所と、申告された時刻がこれ以上ずれていたら食い違いとみなす（秒）。
QUOTE_TOLERANCE_SEC = 20.0

#: 引用された時刻の最大が、会議の終わりからこれ以上手前なら「終盤が落ちた」と見る（秒）。
TAIL_TOLERANCE_SEC = 90.0

#: 時刻が読めなかったことを表す値。**0 にしない**——0 にすると全部が冒頭を指す。
UNKNOWN_AT = -1

#: 根拠の出どころ。**チャットの時刻は時計の時刻で、音声のオフセットではない。**
AUDIO, CHAT = "audio", "chat"

OK_FINISH_REASONS = frozenset({"STOP", "FINISH_REASON_STOP"})

_MMSS = re.compile(r"^\s*(?:(\d+):)?(\d+):(\d+)\s*$")
_SPACE = re.compile(r"\s+")

#: **この議事録が含んでいないもの。** DESIGN 6章の「承知で残す」をそのまま出す。
#: 読む人が「何が入っていないか」を知らないまま共有されるのが、この課題で
#: いちばん危ない出口である。
NOT_INCLUDED = [
    "画面共有に映っていた内容（音声には「これで行きましょう」としか残らない）",
    "発言者の実名（声でしか区別していない。名乗った箇所だけが手がかり）",
    "参加者と欠席者（誰が聞いていたかは音に無い）",
    "無言の合意（頷きは音にならない。「反対が無かった」と「賛成した」は別）",
    "前回の決定と次回の予定（この会議の外の文脈）",
    "議題に上がらなかったこと（話されなかったことは、議事録を見ても分からない）",
]

MINUTES_SCHEMA = {
    "type": "object",
    "properties": {
        "decisions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                    "at": {"type": "string"},
                    "quote": {"type": "string"},
                    "source": {"type": "string", "enum": ["audio", "chat"]},
                },
                "required": ["text", "at", "quote", "source"],
            },
        },
        "todos": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                    "at": {"type": "string"},
                    "owner": {"type": "string"},
                    "due": {"type": "string"},
                    "quote": {"type": "string"},
                    "source": {"type": "string", "enum": ["audio", "chat"]},
                },
                "required": ["text", "at", "owner", "due", "quote", "source"],
            },
        },
        "open_issues": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                    "at": {"type": "string"},
                    "quote": {"type": "string"},
                    "source": {"type": "string", "enum": ["audio", "chat"]},
                },
                "required": ["text", "at", "quote", "source"],
            },
        },
    },
    "required": ["decisions", "todos", "open_issues"],
}


@dataclass(frozen=True)
class Item:
    kind: str
    text: str
    at: int
    quote: str
    #: 根拠の出どころ。**"chat" の根拠は文字起こしに無いのが正しい。**
    #: 2026-09-12 の実機で、チャット由来の TODO を「原文に無い」と誤検知した
    #: ——検査が「根拠は全部文字起こしから来る」と決めつけていた。
    source: str = AUDIO
    #: 相手が書いた時刻の文字列そのまま。**チャットの時刻は秒に直せない**
    #: （時計の時刻なので）。表示にはこちらを使う。
    at_raw: str = ""
    owner: str = ""
    due: str = ""


@dataclass(frozen=True)
class Minutes:
    decisions: list[Item]
    todos: list[Item]
    open_issues: list[Item]
    raw: str
    reply: Reply
    meeting: str
    held_at: str
    attendees: str
    problems: list[str] = field(default_factory=list)


# ------------------------------------------------------------------ 下ごしらえ


def normalize(text: str) -> str:
    """照合のために表記をそろえる。**空白は落とし、全角と半角を寄せる。**

    そろえすぎない。`NFKC` は幅と互換文字だけを寄せるので、
    「大和物産」と「大和商事」は別のままである
    ——*何でも一致するようになったら、照合そのものが意味を失う*。
    """
    return _SPACE.sub("", unicodedata.normalize("NFKC", text or ""))


def parse_mmss(value: str) -> int:
    """``MM:SS`` / ``H:MM:SS`` を秒にする。**読めなければ例外。**"""
    m = _MMSS.match(value or "")
    if not m:
        raise ValueError("時刻として読めません: {!r}".format(value))
    hours, minutes_, seconds = m.group(1), m.group(2), m.group(3)
    return int(minutes_) * 60 + int(seconds) + (int(hours) * 3600 if hours else 0)


def format_mmss(seconds: int) -> str:
    if seconds < 0:
        return "??:??"
    if seconds >= 3600:
        return "{}:{:02d}:{:02d}".format(seconds // 3600, seconds % 3600 // 60, seconds % 60)
    return "{:02d}:{:02d}".format(seconds // 60, seconds % 60)


def build_index(utterances: Sequence[Utterance]) -> tuple[str, list[int]]:
    """文字起こし全体を1本の正規化済み文字列にし、各文字の出どころを覚える。

    **発言ごとに照合しない。** 文字起こしは1文ずつに割れているので
    （実機で101件）、*1つの発言に収まらない引用が普通に出る*。
    """
    body: list[str] = []
    owner: list[int] = []
    for i, u in enumerate(utterances):
        t = normalize(u.text)
        body.append(t)
        owner.extend([i] * len(t))
    return "".join(body), owner


def locate(
    quote: str, body: str, owner: list[int], utterances: Sequence[Utterance]
) -> int | None:
    """引用が実在すれば、その始まりの発言の時刻を返す。無ければ ``None``。

    **空の引用は「無い」に寄せる。** 空文字はどんな文字列にも含まれるので、
    *引用を空にすれば照合が必ず通る*という逃げ道ができてしまう。
    """
    q = normalize(quote)
    if not q:
        return None
    pos = body.find(q)
    if pos < 0:
        return None
    return utterances[owner[pos]].at


# ------------------------------------------------------------------ パース


def _item(kind: str, raw: dict) -> Item:
    try:
        at = parse_mmss(raw.get("at", ""))
    except ValueError:
        at = UNKNOWN_AT
    source = str(raw.get("source", AUDIO))
    return Item(
        kind=kind,
        text=str(raw.get("text", "")),
        at=at,
        at_raw=str(raw.get("at", "")),
        quote=str(raw.get("quote", "")),
        source=source if source in (AUDIO, CHAT) else AUDIO,
        owner=str(raw.get("owner", "")),
        due=str(raw.get("due", "")),
    )


def parse_minutes(text: str) -> tuple[list[Item], list[Item], list[Item]]:
    """JSON を3つの区分に割る。**足りない鍵で落ちない。**

    型を指定して受けても、相手が全部返す保証は無い。落ちると
    *課金は乗ったのに何が返ったか分からない*状態になる。
    """
    try:
        data = json.loads(text)
    except json.JSONDecodeError as error:
        raise ValueError("議事録の JSON が読めません: {}".format(error)) from None
    if not isinstance(data, dict):
        raise ValueError("議事録の JSON が辞書ではありません")
    return (
        [_item("decision", x) for x in data.get("decisions") or [] if isinstance(x, dict)],
        [_item("todo", x) for x in data.get("todos") or [] if isinstance(x, dict)],
        [_item("issue", x) for x in data.get("open_issues") or [] if isinstance(x, dict)],
    )


# ------------------------------------------------------------------ 検査

_LABEL = {"decision": "決定事項", "todo": "TODO", "issue": "論点"}


def audit(
    decisions: list[Item],
    todos: list[Item],
    open_issues: list[Item],
    utterances: Sequence[Utterance],
    duration: float,
    reply: Reply,
    chat_log: str = "",
) -> list[str]:
    """**もっともらしさを疑う。** 空なら問題なし。

    ここに並ぶのは全部「読んで気づけない」失敗である。原文に無い決定も、
    別の場所を指した根拠も、後半を丸ごと落とした要約も、**文章としては自然**。
    """
    problems: list[str] = []
    items = [*decisions, *todos, *open_issues]

    if reply.finish_reason is None:
        problems.append("打ち切りの有無を確認できませんでした（finish_reason が取れていない）")
    elif reply.finish_reason not in OK_FINISH_REASONS:
        problems.append(
            "出力が打ち切られています（finish_reason={}）。"
            "JSON は読めても、途中で切れています".format(reply.finish_reason)
        )

    if not items:
        problems.append(
            "決定事項・TODO・論点が1件もありません。"
            "「何も決まらなかった」のか「何も拾えなかった」のか区別が付きません"
        )

    body, owner = build_index(utterances)
    chat_body = normalize(chat_log)

    for item in items:
        label = "{}「{}」".format(_LABEL[item.kind], item.text)

        # **チャット由来の根拠を文字起こしで探さない。** 2026-09-12 の実機で、
        # 「次回は10/7（火）10:00〜」を「原文に無い」と誤検知した。
        # チャットの 10:26 は時計の時刻で、音声のオフセットではない
        # ——*検査が「根拠は全部文字起こしから来る」と決めつけていた。*
        if item.source == CHAT:
            if not chat_body:
                problems.append(
                    "{}: チャットを根拠にしていますが、チャットログを渡していません".format(label)
                )
            elif not normalize(item.quote):
                problems.append("{}: 根拠の引用が空です。照合できません".format(label))
            elif normalize(item.quote) not in chat_body:
                problems.append(
                    "{}: 根拠の引用がチャットログに無いか、表記が違います（{!r}）".format(
                        label, item.quote
                    )
                )
            continue

        if item.at == UNKNOWN_AT:
            problems.append("{}: 根拠の時刻が読めません".format(label))
        elif item.at > duration:
            problems.append(
                "{}: 根拠が音声の長さ {:.1f} 秒を超える {} を指しています".format(
                    label, duration, format_mmss(item.at)
                )
            )

        if not normalize(item.quote):
            problems.append("{}: 根拠の引用が空です。照合できません".format(label))
            continue

        found = locate(item.quote, body, owner, utterances)
        if found is None:
            problems.append(
                "{}: 根拠の引用が原文に無いか、表記が違います（{!r}）".format(label, item.quote)
            )
        elif item.at != UNKNOWN_AT and abs(found - item.at) > QUOTE_TOLERANCE_SEC:
            problems.append(
                "{}: 引用の場所（{}）と申告された時刻（{}）が食い違います".format(
                    label, format_mmss(found), format_mmss(item.at)
                )
            )

    # **終盤の検査は音声由来だけで見る。** チャットの時刻を混ぜると、
    # 会議の後半を落としていても「終盤まで届いている」ことになってしまう。
    cited = [i.at for i in items if i.source == AUDIO and i.at != UNKNOWN_AT]
    if cited and max(cited) < duration - TAIL_TOLERANCE_SEC:
        problems.append(
            "根拠が会議の終盤に届いていません（最後の引用 {} / 音声 {:.1f} 秒）。"
            "後半を丸めた可能性があります".format(format_mmss(max(cited)), duration)
        )

    return problems


# ------------------------------------------------------------------ プロンプト


def build_prompt(
    transcript_text: str,
    *,
    meeting: str,
    held_at: str,
    attendees: str,
    chat_log: str = "",
) -> str:
    """議事録の指示。**原文に無いことを書かせない。**

    ``chat_log`` は空なら**節ごと入れない**。空の見出しを渡すと、
    *相手はそこを埋めようとする*（この課題でいちばん怖い足し方）。
    """
    parts = [
        "次の会議の文字起こしから、議事録を作ってください。",
        "",
        "会議名: {}".format(meeting),
        "日時: {}".format(held_at),
        "参加者: {}".format(attendees),
        "",
        "守ること:",
        "- 文字起こしに書かれていないことは、**一切書かない**",
        "- 決定事項・TODO・論点のそれぞれに、根拠となる時刻（MM:SS）と、",
        "  **文字起こしからそのまま写した逐語の引用**を必ず付ける",
        "- 引用は要約しない。原文にある文字列をそのまま写す。敬語や語尾を整えない",
        "- source には、その根拠が音声なら audio、チャットログなら chat を入れる",
        "- 結論が出なかったものは論点に入れる。**決定事項に格上げしない**",
        "- 反対が無かっただけのものを「全員が賛成した」と書かない",
        "- TODO の担当や期限が分からなければ「不明」と書く。推測で埋めない",
        "- 言い直された内容は、**後の発言を採る**",
        "- 会議の終わりまで見る。前半だけで済ませない",
        "",
        "文字起こし:",
        transcript_text,
    ]
    if chat_log.strip():
        parts += [
            "",
            "会議中のチャットログ（音声に無い情報が含まれます。ここも根拠として"
        "使ってよい。使ったら source を chat にし、時刻はチャットの表記のまま写す）:",
            chat_log,
        ]
    return chr(10).join(parts)


def render_transcript(utterances: Sequence[Utterance]) -> str:
    return chr(10).join(
        "[{}] {}: {}".format(format_mmss(u.at), u.speaker, u.text) for u in utterances
    )


# ------------------------------------------------------------------ 組み立て

Send = Callable[..., Reply]


def summarize(
    utterances: Sequence[Utterance],
    duration: float,
    *,
    send: Send,
    meeting: str,
    held_at: str,
    attendees: str,
    chat_log: str = "",
) -> Minutes:
    """議事録を作り、**問題の一覧を添えて**返す。"""
    reply = send(
        prompt=build_prompt(
            render_transcript(utterances),
            meeting=meeting,
            held_at=held_at,
            attendees=attendees,
            chat_log=chat_log,
        ),
        schema=MINUTES_SCHEMA,
    )
    decisions, todos, open_issues = parse_minutes(reply.text)
    return Minutes(
        decisions=decisions,
        todos=todos,
        open_issues=open_issues,
        raw=reply.text,
        reply=reply,
        meeting=meeting,
        held_at=held_at,
        attendees=attendees,
        problems=audit(
            decisions, todos, open_issues, utterances, duration, reply, chat_log
        ),
    )


def render(m: Minutes, *, provenance: Sequence[str]) -> str:
    """ドキュメントに入れる本文を組む。

    **4章と5章を必ず入れる。** 「含んでいないもの」が無いと、読む人は
    議事録に書かれていないことを「起きなかったこと」と読む。
    「生成元」が無いと、**二重処理と打ち切りは本文だけ見ても分からない**。

    **疑う理由も本文に出す。** 共有されるのは議事録だけで、
    *実行画面に出しただけの警告は、読む人には届かない*。
    """
    lines = [
        m.meeting,
        "",
        "日時: {}".format(m.held_at),
        "参加者: {}".format(m.attendees),
        "",
        "1. 決定事項",
    ]
    lines += _section(m.decisions, "決定事項はありませんでした")
    lines += ["", "2. TODO"]
    lines += _section(m.todos, "TODO はありませんでした")
    lines += ["", "3. 論点（結論が出なかったもの）"]
    lines += _section(m.open_issues, "論点はありませんでした")

    lines += ["", "4. この議事録が含んでいないもの"]
    lines += ["  - " + s for s in NOT_INCLUDED]

    lines += ["", "5. 生成元"]
    lines += ["  - " + s for s in provenance]
    lines.append("  - 生成: {}".format(datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
    lines.append(
        "  - モデルの申告: 打ち切り {} / 入力 {} トークン / 出力 {} トークン".format(
            m.reply.finish_reason, m.reply.prompt_tokens, m.reply.output_tokens
        )
    )

    lines += ["", "6. この議事録を疑う理由"]
    if m.problems:
        lines += ["  - " + p for p in m.problems]
    else:
        lines.append("  - 自動検査では問題は見つかりませんでした（検査していない穴は4章）")
    return chr(10).join(lines)


def _section(items: list[Item], empty: str) -> list[str]:
    if not items:
        return ["  （{}）".format(empty)]
    out = []
    for i, item in enumerate(items, 1):
        head = "  {}. {}".format(i, item.text)
        if item.kind == "todo":
            head += "（担当: {} / 期限: {}）".format(item.owner or "不明", item.due or "不明")
        out.append(head)
        where = (
            "チャット {}".format(item.at_raw) if item.source == CHAT
            else format_mmss(item.at)
        )
        out.append("     根拠 [{}]「{}」".format(where, item.quote))
    return out


# ------------------------------------------------------------------ 入口


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="文字起こしから議事録を作る")
    parser.add_argument("transcript", type=Path, help="transcribe.py が書いた文字起こし")
    parser.add_argument("--audio", type=Path, default=None, help="長さの照合に使う音声")
    parser.add_argument("--meeting", default="会議", help="会議名")
    parser.add_argument("--held-at", default="不明", help="日時（音声に無いので人が渡す）")
    parser.add_argument("--attendees", default="不明", help="参加者（同上）")
    parser.add_argument("--chat", type=Path, default=None, help="チャットログ（4-②）")
    parser.add_argument("--env", default=".env")
    parser.add_argument("--out", type=Path, default=None, help="議事録の保存先")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    import transcribe as _t

    args = build_parser().parse_args(argv)
    root = Path(__file__).resolve().parents[1]

    try:
        env_path = args.env if Path(args.env).is_absolute() else root / args.env
        api_key = gemini_client.read_api_key(env_file.load(env_path))
    except (env_file.EnvFileError, gemini_client.AuthError) as error:
        print(error, file=sys.stderr)
        return 1

    utterances, skipped = _t.parse_transcript(args.transcript.read_text(encoding="utf-8"))
    if skipped:
        print("文字起こしに読めない行が {} 件あります".format(len(skipped)), file=sys.stderr)

    duration = _t.probe_audio(args.audio).seconds if args.audio else float(utterances[-1].at)
    chat_log = args.chat.read_text(encoding="utf-8") if args.chat else ""

    client = gemini_client.build_client(api_key)

    def send(*, prompt: str, schema) -> Reply:
        return gemini_client.generate_json(
            client, prompt=prompt, schema=schema, api_key=api_key
        )

    try:
        m = summarize(
            utterances, duration, send=send,
            meeting=args.meeting, held_at=args.held_at,
            attendees=args.attendees, chat_log=chat_log,
        )
    except (ValueError, gemini_client.GeminiError) as error:
        print(error, file=sys.stderr)
        return 1

    provenance = ["文字起こし: {} / {} 発言".format(args.transcript.name, len(utterances))]
    if args.audio:
        provenance.append("音声: {} / {:.2f} 秒".format(args.audio.name, duration))
    if chat_log:
        provenance.append("チャットログ: {}".format(args.chat.name))

    body = render(m, provenance=provenance)
    out = args.out or args.transcript.with_name("minutes.txt")
    out.write_text(body + chr(10), encoding="utf-8", newline=chr(10))

    # **型のまま残す。** 整形した本文からは、根拠の出どころ（audio / chat）も
    # 引用の原文も取り出せない。`verify_source.py` はこちらを読む
    # ——*中間成果物を残す理由（DESIGN 2章）は、この層にも同じように効く。*
    raw_out = out.with_suffix(".json")
    raw_out.write_text(m.raw + chr(10), encoding="utf-8", newline=chr(10))

    print("決定事項: {} 件 / TODO: {} 件 / 論点: {} 件".format(
        len(m.decisions), len(m.todos), len(m.open_issues)))
    print("打ち切り: {}".format(m.reply.finish_reason))
    print("トークン: 入力 {} / 出力 {}".format(
        m.reply.prompt_tokens, m.reply.output_tokens))
    print("保存    : {} / {}".format(out, raw_out.name))
    if m.problems:
        print("")
        print("**議事録は作れたが、疑う理由がある**（本文の6章にも書いた）:")
        for p in m.problems:
            print("  -", p)
        return 2
    print("検査    : 問題なし")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
