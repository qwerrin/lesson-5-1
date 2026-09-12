"""task3/transcribe のテスト。**実装より先に書いた。**

守らせる対象は `task3/DESIGN.md` の 5 章。この層で狙うのは
**「音は正常に読めて、文字起こしも成功するのに、中身が欠けている」**失敗である。
文字が化ければ読めば分かるが、**録れていない音は何事も無く短い結果を返す。**

============ ====================================================================
DESIGN の項目 ここで守ること
============ ====================================================================
5-D          音声の長さと、文字起こしが言及した**最後の時刻**を突き合わせる
5-E          文字数が極端に少ない結果を**成功として扱わない**
5-G          `finish_reason` を見る。**打ち切られても本文は返る**
5-I          出力の言語を検査する。日本語の会議が英訳で返っても成功する
5-K          音声の長さを**超える時刻**を指したら、それは原文に無い
3.1          inline data の 20MB を超えたら、黙って送らずに止める
============ ====================================================================

**捨てた行を数える。** 時刻の付かない行を静かに落とすと、
*落とした量に比例して、欠落が見えなくなる*——落とすほど「きれいな結果」に見える。

**外部呼び出しは `send` で差し替える。** テストはネットワークに出ない。
実物の音声も使わない（`meeting.wav` は追跡していないので、
クローンした人の手元では存在しない）。
"""

from __future__ import annotations

import sys
import wave
from array import array
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "task3"))

import transcribe  # noqa: E402
from common.gemini_client import Reply  # noqa: E402


RATE = 16000

#: 実改行。テストの文字列に生のエスケープを書かずに済ませる。
NL = chr(10)


def wav_at(tmp_path: Path, seconds: float, name: str = "a.wav", rate: int = RATE) -> Path:
    """指定した長さの wav を作る。**中身は問わない**——長さだけが物差し。

    長い音声には低い `rate` を渡す。16kHz のままだと 900 秒で 28.8MB になり、
    **inline の 20MB を超えて送れない**（DESIGN 3.1・実測で 20MB は約10.4分）。
    テストのたびに数十MBを書くのも無駄なので、長さだけを大きくする。
    """
    path = tmp_path / name
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(array("h", [100] * int(rate * seconds)).tobytes())
    return path


def reply(text: str, finish: str | None = "STOP") -> Reply:
    return Reply(text=text, finish_reason=finish, prompt_tokens=1, output_tokens=1)


GOOD = """[00:03] 話者A: おはようございます。週次定例を始めます。
[00:12] 話者B: 小林です。よろしくお願いします。
[04:55] 話者A: では、以上で終わります。ありがとうございました。"""


# ----------------------------------------------------------------- パース

def test_時刻と話者と本文に割れる():
    got, skipped = transcribe.parse_transcript(GOOD)
    assert [u.at for u in got] == [3, 12, 295]
    assert got[1].speaker == "話者B"
    assert got[0].text == "おはようございます。週次定例を始めます。"
    assert skipped == []


def test_一時間を超える時刻も読める():
    got, _ = transcribe.parse_transcript("[1:02:03] 話者A: はい。")
    assert got[0].at == 3723


def test_本文にコロンがあっても切れない():
    got, _ = transcribe.parse_transcript("[00:05] 話者A: 内訳は次のとおり: 三点です。")
    assert got[0].text == "内訳は次のとおり: 三点です。"


def test_時刻の無い行は捨てずに数える():
    """**静かに落とさない。** 落とすほど結果がきれいに見える。"""
    got, skipped = transcribe.parse_transcript("前置きです。\n" + GOOD)
    assert len(got) == 3
    assert skipped == ["前置きです。"]


def test_空行は捨てても数えない():
    got, skipped = transcribe.parse_transcript("\n\n" + GOOD + "\n\n")
    assert len(got) == 3
    assert skipped == []


def test_1件も取れなければ例外():
    """0件を静かに返すと、**空の議事録が「会議に何も無かった」として通る。**"""
    with pytest.raises(ValueError, match="1件も"):
        transcribe.parse_transcript("本文だけで時刻がありません。")


# ------------------------------------------------------------- 音声の実測

def test_音声の長さとバイト数を測る(tmp_path):
    info = transcribe.probe_audio(wav_at(tmp_path, 2.0))
    assert info.seconds == pytest.approx(2.0)
    assert info.rate == RATE
    assert info.channels == 1
    assert info.mime == "audio/wav"
    assert info.n_bytes == info.path.stat().st_size


def test_大きすぎる音声は送る前に止める(tmp_path):
    """**20MB は inline data の上限**（DESIGN 3.1）。超えたら黙って送らない。"""
    info = transcribe.probe_audio(wav_at(tmp_path, 1.0))
    big = transcribe.AudioInfo(**{**vars(info), "n_bytes": transcribe.INLINE_LIMIT_BYTES + 1})
    with pytest.raises(ValueError, match="上限"):
        transcribe.ensure_sendable(big)


def test_上限ちょうどは通す(tmp_path):
    info = transcribe.probe_audio(wav_at(tmp_path, 1.0))
    ok = transcribe.AudioInfo(**{**vars(info), "n_bytes": transcribe.INLINE_LIMIT_BYTES})
    transcribe.ensure_sendable(ok)  # 例外にならない


# ----------------------------------------------------------------- 検査

def info_of(seconds: float) -> transcribe.AudioInfo:
    return transcribe.AudioInfo(
        path=Path("dummy.wav"), seconds=seconds, n_bytes=1,
        rate=RATE, channels=1, bits=16, mime="audio/wav",
    )


def test_問題が無ければ空を返す():
    got, skipped = transcribe.parse_transcript(GOOD)
    assert transcribe.audit(got, skipped, info_of(300.0), reply(GOOD)) == []


def test_打ち切られたら見つける():
    """**本文は返る。** finish_reason を見なければ 5-G は永久に見えない。"""
    got, skipped = transcribe.parse_transcript(GOOD)
    problems = transcribe.audit(got, skipped, info_of(300.0), reply(GOOD, "MAX_TOKENS"))
    assert any("MAX_TOKENS" in p for p in problems)


def test_打ち切りが読めなければ見つける():
    """**「STOP だった」と「見られなかった」は別。** 混ぜると片方が消える。"""
    got, skipped = transcribe.parse_transcript(GOOD)
    problems = transcribe.audit(got, skipped, info_of(300.0), reply(GOOD, None))
    assert any("確認できません" in p for p in problems)


def test_末尾が音声の終わりに届いていなければ見つける():
    """録音が切れていても、文字起こしは**何事も無く短い結果を返す**。"""
    got, skipped = transcribe.parse_transcript(GOOD)
    problems = transcribe.audit(got, skipped, info_of(900.0), reply(GOOD))
    assert any("末尾" in p for p in problems)


def test_音声の長さを超える時刻を見つける():
    """存在しない時刻を指していたら、それは原文に無い（5-K）。"""
    got, skipped = transcribe.parse_transcript(GOOD)
    problems = transcribe.audit(got, skipped, info_of(120.0), reply(GOOD))
    assert any("超える" in p for p in problems)


def test_時刻が巻き戻ったら見つける():
    text = "[00:30] 話者A: あとの発言です。\n[00:10] 話者B: 前の発言です。"
    got, skipped = transcribe.parse_transcript(text)
    problems = transcribe.audit(got, skipped, info_of(60.0), reply(text))
    assert any("巻き戻" in p for p in problems)


def test_中身が薄すぎたら見つける():
    """無音のファイルは「（無音）」などを返して**成功する**（5-E）。"""
    text = "[00:00] 話者A: （無音）"
    got, skipped = transcribe.parse_transcript(text)
    problems = transcribe.audit(got, skipped, info_of(300.0), reply(text))
    assert any("薄" in p for p in problems)


def test_日本語でなければ見つける():
    """日本語の会議が英訳で返っても**成功する**（5-I）。"""
    text = (
        "[00:03] Speaker A: Good morning, let us begin the weekly meeting.\n"
        "[04:55] Speaker A: That is all for today, thank you very much."
    )
    got, skipped = transcribe.parse_transcript(text)
    problems = transcribe.audit(got, skipped, info_of(300.0), reply(text))
    assert any("日本語" in p for p in problems)


def test_話者が1人しかいなければ見つける():
    text = "[00:03] 話者A: おはようございます。週次定例を始めます。\n[04:55] 話者A: 以上で終わります。ありがとうございました。"
    got, skipped = transcribe.parse_transcript(text)
    problems = transcribe.audit(got, skipped, info_of(300.0), reply(text))
    assert any("話者" in p for p in problems)


def test_指定した形式でない話者名を見つける():
    """**下限だけ見ていると通る。** 2026-09-12 の実機で実際に踏んだ形。

    3話者の会議で `話者来` と `話者京` が1件ずつ返り、話者が5人に見えた。
    `MIN_SPEAKERS` は下限なので 5 >= 2 で通ってしまい、
    **検査は「問題なし」と報告した**。
    """
    text = NL.join([
        "[00:03] 話者A: おはようございます。週次定例を始めます。",
        "[02:00] 話者B: 小林です。よろしくお願いします。",
        "[04:55] 話者来: 来週も同じ火曜の十時からで進めます。",
    ])
    got, skipped = transcribe.parse_transcript(text)
    problems = transcribe.audit(got, skipped, info_of(300.0), reply(text))
    assert any("話者来" in p for p in problems)


def test_形式を外れた話者名は人数に数えない():
    """水増しされた人数で下限を満たしてはいけない。"""
    text = NL.join([
        "[00:03] 話者A: おはようございます。週次定例を始めます。",
        "[04:55] 話者来: 来週も同じ火曜の十時からで進めます。",
    ])
    got, skipped = transcribe.parse_transcript(text)
    problems = transcribe.audit(got, skipped, info_of(300.0), reply(text))
    assert any("1 人しか" in p for p in problems)


def test_捨てた行があれば見つける():
    got, skipped = transcribe.parse_transcript("前置きです。\n" + GOOD)
    problems = transcribe.audit(got, skipped, info_of(300.0), reply(GOOD))
    assert any("読めなかった行" in p for p in problems)


# --------------------------------------------------------------- 組み立て

def test_送る相手にプロンプトと音声を渡す(tmp_path):
    seen = {}

    def send(*, prompt, audio_bytes, mime_type):
        seen.update(prompt=prompt, n=len(audio_bytes), mime=mime_type)
        return reply(GOOD)

    path = wav_at(tmp_path, 300.0, rate=1000)
    result = transcribe.transcribe(path, send=send)

    assert seen["mime"] == "audio/wav"
    assert seen["n"] == path.stat().st_size
    # **書式そのものを見る。** 「MM:SS を含む」だけだと、書式の行を消しても
    # 「1時間を超える場合だけ [H:MM:SS]」の説明が残って素通りする
    # （ミューテーションで実際に生き延びた）。
    assert "[MM:SS] 話者A: 発言の内容" in seen["prompt"]
    assert len(result.utterances) == 3
    assert result.problems == []


def test_問題があっても結果は返す(tmp_path):
    """**握りつぶさない。呼び手に判断させる。**

    ここで例外にすると、呼び手は「何が返ったか」を見られないまま止まる。
    文字起こしは**捨てるには高い**（課金が乗っている）ので、
    問題の一覧を添えて返し、使うかどうかは上の層が決める。
    """
    path = wav_at(tmp_path, 900.0, rate=1000)
    result = transcribe.transcribe(path, send=lambda **kw: reply(GOOD))
    assert result.utterances
    assert any("末尾" in p for p in result.problems)


def test_大きすぎる音声では送らない(tmp_path):
    called = []

    def send(**kw):
        called.append(1)
        return reply(GOOD)

    path = wav_at(tmp_path, 1.0)
    with pytest.raises(ValueError, match="上限"):
        transcribe.transcribe(path, send=send, limit_bytes=10)
    assert called == []
