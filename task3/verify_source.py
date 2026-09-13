"""議事録を**台本（ソース）**と突き合わせる。文字起こしとではない。

`minutes.py` の照合は「議事録が文字起こしに忠実か」しか見ない。
2026-09-12 の実機で、**全ての引用が逐語で、時刻も合い、検査が「問題なし」を
返した議事録**に、文字起こしの誤変換（特集→特許・試算→資産）が
そのまま入っていた（`DESIGN.md` 13章）。

*下流が上流に忠実であることを検査すると、上流の誤りが「正しい」と証明されてしまう。*
**一貫性の検査は、正しさの検査ではない。**

これは課題2 の講評（11章）が言っていることそのものである:

    ただ商品リンクにアクセスし、取得した情報が正しいかどうかまで確認して
    スクショをとれるとより良いですね！

**`verify_doc.py` と名前を分けてある。** あちらは**出力側**の読み戻し
（書いたものが書いたとおりに入っているか）。こちらは**ソース側**との照合
（書いたものが元の実物と合っているか）。*同じ `verify` で始まる関数を
1つのファイルに並べたら、また「照合した」で終わる。*

逐語一致は期待しない
------------------------------------------------------------------

引用は **台本 → TTS → 音声 → 文字起こし** を通っているので、台本とは必ず少し違う。
だから `difflib` で**いちばん近い台本の行**を探し、*違う文字だけ*を出す。

**そろえすぎない。** 句読点は文字起こしが勝手に付けるので落とすが、
特集と特許は別のままにする——*何でも一致するようになったら、この層は無意味になる。*
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path
from typing import Iterable, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent / "tools"))

import build_audio  # noqa: E402
import minutes  # noqa: E402
from minutes import CHAT, Item  # noqa: E402

#: 台本に「対応する発言がある」とみなす下限。これを割ったら**別のことを言っている**。
FOUND_THRESHOLD = 0.6

#: 「一致した」とみなす下限。1.0 未満は差分を出す。
MATCH_THRESHOLD = 0.999

#: 差分に添える前後の文字数。**0 にすると、どの語が変わったか読めなくなる。**
DIFF_CONTEXT = 2

#: 正解の項目と議事録の項目を結び付ける下限。
LINK_THRESHOLD = 0.4

#: 名前の敬称。**「小林」と「小林さん」は同じ人。**
_HONORIFIC = re.compile(r"(さん|さま|様|氏|くん|君)$")

#: 文字起こしが勝手に付ける記号。**落とさないと差分が句読点で埋まる。**
_PUNCT = re.compile(r"[、。，．・！？!?「」『』（）()〜~\-—…　]")


@dataclass(frozen=True)
class Finding:
    level: str
    kind: str
    message: str


@dataclass(frozen=True)
class Report:
    findings: list[Finding] = field(default_factory=list)
    checked: int = 0
    matched: int = 0


# ------------------------------------------------------------------ 下ごしらえ


def normalize_for_match(text: str) -> str:
    """照合用にそろえる。`minutes.normalize` に**記号落とし**を足しただけ。"""
    return _PUNCT.sub("", minutes.normalize(text))


def coverage(candidate: str, quote: str) -> float:
    """引用のうち、候補に含まれている割合。

    `SequenceMatcher.ratio()` は使わない。**長さの違いで下がる**ので、
    長い台本の行に短い引用が完全に含まれていても低い値になり、
    *「一致しているのに違う」と言い出す*。
    """
    if not quote:
        return 0.0
    matcher = SequenceMatcher(None, candidate, quote, autojunk=False)
    matched = sum(block.size for block in matcher.get_matching_blocks())
    return matched / len(quote)


def _candidates(lines: Sequence) -> Iterable[str]:
    """台本の各行と、**隣り合う2行をつないだもの**。

    文字起こしは1文ずつに割れるので、*1つの台本の行に収まらない引用*が出る。
    """
    norms = [normalize_for_match(getattr(l, "text", str(l))) for l in lines]
    for i, n in enumerate(norms):
        yield n
        if i + 1 < len(norms):
            yield n + norms[i + 1]


def matched_span(candidate: str, quote: str) -> str:
    """候補のうち、引用に対応している範囲だけを切り出す。

    **候補の行まるごとと比べない。** 隣り合う2行をつないだ候補が選ばれると、
    引用に対応しない前半が「が欠落」として大量に出る
    ——*雑音が多い検査は、本物を隠す*（2026-09-13 に実際そうなった）。
    """
    blocks = [
        b for b in SequenceMatcher(None, candidate, quote, autojunk=False).get_matching_blocks()
        if b.size
    ]
    if not blocks:
        return candidate
    return candidate[blocks[0].a : blocks[-1].a + blocks[-1].size]


def closest(quote: str, lines: Sequence, truth: dict | None = None) -> tuple[float, str]:
    """いちばん近い台本の箇所と、その近さを返す。

    `truth` を渡すと、台本の側を**読みから表記へ**そろえてから比べる
    （千八百個 → 1800個）。持っている対応表を使わずに「違う」と言うのは、
    検査ではなく雑音である。
    """
    q = normalize_for_match(quote)
    best = (0.0, "")
    for cand in _candidates(lines):
        cand = apply_readings(cand, truth) if truth else cand
        score = coverage(cand, q)
        if score > best[0]:
            best = (score, matched_span(cand, q))
    return best


def apply_readings(text: str, truth: dict | None) -> str:
    """台本の読み（千八百個）を、議事録に出る表記（1800個）へ寄せる。

    **対応は正解データが持っている**（`numbers` の `spoken` / `value`）。
    長いものから置き換える——短い読みが先に当たると、
    *「千八百個」が「千八百」＋「個」に割れて別物になる*。
    """
    if not truth:
        return text
    pairs = [
        (normalize_for_match(n.get("spoken", "")), normalize_for_match(str(n.get("value", ""))))
        for n in truth.get("numbers") or []
    ]
    for spoken, value in sorted(pairs, key=lambda kv: -len(kv[0])):
        if spoken and value:
            text = text.replace(spoken, value)
    return text


def clean_owner(name: str) -> str:
    """敬称を落とす。**「小林」と「小林さん」は同じ人。**"""
    return _HONORIFIC.sub("", minutes.normalize(name))


def diff_marks(source: str, quote: str) -> str:
    """**違うところ**を「台本→議事録」の形で並べる。同じなら空。

    **前後の文脈を付ける。** 文字単位の差分だけを出すと、
    「特集」と「特許」が `集→許` になり、*どの語が変わったのか読めない*
    （共通の「特」が差分から落ちるため）。2文字ずつ添えて
    `は特[集→許]の前` の形にする。
    """
    a, b = normalize_for_match(source), normalize_for_match(quote)
    marks: list[str] = []
    for tag, i1, i2, j1, j2 in SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if tag == "equal":
            continue
        before, after = a[i1:i2], b[j1:j2]
        left = a[max(0, i1 - DIFF_CONTEXT) : i1]
        right = a[i2 : i2 + DIFF_CONTEXT]
        if tag == "replace":
            core = "[{}→{}]".format(before, after)
        elif tag == "delete":
            core = "[{}が欠落]".format(before)
        else:
            core = "[{}が増加]".format(after)
        marks.append(left + core + right)
    return " / ".join(marks)


# ------------------------------------------------------------------ 引用の照合


def check_quotes(
    items: Sequence[Item], lines: Sequence, chat_log: str, truth: dict | None = None
) -> tuple[list[Finding], int, int]:
    """各項目の引用を**台本**と突き合わせる。チャット由来はチャットと。

    返すのは (所見, 照合した件数, 一致した件数)。
    **件数を返すのは、1件だけ合っていた絵が何も言っていないから**（課題2の講評）。
    """
    findings: list[Finding] = []
    chat_lines = [type("L", (), {"text": l})() for l in (chat_log or "").splitlines() if l.strip()]
    checked = matched = 0

    for it in items:
        if not minutes.normalize(it.quote):
            findings.append(Finding("error", "quote", "{}「{}」: 引用が空です".format(
                minutes._LABEL[it.kind], it.text)))
            continue
        checked += 1
        where = "チャット" if it.source == CHAT else "台本"
        score, span = closest(
            it.quote, chat_lines if it.source == CHAT else lines,
            None if it.source == CHAT else truth,
        )
        label = "{}「{}」".format(minutes._LABEL[it.kind], it.text)

        if score < FOUND_THRESHOLD:
            findings.append(Finding(
                "error", "missing",
                "{}: {}に対応する発言が見つかりません（{!r}）".format(label, where, it.quote)))
        elif score < MATCH_THRESHOLD:
            findings.append(Finding(
                "error", "mismatch",
                "{}: {}と文字が違います → {}".format(label, where, diff_marks(span, it.quote))))
        else:
            matched += 1
    return findings, checked, matched


# ------------------------------------------------------------------ 正解との照合


def _best(text: str, candidates: Sequence[Item]) -> tuple[float, Item | None]:
    best: tuple[float, Item | None] = (0.0, None)
    target = normalize_for_match(text)
    for c in candidates:
        score = coverage(normalize_for_match(c.text), target)
        if score > best[0]:
            best = (score, c)
    return best


def check_truth(
    decisions: Sequence[Item],
    todos: Sequence[Item],
    open_issues: Sequence[Item],
    truth: dict,
    *,
    chat_used: bool = False,
) -> list[Finding]:
    """台本が持っている**正解の構造**と突き合わせる。

    引用の照合（`check_quotes`）が「言葉」を見るのに対し、こちらは
    **「何が決まったか」**を見る。*引用が全部正しくても、拾う項目を
    間違えていれば議事録は間違っている。*
    """
    findings: list[Finding] = []
    decision_text = normalize_for_match(" ".join(d.text for d in decisions))

    for want in truth.get("decisions") or []:
        for token in want.get("must_contain") or []:
            if normalize_for_match(token) not in decision_text:
                findings.append(Finding(
                    "error", "decision",
                    "決定「{}」が見当たりません（{} が決定事項に無い）".format(want["text"], token)))
        for token in want.get("must_not_contain") or []:
            if normalize_for_match(token) in decision_text:
                findings.append(Finding(
                    "error", "retracted",
                    "撤回された値 {} が決定事項に残っています（{}）".format(token, want["text"])))

    for pair in truth.get("confusables") or []:
        wrong = pair.get("wrong")
        if wrong and normalize_for_match(wrong) in decision_text:
            findings.append(Finding(
                "error", "confusable",
                "誤った側「{}」が決定事項にあります（正しくは「{}」）".format(wrong, pair.get("correct"))))

    for issue in truth.get("open_issues") or []:
        score, hit = _best(issue["text"], decisions)
        if score > LINK_THRESHOLD and hit is not None:
            findings.append(Finding(
                "error", "promoted",
                "論点「{}」が決定事項に格上げされています（「{}」）".format(issue["text"], hit.text)))

    for want in truth.get("todos") or []:
        score, hit = _best(want["text"], todos)
        if hit is None or score <= LINK_THRESHOLD:
            continue
        got_owner = clean_owner(hit.owner or "")
        if got_owner and got_owner != clean_owner(want.get("owner", "")):
            findings.append(Finding(
                "error", "owner",
                "TODO「{}」の担当が違います（台本: {} / 議事録: {}）".format(
                    want["text"], want.get("owner"), got_owner)))

    if not chat_used:
        body = normalize_for_match(
            " ".join(i.text for i in [*decisions, *todos, *open_issues]))
        for absent in truth.get("absent_from_audio") or []:
            token = absent.get("text")
            if token and normalize_for_match(token) in body:
                findings.append(Finding(
                    "error", "invented",
                    "音声に無い「{}」が議事録にあります（{}）".format(token, absent.get("about"))))

    return findings


# ------------------------------------------------------------------ まとめ


def verify(
    decisions: Sequence[Item],
    todos: Sequence[Item],
    open_issues: Sequence[Item],
    lines: Sequence,
    truth: dict,
    *,
    chat_log: str = "",
) -> Report:
    items = [*decisions, *todos, *open_issues]
    quote_findings, checked, matched = check_quotes(items, lines, chat_log, truth)
    truth_findings = check_truth(
        decisions, todos, open_issues, truth, chat_used=bool(chat_log.strip())
    )
    return Report(
        findings=[*quote_findings, *truth_findings], checked=checked, matched=matched
    )


# ------------------------------------------------------------------ 入口


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="議事録を台本（ソース）と突き合わせる。出力側の読み戻しではない"
    )
    parser.add_argument("minutes_json", type=Path, help="minutes.py が書いた JSON")
    parser.add_argument("--script", type=Path, default=None,
                        help="台本（既定: 議事録と同じ場所の script.md）")
    parser.add_argument("--chat", type=Path, default=None, help="チャットログを使った回なら渡す")
    return parser


def say(text: str) -> None:
    """**表示で落ちない。** 所見を持っているのに出せずに終わるのが最悪。

    Windows の既定の出力は cp932 で、そこに無い文字（em ダッシュなど）が
    1つ混ざると `UnicodeEncodeError` で**レポート全体が消える**。
    *1文字化けることと、8件の所見を失うことは釣り合わない。*

    それでも本文には cp932 に有る記号だけを使う（2026-09-13 に実際に落ちた）
    ——**落ちない仕組みは、落ちない書き方の代わりにはならない。**
    """
    try:
        print(text)
    except UnicodeEncodeError:
        enc = sys.stdout.encoding or "utf-8"
        print(text.encode(enc, errors="replace").decode(enc))


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    script_path = args.script or args.minutes_json.with_name("script.md")

    for path in (args.minutes_json, script_path):
        if not path.exists():
            say("見つかりません: {}".format(path), file=sys.stderr)
            return 1

    md = script_path.read_text(encoding="utf-8")
    lines = build_audio.parse_script(md)
    truth = build_audio.parse_truth(md)
    decisions, todos, open_issues = minutes.parse_minutes(
        args.minutes_json.read_text(encoding="utf-8")
    )
    chat_log = args.chat.read_text(encoding="utf-8") if args.chat else ""

    report = verify(decisions, todos, open_issues, lines, truth, chat_log=chat_log)

    say("台本    : {} / {} 行".format(script_path.name, len(lines)))
    say("議事録  : 決定 {} 件 / TODO {} 件 / 論点 {} 件".format(
        len(decisions), len(todos), len(open_issues)))
    say("引用照合: {} 件中 {} 件が台本と一致".format(report.checked, report.matched))
    say("チャット: {}".format(args.chat.name if args.chat else "使っていない"))

    if not report.findings:
        say("")
        say("所見なし。**ただし台本に無いことは、この検査でも見えない**（DESIGN 6章）")
        return 0

    say("")
    say("所見 {} 件:".format(len(report.findings)))
    for f in report.findings:
        say("  [{}] {}".format(f.kind, f.message))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
