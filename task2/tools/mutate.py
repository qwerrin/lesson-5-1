#!/usr/bin/env python3
"""課題2の実装を1か所ずつ壊して、テストが落ちることを確かめる。

**テストが通っていることは、守られていることの証拠にならない。**

使い方::

    .venv\\Scripts\\python.exe task2\\tools\\mutate.py

仕組みは課題1の ``task1/tools/mutate.py`` と同じ。リポジトリを一時ディレクトリへ
写し、**写した側だけ**を壊す。成果物には触らないので、途中で強制終了しても
壊れたまま残らない。**置換先が見つからない（NOT FOUND）は素通りと同じ扱い**にする
——実装を直して壊しかたを直し忘れると、何も壊さずに全部通って「穴ゼロ」と出る。

この課題で狙う失敗の形
------------------------------------------------------------------

守りたいのは「**例外にならず、静かに間違った値が残る**」失敗である。
シートは履歴なので、**間違った行は後から見ても間違いだと分からない**。

============================== ================================================
壊すと何が起きるか              なぜ静かなのか
============================== ================================================
改行を空白に置き換えない        API は正常終了する。セルが崩れて初めて分かる
実質価格の式を入れ替える        値は出る。**過去の行と比較できなくなるだけ**
価格が無いとき 0 を入れる        0 円は「値下がりした」に見える
失敗行の状態を「取得」にする    落ちた回が成功として履歴に残る
失敗行の列数を1つ減らす         追記でシートの列が丸ごとズレる
フラグを解釈して入れる          逆に解釈していたら履歴が全部嘘になる
時刻からオフセットを落とす      読む側の解釈次第で物差しが2本になる
0件を失敗として拾わない         **HTTP 200 で返る**。その日の行がただ無くなる
一致しない商品を採用する        別商品の価格が同じ行に積まれる。例外は出ない
失敗を結果から落とす            「書かなかった日」と区別が付かなくなる
タイムアウトを渡さない          欠落ではなく**終わらない**。翌日の実行と重なる
エラー本文を理由に流す          URL に鍵が載る。**シートとスクショの両方に残る**
失敗行を前回として比べる        空の価格が数値に落ち、値下がりが毎日飛ぶ
比較できないのを 0 とする       比べていないのに「変化なし」として履歴に残る
いちばん下を最新とみなす        並べ替えた日から静かに間違える
在庫の変化を見ない              価格は同じ。**買えないことだけが記録に残らない**
============================== ================================================
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PYTHON = ROOT / ".venv" / "Scripts" / "python.exe"

TRANSFORM = "task2/transform.py"
FETCH = "task2/fetch_items.py"
DIFF = "task2/diff.py"
TO_SHEET = "task2/to_sheet.py"
#: **照合器も壊す。** 課題1では、ここが対象に入っていなかったせいで
#: 「素通り0」が「守られている」ではなく「そこを見ていない」を意味していた。
VERIFY = "task2/verify_sheet.py"
SHEETS = "common/sheets_client.py"

IGNORE = shutil.ignore_patterns(
    ".venv", ".git", "__pycache__", ".pytest_cache", "docs", "*.png", "node_modules"
)

#: **common/sheets_client.py も壊す**ので、その検査も回す。
#: 範囲を広げ忘れると、壊したのにテストが1件も走らず「素通り」に見える
#: ——判定が正しくても対象が空なら同じ緑になる。
TEST_PATHS = ("task2/tests", "common/tests/test_sheets_client.py")

# (対象ファイル, 壊した内容, 置換前, 置換後)
MUTATIONS: list[tuple[str, str, str, str]] = [
    (
        TRANSFORM,
        "改行・タブを空白に置き換えない（セルが壊れる）",
        '    for char in _BREAKING:\n        text = text.replace(char, " ")',
        "    pass",
    ),
    (
        TRANSFORM,
        "改行を空白ではなく削除する（語が繋がって別語になる）",
        '        text = text.replace(char, " ")',
        '        text = text.replace(char, "")',
    ),
    (
        TRANSFORM,
        "前後の空白を削らない",
        "    return text.strip()",
        "    return text",
    ),
    (
        TRANSFORM,
        "実質価格の式を「価格のN%」に入れ替える（100円未満の端数で割れる）",
        "    return item_price - (item_price // 100) * point_rate",
        "    return item_price - item_price * point_rate // 100",
    ),
    (
        TRANSFORM,
        "ポイントを引かない（実質価格が本体価格と同じになる）",
        "    return item_price - (item_price // 100) * point_rate",
        "    return item_price",
    ),
    (
        TRANSFORM,
        "価格が読めないときに 0 を入れる（値下がりに見える）",
        '        price = ""\n        rate_value = ""\n        effective = ""',
        "        price = 0\n        rate_value = 0\n        effective = 0",
    ),
    (
        TRANSFORM,
        "ポイント倍率が無いときに 1 とみなす（勝手に割り引く）",
        '    rate = item.get("pointRate", 0)',
        '    rate = item.get("pointRate", 1)',
    ),
    (
        TRANSFORM,
        "失敗行の状態を「取得」にする（落ちた回が成功として残る）",
        '    row[COLUMNS.index("状態")] = STATUS_FAILED',
        '    row[COLUMNS.index("状態")] = STATUS_OK',
    ),
    (
        TRANSFORM,
        "失敗行の理由の既定を空にする（区別できないことを書かない）",
        '    row[COLUMNS.index("理由")] = normalize_text(reason) or REASON_UNKNOWN',
        '    row[COLUMNS.index("理由")] = normalize_text(reason)',
    ),
    (
        TRANSFORM,
        "失敗行に itemCode を残さない（どれが落ちたか分からなくなる）",
        '    row[COLUMNS.index("itemCode")] = normalize_text(item_code)',
        "    pass",
    ),
    (
        TRANSFORM,
        "失敗行の列数を1つ減らす（追記でシートの列がズレる）",
        '    row: list[Any] = [""] * len(COLUMNS)',
        '    row: list[Any] = [""] * (len(COLUMNS) - 1)',
    ),
    (
        TRANSFORM,
        "送料フラグを解釈して入れる（逆だったら履歴が全部嘘になる）",
        '        item.get("postageFlag", ""),',
        '        "送料込み" if item.get("postageFlag") == 0 else "送料別",',
    ),
    (
        TRANSFORM,
        "時刻からオフセットを落とす（物差しが2本になる）",
        '    return datetime.now().astimezone().isoformat(timespec="seconds")',
        '    return datetime.now().isoformat(timespec="seconds")',
    ),
    (
        TRANSFORM,
        "本体価格を生で残さず実質価格だけ書く（式を変えたら過去と比較できない）",
        "        effective: Any = effective_price(price, rate_value)",
        "        effective = effective_price(price, rate_value)\n        price = effective",
    ),

    # ------------------------------------------------------------------ fetch_items
    #
    # ここは**取りに行く層**。壊れかたが transform と違う：値が変になるのではなく、
    # **商品が1件まるごと履歴から消える**。しかも API は 200 を返す。
    (
        FETCH,
        "0件（出品終了・売り切れ）を「別商品」と混同する",
        "        return None, REASON_NOT_FOUND, False",
        "        return None, REASON_MISMATCH, False",
    ),
    (
        FETCH,
        "要求した itemCode と一致するか見ない（別商品の価格を積む）",
        '        if body.get("itemCode") == item_code:',
        "        if True:",
    ),
    (
        FETCH,
        "ラッパ（Items[0]['Item']）を外さない（全列が空になる）",
        "        body = inner if isinstance(inner, dict) else entry",
        "        body = entry",
    ),
    (
        FETCH,
        "平坦な形を拾わない（外側が変わった日に全商品が消える）",
        "        body = inner if isinstance(inner, dict) else entry",
        "        body = inner if isinstance(inner, dict) else {}",
    ),
    (
        FETCH,
        "タイムアウトを渡さない（無限に待つ・定期実行だと積み上がる）",
        "            response = http.get(ENDPOINT, params=params, timeout=timeout)",
        "            response = http.get(ENDPOINT, params=params)",
    ),
    (
        FETCH,
        "accessKey を載せない（2026年の刷新で必須になった）",
        '        "accessKey": access_key,',
        "",
    ),
    (
        FETCH,
        "itemCode ではなく keyword で引く（日によって別商品が混ざる）",
        '        "itemCode": item_code,',
        '        "keyword": item_code,',
    ),
    (
        FETCH,
        "429 をやり直さない（混雑した回の商品が丸ごと落ちる）",
        "        transient = status == 429 or (isinstance(status, int) and status >= 500)",
        "        transient = False",
    ),
    (
        FETCH,
        "パラメータ不正までやり直す（打ち間違いで全商品が遅れる）",
        "        transient = status == 429 or (isinstance(status, int) and status >= 500)",
        "        transient = isinstance(status, int) and status >= 400",
    ),
    (
        FETCH,
        "未文書の状態を「パラメータ不正」に丸める（生値が残らない）",
        "        reason = DOCUMENTED_STATUS_REASONS.get(status) or unknown_reason(status, error)",
        "        reason = DOCUMENTED_STATUS_REASONS.get(status, REASON_BAD_PARAMETER)",
    ),
    (
        FETCH,
        "退避を倍々にしない（混雑時に同じ間隔で叩き続ける）",
        "    seconds = retry_after if retry_after is not None else backoff_base * (2 ** (attempt - 1))",
        "    seconds = retry_after if retry_after is not None else backoff_base",
    ),
    (
        FETCH,
        "Retry-After を無視する（相手の指定より早く投げ直す）",
        "    seconds = retry_after if retry_after is not None else backoff_base * (2 ** (attempt - 1))",
        "    seconds = backoff_base * (2 ** (attempt - 1))",
    ),
    (
        FETCH,
        "読めない Retry-After を 0 秒として扱う（429 を悪化させる）",
        "    except (TypeError, ValueError):\n        return None",
        "    except (TypeError, ValueError):\n        return 0.0",
    ),
    (
        FETCH,
        "退避の上限を外す（Retry-After 次第で何時間でも止まる）",
        "    return min(seconds, MAX_BACKOFF_SECONDS)",
        "    return seconds",
    ),
    (
        FETCH,
        "試行の上限で諦めない（1商品で1日ぶんの QPS を使い切る）",
        "        if not transient or attempt == max_attempts:",
        "        if not transient:",
    ),
    (
        FETCH,
        "失敗した商品を結果から落とす（書かなかった日と区別が付かない）",
        "    return FetchReport(results=results, requested=len(codes))",
        "    return FetchReport(results=[r for r in results if r.ok], requested=len(codes))",
    ),
    (
        FETCH,
        "商品ごとに時計を作り直す（2件目を間髪入れずに投げる）",
        "            pacer=pacer,",
        "            pacer=None,",
    ),
    (
        FETCH,
        "時計を見ずに毎回眠る（退避の直後にも待つ）",
        "            remaining = self._min_interval - (now - self._last)",
        "            remaining = self._min_interval",
    ),
    (
        FETCH,
        "watchlist の順序を入れ替える（突き合わせが目視でできなくなる）",
        "        for code in codes",
        "        for code in reversed(codes)",
    ),
    (
        FETCH,
        "error_description を理由に流す（URL に鍵が載る）",
        '        error = payload.get("error") if isinstance(payload, dict) else None',
        '        error = payload.get("error_description") if isinstance(payload, dict) else None',
    ),
    (
        FETCH,
        "理由の長さを切らない（行が伸びて読めなくなる）",
        "    return text[:_ERROR_TOKEN_LIMIT]",
        "    return text",
    ),
    (
        FETCH,
        "理由の改行を潰さない（セルが壊れる）",
        '    text = " ".join(str(value).split())',
        "    text = str(value)",
    ),
    (
        FETCH,
        "通信以外の例外も握りつぶす（自分のバグが「通信失敗」に化ける）",
        "        except requests.exceptions.RequestException:",
        "        except Exception:",
    ),
    (
        FETCH,
        "本文が読めないときに例外を出す（1件の失敗が全件の欠落になる）",
        "    except ValueError:\n        return None",
        "    except ValueError:\n        raise",
    ),

    # ------------------------------------------------------------------ diff
    #
    # ここは**比べる層**。壊れると「値下がりが飛ばない」か「毎日飛ぶ」かのどちらかで、
    # **前者は無音**。通知が来ないことは、変化が無かったことの証拠にならない。
    (
        DIFF,
        "失敗行を前回に採る（空の価格を数値に落とす）",
        "        if _cell(row, _STATUS) != transform.STATUS_OK:",
        "        if False:",
    ),
    (
        DIFF,
        "同時刻の行を前回に採る（追記後に読み直すと自分自身と比べる）",
        "        if before is not None and at >= before:",
        "        if before is not None and at > before:",
    ),
    (
        DIFF,
        "いちばん下の行を最新とみなす（並べ替えた日から静かに間違える）",
        "        if best_at is None or at > best_at:",
        "        if True:",
    ),
    (
        DIFF,
        "読めない時刻の行を黙って飛ばす（0件になった理由が言えない）",
        "            skipped += 1",
        "            pass",
    ),
    (
        DIFF,
        "オフセット無しの時刻を受け入れる（物差しが2本になる）",
        "    return parsed if parsed.tzinfo is not None else None",
        "    return parsed",
    ),
    (
        DIFF,
        "比較できないときに差を 0 とする（比べていないのに「変化なし」）",
        "            delta=None,",
        "            delta=0,",
    ),
    (
        DIFF,
        "「初回」と「前回が失敗」を区別しない（取れていないことが隠れる）",
        "        note = (\n            NOTE_PREVIOUS_FAILED\n            if _has_earlier_row(history, item_code, current_at)\n            else NOTE_FIRST\n        )",
        "        note = NOTE_FIRST",
    ),
    (
        DIFF,
        "今回が失敗行でも比較する",
        "    if _cell(current, _STATUS) != transform.STATUS_OK:",
        "    if False:",
    ),
    (
        DIFF,
        "空の価格を 0 として扱う（0円は「値下がり」に見える）",
        "    return value if isinstance(value, int) and not isinstance(value, bool) else None",
        "    return value if isinstance(value, int) and not isinstance(value, bool) else 0",
    ),
    (
        DIFF,
        "列が足りない行で例外を出す（1行の崩れで全商品の判定が止まる）",
        "    return row[index] if len(row) > index else \"\"",
        "    return row[index]",
    ),
    (
        DIFF,
        "片方が空でも商品名が変わったと数える（古い行のせいで毎回鳴る）",
        "        name_changed=bool(previous_name and current_name and previous_name != current_name),",
        "        name_changed=bool(previous_name != current_name),",
    ),
    (
        DIFF,
        "在庫の変化を見ない（価格が同じなら「変化なし」で終わる）",
        "        stock_changed=(\n            previous_stock != \"\" and current_stock != \"\" and previous_stock != current_stock\n        ),",
        "        stock_changed=False,",
    ),
    (
        DIFF,
        "値上がりも通知に混ぜる",
        "    return [c for c in comparisons if c.dropped and -(c.delta or 0) >= threshold]",
        "    return [c for c in comparisons if c.comparable and abs(c.delta or 0) >= threshold]",
    ),
    (
        DIFF,
        "閾値を無視する（1円の変動で毎日鳴る）",
        "    return [c for c in comparisons if c.dropped and -(c.delta or 0) >= threshold]",
        "    return [c for c in comparisons if c.dropped]",
    ),
    (
        DIFF,
        "比較できなかった商品を結果から落とす（突き合わせができなくなる）",
        "    return [compare_row(row, history) for row in current_rows]",
        "    return [c for c in (compare_row(row, history) for row in current_rows) if c.comparable]",
    ),
    # ------------------------------------------------------------------ 書き込み層
    #
    # **公式ドキュメントを読まないと出てこない穴。** どれも書いた日は成功し、
    # 壊れるのは読み戻す日・隣に何か置いた日・人が列をいじった日である。
    (
        SHEETS,
        "書いた値を解釈させる（商品名が数式に、時刻が日付に化ける）",
        'VALUE_INPUT_OPTION = "RAW"',
        'VALUE_INPUT_OPTION = "USER_ENTERED"',
    ),
    (
        SHEETS,
        "既定の上書きで追記する（隣に置いたデータを潰す）",
        'INSERT_DATA_OPTION = "INSERT_ROWS"',
        'INSERT_DATA_OPTION = "OVERWRITE"',
    ),
    (
        SHEETS,
        "読み戻しで型を変える（価格が文字列になり、全比較が「価格が無い」になる）",
        'VALUE_RENDER_OPTION = "UNFORMATTED_VALUE"',
        'VALUE_RENDER_OPTION = "FORMATTED_VALUE"',
    ),
    (
        SHEETS,
        "見出しが違っても書く（列の意味が全部ズレる）",
        '    if current == wanted:',
        '    if True:',
    ),
    (
        SHEETS,
        "書かない指定でも見出しを書く",
        "        if not create:",
        "        if False:",
    ),
    (
        SHEETS,
        "末尾に落ちた空セルを補わない（15列で書いた行が14列で戻る）",
        '    return [list(row) + [""] * max(0, width - len(row)) for row in rows]',
        "    return rows",
    ),
    (
        SHEETS,
        "長い行を幅で切る（人が足した列を黙って隠す）",
        '    return [list(row) + [""] * max(0, width - len(row)) for row in rows]',
        '    return [(list(row) + [""] * max(0, width - len(row)))[:width] for row in rows]',
    ),
    (
        SHEETS,
        "空の応答で None を返す（呼ぶ側が落ちる）",
        "    rows = values if isinstance(values, list) else []",
        "    rows = values",
    ),
    (
        SHEETS,
        "書けた行数を確かめず送った行数を返す（部分書き込みが見えない）",
        "    return AppendResult(sent=len(rows), updated=updated if isinstance(updated, int) else 0)",
        "    return AppendResult(sent=len(rows), updated=len(rows))",
    ),
    (
        SHEETS,
        "長さのそろわない行をそのまま送る（シートは受け取ってしまう）",
        "    if len(widths) != 1:",
        "    if False:",
    ),
    (
        SHEETS,
        "見出しの範囲を固定文字列にする（列を足した日に古い範囲を読む）",
        '    header_range = f"{sheet_name}!A1:{column_letter(len(wanted))}1"',
        '    header_range = f"{sheet_name}!A1:D1"',
    ),
    (
        TO_SHEET,
        "watchlist の重複を落とさない（その商品だけ2行入る）",
        "        (duplicates if code in codes else codes).append(code)",
        "        codes.append(code)",
    ),
    (
        TO_SHEET,
        "落とした重複を黙って捨てる（入れたのに行が無い、と混同される）",
        "        duplicates=list(duplicates),",
        "        duplicates=[],",
    ),
    (
        TO_SHEET,
        "空の watchlist で正常終了する（0件を「動いた」と読ませる）",
        "    if not codes:",
        "    if False:",
    ),
    (
        TO_SHEET,
        "取得に失敗した商品を行にしない（5-B が最後の層で消える）",
        "        if result.ok",
        "        if True",
    ),
    (
        TO_SHEET,
        "見出しを確かめる前に取りに行く（書けないのに QPS を使う）",
        "    header_state = sheets_client.ensure_header(",
        "    header_state = \"未確認\" or sheets_client.ensure_header(",
    ),
    (
        TO_SHEET,
        "列数を指定せずに送る（ズレた行をシートが受け取る）",
        "            expected_width=len(transform.COLUMNS),",
        "",
    ),
    (
        TO_SHEET,
        "書かない指定でも書く",
        "    if write:",
        "    if True:",
    ),
    (
        TO_SHEET,
        "取得の失敗を終了コードに出さない（誰も見ていない時間に沈む）",
        "        if self.fetched_failed:",
        "        if False:",
    ),
    (
        TO_SHEET,
        "書き込み不足を終了コードに出さない",
        "        if self.wrote and not self.ok:",
        "        if False:",
    ),
    (
        TO_SHEET,
        "403 でも共有の案内を出さない（本文だけでは直せない）",
        "    if status == 403:",
        "    if False:",
    ),
    (
        TO_SHEET,
        "403 の判定を新しい形だけで見る（古い例外を取りこぼす）",
        '        status = getattr(getattr(error, "resp", None), "status", None)',
        "        status = None",
    ),
    (
        TO_SHEET,
        "書かなかったことを報告しない",
        '        lines.append("書き込み       シートには1行も書いていません（--dry-run）")',
        "        pass",
    ),
    # ------------------------------------------------------------------ 照合層
    #
    # **照合器が壊れると、他の全部が壊れても緑になる。**
    # ここだけは「壊したのにテストが落ちない」を絶対に残さない（DESIGN 5-T）。
    (
        VERIFY,
        "0件でも一致と言う（比べていないのに合格になる）",
        "        return bool(self.compared_rows) and not self.mismatches and not self.notes",
        "        return not self.mismatches and not self.notes",
    ),
    (
        VERIFY,
        "書き込みスコープで読み直す（確かめる経路が書く経路と1本になる）",
        "        key_path, [sheets_client.SCOPE_READ], **kwargs",
        "        key_path, [sheets_client.SCOPE_WRITE], **kwargs",
    ),
    (
        VERIFY,
        "照合側も FORMATTED_VALUE で読む（書き込みの異常を照合の異常が打ち消す）",
        "        service, spreadsheet_id, _data_range(sheet_name), width=len(transform.COLUMNS)",
        "        service, spreadsheet_id, _data_range(sheet_name)",
    ),
    (
        VERIFY,
        "セルを比べない（照合が常に通る）",
        "            if want[index] != got[index]:",
        "            if False:",
    ),
    (
        VERIFY,
        "行数の帳尻を見ない（上書きされた古い行に気づけない）",
        "    if rows_before is not None and rows_before + len(expected) != len(rows):",
        "    if False:",
    ),
    (
        VERIFY,
        "行が足りなくても照合を続ける",
        "    if len(data) < len(expected):",
        "    if False:",
    ),
    (
        VERIFY,
        "見出しも照合の対象に入れる（末尾の空欄で毎回赤くなる）",
        "    data = rows[1:]  # 見出しは照合の対象にしない",
        "    data = rows",
    ),
    (
        VERIFY,
        "補ってから短い行を数える（必ず0になる＝鳴らない検査）",
        "    raw = sheets_client.read_rows(service, spreadsheet_id, _data_range(sheet_name))",
        "    raw = _read_all(service, spreadsheet_id, sheet_name)",
    ),
    (
        VERIFY,
        "点検で0行を異常なしと言う",
        "            and bool(self.checked_rows)",
        "            and True",
    ),
    (
        VERIFY,
        "点検で見出しの不一致を無視する",
        "            self.header_ok",
        "            True",
    ),
    (
        VERIFY,
        "点検で時刻の読めない行を数えない",
        '        if diff.parse_time(row[transform.COLUMNS.index("取得時刻")]) is None:',
        "        if False:",
    ),
    (
        VERIFY,
        "点検で価格が数値でない行を数えない（FORMATTED_VALUE を踏んでも鳴らない）",
        "            if not isinstance(price, int) or isinstance(price, bool):",
        "            if False:",
    ),
    (
        VERIFY,
        "失敗行の空の価格も異常として数える（毎回鳴って本物が埋もれる）",
        "        elif status == transform.STATUS_OK:",
        "        elif True:",
    ),
    (
        VERIFY,
        "報告に比べたセル数を出さない（数字の足りない「一致」になる）",
        '        f"{result.compared_cells} セルを比べました"',
        '        ""',
    ),
    (
        TO_SHEET,
        "照合が失敗しても終了コードに出さない",
        "        if self.verification is not None and not self.verification.ok:",
        "        if False:",
    ),
    (
        TO_SHEET,
        "照合に行数の帳尻を渡さない（上書きを見逃す）",
        "                rows_before=len(history),",
        "",
    ),
]


def run_tests(work: Path) -> bool:
    """写した側でテストを回す。1件でも落ちたら True。"""
    proc = subprocess.run(
        # --basetemp を写した側の中に置く。既定の %TEMP% を使うと、後片付けで
        # PermissionError（共有のシンボリックリンクを消せない）が出て、
        # **壊す前から落ちている**ように見える（2026-09-08 に実際に踏んだ）。
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

            # **照合と書き込みは LF に正規化した文字列で行う。**
            # core.autocrlf の影響で .py が CRLF になりうる。newline="" のまま
            # `\n` を含むパターンを探すと複数行のパターンが一度もマッチせず、
            # 「置換先なし」が素通りと区別できないまま数字だけ出る。
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
