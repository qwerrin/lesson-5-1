#!/usr/bin/env python3
"""課題3の素材生成を1か所ずつ壊して、テストが落ちることを確かめる。

**テストが通っていることは、守られていることの証拠にならない。**

使い方::

    .venv\\Scripts\\python.exe task3\\tools\\mutate.py

仕組みは課題1・2の ``mutate.py`` と同じ。リポジトリを一時ディレクトリへ写し、
**写した側だけ**を壊す。成果物には触らないので、途中で強制終了しても壊れたまま
残らない。**置換先が見つからない（NOT FOUND）は素通りと同じ扱い**にする
——実装を直して壊しかたを直し忘れると、何も壊さずに全部通って「穴ゼロ」と出る。

この課題で狙う失敗の形
------------------------------------------------------------------

素材は**成果物ではなく物差し**である。だから狙うのは
「**音は出るのに、物差しとしては壊れている**」失敗になる。
音を聞いても分からないところが、課題1・2の「静かに間違った値が残る」と違う。

================================== ==============================================
壊すと何が起きるか                  なぜ静かなのか
================================== ==============================================
フェンスが無くても空で返す          **無音の wav** を作って成功する
台本0行を通す                       同上。しかも「台本を読んだ」と報告する
区切りを全部で割る                  本文に縦棒がある行だけ**末尾が切れる**
間隔が数でなくても 0 にする         被りが消える。**音は普通に鳴る**
声名を照合しない                    TTS が 44 回目で落ちる。**原因が遠い**
正解の語が台本に無くても通す        `verify_source` が**永久に一致と言う**
不在の検査を逆向きにする            チャット限定の語が音声に入っても気づけない
順序が壊れる重なりを許す            台本と音声の**順番が食い違ったまま成功**
間隔を足さない                      全部が先頭から重なる。**音としては派手**
重ねずに上書きする                  片方が消える。**「拾えなかった」と同じ見た目**
飽和させない                        折り返して**正の大音量が負に化ける**
余白を残さず切る                    子音の頭が消える。**言葉が変わる**
全区間が無音でも通す                台本の1行が音として**丸ごと消える**
末尾を切らない                      被りが**無音としか重ならない**（実際に踏んだ）
片側の実効値を取り違える            仕込めていない罠を**仕込めたと報告する**
================================== ==============================================

**「末尾を切らない」は 2026-09-12 に実際に起きた形。** 足し算は
12,800/12,800 サンプルとも正しかったのに、重なった相手は TTS が付ける
0.8 秒の無音だった——*混ぜ方が正しいことは、声が被っていることの証拠にならない。*
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PYTHON = ROOT / ".venv" / "Scripts" / "python.exe"

BUILD = "task3/tools/build_audio.py"
TRANSCRIBE = "task3/transcribe.py"
MINUTES = "task3/minutes.py"
TO_DOC = "task3/to_doc.py"
VERIFY_SRC = "task3/verify_source.py"
#: **移植した関数は壊さない。** あれは check_port.py が移植元と文字単位で
#: 照合しているので、壊すと「移植と違う」ほうで落ちる——テストが守っている
#: 証拠にならない。ここで壊すのは、この課題で新しく書いた部分だけ。
DOCS = "common/docs_client.py"
#: **common も壊す。** 音声の口（generate_with_audio）をここに足したので、
#: 対象から外すと「新しく書いた分だけ検査されない」状態になる。
GEMINI = "common/gemini_client.py"

IGNORE = shutil.ignore_patterns(
    ".venv", ".git", "__pycache__", ".pytest_cache", ".pytest_tmp",
    "docs", "*.png", "*.wav", "_parts", "node_modules",
)

#: **範囲を広げ忘れると、壊したのにテストが1件も走らず「素通り」に見える。**
#: 判定が正しくても対象が空なら同じ緑になる（課題2 で踏んだ形）。
TEST_PATHS = (
    "task3/tests",
    "common/tests/test_gemini_client.py",
    "common/tests/test_docs_client.py",
    "common/tests/test_docs_client_read.py",
)

# (対象ファイル, 壊した内容, 置換前, 置換後)
MUTATIONS: list[tuple[str, str, str, str]] = [
    # ---------------------------------------------------------------- 読み取り
    (
        BUILD,
        "フェンスが無くても空を返す（無音の wav を作って成功する）",
        '    raise ValueError("{} のフェンスが見つからない".format(marker))',
        "    return []",
    ),
    (
        BUILD,
        "台本0行を通す（読んだと報告して無音を作る）",
        '        raise ValueError("台本が0行。フェンスはあるが中身が無い")',
        "        pass",
    ),
    (
        BUILD,
        "区切りを全部で割る（本文の縦棒より後ろが消える）",
        '        parts = s.split("|", 2)',
        '        parts = s.split("|")',
    ),
    (
        BUILD,
        "間隔が数でなくても 0 として通す（被りが静かに消える）",
        '            raise ValueError("{}行目: 間隔が数でない: {}".format(n, gap_s)) from None',
        "            gap = 0.0",
    ),
    (
        BUILD,
        "正解の JSON を先頭行だけ読む",
        '    return json.loads(" ".join(_fence(md, "json")))',
        '    return json.loads(_fence(md, "json")[0])',
    ),
    # -------------------------------------------------------------------- 照合
    (
        BUILD,
        "声名を照合しない（TTS の 44 回目で落ちる）",
        "        if speakers and l.voice not in speakers:",
        "        if False:",
    ),
    (
        BUILD,
        "正解にある語が台本に無くても通す（照合器が永久に一致と言う）",
        "            if word and not present(word):",
        "            if False:",
    ),
    (
        BUILD,
        "正解の数値が台本に無くても通す",
        "        if spoken and not present(spoken):",
        "        if False:",
    ),
    (
        BUILD,
        "不在の検査を逆向きにする（チャット限定の語が音声に入っても気づけない）",
        "        if word and present(word):",
        "        if word and not present(word):",
    ),
    # ------------------------------------------------------------------ 時間割
    (
        BUILD,
        "開始が負でも通す",
        "        if s < 0:",
        "        if False:",
    ),
    (
        BUILD,
        "順序が壊れる重なりを許す（台本と音声の順番が食い違ったまま成功）",
        "        if starts and s < starts[-1]:",
        "        if False:",
    ),
    (
        BUILD,
        "間隔を足さない（全部が詰まる）",
        "        s = end + g",
        "        s = end",
    ),
    # ---------------------------------------------------------------- 重ね合わせ
    (
        BUILD,
        "重ねずに上書きする（片方が消える＝拾えなかったのと同じ見た目）",
        "        if start >= written_to:",
        "        if True:",
    ),
    (
        BUILD,
        "正の側で飽和させない（折り返して大音量が負に化ける）",
        "                if v > INT16_MAX:\n                    v = INT16_MAX",
        "                if v > INT16_MAX:\n                    v = 0",
    ),
    (
        BUILD,
        "出力の長さを全パートの合計にする（隙間と重なりを無視）",
        "    total = max((s + len(p) for s, p in zip(starts, parts)), default=0)",
        "    total = sum(len(p) for p in parts)",
    ),
    # ------------------------------------------------------- 無音の切り落とし
    (
        BUILD,
        "余白を残さずに切る（子音の頭が消えて言葉が変わる）",
        "    margin = round(keep * rate)",
        "    margin = 0",
    ),
    (
        BUILD,
        "全区間が無音でも通す（台本の1行が音として丸ごと消える）",
        '        raise ValueError("全区間が無音（発話が入っていない）")',
        "        return samples",
    ),
    (
        BUILD,
        "末尾を切らない（被りが無音としか重ならない・2026-09-12 に実際に踏んだ形）",
        "    hi = n\n    while hi > lo and abs(samples[hi - 1]) < threshold:\n        hi -= 1",
        "    hi = n",
    ),
    (
        BUILD,
        "閾値を無視して常に切る（声まで削る）",
        "    while lo < n and abs(samples[lo]) < threshold:",
        "    while lo < n and abs(samples[lo]) < 99999:",
    ),
    # -------------------------------------------------------- 被りの実効値
    (
        BUILD,
        "実効値を絶対値の平均にする（大きい音の効きが変わる）",
        "    return (sum(v * v for v in samples) / len(samples)) ** 0.5",
        "    return sum(abs(v) for v in samples) / len(samples)",
    ),
    (
        BUILD,
        "空の入力で 0 を返さない（ゼロ除算）",
        "    if len(samples) == 0:",
        "    if False:",
    ),
    (
        BUILD,
        "正の間隔も被りとして報告する",
        "        if i == 0 or g >= 0:",
        "        if i == 0:",
    ),
    (
        BUILD,
        "前側の実効値を後側で埋める（片側無音を見逃す＝仕込めていない罠を仕込めたと報告）",
        '                "rms_prev": rms(parts[i - 1][off : off + span]),',
        '                "rms_prev": rms(parts[i][:span]),',
    ),
    (
        BUILD,
        "被りの長さを秒ではなくサンプル数で返す",
        '                "seconds": span / rate,',
        '                "seconds": span,',
    ),
    # ================================================================ 文字起こし
    #
    # ここから先が狙うのは「**API が成功を返したのに中身が欠けている**」失敗。
    # 例外は1つも出ない。だから壊しても、壊れたことが出力に現れない。
    (
        TRANSCRIBE,
        "読めなかった行を静かに捨てる（落とすほど結果がきれいに見える）",
        "            skipped.append(line)",
        "            pass",
    ),
    (
        TRANSCRIBE,
        "発言0件でも通す（空の議事録が「何も無かった会議」として通る）",
        "    if not utterances:",
        "    if False:",
    ),
    (
        TRANSCRIBE,
        "空行も読めなかった行に数える（毎回ノイズが出て本物の欠落が埋もれる）",
        "        if not line:",
        "        if False:",
    ),
    (
        TRANSCRIBE,
        "話者を最後のコロンまで取る（本文の前半が話者名に化ける）",
        r"(.+?)\s*[:：]",
        r"(.+)\s*[:：]",
    ),
    (
        TRANSCRIBE,
        "時刻の「時」を足さない（1時間超の会議で全部が巻き戻る）",
        "+ (int(hours) * 3600 if hours else 0)",
        "+ 0",
    ),
    (
        TRANSCRIBE,
        "音声の長さをフレーム数ではなくバイト数から出す",
        "        seconds=frames / rate,",
        "        seconds=path.stat().st_size / rate,",
    ),
    (
        TRANSCRIBE,
        "プロンプトから時刻の指定を落とす（5-D と 5-G が見えなくなる）",
        '        "[MM:SS] 話者A: 発言の内容\\n"',
        '        "話者A: 発言の内容\\n"',
    ),
    (
        TRANSCRIBE,
        "上限を超えても送る（拒否が課金や再試行と混ざる）",
        "    if audio.n_bytes > limit_bytes:",
        "    if False:",
    ),
    (
        TRANSCRIBE,
        "打ち切りを見ない（本文は返るので短い会議と見分けが付かない）",
        "    elif reply.finish_reason not in OK_FINISH_REASONS:",
        "    elif False:",
    ),
    (
        TRANSCRIBE,
        "打ち切りが読めなくても黙る（STOP だったと見分けが付かない）",
        "    if reply.finish_reason is None:",
        "    if False:",
    ),
    (
        TRANSCRIBE,
        "読めなかった行を報告しない",
        "    if skipped:",
        "    if False:",
    ),
    (
        TRANSCRIBE,
        "音声の長さを超える時刻を見ない（原文に無いものが議事録の根拠になる）",
        "    if over:",
        "    if False:",
    ),
    (
        TRANSCRIBE,
        "時刻の巻き戻りを見ない",
        "    if backwards:",
        "    if False:",
    ),
    (
        TRANSCRIBE,
        "末尾が音声の終わりに届いているかを見ない",
        "    if last < audio.seconds - TAIL_TOLERANCE_SEC:",
        "    if False:",
    ),
    (
        TRANSCRIBE,
        "末尾の許容を実質無限にする（検査は残っているのに一度も鳴らない）",
        "TAIL_TOLERANCE_SEC = 30.0",
        "TAIL_TOLERANCE_SEC = 100000.0",
    ),
    (
        TRANSCRIBE,
        "中身の薄さを見ない（無音のファイルでも文字起こしは成功する）",
        "    if density < MIN_CHARS_PER_SEC:",
        "    if False:",
    ),
    (
        TRANSCRIBE,
        "出力の言語を見ない（日本語の会議が英訳で返っても成功する）",
        "    if ratio < MIN_JAPANESE_RATIO:",
        "    if False:",
    ),
    (
        TRANSCRIBE,
        "日本語の割合を常に 1 にする（検査は残るが必ず通る）",
        "    return sum(1 for c in letters if _JAPANESE.match(c)) / len(letters)",
        "    return 1.0",
    ),
    (
        TRANSCRIBE,
        "話者の数を見ない（区別に失敗しても、片方が落ちても通る）",
        "    if len(valid) < MIN_SPEAKERS:",
        "    if False:",
    ),
    (
        TRANSCRIBE,
        "書式を外れた話者名を見ない（2026-09-12 に実機で踏んだ形）",
        "    if odd:",
        "    if False:",
    ),
    (
        TRANSCRIBE,
        "書式を外れた話者名も人数に数える（水増しで下限を満たす）",
        "    valid = speakers - set(odd)",
        "    valid = speakers",
    ),
    # ================================================================== 議事録
    #
    # 文字起こしの層は「欠ける」失敗を見た。この層は逆に「**足す**」失敗を見る。
    # 足された文はいちばん自然に読めるので、読んでも気づけない。
    (
        MINUTES,
        "正規化で空白を落とさない（分かち書きの引用が全部「原文に無い」になる）",
        '    return _SPACE.sub("", unicodedata.normalize("NFKC", text or ""))',
        '    return unicodedata.normalize("NFKC", text or "")',
    ),
    (
        MINUTES,
        "全角と半角をそろえない（10月3日 と １０月３日 が別物になる）",
        '    return _SPACE.sub("", unicodedata.normalize("NFKC", text or ""))',
        '    return _SPACE.sub("", text or "")',
    ),
    (
        MINUTES,
        "読めない時刻を 0 として通す（全部が会議の冒頭を指す）",
        '        raise ValueError("時刻として読めません: {!r}".format(value))',
        "        return 0",
    ),
    (
        MINUTES,
        "時刻の「時」を足さない",
        "+ (int(hours) * 3600 if hours else 0)",
        "+ 0",
    ),
    (
        MINUTES,
        "空の引用を一致とみなす（引用を空にすれば照合が必ず通る）",
        "    if not q:",
        "    if False:",
    ),
    (
        MINUTES,
        "見つからなくても先頭の位置を返す（無い引用に時刻が付く）",
        "    if pos < 0:\n        return None",
        "    if pos < 0:\n        pos = 0",
    ),
    (
        MINUTES,
        "引用の出どころを全部先頭の発言にする",
        "        owner.extend([i] * len(t))",
        "        owner.extend([0] * len(t))",
    ),
    (
        MINUTES,
        "打ち切りを見ない（JSON は読めても途中で切れている）",
        "    elif reply.finish_reason not in OK_FINISH_REASONS:",
        "    elif False:",
    ),
    (
        MINUTES,
        "打ち切りが読めなくても黙る",
        "    if reply.finish_reason is None:",
        "    if False:",
    ),
    (
        MINUTES,
        "1件も無いのを見ない（決まらなかったのか拾えなかったのか分からなくなる）",
        "    if not items:\n        problems.append(",
        "    if False:\n        problems.append(",
    ),
    (
        MINUTES,
        "読めない時刻を報告しない",
        "        if item.at == UNKNOWN_AT:",
        "        if False:",
    ),
    (
        MINUTES,
        "音声の長さを超える根拠を見ない",
        "        elif item.at > duration:",
        "        elif False:",
    ),
    (
        MINUTES,
        "空の引用を見ない",
        "        if not normalize(item.quote):",
        "        if False:",
    ),
    (
        MINUTES,
        "**原文に無い引用を見ない**（この層の本命・5-K）",
        "        if found is None:",
        "        if False:",
    ),
    (
        MINUTES,
        "引用の場所と時刻の食い違いを見ない（5-P）",
        "        elif item.at != UNKNOWN_AT and abs(found - item.at) > QUOTE_TOLERANCE_SEC:",
        "        elif False:",
    ),
    (
        MINUTES,
        "食い違いの許容を実質無限にする（検査は残るが一度も鳴らない）",
        "QUOTE_TOLERANCE_SEC = 20.0",
        "QUOTE_TOLERANCE_SEC = 100000.0",
    ),
    (
        MINUTES,
        "終盤まで届いているかを見ない（後半を丸めても成功する・5-F）",
        "    if cited and max(cited) < duration - TAIL_TOLERANCE_SEC:",
        "    if False:",
    ),
    (
        MINUTES,
        "終盤の許容を実質無限にする",
        "TAIL_TOLERANCE_SEC = 90.0",
        "TAIL_TOLERANCE_SEC = 100000.0",
    ),
    (
        MINUTES,
        "チャットログが空でも節を渡す（空の見出しは埋められる）",
        "    if chat_log.strip():",
        "    if True:",
    ),
    (
        MINUTES,
        "議事録に「含んでいないもの」を書かない（読む人は無いものを無かったと読む）",
        '    lines += ["  - " + s for s in NOT_INCLUDED]',
        "    lines += []",
    ),
    (
        MINUTES,
        "議事録に生成元を書かない（二重処理と打ち切りが本文から見えなくなる）",
        '    lines += ["  - " + s for s in provenance]',
        "    lines += []",
    ),
    (
        MINUTES,
        "疑う理由を本文に書かない（共有されるのは議事録だけ）",
        '        lines += ["  - " + p for p in m.problems]',
        "        lines += []",
    ),
    (
        MINUTES,
        "チャット由来の根拠を文字起こしで探す（2026-09-12 に実機で誤検知した形）",
        "        if item.source == CHAT:",
        "        if False:",
    ),
    (
        MINUTES,
        "チャットログを渡していなくても、チャットを根拠にできる",
        "            if not chat_body:",
        "            if False:",
    ),
    (
        MINUTES,
        "チャットに無い引用でも通す（チャットを根拠にすれば何でも書ける）",
        "            elif normalize(item.quote) not in chat_body:",
        "            elif False:",
    ),
    (
        MINUTES,
        "終盤の検査にチャットの時刻を混ぜる（後半を落としても届いたことになる）",
        "    cited = [i.at for i in items if i.source == AUDIO and i.at != UNKNOWN_AT]",
        "    cited = [i.at for i in items if i.at != UNKNOWN_AT]",
    ),
    (
        MINUTES,
        "知らない出どころをそのまま通す（勝手な値で検査を迂回できる）",
        "        source=source if source in (AUDIO, CHAT) else AUDIO,",
        "        source=source,",
    ),
    (
        MINUTES,
        "議事録にチャット由来と書かない（録音から出たように読める）",
        '            "チャット {}".format(item.at_raw) if item.source == CHAT',
        '            "チャット {}".format(item.at_raw) if False',
    ),
    (
        MINUTES,
        "根拠の時刻と引用を本文に併記しない",
        '        out.append("     根拠 [{}]「{}」".format(where, item.quote))',
        '        out.append("     根拠")',
    ),
    # ====================================================== ソース側との照合
    #
    # ここが落ちても議事録は出る。**出たものが正しいかを、誰も見なくなるだけ。**
    # 課題2 の講評（DESIGN 11章）が要求している層なので、素通りは許さない。
    (
        VERIFY_SRC,
        "句読点を落とさない（差分が句読点で埋まって本物が隠れる）",
        '    return _PUNCT.sub("", minutes.normalize(text))',
        "    return minutes.normalize(text)",
    ),
    (
        VERIFY_SRC,
        "空の引用で 0 を返さない（ゼロ除算）",
        "    if not quote:",
        "    if False:",
    ),
    (
        VERIFY_SRC,
        "近さを ratio で測る（長い行に短い引用が入っていても低く出る）",
        "    matched = sum(block.size for block in matcher.get_matching_blocks())\n"
        "    return matched / len(quote)",
        "    return matcher.ratio()",
    ),
    (
        VERIFY_SRC,
        "隣り合う2行を候補にしない（行をまたぐ引用が「見つからない」になる）",
        "        if i + 1 < len(norms):",
        "        if False:",
    ),
    (
        VERIFY_SRC,
        "候補の行まるごとと比べる（対応しない前半が大量に「欠落」として出る）",
        "            best = (score, matched_span(cand, q))",
        "            best = (score, cand)",
    ),
    (
        VERIFY_SRC,
        "読みから表記へそろえない（正しい引用まで「台本と違う」と言う）",
        "        cand = apply_readings(cand, truth) if truth else cand",
        "        cand = cand",
    ),
    (
        VERIFY_SRC,
        "読みの置き換えを短い順にする（長い読みが割れて別物になる）",
        "    for spoken, value in sorted(pairs, key=lambda kv: -len(kv[0])):",
        "    for spoken, value in sorted(pairs, key=lambda kv: len(kv[0])):",
    ),
    (
        VERIFY_SRC,
        "差分に文脈を付けない（どの語が変わったか読めない）",
        "DIFF_CONTEXT = 2",
        "DIFF_CONTEXT = 0",
    ),
    (
        VERIFY_SRC,
        "置き換えと増加を差分に出さない（欠落だけ見る）",
        '        if tag == "equal":',
        '        if tag != "delete":',
    ),
    (
        VERIFY_SRC,
        "「見つからない」の閾値を 0 にする（別のことを言っていても通る）",
        "FOUND_THRESHOLD = 0.6",
        "FOUND_THRESHOLD = 0.0",
    ),
    (
        VERIFY_SRC,
        "「一致」の閾値を下げる（**誤変換を一致として通す**）",
        "MATCH_THRESHOLD = 0.999",
        "MATCH_THRESHOLD = 0.5",
    ),
    (
        VERIFY_SRC,
        "空の引用を通す（引用を空にすれば照合が必ず通る）",
        "        if not minutes.normalize(it.quote):",
        "        if False:",
    ),
    (
        VERIFY_SRC,
        "チャット由来の根拠も台本で探す（必ず「無い」になる）",
        "            it.quote, chat_lines if it.source == CHAT else lines,",
        "            it.quote, lines,",
    ),
    (
        VERIFY_SRC,
        "照合した件数を数えない（N件中N件が出せなくなる）",
        "        checked += 1",
        "        pass",
    ),
    (
        VERIFY_SRC,
        "一致した件数を数えない",
        "            matched += 1",
        "            pass",
    ),
    (
        VERIFY_SRC,
        "決定に必要な語を見ない",
        "            if normalize_for_match(token) not in decision_text:",
        "            if False:",
    ),
    (
        VERIFY_SRC,
        "**撤回された値が決定に残っても通す**（5-K）",
        "            if normalize_for_match(token) in decision_text:",
        "            if False:",
    ),
    (
        VERIFY_SRC,
        "誤った固有名詞が決定にあっても通す（大和商事／千二百個）",
        "        if wrong and normalize_for_match(wrong) in decision_text:",
        "        if False:",
    ),
    (
        VERIFY_SRC,
        "論点の決定への格上げを見ない（T6）",
        "        if score > LINK_THRESHOLD and hit is not None:",
        "        if False:",
    ),
    (
        VERIFY_SRC,
        "**担当の食い違いを見ない**（5-J が表に出なくなる）",
        '        if got_owner and got_owner != clean_owner(want.get("owner", "")):',
        "        if False:",
    ),
    (
        VERIFY_SRC,
        "敬称を落とさない（小林 と 小林さん を別人にする）",
        '    return _HONORIFIC.sub("", minutes.normalize(name))',
        "    return minutes.normalize(name)",
    ),
    (
        VERIFY_SRC,
        "チャットを使っていなくても、音声に無い語を許す（創作が通る）",
        "    if not chat_used:",
        "    if False:",
    ),
    # ========================================================== ドキュメント
    #
    # 移植した関数は壊さない（check_port.py の担当）。ここは新しく書いた分だけ。
    (
        DOCS,
        "空の本文でも挿入する（空のドキュメントだけがドライブに残る）",
        "    if not normalized:",
        "    if False:",
    ),
    (
        DOCS,
        "検査を通さずに挿入する（ensure_insertable を素通り）",
        "    body = ensure_insertable(text)",
        "    body = text",
    ),
    (
        DOCS,
        "**返った replies の数を見ない**（部分的に成功しても「書けた」と流す・5-L）",
        "    if got != sent:",
        "    if False:",
    ),
    (
        DOCS,
        "replies の数を送った数で埋める（照合が必ず通る）",
        '    got = len(response.get("replies") or [])',
        "    got = sent",
    ),
    (
        TO_DOC,
        "ハッシュを短くしすぎる（別の議事録が同じ鍵になる）",
        "HASH_LENGTH = 16",
        "HASH_LENGTH = 4",
    ),
    (
        TO_DOC,
        "内容によらず同じハッシュを返す（何を書いても「もうある」になる）",
        '    return hashlib.sha256(body.encode("utf-8")).hexdigest()[:HASH_LENGTH]',
        '    return "x" * HASH_LENGTH',
    ),
    (
        TO_DOC,
        "会議名が空でもタイトルを作る（無題のドキュメントを量産する）",
        "    if not name:",
        "    if False:",
    ),
    (
        TO_DOC,
        "台帳が無いときにファイルを読みに行く",
        "    if not p.exists():",
        "    if False:",
    ),
    (
        TO_DOC,
        "台帳が辞書でなくてもそのまま返す",
        "    return data if isinstance(data, dict) else {}",
        "    return data",
    ),
    (
        TO_DOC,
        "**重複を見ない**（同じ会議の議事録が2本できる・5-H）",
        "    if not force and digest in ledger:",
        "    if False:",
    ),
    (
        TO_DOC,
        "--force を無視する（作り直せなくなる）",
        "    if not force and digest in ledger:",
        "    if digest in ledger:",
    ),
    (
        TO_DOC,
        "空の検査を外す（空のドキュメントが残る）",
        "    text = docs_client.ensure_insertable(body)  # 空なら API を呼ぶ前に落ちる",
        "    text = body",
    ),
    (
        TO_DOC,
        "台帳に残さない（次回に重複を検出できない）",
        "    save_ledger(ledger_path, ledger)\n    return created",
        "    return created",
    ),
    (
        TO_DOC,
        "**書き出す前に台帳へ残す**（どこにも無い議事録を「ある」と信じ続ける）",
        "    created = docs_client.create_document_with_text(service, title, text)",
        '    ledger[digest] = {"documentId": "?", "title": title, "url": ""}\n'
        "    save_ledger(ledger_path, ledger)\n"
        "    created = docs_client.create_document_with_text(service, title, text)",
    ),
    # ============================================================ 音声と型の口
    (
        GEMINI,
        "型を設定に載せない（自由文が返り、空と拾い損ねが同じ見た目になる）",
        '        options["response_schema"] = json_schema',
        "        pass",
    ),
    (
        GEMINI,
        "JSON の mime を立てない",
        '        options["response_mime_type"] = "application/json"',
        "        pass",
    ),
    (
        GEMINI,
        "型を渡していなくても JSON にする（既存の要約の呼び手を壊す）",
        "    if json_schema is not None:",
        "    if True:",
    ),
    (
        GEMINI,
        "音声を載せずに送る（プロンプトだけで「文字起こし」が返る）",
        "            model=model, contents=[prompt, part], config=build_config()",
        "            model=model, contents=[prompt], config=build_config()",
    ),
    (
        GEMINI,
        "空の音声でも呼ぶ（材料の無いところから作った文章が返る）",
        "    if not audio_bytes:",
        "    if False:",
    ),
    (
        GEMINI,
        "20MB を超えても送る",
        "    if len(audio_bytes) > limit_bytes:",
        "    if False:",
    ),
    (
        GEMINI,
        "空の答えを「できた」にする",
        "    if not text:",
        "    if False:",
    ),
    (
        GEMINI,
        "打ち切りの理由を常に STOP と報告する",
        '    return getattr(reason, "name", None) or str(reason)',
        '    return "STOP"',
    ),
    (
        GEMINI,
        "使用量を読まない（課金された側の値が残らない）",
        "    return int(value) if isinstance(value, int) else None",
        "    return None",
    ),
]


def run_tests(work: Path) -> bool:
    """写した側でテストを回す。1件でも落ちたら True。"""
    proc = subprocess.run(
        # --basetemp を写した側の中に置く。既定の %TEMP% を使うと後片付けで
        # PermissionError が出て、**壊す前から落ちている**ように見える。
        [str(PYTHON), "-m", "pytest", *TEST_PATHS, "-x", "-q", "--no-header",
         "-p", "no:cacheprovider", "--basetemp", str(work / ".pytest_tmp")],
        cwd=work,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return proc.returncode != 0


def main() -> int:
    if not PYTHON.exists():
        print(f"仮想環境の Python が見つかりません: {PYTHON}", file=sys.stderr)
        return 1

    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp) / "repo"
        shutil.copytree(ROOT, work, ignore=IGNORE)

        if run_tests(work):
            print("壊す前からテストが落ちています。先にそちらを直してください。", file=sys.stderr)
            return 1

        killed: list[str] = []
        survived: list[str] = []
        not_found: list[str] = []

        for index, (target, label, before, after) in enumerate(MUTATIONS, start=1):
            path = work / target
            original = path.read_text(encoding="utf-8", newline="")

            # **照合も書き込みも LF に正規化した文字列で行う。**
            # core.autocrlf で .py が CRLF になりうる。newline="" のまま複数行の
            # パターンを探すと一度もマッチせず、素通りと区別が付かない。
            haystack = original.replace("\r\n", "\n")

            if haystack.count(before) != 1:
                not_found.append(
                    f"{index:3}. {label}（{target}・{haystack.count(before)}件一致）"
                )
                continue

            path.write_text(haystack.replace(before, after, 1), encoding="utf-8", newline="\n")
            if run_tests(work):
                killed.append(f"{index:3}. {label}")
            else:
                survived.append(f"{index:3}. {label}（{target}）")
            path.write_text(original, encoding="utf-8", newline="")

        print(f"壊した箇所: {len(MUTATIONS)}")
        print(f"  kill（テストが落ちた）: {len(killed)}")
        print(f"  素通り: {len(survived)}")
        print(f"  置換先なし: {len(not_found)}")

        if survived:
            print("\n素通りしたもの（テストが守っていない）:")
            for line in survived:
                print(f"  {line}")
        if not_found:
            print("\n置換先が見つからなかったもの（壊しかたが古い）:")
            for line in not_found:
                print(f"  {line}")

    # **置換先なしを成功にしない。** 何も壊さずに全部通ると「穴ゼロ」に見える。
    return 0 if not survived and not not_found else 1


if __name__ == "__main__":
    raise SystemExit(main())
