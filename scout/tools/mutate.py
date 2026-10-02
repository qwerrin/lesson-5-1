#!/usr/bin/env python3
"""scout を1か所ずつ壊して、テストが落ちることを確かめる。

**テストが通っていることは、守られていることの証拠にならない。**

使い方::

    .venv\\Scripts\\python.exe scout\\tools\\mutate.py

仕組みは `figset/tools/mutate.py` と同じ。リポジトリを一時ディレクトリへ写し、
**写した側だけ**を壊す。数え方も同じで、**pytest の終了コード 1 だけを kill と数え、
2〜5 は「測定不能」として混ぜない**。バイトコードのキャッシュも止める
——*置換前後が同じ長さだと `.pyc` が生き残り、以降の全件が偽の kill に化ける*
（2026-09-20 に `figset` で実際に踏んだ。教訓
`restoring-a-file-does-not-restore-its-code`）。

`fetch` で狙う失敗の形
------------------------------------------------------------------

この段の仕事は「取れなかったことを、無かったことにしない」だけなので、
狙うのは **「静かに0件になること」** と **「静かに成功に見えること」**。

================================== ==============================================
壊すと何が起きるか                  なぜ静かなのか
================================== ==============================================
0件を成功にする                     **取れていないのに順調に見える**
例外を0件として握る                 繋がっていないのに「今日は無かった」になる
非200 を通す                        エラー本文を記事として読もうとする
配列でない応答を通す                 Qiita のエラーオブジェクトが0件に化ける
1つ死んでも全体を成功にする          **他が生きていれば見た目は変わらない**
時差を捨てる                        9時間ずれた日付が、そのまま台帳に入る
本文の空文字を本文として通す         中身の無い本文で要約が作られる
総数が無いのを0にする                「まだ先がある」が永久に出なくなる
期間に時刻を入れる                   Qiita が受け付けず、**絞り込みが効かない**
上限を外す                          100 を超える要求が黙って切られる
================================== ==============================================
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PYTHON = ROOT / ".venv" / "Scripts" / "python.exe"

FETCH = "scout/fetch.py"
DEDUPE = "scout/dedupe.py"
RANK = "scout/rank.py"
SPLIT = "scout/split.py"
SUMMARIZE = "scout/summarize.py"

IGNORE = shutil.ignore_patterns(
    ".venv", ".git", "__pycache__", ".pytest_cache", ".pytest_tmp",
    "docs", "*.png", "*.wav", "_parts", "node_modules",
)

#: **範囲を広げ忘れると、壊したのにテストが1件も走らず「素通り」に見える。**
TEST_PATHS = ("scout/tests",)

#: pytest の終了コード。**「テストが落ちた」は 1 だけ。**
PYTEST_FAILED = 1

# (対象ファイル, 壊した内容, 置換前, 置換後)
MUTATIONS: list[tuple[str, str, str, str]] = [
    # ------------------------------------------------------------ 取得元の指定
    (
        FETCH,
        "取得元が空でも回す（何も取らずに異常なしと答える）",
        "    if not sources:",
        "    if False:",
    ),
    (
        FETCH,
        "取得元の順を名前順に並べ替える（どれが落ちたか追いにくくなる）",
        "    results = [_one(source, get, since=since) for source in sources]",
        "    results = [_one(source, get, since=since) for source in sorted(sources, key=lambda s: s.name)]",
    ),
    (
        FETCH,
        "最初の取得元の記事しか返さない",
        "        return tuple(a for result in self.results for a in result.articles)",
        "        return tuple(a for result in self.results[:1] for a in result.articles)",
    ),
    # ------------------------------------------------------------ 3値の判定
    (
        FETCH,
        "0件を成功にする（取れていないのに順調に見える）",
        "        status=OK if articles else EMPTY,",
        "        status=OK,",
    ),
    (
        FETCH,
        "非200 を通す（エラー本文を記事として読もうとする）",
        "    if reply.status != 200:",
        "    if False:",
    ),
    (
        FETCH,
        "レート制限を成功扱いにする",
        "    if reply.status != 200:",
        "    if reply.status not in (200, 429):",
    ),
    (
        FETCH,
        "繋がらなかったことを0件として握る",
        '        return _failed(source, f"{type(error).__name__}: {error}")',
        '        return SourceResult(source.name, EMPTY, (), "", None, False)',
    ),
    (
        FETCH,
        "読めなかった応答を0件として握る",
        '        return _failed(source, f"応答を読めない（{type(error).__name__}: {error}）")',
        '        return SourceResult(source.name, EMPTY, (), "", None, False)',
    ),
    (
        FETCH,
        "配列でない応答を通す（Qiita のエラーオブジェクトが0件に化ける）",
        "    if not isinstance(payload, list):",
        "    if False:",
    ),
    (
        FETCH,
        "1つ死んでも全体を成功にする（他が生きていれば見た目は変わらない）",
        "        if any(result.status == FAILED for result in self.results):",
        "        if False:",
    ),
    (
        FETCH,
        "全部空でも成功にする",
        "        if all(result.status == EMPTY for result in self.results):",
        "        if False:",
    ),
    (
        FETCH,
        "1件でも空なら全体を空にする",
        "        if all(result.status == EMPTY for result in self.results):",
        "        if any(result.status == EMPTY for result in self.results):",
    ),
    # ---------------------------------------------------------------- 日時
    (
        FETCH,
        "時差を捨てる（9時間ずれた日付が台帳に入る）",
        "    return value.astimezone().replace(tzinfo=None)",
        "    return value.replace(tzinfo=None)",
    ),
    (
        FETCH,
        "揃えずにそのまま返す（時差つきと素の日時が混ざる）",
        "    if value.tzinfo is None:\n        return value",
        "    if True:\n        return value",
    ),
    (
        FETCH,
        "公開日に更新日を入れる（更新で古い記事が新着に化ける）",
        '            published_at=_local(datetime.fromisoformat(item["created_at"])),',
        '            published_at=_local(datetime.fromisoformat(item["updated_at"])),',
    ),
    # ---------------------------------------------------------------- 本文
    (
        FETCH,
        "本文の空文字を本文として通す（中身の無い本文で要約が作られる）",
        "        return bool(self.body)",
        "        return self.body is not None",
    ),
    (
        FETCH,
        "Qiita の本文を読まない",
        '            body=item.get("body"),',
        "            body=None,",
    ),
    (
        FETCH,
        "Zenn の打ち切られた説明を本文として通す",
        "                body=None,",
        '                body=_text(item, "description"),',
    ),
    # ---------------------------------------------------------------- URL
    (
        FETCH,
        "期間の指定を無視する（毎回ぜんぶ取りに行く）",
        "        if since is not None:",
        "        if False:",
    ),
    (
        FETCH,
        "期間に時刻まで入れる（Qiita が受け付けず絞り込みが効かない）",
        'query = f"{query} created:>={since:%Y-%m-%d}"',
        'query = f"{query} created:>={since:%Y-%m-%dT%H:%M}"',
    ),
    (
        FETCH,
        "件数の上限を外す（100 を超える要求が黙って切られる）",
        "        per_page = min(source.limit, QIITA_MAX_PER_PAGE)",
        "        per_page = source.limit",
    ),
    (
        FETCH,
        "Zenn のトピックを外した URL を叩く",
        'return f"https://zenn.dev/topics/{quote(source.query)}/feed"',
        'return f"https://zenn.dev/{quote(source.query)}/feed"',
    ),
    # ------------------------------------------------------------ 総数・続き
    (
        FETCH,
        "総数が分からないのを0にする（まだ先があるが永久に出ない）",
        "    total = int(raw_total) if raw_total is not None else None",
        "    total = int(raw_total) if raw_total is not None else 0",
    ),
    (
        FETCH,
        "まだ先があることを言わない",
        "        more=total is not None and total > len(articles),",
        "        more=False,",
    ),
    (
        FETCH,
        "いつでも先があると言う",
        "        more=total is not None and total > len(articles),",
        "        more=True,",
    ),
    # ---------------------------------------------------------------- 項目
    (
        FETCH,
        "タグを取らない",
        '            tags=tuple(tag["name"] for tag in item.get("tags") or ()),',
        "            tags=(),",
    ),
    (
        FETCH,
        "いいね数を取らない",
        '                "likes": item.get("likes_count", 0),',
        '                "likes": 0,',
    ),
    # ================================================================= dedupe
    # **ここで狙うのは「効きすぎ」と「効かなすぎ」の両方。**
    # 効かなければ重複が1行増えるだけだが、**効きすぎるとその記事は二度と来ない**。
    # ------------------------------------------------------------ 正規化
    (
        DEDUPE,
        "追跡パラメータを落とさない（`?utm_source=` 違いが別物として重複する）",
        "        if key.lower() not in TRACKING_PARAMS",
        "        if True",
    ),
    (
        DEDUPE,
        "クエリを全部落とす（**効きすぎ**。別の記事が同じものになる）",
        "        if key.lower() not in TRACKING_PARAMS",
        "        if False",
    ),
    (
        DEDUPE,
        "追跡パラメータを大小で取りこぼす（`UTM_SOURCE` が残って重複する）",
        "        if key.lower() not in TRACKING_PARAMS",
        "        if key not in TRACKING_PARAMS",
    ),
    (
        DEDUPE,
        "クエリを並べ替えない（並びだけ違うものが別物になる）",
        'query = "&".join(f"{key}={value}" for key, value in sorted(kept))',
        'query = "&".join(f"{key}={value}" for key, value in kept)',
    ),
    (
        DEDUPE,
        "フラグメントを残す（同じページの中の位置が別の記事になる）",
        '            "",  # フラグメントは同じページの中の位置であって、別の記事ではない',
        "            parts.fragment,",
    ),
    (
        DEDUPE,
        "末尾のスラッシュを落とさない",
        '    if len(path) > 1 and path.endswith("/"):',
        "    if False:",
    ),
    (
        DEDUPE,
        "根のスラッシュまで落とす（別物に見えるだけで得が無い）",
        '    if len(path) > 1 and path.endswith("/"):',
        '    if path.endswith("/"):',
    ),
    (
        DEDUPE,
        "パスの大小を揃える（**効きすぎ**。大小が意味を持つ URL が潰れる）",
        "            path,  # **パスの大小は揃えない**（大小が意味を持つ URL がある）",
        "            path.lower(),",
    ),
    (
        DEDUPE,
        "ホストの大小を揃えない",
        "            parts.netloc.lower(),",
        "            parts.netloc,",
    ),
    (
        DEDUPE,
        "www を落とす（**踏み込みすぎ**。落として困る場合が理論上ある）",
        "            parts.netloc.lower(),",
        '            parts.netloc.lower().removeprefix("www."),',
    ),
    # **スキームの `.lower()` は仕込まない。** `urlsplit` が既に小文字にしているので、
    # 重ねても観測できるものが1つも変わらない（2026-09-23 に素通りして分かり、実装から消した）。
    (
        DEDUPE,
        "正規化できないものを鍵にする（台帳の空行で、URL 無しの記事が全部消える）",
        "        if key:\n            keys.add(key)",
        "        if True:\n            keys.add(key)",
    ),
    # ------------------------------------------------------------ 突き合わせ
    (
        DEDUPE,
        "台帳を見ない（毎日同じ記事が積み上がる）",
        "    if key in before:",
        "    if False:",
    ),
    (
        DEDUPE,
        "vault を見ない（既に持っている記事をまた持ってくる）",
        "    if key in in_vault:",
        "    if False:",
    ),
    (
        DEDUPE,
        "同じ取り込みの中の重複を見ない",
        "    if key in batch:",
        "    if False:",
    ),
    (
        DEDUPE,
        "理由の順番を入れ替える（直しやすいほうを先に出さない）",
        "    if key in before:\n        return SEEN_BEFORE\n    if key in in_vault:\n        return IN_VAULT",
        "    if key in in_vault:\n        return IN_VAULT\n    if key in before:\n        return SEEN_BEFORE",
    ),
    (
        DEDUPE,
        "残したものを取り込みに覚えさせない（同じ回の重複が素通りする）",
        "            batch.add(key)",
        "            pass",
    ),
    (
        DEDUPE,
        "捨てたものを返さない（**H8：何を捨てたか残らない**）",
        "            dropped.append(Dropped(article=article, key=key, reason=reason))",
        "            pass",
    ),
    (
        DEDUPE,
        "渡された順を逆にする",
        "    for article in articles:\n        key = normalize(article.url)",
        "    for article in reversed(articles):\n        key = normalize(article.url)",
    ),
    # ---------------------------------------------------------------- 件数
    (
        DEDUPE,
        "見た件数を残した件数と同じにする（何件落としたか分からない）",
        "        return len(self.kept) + len(self.dropped)",
        "        return len(self.kept)",
    ),
    (
        DEDUPE,
        "理由の内訳を出さない",
        "        return dict(Counter(d.reason for d in self.dropped))",
        "        return {}",
    ),
    (
        DEDUPE,
        "件数を出さずに「重複を除きました」とだけ言う",
        '            f"{self.total} 件中 {len(self.kept)} 件を残した"',
        '            "重複を除きました"',
    ),
    (
        DEDUPE,
        "内訳を空にする（落とした理由が伝わらない）",
        'breakdown = "・".join(f"{r} {n}" for r, n in sorted(self.reasons.items()))',
        'breakdown = ""',
    ),
    # ================================================================ rank
    # ---------------------------------------------------------------- 上限
    (
        RANK,
        "上限0でも回す（全部を捨てて「上限どおり」と答える）",
        "    if profile.cap < 1:",
        "    if False:",
    ),
    (
        RANK,
        "上限の境界をずらして0を通す",
        "    if profile.cap < 1:",
        "    if profile.cap < 0:",
    ),
    (
        RANK,
        "上限より1件多く選ぶ",
        "        picked=tuple(scored[: profile.cap]),",
        "        picked=tuple(scored[: profile.cap + 1]),",
    ),
    (
        RANK,
        "上限で落としたものを返さない（**H8：何を捨てたか残らない**）",
        "        dropped=tuple(dropped + over),",
        "        dropped=tuple(dropped),",
    ),
    (
        RANK,
        "上限で落としたものの点と内訳を捨てる（上限が妥当か検証できない）",
        'reason=OVER_CAP, detail="", score=s.score, hits=s.hits)',
        'reason=OVER_CAP, detail="", score=None, hits=())',
    ),
    # ---------------------------------------------------------------- 点
    (
        RANK,
        "**いいね数を点に混ぜる**（生まれたての記事が沈む）",
        "        scored.append(Scored(kept=item, score=len(hits), hits=hits))",
        '        scored.append(Scored(kept=item, score=len(hits) + item.article.metrics.get("likes", 0), hits=hits))',
    ),
    (
        RANK,
        "タグを部分一致で見る（`python3` が `python` に化ける）",
        "if tag.casefold() in tags]",
        "if any(t in tag.casefold() for t in tags)]",
    ),
    (
        RANK,
        "興味のタグの大小を見る",
        'found = [f"tag:{tag}" for tag in item.article.tags if tag.casefold() in tags]',
        'found = [f"tag:{tag}" for tag in item.article.tags if tag in tags]',
    ),
    (
        RANK,
        "内訳のタグを小文字に書き換える（記録が実物と合わない）",
        'found = [f"tag:{tag}" for tag in item.article.tags if tag.casefold() in tags]',
        'found = [f"tag:{tag.casefold()}" for tag in item.article.tags if tag.casefold() in tags]',
    ),
    (
        RANK,
        "興味の語の大小を見る",
        "for word, folded in words if folded in title]",
        "for word, folded in words if word in title]",
    ),
    (
        RANK,
        "タイトルの語で点を付けない",
        '    found += [f"keyword:{word}" for word, folded in words if folded in title]',
        "    pass",
    ),
    (
        RANK,
        "設定の前後の空白を落とさない",
        "    return {v.strip().casefold() for v in values}",
        "    return {v.casefold() for v in values}",
    ),
    # ---------------------------------------------------------------- 並び
    (
        RANK,
        "並べ替えない（取得元が返した順を意味に使う・U8）",
        "    scored.sort(key=_order)",
        "    pass",
    ),
    (
        RANK,
        "**いいね数を点より先に見る**",
        "    return (-s.score, -popularity,",
        "    return (-popularity, -s.score,",
    ),
    (
        RANK,
        "同点の並べ替えでストックを見ない",
        '    popularity = metrics.get("likes", 0) + metrics.get("stocks", 0)',
        '    popularity = metrics.get("likes", 0)',
    ),
    (
        RANK,
        "同点を古い順に並べる",
        "-s.kept.article.published_at.timestamp(), s.kept.key)",
        "s.kept.article.published_at.timestamp(), s.kept.key)",
    ),
    (
        RANK,
        "最後の決め手を渡された順にする（全部同点だと取得元の順になる）",
        "-s.kept.article.published_at.timestamp(), s.kept.key)",
        '-s.kept.article.published_at.timestamp(), "")',
    ),
    # ---------------------------------------------------------------- 物差しが無いもの
    (
        RANK,
        "**タグの無い記事を0点として並べる**（上限を超えた日に黙って全部落ちる）",
        "        if not item.article.tags:",
        "        if False:",
    ),
    (
        RANK,
        "点を付けない記事を並べ替える（フィードの新着順を壊す）",
        "        unranked=tuple(unranked),",
        "        unranked=tuple(sorted(unranked, key=lambda k: k.key)),",
    ),
    # ---------------------------------------------------------------- ミュート
    (
        RANK,
        "ミュートしたものを返さない（**H8**）",
        "            dropped.append(Rejected(kept=item, reason=MUTED, detail=muted, score=None, hits=()))",
        "            pass",
    ),
    (
        RANK,
        "点を付けない記事にミュートを効かせない",
        "        if muted is not None:",
        "        if muted is not None and item.article.tags:",
    ),
    (
        RANK,
        "ミュートのタグの大小を見る",
        "        if tag.casefold() in mute_tags:",
        "        if tag in mute_tags:",
    ),
    (
        RANK,
        "ミュートの語を見るときタイトルの大小を揃えない",
        "    title = item.article.title.casefold()\n    for word, folded in mute_words:",
        "    title = item.article.title\n    for word, folded in mute_words:",
    ),
    (
        RANK,
        "ミュートの理由の中身を捨てる（どのタグで落ちたか分からない）",
        '            return f"tag:{tag}"',
        '            return "muted"',
    ),
    (
        RANK,
        "**空の語を捨てない**（その日の記事が全部消える）",
        "    return [(v.strip(), v.strip().casefold()) for v in values if v.strip()]",
        "    return [(v.strip(), v.strip().casefold()) for v in values]",
    ),
    # ---------------------------------------------------------------- 件数
    (
        RANK,
        "総数から点を付けなかったぶんを抜く（どこかで黙って消える）",
        "        return len(self.picked) + len(self.unranked) + len(self.dropped)",
        "        return len(self.picked) + len(self.dropped)",
    ),
    (
        RANK,
        "理由の内訳を出さない",
        "        return dict(Counter(d.reason for d in self.dropped))\n\n    @property\n    def summary(self) -> str:\n        \"\"\"**件数を必ず出す。** 点を付けなかった",
        "        return {}\n\n    @property\n    def summary(self) -> str:\n        \"\"\"**件数を必ず出す。** 点を付けなかった",
    ),
    (
        RANK,
        "件数を出さずに「選びました」とだけ言う",
        '            f"{self.total} 件中 {len(self.picked)} 件を選んだ"',
        '            "選びました"',
    ),
    (
        RANK,
        "点を付けなかった件数を出さない",
        '            f"／点を付けなかった {len(self.unranked)} 件"',
        '            ""',
    ),
    # ================================================================ split
    # ---------------------------------------------------------------- 入口
    (
        SPLIT,
        "要約してよい取得元が空でも回す（1件も要約せずに異常なしと答える）",
        "    if not summarizable:",
        "    if False:",
    ),
    (
        SPLIT,
        "しきい値0でも回す（空白1字でも本文になる）",
        "    if min_body < 1:",
        "    if False:",
    ),
    (
        SPLIT,
        "しきい値の境界をずらして0を通す",
        "    if min_body < 1:",
        "    if min_body < 0:",
    ),
    # ---------------------------------------------------------------- 経路
    (
        SPLIT,
        "**取得元を見ない**（本文の有無だけで要約の段が開く）",
        "    if article.source not in summarizable:\n        return NOT_SUMMARIZABLE",
        "    if False:\n        return NOT_SUMMARIZABLE",
    ),
    (
        SPLIT,
        "本文の有無を取得元より先に見る",
        "    if article.source not in summarizable:\n        return NOT_SUMMARIZABLE\n    if not article.has_body:\n        return NO_BODY",
        "    if not article.has_body:\n        return NO_BODY\n    if article.source not in summarizable:\n        return NOT_SUMMARIZABLE",
    ),
    (
        SPLIT,
        "本文が無いことを「短い」に混ぜる",
        "    if not article.has_body:",
        "    if False:",
    ),
    (
        SPLIT,
        "前後の空白も長さに数える",
        '    if len((article.body or "").strip()) < min_body:',
        '    if len(article.body or "") < min_body:',
    ),
    (
        SPLIT,
        "しきい値ちょうどを短いとみなす",
        '    if len((article.body or "").strip()) < min_body:',
        '    if len((article.body or "").strip()) <= min_body:',
    ),
    (
        SPLIT,
        "既定のしきい値を実測より低くする",
        "MIN_BODY = 500",
        "MIN_BODY = 200",
    ),
    (
        SPLIT,
        "既定のしきい値を使わない",
        "    min_body: int = MIN_BODY,",
        "    min_body: int = 1,",
    ),
    (
        SPLIT,
        "**点の無い記事の理由を取得元のせいにする**（直し方を間違える）",
        "            reason = UNRANKED",
        "            reason = NOT_SUMMARIZABLE",
    ),
    # ---------------------------------------------------------------- 運ぶもの
    (
        SPLIT,
        "点を付けなかった記事を運ばない（Zenn が消える）",
        "    for kept in ranking.unranked:",
        "    for kept in ():",
    ),
    (
        SPLIT,
        "**捨てたものを生き返らせる**",
        "    for kept in ranking.unranked:",
        "    for kept in (*ranking.unranked, *(r.kept for r in ranking.dropped)):",
    ),
    (
        SPLIT,
        "選んだ順を逆にする",
        "    for scored in ranking.picked:",
        "    for scored in reversed(ranking.picked):",
    ),
    (
        SPLIT,
        "点を付けなかった記事を見出しの先頭へ差し込む",
        "        headline.append(Headline(kept=kept, reason=reason, score=None))",
        "        headline.insert(0, Headline(kept=kept, reason=reason, score=None))",
    ),
    (
        SPLIT,
        "見出しにした記事の点を捨てる",
        "            headline.append(Headline(kept=scored.kept, reason=reason, score=scored.score))",
        "            headline.append(Headline(kept=scored.kept, reason=reason, score=None))",
    ),
    # ---------------------------------------------------------------- 件数
    (
        SPLIT,
        "総数から見出しのぶんを抜く",
        "        return len(self.summarize) + len(self.headline)",
        "        return len(self.summarize)",
    ),
    (
        SPLIT,
        "理由の内訳を出さない",
        "        return dict(Counter(h.reason for h in self.headline))",
        "        return {}",
    ),
    (
        SPLIT,
        "件数を出さずに「要約しました」とだけ言う",
        '            f"{self.total} 件中 {len(self.summarize)} 件を要約へ"',
        '            "要約しました"',
    ),
    (
        SPLIT,
        "見出しだけの件数を出さない",
        "            f\"／見出しだけ {len(self.headline)} 件（{breakdown or 'なし'}）\"",
        "            f\"（{breakdown or 'なし'}）\"",
    ),
    (
        SPLIT,
        "内訳を空にする",
        'breakdown = "・".join(f"{r} {n}" for r, n in sorted(self.reasons.items()))',
        'breakdown = ""',
    ),
    # ================================================================ summarize
    # ---------------------------------------------------------------- 課金
    (
        SUMMARIZE,
        "**上限を見ない**（呼んでから気づいても課金は戻らない）",
        "    if len(items) > max_calls:",
        "    if False:",
    ),
    (
        SUMMARIZE,
        "上限ちょうどを超えたとみなす",
        "    if len(items) > max_calls:",
        "    if len(items) >= max_calls:",
    ),
    (
        SUMMARIZE,
        "既定の上限を rank より広げる",
        "MAX_CALLS = 10",
        "MAX_CALLS = 20",
    ),
    (
        SUMMARIZE,
        "既定の上限を使わない",
        "    max_calls: int = MAX_CALLS,",
        "    max_calls: int = 1000,",
    ),
    # ---------------------------------------------------------------- 失敗の扱い
    (
        SUMMARIZE,
        "**何でも握る**（こちらのバグが API の失敗に化ける）",
        "        except GeminiError as error:",
        "        except Exception as error:",
    ),
    (
        SUMMARIZE,
        "API の失敗の中身を捨てる",
        "            failed.append(Failure(scored=scored, reason=CALL_FAILED, detail=str(error)))",
        '            failed.append(Failure(scored=scored, reason=CALL_FAILED, detail=""))',
    ),
    (
        SUMMARIZE,
        "**打ち切りを見ない**（途中で切れた要約が通る）",
        '        if reply.finish_reason != "STOP":',
        "        if False:",
    ),
    (
        SUMMARIZE,
        "終わり方が分からないものを STOP と混ぜる",
        '        if reply.finish_reason != "STOP":',
        '        if reply.finish_reason not in ("STOP", None):',
    ),
    (
        SUMMARIZE,
        "打ち切りの理由を捨てる",
        '                    detail=f"finish_reason={reply.finish_reason}",',
        '                    detail="",',
    ),
    (
        SUMMARIZE,
        "壊れた答えの原文を捨てる（何が返ったか分からないまま課金だけ乗る）",
        '                    detail=f"{error}／原文: {reply.text[:DETAIL_LIMIT]}",',
        "                    detail=str(error),",
    ),
    # ---------------------------------------------------------------- 渡すもの
    (
        SUMMARIZE,
        "**本文を切って渡す**（切った先の主張が照合できない）",
        '        f"# {article.title}\\n\\n{article.body}"',
        "        f\"# {article.title}\\n\\n{(article.body or '')[:20000]}\"",
    ),
    (
        SUMMARIZE,
        "タイトルを渡さない",
        '        f"# {article.title}\\n\\n{article.body}"',
        '        f"\\n\\n{article.body}"',
    ),
    (
        SUMMARIZE,
        "「そのまま抜け」と指示しない",
        "**本文からそのまま**抜き出して",
        "抜き出して",
    ),
    # ---------------------------------------------------------------- 答えの解釈
    (
        SUMMARIZE,
        "オブジェクトかどうかを見ない",
        "    if not isinstance(payload, dict):",
        "    if False:",
    ),
    (
        SUMMARIZE,
        "空白だけの要約を通す",
        "    if not isinstance(text, str) or not text.strip():",
        "    if not isinstance(text, str) or not text:",
    ),
    (
        SUMMARIZE,
        "要約が文字列かを見ない",
        "    if not isinstance(text, str) or not text.strip():",
        "    if not text:",
    ),
    (
        SUMMARIZE,
        "引用の形を見ない",
        "    if not isinstance(quotes, list) or not all(isinstance(q, str) for q in quotes):",
        "    if False:",
    ),
    (
        SUMMARIZE,
        "引用の中身が文字列かを見ない",
        "    if not isinstance(quotes, list) or not all(isinstance(q, str) for q in quotes):",
        "    if not isinstance(quotes, list):",
    ),
    (
        SUMMARIZE,
        "**引用の欄が無いと失敗にする**（証拠ではないのに要約を捨てる）",
        '    quotes = payload.get("quotes", [])',
        '    quotes = payload.get("quotes")',
    ),
    (
        SUMMARIZE,
        "要約の前後の空白を残す",
        "    return text.strip(), tuple(quotes)",
        "    return text, tuple(quotes)",
    ),
    # ---------------------------------------------------------------- 記録
    (
        SUMMARIZE,
        "記事ごとのトークン数を捨てる",
        "                quotes=quotes,\n                prompt_tokens=reply.prompt_tokens,",
        "                quotes=quotes,\n                prompt_tokens=None,",
    ),
    (
        SUMMARIZE,
        "入力の合計に出力を数える",
        "        return _sum(item.prompt_tokens for item in self._answered())",
        "        return _sum(item.output_tokens for item in self._answered())",
    ),
    (
        SUMMARIZE,
        "**要約に使えなかった呼び出しのトークンを合計から落とす**（課金されたのに 0 に見える）",
        "        return [*self.done, *(f for f in self.failed if f.reason != CALL_FAILED)]",
        "        return [*self.done]",
    ),
    (
        SUMMARIZE,
        "例外で終わった呼び出しも合計に入れる（数が無いので合計が消える）",
        "        return [*self.done, *(f for f in self.failed if f.reason != CALL_FAILED)]",
        "        return [*self.done, *self.failed]",
    ),
    (
        SUMMARIZE,
        "打ち切られた呼び出しのトークンを残さない",
        "                    detail=f\"finish_reason={reply.finish_reason}\",\n                    prompt_tokens=reply.prompt_tokens,",
        "                    detail=f\"finish_reason={reply.finish_reason}\",\n                    prompt_tokens=None,",
    ),
    (
        SUMMARIZE,
        "壊れた答えのトークンを残さない",
        "                    detail=f\"{error}／原文: {reply.text[:DETAIL_LIMIT]}\",\n                    prompt_tokens=reply.prompt_tokens,",
        "                    detail=f\"{error}／原文: {reply.text[:DETAIL_LIMIT]}\",\n                    prompt_tokens=None,",
    ),
    (
        SUMMARIZE,
        "**分からないトークン数を0として足す**（実際より安く見える）",
        "        if value is None:\n            return None",
        "        if value is None:\n            continue",
    ),
    (
        SUMMARIZE,
        "渡された順を逆にする",
        "    for scored in items:",
        "    for scored in reversed(items):",
    ),
    # ---------------------------------------------------------------- 件数
    (
        SUMMARIZE,
        "総数からできなかったぶんを抜く",
        "        return len(self.done) + len(self.failed)",
        "        return len(self.done)",
    ),
    (
        SUMMARIZE,
        "**1件失敗しても全体を成功にする**（M2）",
        "        return not self.failed",
        "        return True",
    ),
    (
        SUMMARIZE,
        "理由の内訳を出さない",
        "        return dict(Counter(f.reason for f in self.failed))",
        "        return {}",
    ),
    (
        SUMMARIZE,
        "件数を出さずに「要約しました」とだけ言う",
        '            f"{self.total} 件中 {len(self.done)} 件を要約した"',
        '            "要約しました"',
    ),
    (
        SUMMARIZE,
        "できなかった件数を出さない",
        "            f\"／できなかった {len(self.failed)} 件（{breakdown or 'なし'}）\"",
        "            f\"（{breakdown or 'なし'}）\"",
    ),
    (
        SUMMARIZE,
        "内訳を空にする",
        'breakdown = "・".join(f"{r} {n}" for r, n in sorted(self.reasons.items()))',
        'breakdown = ""',
    ),
]


def run_tests(work: Path) -> tuple[int, str]:
    """写した側でテストを回して、終了コードと出力の末尾を返す。

    **`!= 0` を kill と数えない。** テストの失敗は 1 だけで、
    2〜5（中断・内部エラー・使い方の誤り・1件も集まらなかった）は測定不能。

    **バイトコードのキャッシュを止める。** `.pyc` は (mtime の秒, サイズ) で
    有効性を判断するので、置換前後が同じ長さだと壊れたまま生き残る。
    """
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    proc = subprocess.run(
        [str(PYTHON), "-m", "pytest", *TEST_PATHS, "-x", "-q", "--no-header",
         "-p", "no:cacheprovider", "--basetemp", str(work / ".pytest_tmp")],
        cwd=work, capture_output=True, text=True, encoding="utf-8", errors="replace", env=env,
    )
    tail = "\n".join((proc.stdout + proc.stderr).splitlines()[-6:])
    return proc.returncode, tail


def main() -> int:
    if not PYTHON.exists():
        print(f"仮想環境の Python が見つかりません: {PYTHON}", file=sys.stderr)
        return 1

    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp) / "repo"
        shutil.copytree(ROOT, work, ignore=IGNORE)

        baseline, tail = run_tests(work)
        if baseline != 0:
            print("壊す前からテストが落ちています。先にそちらを直してください。", file=sys.stderr)
            print(tail, file=sys.stderr)
            return 1

        killed: list[str] = []
        survived: list[str] = []
        not_found: list[str] = []
        errored: list[str] = []

        for index, (target, label, before, after) in enumerate(MUTATIONS, start=1):
            path = work / target
            original = path.read_text(encoding="utf-8", newline="")
            haystack = original.replace("\r\n", "\n")

            if haystack.count(before) != 1:
                not_found.append(
                    f"{index:3}. {label}（{target}・{haystack.count(before)}件一致）"
                )
                continue

            path.write_text(haystack.replace(before, after, 1), encoding="utf-8", newline="\n")
            code, tail = run_tests(work)
            if code == PYTEST_FAILED:
                killed.append(f"{index:3}. {label}")
            elif code == 0:
                survived.append(f"{index:3}. {label}（{target}）")
            else:
                errored.append(f"{index:3}. {label}（exit {code}）\n      {tail}")
            path.write_text(original, encoding="utf-8", newline="")

        print(f"壊した箇所: {len(MUTATIONS)}")
        print(f"  kill（テストが落ちた）: {len(killed)}")
        print(f"  素通り: {len(survived)}")
        print(f"  置換先なし: {len(not_found)}")
        print(f"  測定不能: {len(errored)}")

        for title, rows in (
            ("素通りしたもの（テストが守っていない）", survived),
            ("置換先が見つからなかったもの（壊しかたが古い）", not_found),
            ("測定不能だったもの（テストの失敗ではない理由で終了した）", errored),
        ):
            if rows:
                print(f"\n{title}:")
                for row in rows:
                    print(f"  {row}")

    # **置換先なしも測定不能も、成功にしない。**
    return 0 if not (survived or not_found or errored) else 1


if __name__ == "__main__":
    raise SystemExit(main())
