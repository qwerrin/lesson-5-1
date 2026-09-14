"""`meeting/script.md`（正本）から会議音声 `meeting/meeting.wav` を作る。

**素材は成果物ではなく物差しである。** ここが狂うと `verify_source.py` の照合が
そのまま嘘になるので、TTS を叩く**前に**台本と正解の食い違いを全部出す。

============ ====================================================================
DESIGN       ここで引き受けること
============ ====================================================================
3.3-5        3話者は WinRT 経由（`System.Speech` からは Haruka しか呼べない）
5-A / T4     負の間隔で**実際に波形を重ねる**。直列に並べたら被りは作れない
5-R          台本と正解の食い違いを、**音を作る前に**検出する
============ ====================================================================

**numpy は使わない。** 素材生成のために採点者の `pip install` を1つ増やす価値がない。
16bit PCM の足し算は `array` で足りる。

**日本語は `.ps1` に渡さない。** PowerShell 5.1 は BOM 無しの `.ps1` を cp932 で読むため、
UTF-8 の日本語コメント1行がその**次の行**を構文エラーにする。台本は `lines.txt`
（UTF-8）へ書き出し、`.ps1` 側は ASCII だけで書く。
"""

from __future__ import annotations

import json
import subprocess
import sys
import wave
from array import array
from dataclasses import dataclass
from pathlib import Path

FENCE = chr(96) * 3
INT16_MIN, INT16_MAX = -32768, 32767

# 無音とみなす振幅。実測で、無音区間の実効値は 19 前後・発話は 2,400 前後だった。
SILENCE_THRESHOLD = 256
# 切り落としたあとに残す余白（秒）。ぴったり切ると子音の頭が消える。
TRIM_KEEP_SEC = 0.05
# 被り区間の**どちら側も**これを超えていなければ、声としては被っていない。
OVERLAP_MIN_RMS = 300.0

HERE = Path(__file__).resolve().parent
MEETING = HERE.parent / "meeting"
SCRIPT_MD = MEETING / "script.md"
LINES_TXT = MEETING / "lines.txt"
PARTS_DIR = MEETING / "_parts"
OUT_WAV = MEETING / "meeting.wav"
TTS_PS1 = HERE / "tts_lines.ps1"
REPO = HERE.parents[1]


def shown(path: Path) -> str:
    """画面に出す用のパス。**リポジトリからの相対にする。**

    絶対パスのまま出すと利用者名を含むホームのパスが実行画面に残り、
    *そのままスクリーンショットとして公開される*（課題2 で実際に踏んだ）。
    **伏せるのではなく、最初から出さない。**
    """
    try:
        return str(path.relative_to(REPO))
    except ValueError:
        return str(path)


@dataclass(frozen=True)
class Line:
    """台本の1行。`gap` は**前の行が終わってからの秒数**で、負なら重なる。"""

    voice: str
    gap: float
    text: str


# --------------------------------------------------------------------- 読み取り

def _fence(md: str, lang: str) -> list[str]:
    """指定した言語タグのフェンスの中身を、行の列で返す。

    **見つからなければ例外にする。** 空の列を返すと、呼び手は0行の台本で
    無音の wav を作り、成功として報告してしまう。
    """
    marker = FENCE + lang
    body = md.splitlines()
    for i, raw in enumerate(body):
        if raw.strip() != marker:
            continue
        for j in range(i + 1, len(body)):
            if body[j].strip().startswith(FENCE):
                return body[i + 1 : j]
        raise ValueError("{} のフェンスが閉じていない".format(marker))
    raise ValueError("{} のフェンスが見つからない".format(marker))


def parse_script(md: str) -> list[Line]:
    """台本のフェンスを `Line` の列にする。区切りは**最初の2つの縦棒だけ**。

    本文に縦棒が入ることがある（「3980|4380 のどちらか」）ので、
    全部で割ると本文が切れる。
    """
    lines: list[Line] = []
    for n, raw in enumerate(_fence(md, "text"), 1):
        s = raw.strip()
        if not s:
            continue
        parts = s.split("|", 2)
        if len(parts) != 3:
            raise ValueError("{}行目: 声|間隔|本文 の形でない: {}".format(n, s))
        voice, gap_s, text = parts
        try:
            gap = float(gap_s)
        except ValueError:
            raise ValueError("{}行目: 間隔が数でない: {}".format(n, gap_s)) from None
        lines.append(Line(voice.strip(), gap, text))
    if not lines:
        raise ValueError("台本が0行。フェンスはあるが中身が無い")
    return lines


def parse_truth(md: str) -> dict:
    """正解の JSON フェンスを読む。

    行は**空白で連結する**。JSON はトークンの間の空白を無視し、
    文字列の中に生の改行は入れられないので、これで必ず等価になる。
    """
    return json.loads(" ".join(_fence(md, "json")))


# ----------------------------------------------------------------------- 照合

def audit(lines: list[Line], truth: dict) -> list[str]:
    """台本と正解の食い違いを**全部**返す。空なら食い違い無し。

    **TTS を44回叩いた後で落ちるのでは遅い。** 声名の打ち間違いも、
    正解にしか無い語も、ここで一度に出す。
    """
    problems: list[str] = []
    texts = [l.text for l in lines]

    def present(s: str) -> bool:
        return any(s in t for t in texts)

    speakers = (truth.get("meeting") or {}).get("speakers") or {}
    if not speakers:
        problems.append("正解に meeting.speakers が無い")
    for n, l in enumerate(lines, 1):
        if speakers and l.voice not in speakers:
            problems.append("{}行目: 正解に無い声名 {}".format(n, l.voice))

    for c in truth.get("confusables") or []:
        for key in ("correct", "wrong"):
            word = c.get(key)
            if word and not present(word):
                problems.append("confusables.{} が台本に無い: {}".format(key, word))

    for num in truth.get("numbers") or []:
        spoken = num.get("spoken")
        if spoken and not present(spoken):
            problems.append("numbers.spoken が台本に無い: {}".format(spoken))

    # 逆向きの検査。**在ってはいけないものが在る**と、4-② の実験が成立しない。
    for a in truth.get("absent_from_audio") or []:
        word = a.get("text")
        if word and present(word):
            problems.append("absent_from_audio なのに台本にある: {}".format(word))

    return problems


# --------------------------------------------------------------------- 時間割

def plan_starts(durations: list[float], gaps: list[float]) -> list[float]:
    """各行の開始秒を出す。

    **順序が壊れる重なりは例外にする。** 黙って並べ替えると、
    台本の順番と音声の順番が食い違ったまま「成功」する。
    """
    if len(durations) != len(gaps):
        raise ValueError("長さが違う: {} と {}".format(len(durations), len(gaps)))
    starts: list[float] = []
    end = 0.0
    for i, (d, g) in enumerate(zip(durations, gaps)):
        s = end + g
        if s < 0:
            raise ValueError("{}: 開始が負になる（{:.3f}）".format(i, s))
        if starts and s < starts[-1]:
            raise ValueError(
                "{}: 直前（{:.3f}）より前から始まる（{:.3f}）。重ねすぎ".format(
                    i, starts[-1], s
                )
            )
        starts.append(s)
        end = max(end, s + d)
    return starts


def mix(parts: list[array], starts: list[int]) -> array:
    """16bit PCM を開始位置に置いて**足し合わせる**。上書きしない。

    重なった区間で片方が消えると、「被りを拾えなかった」という実験結果と
    **見た目がまったく同じになる**——仕込めていない罠は、突破された罠と区別が付かない。
    """
    if len(parts) != len(starts):
        raise ValueError("長さが違う: {} と {}".format(len(parts), len(starts)))
    total = max((s + len(p) for s, p in zip(starts, parts)), default=0)
    buf = array("h", bytes(2 * total))
    written_to = 0
    for start, part in zip(starts, parts):
        if start >= written_to:
            buf[start : start + len(part)] = part
        else:
            # buf には**それまでの全部**が既に混ざっている。重なる区間だけ足す。
            overlap = min(written_to - start, len(part))
            for i in range(overlap):
                v = buf[start + i] + part[i]
                if v > INT16_MAX:
                    v = INT16_MAX
                elif v < INT16_MIN:
                    v = INT16_MIN
                buf[start + i] = v
            if overlap < len(part):
                buf[start + overlap : start + len(part)] = part[overlap:]
        written_to = max(written_to, start + len(part))
    return buf


# --------------------------------------------------- 無音の切り落としと被りの実効

def trim_edges(
    samples: array,
    rate: int,
    threshold: int = SILENCE_THRESHOLD,
    keep: float = TRIM_KEEP_SEC,
) -> array:
    """前後の無音を落とす。**`keep` 秒だけ余白を残す**（声の頭を削らないため）。

    TTS は1発話ごとに前後へ無音を付ける（実測で末尾 平均 0.810 秒）。
    落とさないと、台本の `gap` は「声と声の間隔」ではなく
    「**無音を挟んだあとの間隔**」になり、**負の間隔が無音としか重ならない**。
    """
    n = len(samples)
    lo = 0
    while lo < n and abs(samples[lo]) < threshold:
        lo += 1
    if lo == n:
        raise ValueError("全区間が無音（発話が入っていない）")
    hi = n
    while hi > lo and abs(samples[hi - 1]) < threshold:
        hi -= 1
    margin = round(keep * rate)
    return samples[max(0, lo - margin) : min(n, hi + margin)]


def rms(samples: array) -> float:
    """実効値。**「無音と重なった」を数で言えるようにするために要る。**"""
    if len(samples) == 0:
        return 0.0
    return (sum(v * v for v in samples) / len(samples)) ** 0.5


def overlap_report(
    parts: list[array], starts: list[int], gaps: list[float], rate: int
) -> list[dict]:
    """負の間隔ごとに、重なった区間の**両側**の実効値を返す。

    **片側が無音なら、被りは仕込めていない。** 2026-09-12 に実際に踏んだ:
    足し算は 12,800/12,800 サンプルとも正しかったのに、重なった相手は
    TTS が末尾に付ける 0.8 秒の無音だった。*混ぜ方が正しいことは、
    声が被っていることの証拠にならない。*
    """
    found: list[dict] = []
    for i, g in enumerate(gaps):
        if i == 0 or g >= 0:
            continue
        lo, hi = starts[i], starts[i - 1] + len(parts[i - 1])
        if hi <= lo:
            continue
        off = lo - starts[i - 1]
        span = hi - lo
        found.append(
            {
                "index": i,
                "seconds": span / rate,
                "rms_prev": rms(parts[i - 1][off : off + span]),
                "rms_next": rms(parts[i][:span]),
            }
        )
    return found


# ------------------------------------------------------------------- wav 入出力

def read_wav(path: Path) -> tuple[array, int]:
    with wave.open(str(path), "rb") as w:
        if w.getnchannels() != 1 or w.getsampwidth() != 2:
            raise ValueError(
                "{}: mono / 16bit でない（{}ch {}byte）".format(
                    path.name, w.getnchannels(), w.getsampwidth()
                )
            )
        rate = w.getframerate()
        raw = w.readframes(w.getnframes())
    samples = array("h")
    samples.frombytes(raw)
    if sys.byteorder != "little":
        samples.byteswap()
    return samples, rate


def write_wav(path: Path, samples: array, rate: int) -> None:
    payload = samples
    if sys.byteorder != "little":
        payload = array("h", samples)
        payload.byteswap()
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(payload.tobytes())


def write_lines(lines: list[Line], path: Path) -> None:
    """TTS へ渡す UTF-8 の入力。**派生物なので追跡しない**（.gitignore）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline=chr(10)) as f:
        for l in lines:
            print("{}|{}|{}".format(l.voice, l.gap, l.text), file=f)


# --------------------------------------------------------------------- 組み立て

def main() -> int:
    md = SCRIPT_MD.read_text(encoding="utf-8")
    lines = parse_script(md)
    truth = parse_truth(md)

    problems = audit(lines, truth)
    if problems:
        print("台本と正解が食い違っている（音は作らない）:")
        for p in problems:
            print("  -", p)
        return 1
    print("照合 : 台本 {} 行 / 食い違い 0 件".format(len(lines)))

    write_lines(lines, LINES_TXT)
    PARTS_DIR.mkdir(parents=True, exist_ok=True)
    for old in PARTS_DIR.glob("*.wav"):
        old.unlink()

    r = subprocess.run(
        [
            "powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
            "-File", str(TTS_PS1),
            "-LinesPath", str(LINES_TXT),
            "-OutDir", str(PARTS_DIR),
        ],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if r.returncode != 0:
        print(r.stdout)
        print(r.stderr, file=sys.stderr)
        return r.returncode

    made = sorted(PARTS_DIR.glob("*.wav"))
    if len(made) != len(lines):
        print(
            "台本 {} 行に対して wav が {} 個".format(len(lines), len(made)),
            file=sys.stderr,
        )
        return 1

    raw, rates = [], set()
    for p in made:
        s, rate = read_wav(p)
        raw.append(s)
        rates.add(rate)
    if len(rates) != 1:
        raise ValueError("標本化周波数が揃っていない: {}".format(sorted(rates)))
    rate = rates.pop()

    parts = [trim_edges(s, rate) for s in raw]
    cut = (sum(len(a) for a in raw) - sum(len(a) for a in parts)) / rate
    print("無音 : 前後を切り落として {:.1f} 秒ぶん減らした".format(cut))

    gaps = [l.gap for l in lines]
    durations = [len(p) / rate for p in parts]
    starts = plan_starts(durations, gaps)
    starts_smp = [round(s * rate) for s in starts]

    # **書く前に見る。** 後ろへ置いた検査は「仕込めていない」を報告できない。
    overlaps = overlap_report(parts, starts_smp, gaps, rate)
    if not overlaps:
        print("被り : 0 箇所（負の間隔が台本に無い）")
    for o in overlaps:
        print(
            "被り : {}行目 {:.2f} 秒 / 実効値 前 {:.0f} 後 {:.0f}".format(
                o["index"] + 1, o["seconds"], o["rms_prev"], o["rms_next"]
            )
        )
    dead = [o for o in overlaps if min(o["rms_prev"], o["rms_next"]) < OVERLAP_MIN_RMS]
    if dead:
        print("被った相手が無音の箇所がある＝罠が仕込めていない:", file=sys.stderr)
        for o in dead:
            print(
                "  - {}行目（前 {:.0f} / 後 {:.0f} / 閾値 {:.0f}）".format(
                    o["index"] + 1, o["rms_prev"], o["rms_next"], OVERLAP_MIN_RMS
                ),
                file=sys.stderr,
            )
        return 1

    out = mix(parts, starts_smp)
    write_wav(OUT_WAV, out, rate)

    total = len(out) / rate
    chars = sum(len(l.text) for l in lines)
    size = OUT_WAV.stat().st_size
    by_voice: dict[str, float] = {}
    for l, d in zip(lines, durations):
        by_voice[l.voice] = by_voice.get(l.voice, 0.0) + d

    print("出力 : {}".format(shown(OUT_WAV)))
    print(
        "長さ : {:.2f} 秒（{}分{:02d}秒）".format(
            total, int(total // 60), int(total % 60)
        )
    )
    print(
        "容量 : {:,} バイト（{:.2f} MB / inline 上限 20MB）".format(
            size, size / 1024 / 1024
        )
    )
    print("速さ : {:.2f} 文字/秒（発話 {} 文字）".format(chars / sum(durations), chars))
    for v, sec in sorted(by_voice.items(), key=lambda kv: -kv[1]):
        n = sum(1 for l in lines if l.voice == v)
        print("  {:<8} {:6.2f} 秒 / {} 行".format(v, sec, n))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
