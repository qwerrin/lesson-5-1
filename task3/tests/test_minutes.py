"""task3/minutes のテスト。**実装より先に書いた。**

この層で狙うのは「**もっともらしいが、原文に無い**」失敗である。
文字起こしの層（5-D / 5-E / 5-G / 5-I）は「欠ける」失敗を見たが、
要約の層は逆に**足してしまう**。しかも足された文は、いちばん自然に読める。

============ ====================================================================
DESIGN の項目 ここで守ること
============ ====================================================================
5-F          引用が会議の終盤まで届いているか。**丸めても例外は出ない**
5-K          決定の**逐語引用が文字起こしに実在する**ことを機械で照合する
5-P          根拠（MM:SS）を必ず持たせる。無い主張は原文と結び付けられない
5-G          `finish_reason` を見る。**打ち切られた JSON は途中で切れる**
============ ====================================================================

**照合の前に正規化する。** 文字起こしは表記を勝手に変える
——2026-09-12 の実機で「十月三日」が「10 月 3 日」（分かち書きの空白つき）に
なっていた。*素朴な部分一致は、正しい引用まで「原文に無い」と言う。*
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "task3"))

import minutes  # noqa: E402
import transcribe  # noqa: E402
from common.gemini_client import Reply  # noqa: E402

NL = chr(10)


def utt(at, speaker, text):
    return transcribe.Utterance(at=at, speaker=speaker, text=text)


TRANSCRIPT = [
    utt(3, "話者A", "おはようございます。週次定例を始めます。"),
    utt(133, "話者B", "では初回は千八百個に変更でお願いします。"),
    utt(215, "話者A", "大和物産に切り替えます。"),
    utt(224, "話者B", "10 月 3 日 まで に 用意 し ます。"),
    utt(271, "話者A", "これは持ち帰りにしましょう。"),
    utt(310, "話者A", "以上で終わります。"),
]

DURATION = 312.0


def payload(decisions=None, todos=None, issues=None):
    return json.dumps(
        {
            "decisions": decisions if decisions is not None else [
                {"text": "初回発注は1800個", "at": "02:13", "quote": "初回は千八百個に変更でお願いします"},
            ],
            "todos": todos if todos is not None else [
                {"text": "契約書を用意する", "at": "03:44", "owner": "小林",
                 "due": "10月3日", "quote": "10月3日までに用意します"},
            ],
            "open_issues": issues if issues is not None else [
                {"text": "価格改定のタイミング", "at": "04:31", "quote": "これは持ち帰りにしましょう"},
            ],
        },
        ensure_ascii=False,
    )


def reply(text, finish="STOP"):
    return Reply(text=text, finish_reason=finish, prompt_tokens=1, output_tokens=1)


# ------------------------------------------------------------------ 正規化

def test_全角と半角と空白をそろえる():
    """文字起こしは表記を勝手に変える。**照合の前にそろえる。**"""
    assert minutes.normalize("10 月 3 日") == minutes.normalize("１０月３日")
    assert minutes.normalize(" あ　い \n う ") == "あいう"


def test_正規化しても別の語は一致しない():
    """そろえすぎて何でも一致しては、照合そのものが意味を失う。"""
    assert minutes.normalize("大和物産") != minutes.normalize("大和商事")


# ------------------------------------------------------------------ 時刻

def test_時刻を秒にする():
    assert minutes.parse_mmss("02:13") == 133
    assert minutes.parse_mmss("1:02:03") == 3723


def test_時刻が読めなければ例外():
    """**黙って0にしない。** 0 にすると、全部が会議の冒頭を指す。"""
    with pytest.raises(ValueError):
        minutes.parse_mmss("あとで")


# ------------------------------------------------------------------ パース

def test_三つの区分に割れる():
    d, t, i = minutes.parse_minutes(payload())
    assert [x.text for x in d] == ["初回発注は1800個"]
    assert t[0].owner == "小林" and t[0].due == "10月3日"
    assert i[0].at == 271


def test_壊れたJSONは例外():
    with pytest.raises(ValueError):
        minutes.parse_minutes("{これはJSONではない")


def test_区分が欠けていても空として扱う():
    """**足りない鍵で落ちない。** 型で受けても、相手が全部返す保証は無い。"""
    d, t, i = minutes.parse_minutes('{"decisions": []}')
    assert (d, t, i) == ([], [], [])


# ------------------------------------------------------------------ 引用の照合

def test_引用のある発言の時刻を返す():
    body, owner = minutes.build_index(TRANSCRIPT)
    assert minutes.locate("千八百個に変更", body, owner, TRANSCRIPT) == 133


def test_表記が違っても引用を見つける():
    """実機で「十月三日」が「10 月 3 日」になっていた。**空白と全半角をそろえる。**"""
    body, owner = minutes.build_index(TRANSCRIPT)
    assert minutes.locate("10月3日までに用意します", body, owner, TRANSCRIPT) == 224


def test_発言をまたぐ引用も見つける():
    """文字起こしは1文ずつに割れている。**1発言に収まらない引用がある。**"""
    body, owner = minutes.build_index(TRANSCRIPT)
    assert minutes.locate("週次定例を始めます。では初回は", body, owner, TRANSCRIPT) == 3


def test_原文に無い引用は見つからない():
    body, owner = minutes.build_index(TRANSCRIPT)
    assert minutes.locate("初回発注は二千個で決まりました", body, owner, TRANSCRIPT) is None


def test_空の引用は見つからない():
    """**空文字はどんな文字列にも含まれる。** 0件と一致を同じにしない。"""
    body, owner = minutes.build_index(TRANSCRIPT)
    assert minutes.locate("   ", body, owner, TRANSCRIPT) is None


# ------------------------------------------------------------------ 検査

def audit_of(text, finish="STOP"):
    d, t, i = minutes.parse_minutes(text)
    return minutes.audit(d, t, i, TRANSCRIPT, DURATION, reply(text, finish))


def test_問題が無ければ空を返す():
    assert audit_of(payload()) == []


def test_原文に無い引用を見つける():
    """**本命。** もっともらしい決定ほど、原文に無くても自然に読める（5-K）。"""
    bogus = [{"text": "初回発注は2000個", "at": "02:13",
              "quote": "初回は二千個で決まりました"}]
    problems = audit_of(payload(decisions=bogus))
    assert any("原文に無い" in p for p in problems)


def test_引用が空でも見つける():
    """引用を空にすれば照合は必ず通る。**逃げ道を塞ぐ。**

    **「引用が空」で見る。** 「引用」だけだと、空の検査を外しても
    「根拠の引用が原文に無いか…」のほうが同じ語を含んで素通りする。
    *原因が違えば直し方も違うので、混ぜない。*
    """
    empty = [{"text": "初回発注は1800個", "at": "02:13", "quote": ""}]
    problems = audit_of(payload(decisions=empty))
    assert any("引用が空" in p for p in problems)


def test_引用の位置と時刻が食い違えば見つける():
    """引用は実在するが、**別の場所の時刻を指している**（5-P）。"""
    off = [{"text": "初回発注は1800個", "at": "00:03",
            "quote": "初回は千八百個に変更でお願いします"}]
    problems = audit_of(payload(decisions=off))
    assert any("食い違" in p for p in problems)


def test_音声の長さを超える時刻を見つける():
    over = [{"text": "初回発注は1800個", "at": "09:00",
             "quote": "初回は千八百個に変更でお願いします"}]
    problems = audit_of(payload(decisions=over))
    assert any("超える" in p for p in problems)


def test_時刻が読めなくても落ちずに問題として出す():
    """**1件の壊れた時刻で、課金の乗った応答を捨てない。**

    落とすと「何が返ったか」が見られないまま止まる。読めなかったことを
    問題として出し、他の項目はそのまま使えるようにする。
    """
    bad = [{"text": "初回発注は1800個", "at": "あとで",
            "quote": "初回は千八百個に変更でお願いします"}]
    d, t, i = minutes.parse_minutes(payload(decisions=bad))
    assert d[0].at == minutes.UNKNOWN_AT
    problems = minutes.audit(d, t, i, TRANSCRIPT, DURATION, reply(""))
    assert any("時刻が読めません" in p for p in problems)


def test_時刻が読めなければ食い違いは言わない():
    """**読めなかったことと、食い違うことは別。** 混ぜると原因が分からなくなる。"""
    bad = [{"text": "初回発注は1800個", "at": "あとで",
            "quote": "初回は千八百個に変更でお願いします"}]
    d, t, i = minutes.parse_minutes(payload(decisions=bad))
    problems = minutes.audit(d, t, i, TRANSCRIPT, DURATION, reply(""))
    assert not any("食い違" in p for p in problems)


def test_打ち切りが読めなければ見つける():
    """**「STOP だった」と「見られなかった」は別。**"""
    problems = audit_of(payload(), finish=None)
    assert any("確認できません" in p for p in problems)


def test_全部空なら見つける():
    """**「何も決まらなかった」と「何も拾えなかった」を分ける。**"""
    problems = audit_of(payload(decisions=[], todos=[], issues=[]))
    assert any("1件も" in p for p in problems)


def test_引用が終盤に届いていなければ見つける():
    """会議の後半を丸ごと落としても、要約は成功する（5-F）。"""
    early = [{"text": "定例の開始", "at": "00:03", "quote": "週次定例を始めます"}]
    problems = audit_of(payload(decisions=early, todos=[], issues=[]))
    assert any("終盤" in p for p in problems)


def test_打ち切られたら見つける():
    problems = audit_of(payload(), finish="MAX_TOKENS")
    assert any("MAX_TOKENS" in p for p in problems)


# ------------------------------------------------- チャット由来の根拠（4-②）

CHAT = NL.join([
    "10:12 小林 大和物産さんの見積書、共有フォルダに置きました",
    "10:26 田村 次回は10/7（火）10:00〜 でカレンダー入れておきます",
])

CHAT_TODO = {"text": "次回の日程を入れる", "at": "10:26", "owner": "田村",
             "due": "不明", "quote": "次回は10/7（火）10:00〜 でカレンダー入れておきます",
             "source": "chat"}


def test_チャット由来の根拠を文字起こしで探さない():
    """**2026-09-12 の実機で誤検知した形。**

    チャットの 10:26 は時計の時刻で、音声のオフセットではない。
    文字起こしを物差しにすると「原文に無い」「音声の長さを超える」の
    2つを同時に鳴らす——*検査が「根拠は全部文字起こしから来る」と決めつけていた。*
    """
    d, t, i = minutes.parse_minutes(payload(todos=[CHAT_TODO]))
    problems = minutes.audit(d, t, i, TRANSCRIPT, DURATION, reply(""), chat_log=CHAT)
    assert not any("原文に無い" in p for p in problems)
    assert not any("超える" in p for p in problems)


def test_チャットに無い引用は見つける():
    """**チャットを根拠にすれば何でも書ける、にしない。**"""
    bogus = dict(CHAT_TODO, quote="次回は11/4（水）に決まりました")
    d, t, i = minutes.parse_minutes(payload(todos=[bogus]))
    problems = minutes.audit(d, t, i, TRANSCRIPT, DURATION, reply(""), chat_log=CHAT)
    assert any("チャットログに無い" in p for p in problems)


def test_チャットログを渡していないのにチャットを根拠にしたら見つける():
    d, t, i = minutes.parse_minutes(payload(todos=[CHAT_TODO]))
    problems = minutes.audit(d, t, i, TRANSCRIPT, DURATION, reply(""), chat_log="")
    assert any("チャットログを渡していません" in p for p in problems)


def test_終盤の検査は音声由来だけで見る():
    """チャットの遅い時刻で終盤を満たしてはいけない。

    混ぜると、**会議の後半を丸ごと落としていても「終盤まで届いている」**
    ことになってしまう（5-F が鳴らなくなる）。
    """
    early = [{"text": "定例の開始", "at": "00:03", "quote": "週次定例を始めます",
              "source": "audio"}]
    d, t, i = minutes.parse_minutes(
        payload(decisions=early, todos=[CHAT_TODO], issues=[]))
    problems = minutes.audit(d, t, i, TRANSCRIPT, DURATION, reply(""), chat_log=CHAT)
    assert any("終盤" in p for p in problems)


def test_知らない出どころは音声に寄せる():
    """**勝手な値で検査を迂回させない。**"""
    odd = [dict(CHAT_TODO, source="どこか")]
    d, t, i = minutes.parse_minutes(payload(todos=odd))
    assert t[0].source == minutes.AUDIO


def test_議事録にチャット由来と書く():
    """**読む人にとっても「録音ではなくチャット」は重要な情報。**"""
    got = minutes.summarize(
        TRANSCRIPT, DURATION,
        send=lambda **kw: reply(payload(todos=[CHAT_TODO])),
        meeting="週次定例", held_at="不明", attendees="不明", chat_log=CHAT,
    )
    body = minutes.render(got, provenance=[])
    assert "チャット 10:26" in body


# ------------------------------------------------------------------ 組み立て

def test_送る相手に文字起こしと前提を渡す():
    seen = {}

    def send(*, prompt, schema):
        seen.update(prompt=prompt, schema=schema)
        return reply(payload())

    got = minutes.summarize(
        TRANSCRIPT, DURATION,
        send=send, meeting="週次定例", held_at="2026-09-12 10:00",
        attendees="田村・小林・佐藤", chat_log="10:26 田村 次回は10/7",
    )
    assert "10 月 3 日" in seen["prompt"]          # 文字起こしがそのまま入る
    assert "2026-09-12 10:00" in seen["prompt"]    # 人が渡した日時
    assert "10:26 田村" in seen["prompt"]          # チャットログ（4-②）
    assert seen["schema"] is minutes.MINUTES_SCHEMA
    assert got.problems == []
    assert len(got.decisions) == 1


def test_問題があっても結果は返す():
    bogus = [{"text": "x", "at": "02:13", "quote": "原文に無い文字列"}]
    got = minutes.summarize(
        TRANSCRIPT, DURATION,
        send=lambda **kw: reply(payload(decisions=bogus)),
        meeting="週次定例", held_at="不明", attendees="不明", chat_log="",
    )
    assert got.decisions
    assert any("原文に無い" in p for p in got.problems)


def test_チャットログが無ければ渡さない():
    seen = {}

    def send(*, prompt, schema):
        seen["prompt"] = prompt
        return reply(payload())

    minutes.summarize(
        TRANSCRIPT, DURATION, send=send,
        meeting="週次定例", held_at="不明", attendees="不明", chat_log="",
    )
    # **節そのものが出ないことを見る。** 「チャットログ」という語だけで見ると、
    # source の説明文にも出るので、言葉狩りになって本当の契約を守らない。
    assert "音声に無い情報が含まれます" not in seen["prompt"]


# ------------------------------------------------------------------ 出力

def test_議事録に含んでいないものを必ず書く():
    """**読む人が「何が入っていないか」を知らないまま共有されるのが、いちばん危ない。**

    DESIGN 6章の「承知で残す」をそのまま本文に出す。
    """
    got = minutes.summarize(
        TRANSCRIPT, DURATION, send=lambda **kw: reply(payload()),
        meeting="週次定例", held_at="不明", attendees="不明", chat_log="",
    )
    body = minutes.render(got, provenance=["元: meeting.wav"])
    assert "含んでいないもの" in body
    assert "画面共有" in body


def test_議事録に生成元を書く():
    """二重処理と打ち切りは、**本文だけ見ても絶対に分からない**（5-H / 5-G）。"""
    got = minutes.summarize(
        TRANSCRIPT, DURATION, send=lambda **kw: reply(payload()),
        meeting="週次定例", held_at="不明", attendees="不明", chat_log="",
    )
    body = minutes.render(got, provenance=["元: meeting.wav / 312.38 秒"])
    assert "meeting.wav / 312.38 秒" in body


def test_根拠の時刻を本文に併記する():
    got = minutes.summarize(
        TRANSCRIPT, DURATION, send=lambda **kw: reply(payload()),
        meeting="週次定例", held_at="不明", attendees="不明", chat_log="",
    )
    body = minutes.render(got, provenance=[])
    assert "02:13" in body


def test_問題があれば議事録の本文にも出す():
    """**議事録だけが共有される。** 疑う理由が実行画面にしか無いと、届かない。

    引用文に判定語を入れない。「原文に無い文字列」を引用にすると、
    根拠の行にその語が出るので、**6章を丸ごと消してもテストが通る**
    （ミューテーションで実際に生き延びた）。
    """
    bogus = [{"text": "x", "at": "02:13", "quote": "架空の発言です"}]
    got = minutes.summarize(
        TRANSCRIPT, DURATION, send=lambda **kw: reply(payload(decisions=bogus)),
        meeting="週次定例", held_at="不明", attendees="不明", chat_log="",
    )
    body = minutes.render(got, provenance=[])
    assert "原文に無い" in body
