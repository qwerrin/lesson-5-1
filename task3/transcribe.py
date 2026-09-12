"""会議音声を文字起こしする。**議事録にはしない。**

文字起こしと要約を1回のリクエストにまとめない（`DESIGN.md` 2章）。
まとめると中間成果物が残らず、議事録が変だったときに原因が**聞き取り**か
**まとめ**かを切り分けられなくなる。分ければ、文字起こし全文が物差しとして残る。

============ ====================================================================
DESIGN       ここで引き受ける穴
============ ====================================================================
3.1          inline data の 20MB を超えたら、**送る前に**止める
5-D          音声の長さと、言及された**最後の時刻**を突き合わせる
5-E          文字数が極端に少ない結果を、成功として扱わない
5-G          ``finish_reason`` を見る。**打ち切られても本文は返る**
5-I          出力の言語を検査する。日本語の会議が英訳で返っても成功する
5-K          音声の長さを**超える時刻**は、原文に無いものを指している
============ ====================================================================

**問題があっても例外にしない。** 問題の一覧を添えて返し、使うかどうかは上の層に
決めさせる。文字起こしは呼ぶたびに課金が乗るので、*少しでも欠けていたら捨てる*
という作りにすると、確かめるたびに金額が動く。

**外部呼び出しは ``send`` で受け取る。** 差し替えられる形にしてあるので、
テストはネットワークにも課金にも触れない。
"""

from __future__ import annotations

import argparse
import re
import sys
import wave
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common import env_file, gemini_client  # noqa: E402
from common.gemini_client import INLINE_LIMIT_BYTES, Reply  # noqa: E402

#: 末尾がここまで手前で終わっていたら、録音が切れたか出力が打ち切られたと見る（秒）。
TAIL_TOLERANCE_SEC = 30.0

#: 1秒あたりの文字数の下限。**「極端に少ない」を数で決める。**
#: 実物の台本は 4.7 文字/秒。0.1 は5分で30文字——これを割るのは、
#: 会話が薄いのではなく**音が入っていない**ときである。
MIN_CHARS_PER_SEC = 0.1

#: 本文に占める日本語の割合の下限。
MIN_JAPANESE_RATIO = 0.5

#: 会議として成り立つ最少の話者数。1人しか出てこないのは、
#: 話者の区別に失敗したか、片方が丸ごと落ちたかのどちらか。
MIN_SPEAKERS = 2

#: 打ち切られていない、とみなす理由。**``None`` は含めない**
#: ——「STOP だった」と「見られなかった」を混同させない。
OK_FINISH_REASONS = frozenset({"STOP", "FINISH_REASON_STOP"})

_LINE = re.compile(
    r"^\[\s*(?:(\d+):)?(\d+):(\d+)\s*\]\s*(.+?)\s*[:：]\s*(.*)$"
)

_JAPANESE = re.compile(r"[぀-ゟ゠-ヿ一-鿿]")

#: プロンプトで指定した話者名の形。**これを外れた名前は数に入れない。**
#:
#: 2026-09-12 の実機で `話者来` と `話者京` が1件ずつ返り、話者が5人に
#: 見えていた（実際は3人）。**下限しか見ていなかったので検査は通った**
#: ——多すぎる側と、そもそも書式を外れている側を見ていなかった。
_SPEAKER_LABEL = re.compile(r"^話者[A-Z]$")

MIME_BY_SUFFIX = {".wav": "audio/wav", ".mp3": "audio/mpeg", ".m4a": "audio/mp4"}


@dataclass(frozen=True)
class Utterance:
    at: int
    speaker: str
    text: str


@dataclass(frozen=True)
class AudioInfo:
    path: Path
    seconds: float
    n_bytes: int
    rate: int
    channels: int
    bits: int
    mime: str


@dataclass(frozen=True)
class Transcript:
    utterances: list[Utterance]
    skipped: list[str]
    raw: str
    reply: Reply
    audio: AudioInfo
    problems: list[str] = field(default_factory=list)


# ------------------------------------------------------------------ プロンプト


def build_prompt() -> str:
    """文字起こしの指示。**要約させない。**

    時刻を必ず付けさせるのは、5-D と 5-G が**時刻でしか見えない**ため。
    本文だけを見ても、途中で切れた文字起こしは「短い会議」として通ってしまう。

    言い直しも相槌も残させる。要約側の入力になるので、*ここで整えると
    5-B（言い直しの取り違え）を試す材料が消える*。
    """
    return (
        "これは日本語の会議の録音です。全文を文字起こししてください。\n"
        "\n"
        "出力の形式（1行に1発言。これ以外は書かないこと）:\n"
        "[MM:SS] 話者A: 発言の内容\n"
        "\n"
        "守ること:\n"
        "- 時刻は音声の先頭からの経過時間。1時間を超える場合だけ [H:MM:SS] にする\n"
        "- 話者は声で区別し、出てきた順に 話者A / 話者B / 話者C と付ける\n"
        "- 話者名はこの形だけを使う。他の文字を混ぜない\n"
        "- 単語の間に空白を入れない（分かち書きにしない）\n"
        "- 要約しない。省略しない。相槌も、言い直しも、そのまま書く\n"
        "- 言い直された内容は、**両方とも**残す（後の発言で上書きしない）\n"
        "- 聞き取れなかった箇所は [不明] と書く。推測で埋めない\n"
        "- 複数人が同時に話している箇所は、聞き取れた発言をそれぞれ1行ずつ書く\n"
        "- 最後の発言まで必ず書く。途中で止めない\n"
    )


# ------------------------------------------------------------------ 音声を測る


def probe_audio(path: Path) -> AudioInfo:
    """送る前に音声そのものを測る。

    **長さは相手に聞かない。** 文字起こしが言及した最後の時刻と突き合わせる
    物差しなので、*同じ応答の中の値どうしを比べても、何も確かめたことにならない*。
    """
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix != ".wav":
        raise ValueError(
            "{} は測れません。長さを実測できるのは wav だけです"
            "（対応: {}）".format(suffix or "拡張子なし", ", ".join(sorted(MIME_BY_SUFFIX)))
        )
    with wave.open(str(path), "rb") as w:
        rate = w.getframerate()
        frames = w.getnframes()
        channels = w.getnchannels()
        bits = w.getsampwidth() * 8
    if rate <= 0:
        raise ValueError("{}: 標本化周波数が 0 です".format(path.name))
    return AudioInfo(
        path=path,
        seconds=frames / rate,
        n_bytes=path.stat().st_size,
        rate=rate,
        channels=channels,
        bits=bits,
        mime=MIME_BY_SUFFIX[suffix],
    )


def ensure_sendable(audio: AudioInfo, limit_bytes: int = INLINE_LIMIT_BYTES) -> None:
    """1リクエストに載る大きさかを確かめる。**超えていたら送らない。**

    送ってしまうと、相手の拒否が課金や再試行と混ざり、
    *原因が「音声の大きさ」だったことが読み取りにくくなる*。
    """
    if audio.n_bytes > limit_bytes:
        raise ValueError(
            "{} は {:,} バイトで、1リクエストの上限 {:,} バイトを超えています。"
            "短く区切るか Files API に回してください".format(
                audio.path.name, audio.n_bytes, limit_bytes
            )
        )


# ------------------------------------------------------------------ パース


def parse_transcript(text: str) -> tuple[list[Utterance], list[str]]:
    """``[MM:SS] 話者A: 本文`` を組に割る。**読めなかった行は捨てずに返す。**

    静かに落とすと、*落とした量に比例して結果がきれいに見える*。
    読めない行が増えるほど「整った文字起こし」に近づくのが、この失敗の質の悪さ。
    """
    utterances: list[Utterance] = []
    skipped: list[str] = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        m = _LINE.match(line)
        if not m:
            skipped.append(line)
            continue
        hours, minutes, seconds = m.group(1), m.group(2), m.group(3)
        at = int(minutes) * 60 + int(seconds) + (int(hours) * 3600 if hours else 0)
        utterances.append(Utterance(at=at, speaker=m.group(4), text=m.group(5)))
    if not utterances:
        raise ValueError(
            "文字起こしから発言を1件も読み取れませんでした"
            "（読めなかった行 {} 件）".format(len(skipped))
        )
    return utterances, skipped


def japanese_ratio(texts: list[str]) -> float:
    body = "".join(texts)
    letters = [c for c in body if not c.isspace()]
    if not letters:
        return 0.0
    return sum(1 for c in letters if _JAPANESE.match(c)) / len(letters)


# ------------------------------------------------------------------ 検査


def audit(
    utterances: list[Utterance],
    skipped: list[str],
    audio: AudioInfo,
    reply: Reply,
) -> list[str]:
    """**正常終了した結果**を疑う。空なら問題なし。

    ここに並ぶのは全部「例外にならない失敗」である。音声が途中で切れていても、
    無音でも、英訳されても、打ち切られても、**API は成功を返す**。
    """
    problems: list[str] = []
    texts = [u.text for u in utterances]

    if reply.finish_reason is None:
        problems.append(
            "打ち切りの有無を確認できませんでした（finish_reason が取れていない）"
        )
    elif reply.finish_reason not in OK_FINISH_REASONS:
        problems.append(
            "出力が打ち切られています（finish_reason={}）。"
            "本文は返っているので、短い会議と見分けが付きません".format(reply.finish_reason)
        )

    if skipped:
        problems.append(
            "読めなかった行が {} 件あります（先頭: {!r}）".format(len(skipped), skipped[0])
        )

    over = [u for u in utterances if u.at > audio.seconds]
    if over:
        problems.append(
            "音声の長さ {:.1f} 秒を超える時刻が {} 件あります（最大 {} 秒）。"
            "原文に無いものを指しています".format(
                audio.seconds, len(over), max(u.at for u in over)
            )
        )

    backwards = [
        (b.at, a.at)
        for a, b in zip(utterances, utterances[1:])
        if b.at < a.at
    ]
    if backwards:
        problems.append(
            "時刻が巻き戻っている箇所が {} 件あります（例: {} 秒 のあとに {} 秒）".format(
                len(backwards), backwards[0][1], backwards[0][0]
            )
        )

    last = utterances[-1].at
    if last < audio.seconds - TAIL_TOLERANCE_SEC:
        problems.append(
            "末尾が音声の終わりに届いていません（最後の発言 {} 秒 / 音声 {:.1f} 秒）。"
            "録音が切れたか、出力が打ち切られた可能性があります".format(last, audio.seconds)
        )

    density = sum(len(t) for t in texts) / audio.seconds if audio.seconds else 0.0
    if density < MIN_CHARS_PER_SEC:
        problems.append(
            "中身が薄すぎます（{:.3f} 文字/秒 < {} 文字/秒）。"
            "無音や雑音だけのファイルでも文字起こしは成功します".format(
                density, MIN_CHARS_PER_SEC
            )
        )

    ratio = japanese_ratio(texts)
    if ratio < MIN_JAPANESE_RATIO:
        problems.append(
            "日本語の割合が {:.0%} しかありません（下限 {:.0%}）。"
            "翻訳されている可能性があります".format(ratio, MIN_JAPANESE_RATIO)
        )

    speakers = {u.speaker for u in utterances}
    odd = sorted(s for s in speakers if not _SPEAKER_LABEL.match(s))
    if odd:
        problems.append(
            "指定した形式でない話者名が {} 件あります（{}）。"
            "話者の数が水増しされ、取り違えが見えなくなります".format(len(odd), ", ".join(odd))
        )

    valid = speakers - set(odd)
    if len(valid) < MIN_SPEAKERS:
        problems.append(
            "話者が {} 人しか区別できていません（{}）。"
            "区別に失敗したか、片方が落ちています".format(
                len(valid), ", ".join(sorted(valid)) or "なし"
            )
        )

    return problems


# ------------------------------------------------------------------ 組み立て

Send = Callable[..., Reply]


def transcribe(
    path: Path,
    *,
    send: Send,
    limit_bytes: int = INLINE_LIMIT_BYTES,
) -> Transcript:
    """音声を文字起こしして、**問題の一覧を添えて**返す。"""
    audio = probe_audio(path)
    ensure_sendable(audio, limit_bytes)

    reply = send(
        prompt=build_prompt(),
        audio_bytes=audio.path.read_bytes(),
        mime_type=audio.mime,
    )
    utterances, skipped = parse_transcript(reply.text)
    return Transcript(
        utterances=utterances,
        skipped=skipped,
        raw=reply.text,
        reply=reply,
        audio=audio,
        problems=audit(utterances, skipped, audio, reply),
    )


# ------------------------------------------------------------------ 入口


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="会議音声を文字起こしする（議事録にはしない）",
    )
    parser.add_argument("audio", type=Path, help="音声ファイル（wav）")
    parser.add_argument("--env", default=".env", help="資格情報の置き場（既定: .env）")
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="文字起こしの保存先（既定: 音声と同じ場所の transcript.txt）",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """**問題があっても保存する。** 呼び直すと、そのぶん課金が乗る。

    終了コードで区別する: 0 なら問題なし、2 なら**保存はしたが問題がある**。
    1（失敗）と分けるのは、*「取れなかった」と「取れたが疑わしい」は
    次にやることが違う*ため。
    """
    args = build_parser().parse_args(argv)
    root = Path(__file__).resolve().parents[1]

    try:
        env = env_file.load(root / args.env if not Path(args.env).is_absolute() else args.env)
        api_key = gemini_client.read_api_key(env)
    except (env_file.EnvFileError, gemini_client.AuthError) as error:
        print(error, file=sys.stderr)
        return 1

    client = gemini_client.build_client(api_key)

    def send(*, prompt: str, audio_bytes: bytes, mime_type: str) -> Reply:
        return gemini_client.generate_with_audio(
            client,
            prompt=prompt,
            audio_bytes=audio_bytes,
            mime_type=mime_type,
            api_key=api_key,
        )

    try:
        result = transcribe(args.audio, send=send)
    except (ValueError, gemini_client.GeminiError) as error:
        print(error, file=sys.stderr)
        return 1

    out = args.out or args.audio.parent / "transcript.txt"
    out.write_text(result.raw + chr(10), encoding="utf-8", newline=chr(10))

    audio = result.audio
    print("音声   : {} / {:.2f} 秒 / {:,} バイト".format(
        audio.path.name, audio.seconds, audio.n_bytes))
    print("発言   : {} 件 / 話者 {} 人".format(
        len(result.utterances), len({u.speaker for u in result.utterances})))
    print("最後   : {} 秒（音声の {:.0%}）".format(
        result.utterances[-1].at, result.utterances[-1].at / audio.seconds))
    print("文字数 : {:,}（{:.2f} 文字/秒）".format(
        sum(len(u.text) for u in result.utterances),
        sum(len(u.text) for u in result.utterances) / audio.seconds))
    print("打切り : {}".format(result.reply.finish_reason))
    print("トークン: 入力 {} / 出力 {}".format(
        result.reply.prompt_tokens, result.reply.output_tokens))
    print("保存   : {}".format(out))

    if result.problems:
        print("")
        print("**正常終了したが、疑う理由がある**:")
        for p in result.problems:
            print("  -", p)
        return 2
    print("検査   : 問題なし")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
