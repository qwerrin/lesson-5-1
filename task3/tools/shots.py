#!/usr/bin/env python3
"""スクリーンショットを撮るための実行係。

使い方::

    .venv\\Scripts\\python.exe task3\\tools\\shots.py        # 一覧
    .venv\\Scripts\\python.exe task3\\tools\\shots.py 04     # 04 を実行

**1枚ごとに「この絵が何を証明するか」を帯に出す。**
課題2 の講評（`DESIGN.md` 11章）と、教訓「証拠は貼るだけでは主張にならない」の両方が
同じことを言っている——*スクリーンショットに何が写っているかと、何を確かめたかは別*。
帯に主張を書いておけば、記事へ貼るときに**絵と主張が離れない**。

順番にも意味がある
------------------------------------------------------------------

**03（議事録の「問題なし」）の次に 04（ソース側の照合）を置く。**
*同じ成果物について、内部整合の検査が「問題なし」と言った直後に、
台本と8件中2件しか合っていない絵が出る。* これが今回の山場で、
「出力を2回読むのは照合ではない」の一枚絵になる。

画面に出すもの
------------------------------------------------------------------

**絶対パスを出さない。** ホームのパスが写ると、そのまま公開物に残る
（課題2 で実際に踏んだ）。この道具も、呼ぶ側のコマンドも相対パスで書く。

撮影の段取り
------------------------------------------------------------------

課題2 では「`cd` を**先に**やってから `cls` で消す」と決めた。
**それだけでは足りない**（2026-09-14 に実際に写った）。

**消えるのは履歴だけで、枠は毎回ホームのパスを出す**。実測（2026-09-14）:

    プロンプト  : PS C:/Users/<利用者名>/Documents/.../lesson-5-1>
    タイトルバー: （同じくパスが入る）

プロンプトは**毎行**、タイトルバーは**常時**写る:

    PS C:/Users/<利用者名>/Documents/.../lesson-5-1>

コマンドの出力は3つとも実測でクリーンだった。*漏れていたのは、
自分が書いたものではなく、シェルが毎行書いているほうだった。*

順番:

1. `cd <リポジトリ>`  ——ここで打つフルパスは、後で消える
2. `function prompt { "PS lesson-5-1> " }`  ——**プロンプトを短くする**
3. `$Host.UI.RawUI.WindowTitle = "lesson-5-1"`  ——**タイトルバーを短くする**
4. `cls`  ——ここまでの行を消す
5. 撮る

**2 と 3 を飛ばすと、1 と 4 をどれだけ丁寧にやっても写る。**

*自分の出力だけを検査しても見つからない。* コマンド3つの出力を
`Users|sprin|C:` で grep したら**全部きれいだった**——
漏れていたのは、**自分が書いたものではなく、環境が毎行書いているほう**である。
"""

from __future__ import annotations

import subprocess
import sys
import unicodedata
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PY = str(Path(".venv") / "Scripts" / "python.exe")
MEETING = Path("task3") / "meeting"

MINUTES = MEETING / "minutes.txt"
MEETING_ARGS = ["--meeting", "週次定例（EC運営）", "--held-at", "2026-09-12 10:00"]

#: (番号, 見出し, この絵が証明すること, コマンド, 課金が乗るか)
STEPS = [
    (
        "01",
        "台本から音声を作る",
        "台本と正解データが食い違っていないこと（56行・0件）と、"
        "被り2箇所が**両側とも声**であること（実効値が出る）。"
        "混ぜ方が正しいだけでは、声が被っている証拠にならない",
        [PY, "task3/tools/build_audio.py"],
        False,
    ),
    (
        "02",
        "音声を文字起こしする",
        "打ち切りが無く（finish_reason=STOP）、末尾が音声の99%まで届き、"
        "話者が3人で、書式を外れた話者名が無いこと。"
        "**どれも例外にならない失敗**なので、検査の行そのものが証拠になる",
        [PY, "task3/transcribe.py", str(MEETING / "meeting.wav")],
        True,
    ),
    (
        "03",
        "議事録を作る（内部整合の検査つき）",
        "**「検査: 問題なし」と出ること。** 全ての引用が文字起こしに逐語で存在し、"
        "時刻も合い、終盤まで届いている。**この絵は次の04と対にして初めて意味を持つ**",
        [PY, "task3/minutes.py", str(MEETING / "transcript.txt"),
         "--audio", str(MEETING / "meeting.wav"), "--chat", str(MEETING / "chat.txt"),
         "--attendees", "田村・小林・佐藤", "--out", str(MINUTES)] + MEETING_ARGS,
        True,
    ),
    (
        "04",
        "★ 議事録を台本（ソース）と突き合わせる",
        "**03 が「問題なし」と言った同じ議事録が、台本とは8件中2件しか一致しないこと。** "
        "文字起こしの誤変換（試算→資産・特集→特許）が、忠実な引用として固定されている。"
        "*下流が上流に忠実であることを検査すると、上流の誤りが「正しい」と証明されてしまう*",
        [PY, "task3/verify_source.py", str(MEETING / "minutes.json"),
         "--chat", str(MEETING / "chat.txt")],
        False,
    ),
    (
        "05",
        "Google ドキュメントへ書き出す",
        "会議1本につき1つ新規作成され、リンクと内容のハッシュが出ること。"
        "作成と挿入は別（`documents().create` は本文を無視して成功する）",
        [PY, "task3/to_doc.py", str(MINUTES), "--force"] + MEETING_ARGS,
        False,
    ),
    (
        "06",
        "★ 書いたものを読み返して照合する（出力側）",
        "**書いた `batchUpdate` ではなく、読む `documents().get` で読み直す。** "
        "ID・タイトル・本文・文字数・段落数の5項目。"
        "04 とは**見ている相手が違う**（あちらはソース側）",
        None,  # ドキュメントIDが要るので下で組む
        False,
    ),
    (
        "07",
        "同じ議事録を二度書かないこと（5-H）",
        "**止まること自体が証拠。** 会議1本につき1つ作る作りなので、"
        "二度目は別の議事録として並んでしまう。内容のハッシュを台帳に残して止める",
        [PY, "task3/to_doc.py", str(MINUTES)] + MEETING_ARGS,
        False,
    ),
    (
        "08",
        "テストとミューテーション",
        "テストが緑であることを結論にしない。**1か所ずつ壊して、全部落ちること**を確かめる。"
        "素通り0・置換先なし0（置換先が古いのも素通り扱いにしている）",
        [PY, "task3/tools/mutate.py"],
        False,
    ),
    (
        "09",
        "移植が移植元と1文字も違わないこと",
        "`common/docs_client.py` と `task3/verify_doc.py` は合格済みの課題から移した。"
        "**「そのまま移した」は主張であって証拠ではない**ので、"
        "元のソースと文字単位で比べる",
        [PY, "task3/tools/check_port.py"],
        False,
    ),
    (
        "10",
        "発展：決定事項だけを LINE に流す",
        "決定2件が**時刻と逐語つき**で組まれ、全文へのリンクが**台帳から引かれる**こと。"
        "リンクを引数で渡せる作りにすると、*書いたのとは別のドキュメントを指す通知*が"
        "送れてしまう（どちらも成功するので見た目では気づけない）。"
        "通数の増分（N→N+1）は、**別のエンドポイントが送信を認めた**という間接材料",
        [PY, "task3/notify.py", str(MEETING / "minutes.json"), "--force"],
        # 課金ではないが**無料枠の通数を1通使う**（月200通）。
        # 撮り直すたびに1通減るので、本文は先に --dry-run で確かめてから撮る。
        False,
    ),
]


def say(text: str) -> None:
    """表示で落ちない。**絵の説明を持っているのに出せずに終わるのが最悪。**

    **必ず flush する。** print は行バッファで、子プロセスは**直接**画面へ書く。
    流さずに `subprocess.run` へ渡すと、*帯が本文の後ろに出る*
    ——2026-09-14 に実際そうなった。順番そのものが説明の一部なので、
    「出た」ことと「正しい位置に出た」ことは別である。
    """
    try:
        print(text, flush=True)
    except UnicodeEncodeError:
        enc = sys.stdout.encoding or "utf-8"
        print(text.encode(enc, errors="replace").decode(enc), flush=True)


def width(text: str) -> int:
    """表示幅。全角を2、半角を1で数える。"""
    return sum(2 if unicodedata.east_asian_width(c) in "FWA" else 1 for c in text)


def wrap(text: str, limit: int) -> list[str]:
    """表示幅で折り返す。**文の区切りでは切らない。**

    `。` で割ると、強調の記号が文をまたいだときに*変な位置で割れる*。
    ここは画面なので、記号は先に落として幅だけで折る。
    """
    plain = text.replace("**", "").replace("*", "")
    lines, current = [], ""
    for ch in plain:
        if width(current) + width(ch) > limit:
            lines.append(current)
            current = ""
        current += ch
    if current:
        lines.append(current)
    return lines


def document_id() -> str | None:
    """台帳から、いまの議事録に対応するドキュメントIDを引く。

    **手で貼らせない。** 貼り間違えた ID で照合すると、
    *別のドキュメントと比べて「一致」と出る*ことがありうる。
    """
    sys.path.insert(0, str(ROOT / "task3"))
    sys.path.insert(0, str(ROOT))
    import to_doc  # noqa: PLC0415  ここでしか使わない

    body = (ROOT / MINUTES).read_text(encoding="utf-8")
    entry = to_doc.load_ledger(ROOT / MEETING / "posted.json").get(to_doc.content_hash(body))
    return entry.get("documentId") if entry else None


def command_of(step) -> list[str] | None:
    number, _, _, cmd, _ = step
    if number != "06":
        return cmd
    doc = document_id()
    if doc is None:
        return None
    title = "議事録 週次定例（EC運営） 2026-09-12 10:00"
    return [PY, "task3/verify_doc.py", doc, "--body", str(MINUTES), "--title", title]


def banner(step) -> None:
    number, label, claim, _, paid = step
    say("=" * 74)
    say("  {}  {}{}".format(number, label, "   ※この回は課金が乗る" if paid else ""))
    say("-" * 74)
    for line in wrap(claim, 68):
        say("  " + line)
    say("=" * 74)
    say("")


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        say("スクリーンショットの順番（引数に番号を渡すと実行する）")
        say("")
        for number, label, _, _, paid in STEPS:
            say("  {}  {}{}".format(number, label, "   ※課金" if paid else ""))
        say("")
        say("撮影の段取り（この順で）:")
        say("  1. cd <リポジトリ>")
        say('  2. function prompt { "PS lesson-5-1> " }')
        say('  3. $Host.UI.RawUI.WindowTitle = "lesson-5-1"')
        say("  4. cls")
        say("  5. 撮る")
        say("")
        say("2 と 3 を飛ばすと、プロンプトとタイトルバーにホームのパスが写る。")
        say("cls が消すのは履歴だけで、枠が毎回書くほうは消せない。")
        say("コマンドの出力自体は実測でクリーン（2026-09-14）。")
        return 0

    wanted = args[0].zfill(2)
    found = [s for s in STEPS if s[0] == wanted]
    if not found:
        say("そんな番号は無い: {}".format(args[0]))
        return 1
    step = found[0]

    cmd = command_of(step)
    if cmd is None:
        say("台帳にこの議事録のドキュメントIDがありません。先に 05 を実行してください。")
        return 1

    banner(step)
    return subprocess.run(cmd, cwd=ROOT).returncode


if __name__ == "__main__":
    raise SystemExit(main())
