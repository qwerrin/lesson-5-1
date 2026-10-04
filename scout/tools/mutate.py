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
VERIFY = "scout/verify_source.py"
EMIT = "scout/emit.py"
NOTIFY = "scout/notify.py"
CLI = "scout/cli.py"

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
    # ================================================================ verify_source
    # **正規表現は raw 文字列で書く。** 逆スラッシュが崩れると「置換先なし」で止まる。
    # ---------------------------------------------------------------- 主張を抜く
    (
        VERIFY,
        "桁区切りの組を1つの数として抜かない",
        r'r"(?P<num>[0-9]{1,3}(?:,[0-9]{3})+(?![0-9])(?:\.[0-9]+)?|',
        r'r"(?P<num>',
    ),
    (
        VERIFY,
        "小数を1つの数として抜かない",
        r'|[0-9]+(?:\.[0-9]+)*)"',
        r'|[0-9]+)"',
    ),
    (
        VERIFY,
        "**どのカンマも桁区切りにする**（`1,2,3` が `123` になる）",
        r'|[0-9]+(?:\.[0-9]+)*)"',
        r'|[0-9]+(?:[.,][0-9]+)*)"',
    ),
    (
        VERIFY,
        "語の記号を抜かない（`C++` が `C` になる）",
        r'(?P<word>[A-Za-z][A-Za-z0-9_.+#-]*)"',
        r'(?P<word>[A-Za-z][A-Za-z0-9_]*)"',
    ),
    (
        VERIFY,
        "**数の後ろの単位を抜かない**（A：`2倍` が裸の `2` になり、`System 2` で裏付けられる）",
        r'(?P<unit>{_UNIT})(?![A-Za-z])"',
        r'(?P<unit>(?!))"',
    ),
    (
        VERIFY,
        "数と単位の間の空白を許さない（要約器は `200 倍` と書く）",
        r'(?:[ \t]*(?P<unit>{_UNIT})',
        r'(?:(?P<unit>{_UNIT})',
    ),
    (
        VERIFY,
        "英字の単位の後ろの英字を見ない（`5msec` から `ms` を切り取る）",
        r'(?P<unit>{_UNIT})(?![A-Za-z])"',
        r'(?P<unit>{_UNIT})"',
    ),
    (
        VERIFY,
        "要約の全角をそろえない",
        "    for match in TOKEN.finditer(_nfkc(text)):",
        "    for match in TOKEN.finditer(text):",
    ),
    (
        VERIFY,
        "抜いた数の桁区切りを落とさない",
        '            claim = match["num"].replace(",", "") + unit',
        '            claim = match["num"] + unit',
    ),
    (
        VERIFY,
        "抜いた数から単位を落とす",
        '            claim = match["num"].replace(",", "") + unit',
        '            claim = match["num"].replace(",", "")',
    ),
    (
        VERIFY,
        "語の後ろのハイフンを残す",
        '            claim = match["word"].rstrip(".-")',
        '            claim = match["word"].rstrip(".")',
    ),
    (
        VERIFY,
        "文末の句点も語に含める",
        '            claim = match["word"].rstrip(".-")',
        '            claim = match["word"]',
    ),
    # ---------------------------------------------------------------- 弱い数（B）
    (
        VERIFY,
        "**弱い数を見分けない**（1桁の裸の数が裏付けに化ける）",
        '    return re.fullmatch(r"[0-9]", _nfkc(claim)) is not None',
        "    return False",
    ),
    (
        VERIFY,
        "2桁以上の数まで弱いことにする",
        'return re.fullmatch(r"[0-9]", _nfkc(claim))',
        'return re.fullmatch(r"[0-9]+", _nfkc(claim))',
    ),
    (
        VERIFY,
        "弱いかを見るとき全角をそろえない",
        'return re.fullmatch(r"[0-9]", _nfkc(claim)) is not None',
        'return re.fullmatch(r"[0-9]", claim) is not None',
    ),
    (
        VERIFY,
        "**弱い数も照合に数える**",
        "    asserted = tuple(c for c in every if c not in weak_ones)",
        "    asserted = every",
    ),
    (
        VERIFY,
        "弱い数を記録しない（使わなかったことが隠れる）",
        "        weak=weak_ones,",
        "        weak=(),",
    ),
    (
        VERIFY,
        "弱くて使わなかった数を出力に書かない",
        '            f"／弱くて照合に使わなかった数 {sum(len(c.weak) for c in self.checks)} 個"',
        '            ""',
    ),
    (
        VERIFY,
        "同じ主張を何度も数える",
        "        if claim not in found:",
        "        if True:",
    ),
    # ---------------------------------------------------------------- 境界
    (
        VERIFY,
        "主張の桁区切りを落とさない",
        "    target = _ungroup(_nfkc(claim))",
        "    target = _nfkc(claim)",
    ),
    (
        VERIFY,
        "主張の全角をそろえない",
        "    target = _ungroup(_nfkc(claim))",
        "    target = _ungroup(claim)",
    ),
    (
        VERIFY,
        "**空の主張を本文にあることにする**（空文字はどこにでもある）",
        "    if not target:\n        # **空文字",
        "    if False:\n        # **空文字",
    ),
    (
        VERIFY,
        "**数を語として比べる**（小数の一部に当たる）",
        "    if target[:1] in DIGITS:",
        "    if False:",
    ),
    (
        VERIFY,
        "ASCII でない数字を数として扱う（`٣` で落ちる）",
        "    if target[:1] in DIGITS:",
        "    if target[:1].isdigit():",
    ),
    (
        VERIFY,
        "数の前の数字を見ない（`50` が `150` に当たる）",
        r'pattern = rf"(?<![0-9])(?<![0-9]\.){bare}"',
        r'pattern = rf"(?<![0-9]\.){bare}"',
    ),
    (
        VERIFY,
        "小数点の後ろの数字に当てる（`5` が `2.5` に当たる）",
        r'pattern = rf"(?<![0-9])(?<![0-9]\.){bare}"',
        r'pattern = rf"(?<![0-9]){bare}"',
    ),
    (
        VERIFY,
        "**照合で単位を見ない**（A：`2倍` が `System 1/2` で裏付けられる）",
        "        if unit:",
        "        if False:",
    ),
    (
        VERIFY,
        "本文の数と単位の間の空白を許さない",
        r'pattern += rf"[ \t]*{re.escape(unit)}"',
        r'pattern += rf"{re.escape(unit)}"',
    ),
    (
        VERIFY,
        "**数と単位の間で行をまたぐ**（行末の番号が次の行の頭の字と組む）",
        r'pattern += rf"[ \t]*{re.escape(unit)}"',
        r'pattern += rf"\s*{re.escape(unit)}"',
    ),
    (
        VERIFY,
        "照合で英字の単位の後ろを見ない（`5m` を `5ms` で裏付ける）",
        '("(?![A-Za-z])" if unit[-1].isascii() else "")',
        '""',
    ),
    (
        VERIFY,
        "英字の単位と日本語の単位を取り違える",
        '("(?![A-Za-z])" if unit[-1].isascii() else "")',
        '("(?![A-Za-z])" if not unit[-1].isascii() else "")',
    ),
    (
        VERIFY,
        "**数の後ろの数字を見ない**（`2倍` が `200倍` に当たる・U13 の罠）",
        r'(?![0-9])(?!\.[0-9])"',
        r'(?!\.[0-9])"',
    ),
    (
        VERIFY,
        "小数点の前の数字に当てる（`2` が `2.5` に当たる）",
        r'(?![0-9])(?!\.[0-9])"',
        r'(?![0-9])"',
    ),
    (
        VERIFY,
        "語の前の英字を見ない（`Script` が `JavaScript` に当たる）",
        r'rf"(?<![A-Za-z0-9_]){word}',
        r'rf"{word}',
    ),
    (
        VERIFY,
        "語の後ろの記号を見ない（`C` が `C++` に当たる）",
        "(?![A-Za-z0-9_+#])",
        "(?![A-Za-z0-9_])",
    ),
    (
        VERIFY,
        "**語の後ろの英字を見ない**（`Java` が `JavaScript` に当たる）",
        "(?![A-Za-z0-9_+#])",
        "(?![+#])",
    ),
    (
        VERIFY,
        "記号の後ろの続きを見ない（`Node` が `Node.js` に当たる）",
        r'(?![.\-][A-Za-z0-9])"',
        '"',
    ),
    (
        VERIFY,
        "長い語も大小を見る",
        "    flags = 0 if short else re.IGNORECASE",
        "    flags = 0",
    ),
    (
        VERIFY,
        "**短い語の大小を見ない**（`Go` が英文の `go` で裏付けられる）",
        "    flags = 0 if short else re.IGNORECASE",
        "    flags = re.IGNORECASE",
    ),
    (
        VERIFY,
        "3字の語の大小を見ない",
        "SHORT_WORD = 3",
        "SHORT_WORD = 2",
    ),
    (
        VERIFY,
        "4字の語まで大小を見る",
        "SHORT_WORD = 3",
        "SHORT_WORD = 4",
    ),
    # ---------------------------------------------------------------- 大文字の継ぎ目（U21）
    (
        VERIFY,
        "**継ぎ目に空白を許さない**（`VSCode` が本文の `VS Code` で外れる）",
        r'            parts.append("[ \t]?")',
        "            pass",
    ),
    (
        VERIFY,
        "継ぎ目の空白を何字でも許す",
        r'            parts.append("[ \t]?")',
        r'            parts.append("[ \t]*")',
    ),
    (
        VERIFY,
        "**継ぎ目で改行をまたぐ**（行末の語と次の行の頭が組む）",
        r'            parts.append("[ \t]?")',
        r'            parts.append("\s?")',
    ),
    (
        VERIFY,
        "**短い語にも継ぎ目を許す**（`AIs` が英文の `A Is` で裏付けられる）",
        "    word = re.escape(target) if short else _seams(target)",
        "    word = _seams(target)",
    ),
    (
        VERIFY,
        "**頭字語の後ろの小文字1字でも継ぎ目にする**（`APIs` が `AP Is` で裏付けられる）",
        "    return len(chars) == 2 and all(_lower(ch) for ch in chars)",
        "    return all(_lower(ch) for ch in chars)",
    ),
    (
        VERIFY,
        "頭字語の最後を継ぎ目にしない（小文字2字を見られない）",
        "    return len(chars) == 2 and all(_lower(ch) for ch in chars)",
        "    return len(chars) == 3 and all(_lower(ch) for ch in chars)",
    ),
    (
        VERIFY,
        "**大文字でない字の前も継ぎ目にする**（`Pyth on` を `Python` にする）",
        "        if _upper(ch) and (_lower(before) or (_upper(before) and _two_lower(word[i + 1 : i + 3]))):",
        "        if _lower(before) or (_upper(before) and _two_lower(word[i + 1 : i + 3])):",
    ),
    (
        VERIFY,
        "**頭字語の中も継ぎ目にする**（`V SCode` を `VSCode` にする）",
        "        if _upper(ch) and (_lower(before) or (_upper(before) and _two_lower(word[i + 1 : i + 3]))):",
        "        if _upper(ch) and (_lower(before) or _upper(before)):",
    ),
    (
        VERIFY,
        "小文字の後ろを継ぎ目にしない（`Claude Code` が外れる）",
        "        if _upper(ch) and (_lower(before) or (_upper(before) and _two_lower(word[i + 1 : i + 3]))):",
        "        if _upper(ch) and _upper(before) and _two_lower(word[i + 1 : i + 3]):",
    ),
    (
        VERIFY,
        "頭字語の最後を継ぎ目にしない（`VS Code` が外れる）",
        "        if _upper(ch) and (_lower(before) or (_upper(before) and _two_lower(word[i + 1 : i + 3]))):",
        "        if _upper(ch) and _lower(before):",
    ),
    (
        VERIFY,
        "**大文字の前ならどこでも継ぎ目にする**（記号の後ろ・頭字語の中で切れる）",
        "        if _upper(ch) and (_lower(before) or (_upper(before) and _two_lower(word[i + 1 : i + 3]))):",
        "        if _upper(ch):",
    ),
    (
        VERIFY,
        "継ぎ目を要約の大文字から決めない（`VSCode` が `Vscode` 扱いになって外れる）",
        "    word = re.escape(target) if short else _seams(target)",
        "    word = re.escape(target) if short else _seams(target.title())",
    ),
    # ---------------------------------------------------------------- 本文の書式
    (
        VERIFY,
        "本文の全角をそろえない",
        "    text = _nfkc(text)\n    text = LINK",
        "    text = LINK",
    ),
    (
        VERIFY,
        "**上付き・下付き・分数を数にする**（`10²` が `102` に、`1½` が `11⁄2` に）",
        "        _SEVER if unicodedata.decomposition(ch).startswith(_PHANTOM) else ch for ch in text",
        "        ch for ch in text",
    ),
    (
        VERIFY,
        "その字を空白にする（`10² 回` が「10回」になる）",
        '_SEVER = "|"',
        '_SEVER = " "',
    ),
    (
        VERIFY,
        "上付き文字を見逃す",
        '_PHANTOM = ("<super>", "<sub>", "<fraction>")',
        '_PHANTOM = ("<sub>", "<fraction>")',
    ),
    (
        VERIFY,
        "下付き文字を見逃す",
        '_PHANTOM = ("<super>", "<sub>", "<fraction>")',
        '_PHANTOM = ("<super>", "<fraction>")',
    ),
    (
        VERIFY,
        "分数を見逃す",
        '_PHANTOM = ("<super>", "<sub>", "<fraction>")',
        '_PHANTOM = ("<super>", "<sub>")',
    ),
    (
        VERIFY,
        "**リンクを文字だけにしない**（U12 で実測した引用が本文に無いことになる）",
        r'    text = LINK.sub(r"\1", text)',
        "    pass",
    ),
    (
        VERIFY,
        "むき出しの URL を根拠にする",
        '    text = BARE_URL.sub(" ", text)',
        "    pass",
    ),
    (
        VERIFY,
        "**書式記号を落とさない**（U12：強調を描画後の見た目で読んだ主張が外れる）",
        '    text = DECOR.sub("", text)',
        "    pass",
    ),
    (
        VERIFY,
        "下線の強調を書式と見ない",
        r'DECOR = re.compile(r"\*\*|__|`")',
        r'DECOR = re.compile(r"\*\*|`")',
    ),
    (
        VERIFY,
        "本文の桁区切りを落とさない",
        "    return _ungroup(text)",
        "    return text",
    ),
    (
        VERIFY,
        "桁区切りの組からカンマを落とさない",
        '    return THOUSANDS.sub(lambda m: m.group().replace(",", ""), text)',
        "    return text",
    ),
    (
        VERIFY,
        "組の前に数字があってもつなげる（`1234,567` が `1234567` になる）",
        r'THOUSANDS = re.compile(r"(?<![0-9,])',
        r'THOUSANDS = re.compile(r"',
    ),
    (
        VERIFY,
        "組の後ろに数字があってもつなげる（`1,2345` が `12345` になる）",
        r'(?:,[0-9]{3})+(?![0-9])")',
        r'(?:,[0-9]{3})+")',
    ),
    # ---------------------------------------------------------------- 判定
    (
        VERIFY,
        "本文が無いのに照合する（M8）",
        "    if not body:",
        "    if False:",
    ),
    (
        VERIFY,
        "**主張0件を照合済みにする**（M9）",
        "    if not asserted:\n        verdict = UNVERIFIABLE",
        "    if False:\n        verdict = UNVERIFIABLE",
    ),
    (
        VERIFY,
        "**本文に無い主張を数えない**",
        "    missing = tuple(c for c in asserted if c not in found)",
        "    missing = ()",
    ),
    (
        VERIFY,
        "**引用で判定する**（U13：引用は証拠ではない）",
        "    elif missing:",
        "    elif missing or quotes_missing:",
    ),
    (
        VERIFY,
        "引用の全角をそろえない",
        "_clean(q) not in cleaned",
        "q not in cleaned",
    ),
    # ---------------------------------------------------------------- 件数
    (
        VERIFY,
        "要約の順を逆にする",
        "checks=tuple(_check(s) for s in summaries)",
        "checks=tuple(_check(s) for s in reversed(summaries))",
    ),
    (
        VERIFY,
        "**確認できなかったものを問題なしにする**",
        "        return all(c.verdict == CONFIRMED for c in self.checks)",
        "        return all(c.verdict != MISMATCH for c in self.checks)",
    ),
    (
        VERIFY,
        "判定ごとの件数を出さない",
        "        return dict(Counter(c.verdict for c in self.checks))",
        "        return {}",
    ),
    (
        VERIFY,
        "件数を出さずに「照合しました」とだけ言う",
        '            f"{len(self.checks)} 件中 {counts.get(CONFIRMED, 0)} 件を照合できた"',
        '            "照合しました"',
    ),
    (
        VERIFY,
        "主張の個数から、本文に無かったぶんを抜く",
        "        looked = found + sum(len(c.missing) for c in self.checks)",
        "        looked = found",
    ),
    (
        VERIFY,
        "**見ていないものを書かない**（数字と英語の語だけだと隠れる）",
        '            "（照合したのは数字と英語の語だけ。日本語の言い回しと、"',
        '            "（"',
    ),
    (
        VERIFY,
        "**承知で残した穴を書かない**（`3分` が `3分類` で裏付けられることが隠れる）",
        '            "単位の字が別の語の頭かは見ていない）"',
        '            "）"',
    ),
    # ---------------------------------------------------------------- U19（本物の初回で出た誤報）
    (
        VERIFY,
        "**数に直接続く英字を数ごとにしない**（`1M` が `1` と `M` に分かれ、本文の `1M` で外れる）",
        r'    r"|(?P<suffix>[A-Za-z]+)(?![A-Za-z0-9]))?"',
        r'    r"|(?P<suffix>(?!)))?"',
    ),
    (
        VERIFY,
        "数に続く英字の後ろの英数字を見ない（ハッシュの `3cb4f` から `3cb` を切り取る）",
        r'(?P<suffix>[A-Za-z]+)(?![A-Za-z0-9])',
        r'(?P<suffix>[A-Za-z]+)',
    ),
    (
        VERIFY,
        "抜いた英字を主張に付けない",
        '            unit = match["unit"] or match["suffix"] or ""',
        '            unit = match["unit"] or ""',
    ),
    (
        VERIFY,
        "**通貨記号を見ない**（`$250` が裸の `250` になる）",
        r'    r"(?:(?P<sign>[$¥])[ \t]*)?"',
        r'    r"(?P<sign>(?!))?"',
    ),
    (
        VERIFY,
        "通貨記号を単位にしない",
        '            if match["sign"] and not unit:',
        '            if False:',
    ),
    (
        VERIFY,
        "**通貨記号で英字の単位を上書きする**（`$250M` が `250ドル` になる）",
        '            if match["sign"] and not unit:',
        '            if match["sign"]:',
    ),
    (
        VERIFY,
        "ドルと円を取り違える",
        'CURRENCY = {"$": "ドル", "¥": "円"}',
        'CURRENCY = {"$": "円", "¥": "ドル"}',
    ),
    (
        VERIFY,
        "**本文の通貨記号で裏付けない**（`250ドル` が `$250` で外れる）",
        "            if sign:",
        "            if False:",
    ),
    (
        VERIFY,
        "**通貨記号の後ろの英字を見ない**（`250ドル` を `$250M` で裏付ける）",
        r'{re.escape(sign)}[ \t]*{bare}(?![A-Za-z]))"',
        r'{re.escape(sign)}[ \t]*{bare})"',
    ),
    # ================================================================ emit
    # 狙うのは **「書けたように見えて、読み戻すと違う」** と **「外の文字列が構造を壊す」**。
    # ---------------------------------------------------------------- 書く前に止める
    (
        EMIT,
        "**無い Inbox を黙って作る／素の例外にする**",
        "    if not inbox.is_dir():",
        "    if False:",
    ),
    (
        EMIT,
        "**ファイル名を実行日にする**（`today()` を使う実装）",
        'path = inbox / f"{at:%Y-%m-%d}-scout.md"',
        'path = inbox / f"{datetime.now():%Y-%m-%d}-scout.md"',
    ),
    (
        EMIT,
        "時差つきの日時を受け取る",
        "    if at.tzinfo is not None:",
        "    if False:",
    ),
    (
        EMIT,
        "run-id の `--` を通す（HTML コメントの中で壊れる）",
        '    if not RUN_ID.fullmatch(run_id) or "--" in run_id:',
        "    if not RUN_ID.fullmatch(run_id):",
    ),
    (
        EMIT,
        "run-id を何でも通す",
        'RUN_ID = re.compile(r"[0-9A-Za-z][0-9A-Za-z_.-]*")',
        'RUN_ID = re.compile(r".+")',
    ),
    (
        EMIT,
        "run-id を頭だけ見る",
        "    if not RUN_ID.fullmatch(run_id)",
        "    if not RUN_ID.match(run_id)",
    ),
    (
        EMIT,
        "**split と digest の突き合わせを外す**",
        '    _same("split の要約対象", split.summarize, "digest の結果", [*digest.done, *digest.failed])\n',
        "",
    ),
    (
        EMIT,
        "**件数だけ合わせる**（数が同じで中身が違うのを通す）",
        "    if a != b:",
        "    if sum(a.values()) != sum(b.values()):",
    ),
    (
        EMIT,
        "**digest と audit を件数だけ合わせる**（照合した要約と書く要約がずれる）",
        "    if tuple(c.summary for c in audit.checks) != digest.done:",
        "    if len(audit.checks) != len(digest.done):",
    ),
    (
        EMIT,
        "同じ記事が要約と見出しの両方に出ても止めない",
        "    if twice:",
        "    if False:",
    ),
    (
        EMIT,
        "**符号化できない字を通す**（開いてから落ちて空ファイルが残る）",
        '        return text.encode("utf-8")',
        '        return text.encode("utf-8", errors="surrogatepass")',
    ),
    (
        EMIT,
        "**使用済みの run-id でも書く**",
        "        if _section_start(old, run_id) >= 0:",
        "        if False:",
    ),
    (
        EMIT,
        "**節を部分一致で探す**（前の実行のタイトルに当たる）",
        "    match = re.search(pattern, _normalize(text), flags=re.MULTILINE)",
        "    match = re.search(re.escape(_label(run_id)), text)",
    ),
    (
        EMIT,
        "**CRLF のファイルで節を探せない**（使用済みの run-id を見落とす）",
        "    match = re.search(pattern, _normalize(text), flags=re.MULTILINE)",
        "    match = re.search(pattern, text, flags=re.MULTILINE)",
    ),
    # ---------------------------------------------------------------- 書き込み
    (
        EMIT,
        "新規ファイルに frontmatter を書かない",
        '        _write(path, "xb", _encode(_head(at) + block))',
        '        _write(path, "xb", _encode(block))',
    ),
    (
        EMIT,
        "**追記ではなく上書きする**",
        '        _write(path, "ab", data)',
        '        _write(path, "wb", data)',
    ),
    (
        EMIT,
        "節の頭の改行を外す（手で書かれた最終行に見出しが続く）",
        '        f"\\n## {at:%H:%M}',
        '        f"## {at:%H:%M}',
    ),
    (
        EMIT,
        "**BOM を本文として読む**（手で開いたファイルが誤報になる）",
        '    return raw.decode("utf-8-sig")',
        '    return raw.decode("utf-8")',
    ),
    (
        EMIT,
        "frontmatter の日付を外す",
        'date: {at:%Y-%m-%d}\\n---',
        'date: \\n---',
    ),
    # ---------------------------------------------------------------- 描く
    (
        EMIT,
        "見出しの時刻を秒にする",
        'f"\\n## {at:%H:%M} {_label(run_id)}\\n\\n"',
        'f"\\n## {at:%H:%S} {_label(run_id)}\\n\\n"',
    ),
    (
        EMIT,
        "**節の終わりの印を書かない**",
        '        + f"\\n{_end(run_id)}\\n"',
        '        + "\\n"',
    ),
    (
        EMIT,
        "段の文字列を1行に潰さない",
        '    lines = [f"- {_inline(stage)}" for stage in stages]',
        '    lines = [f"- {stage}" for stage in stages]',
    ),
    (
        EMIT,
        "要約できなかった件数に要約の件数を書く",
        "・要約できなかった {len(digest.failed)}",
        "・要約できなかった {len(digest.done)}",
    ),
    (
        EMIT,
        "要約した記事の URL を読み戻しの期待値に入れない",
        "        url = _url(scored.kept.article.url)\n        urls.append(url)\n",
        "        url = _url(scored.kept.article.url)\n",
    ),
    (
        EMIT,
        "失敗した記事を先頭に出す",
        "        entries.append(_failure(failure, url))",
        "        entries.insert(0, _failure(failure, url))",
    ),
    (
        EMIT,
        "**要約を引用にしない**（中の `## ` が節になる）",
        '    return "".join(f"> {_escape(line)}\\n" for line in text.splitlines())',
        '    return "".join(f"{_escape(line)}\\n" for line in text.splitlines())',
    ),
    (
        EMIT,
        "照合の判定名を取り違える",
        '    CONFIRMED: "照合できた",',
        '    CONFIRMED: "確認できない",',
    ),
    (
        EMIT,
        "**本文に無い主張を書かない**",
        "    if not marked:",
        "    if True:",
    ),
    (
        EMIT,
        "**本文に無い主張を逃がさない**（終わりの印と `[[` を偽造できる）",
        '    marked = "・".join(f"「{_inline(c)}」" for c in check.missing if _line(c))',
        '    marked = "・".join(f"「{c}」" for c in check.missing if _line(c))',
    ),
    (
        EMIT,
        "空の主張にも印を付ける",
        "for c in check.missing if _line(c))",
        "for c in check.missing)",
    ),
    (
        EMIT,
        "要約できなかった理由を書かない",
        "・要約できなかった（{_inline(failure.reason)}）",
        "・要約できなかった",
    ),
    (
        EMIT,
        "**点 0 を「点なし」にする**",
        '    score = "なし" if headline.score is None else str(headline.score)',
        '    score = "なし" if not headline.score else str(headline.score)',
    ),
    # ---------------------------------------------------------------- 外から来た文字列
    (
        EMIT,
        "タイトルの改行を残す",
        '    return " ".join(text.split())',
        "    return text",
    ),
    (
        EMIT,
        "**`[` を逃がさない**（`[[` が vault のリンクになる）",
        '.replace("[", "\\\\[")',
        "",
    ),
    (
        EMIT,
        "`]` を逃がさない（リンクの文字部分がそこで閉じる）",
        '.replace("]", "\\\\]")',
        "",
    ),
    (
        EMIT,
        "`\\` を逃がさない（末尾の `\\` が `]` を消す）",
        '    return text.replace("\\\\", "\\\\\\\\")',
        "    return text",
    ),
    (
        EMIT,
        "**`<` を逃がさない**（要約が節の終わりの印を偽造できる）",
        '.replace("<", "&lt;")',
        "",
    ),
    (
        EMIT,
        "URL の `(` を符号化しない",
        '"(": "%28"',
        '"(": "("',
    ),
    (
        EMIT,
        "URL の `\\` を符号化しない（`\\)` でリンクが閉じない）",
        '    "\\\\": "%5C", ',
        "    ",
    ),
    (
        EMIT,
        "**URL の `[` を符号化しない**（`[[` が vault のリンクになる）",
        '"[": "%5B", ',
        "",
    ),
    (
        EMIT,
        "URL の `` ` `` を符号化しない（タイトル側と組んで code span になる）",
        '"`": "%60",',
        "",
    ),
    (
        EMIT,
        "**http(s) かを見ない**（`ftp:` を通す）",
        '    if parts.scheme not in ("http", "https") or not parts.netloc:',
        "    if not parts.netloc:",
    ),
    (
        EMIT,
        "ホスト名の無い URL を通す",
        ' or not parts.netloc:\n        raise ValueError(f"http(s)',
        ':\n        raise ValueError(f"http(s)',
    ),
    (
        EMIT,
        "**URL の前後の空白を見ない**（urlsplit が黙って落とす）",
        "    if url != url.strip() or any(",
        "    if any(",
    ),
    (
        EMIT,
        "URL の制御文字を通す",
        'unicodedata.category(ch) == "Cc" or ',
        "",
    ),
    (
        EMIT,
        "URL の改行以外の空白（行区切りなど）を通す",
        ' or (ch.isspace() and ch != " ") for ch in url',
        " for ch in url",
    ),
    # ---------------------------------------------------------------- 読み戻し
    (
        EMIT,
        "**読み戻しの件数の期待値を、描いたものから取る**",
        "urls=urls, entries=split.total, day=",
        "urls=urls, entries=len(split.headline), day=",
    ),
    (
        EMIT,
        "**CRLF を正規化しない**（git が触ったファイルを毎回誤報にする）",
        '    return text.replace("\\r\\n", "\\n")',
        "    return text",
    ),
    (
        EMIT,
        "**書いたとおりかを見ない**（位置と件数だけ）",
        "    if times == 0:",
        "    if False:",
    ),
    (
        EMIT,
        "同じ節が2回あっても言わない",
        "    elif times > 1:",
        "    elif False:",
    ),
    (
        EMIT,
        "**記事の件数を見ない**",
        "    if len(headings) != entries:",
        "    if False:",
    ),
    (
        EMIT,
        "**URL があるかを見ない**",
        '    problems.extend(f"URL が無い: {url}" for url in sorted((Counter(urls) - linked).elements()))',
        "",
    ),
    (
        EMIT,
        "同じ URL が2件あっても1件で通す",
        "sorted((Counter(urls) - linked).elements())",
        "sorted((Counter(set(urls)) - linked).elements())",
    ),
    (
        EMIT,
        "**見出しの URL を控えめに取る**（タイトルに仕込んだ `](URL)` に当たる）",
        '_HEADING_URL = re.compile(r"^### \\[.*\\]\\((\\S*)\\)$")',
        '_HEADING_URL = re.compile(r"^### \\[.*?\\]\\((\\S*)\\)")',
    ),
    (
        EMIT,
        "frontmatter が先頭にあるかを見ない",
        '    if not text.startswith("---\\n"):',
        "    if False:",
    ),
    (
        EMIT,
        "frontmatter が閉じているかを見ない",
        "    if close < 0:",
        "    if False:",
    ),
    (
        EMIT,
        "frontmatter の tags を見ない",
        '    if not isinstance(tags, list) or "scout" not in tags:',
        "    if False:",
    ),
    (
        EMIT,
        "**frontmatter の date を見ない**（形が崩れても別の日でも通る）",
        "    if date != day:",
        "    if False:",
    ),
    (
        EMIT,
        "frontmatter の箇条書きを読まない",
        '        if line.startswith("  - ") and isinstance(fields.get(key), list):',
        "        if False:",
    ),
    (
        EMIT,
        "frontmatter の `[a, b]` 形を読まない（プロパティ画面で直すと誤報）",
        '            if value.startswith("[") and value.endswith("]"):',
        "            if False:",
    ),
    (
        EMIT,
        "frontmatter の引用符を外さない",
        "        return value[1:-1]",
        "        return value",
    ),
    (
        EMIT,
        "**読み戻しに問題があっても成功にする**",
        "        return not self.problems",
        "        return True",
    ),
    (
        EMIT,
        "新規と追記を取り違える",
        '        how = "新規" if self.created else "追記"',
        '        how = "追記" if self.created else "新規"',
    ),
    # ================================================================ notify
    # 狙うのは **「異常が正常として届く」** と **「送れたのに送れなかった／その逆」**。
    # ---------------------------------------------------------------- 判定
    (
        NOTIFY,
        "emitted と emit_error を両方受け取る",
        "    if (emitted is None) == (emit_error is None):",
        "    if False:",
    ),
    (
        NOTIFY,
        "**取得元が0個でも正常にする**（M9）",
        "    if not harvest.results:",
        "    if False:",
    ),
    (
        NOTIFY,
        "**取れなかった取得元を見ない**（M2）",
        "        if result.status == FAILED:",
        "        if False:",
    ),
    (
        NOTIFY,
        "取れなかった理由を書かない",
        "が取れなかった（{_line(result.detail)}）",
        "が取れなかった",
    ),
    (
        NOTIFY,
        "取りこぼしを見ない（H1・M1）",
        "        elif result.more:",
        "        elif False:",
    ),
    (
        NOTIFY,
        "**要約の失敗を見ない**",
        "    if digest.failed:",
        "    if False:",
    ),
    (
        NOTIFY,
        "要約の失敗の件数を書かない",
        'abnormal.append(f"要約できなかった {len(digest.failed)} 件")',
        'abnormal.append("要約できなかった")',
    ),
    (
        NOTIFY,
        "**書けなかった回を異常にしない**",
        '        abnormal.append("Inbox に書けなかった")',
        "        pass",
    ),
    (
        NOTIFY,
        "**読み戻しの問題を見ない**",
        "    elif not emitted.ok:",
        "    elif False:",
    ),
    (
        NOTIFY,
        "**照合できなかった要約を見ない**",
        "    if unconfirmed:",
        "    if False:",
    ),
    (
        NOTIFY,
        "本文に無い主張だけを数える（確認できないを数えない）",
        "c.verdict != CONFIRMED",
        "c.verdict == \"mismatch\"",  # import していない名前を使うと NameError で偽の kill になる
    ),
    (
        NOTIFY,
        "**注意を異常より強くする**",
        "    level = ABNORMAL if abnormal else ATTENTION if attention else NORMAL",
        "    level = ATTENTION if attention else ABNORMAL if abnormal else NORMAL",
    ),
    (
        NOTIFY,
        "注意の理由を落とす",
        "    return Health(level=level, reasons=(*abnormal, *attention))",
        "    return Health(level=level, reasons=tuple(abnormal) or tuple(attention))",
    ),
    # ---------------------------------------------------------------- 本文
    (
        NOTIFY,
        "状態の名前を取り違える",
        'LABELS = {NORMAL: "正常", ATTENTION: "注意", ABNORMAL: "異常"}',
        'LABELS = {NORMAL: "正常", ATTENTION: "正常", ABNORMAL: "異常"}',
    ),
    (
        NOTIFY,
        "1行目に時刻を書かない",
        '｜{at:%Y-%m-%d %H:%M}"',
        '"',
    ),
    (
        NOTIFY,
        "run-id を書かない",
        ', f"run {run_id}"]',
        "]",
    ),
    (
        NOTIFY,
        "**理由を本文に書かない**",
        '        lines.extend(f"・{_line(r)}" for r in health.reasons)',
        "        pass",
    ),
    (
        NOTIFY,
        "段の行を書かない",
        '    lines.extend(f"- {_line(s)}" for s in stages)',
        "    pass",
    ),
    (
        NOTIFY,
        "段の行を1行に潰さない",
        'f"- {_line(s)}"',
        'f"- {s}"',
    ),
    (
        NOTIFY,
        "**書けなかった回に理由を書かない**",
        "Inbox に書けなかった: {_line(emit_error or '')}",
        "Inbox に書けなかった",
    ),
    (
        NOTIFY,
        "書けた回に emit の結果を書かない",
        "        lines.append(_line(emitted.summary))",
        "        pass",
    ),
    # ---------------------------------------------------------------- 再送キー
    (
        NOTIFY,
        "**再送キーに本文を混ぜない**（同じキーで中身を変える）",
        "json.dumps([run_id, body], ensure_ascii=False)",
        "json.dumps([run_id], ensure_ascii=False)",
    ),
    (
        NOTIFY,
        "再送キーの区切りを外す（`r1`+`2本文` と `r12`+`本文` がぶつかる）",
        "json.dumps([run_id, body], ensure_ascii=False)",
        "run_id + body",
    ),
    (
        NOTIFY,
        "再送キーを改行で区切る（改行を含む run-id でぶつかる）",
        "json.dumps([run_id, body], ensure_ascii=False)",
        'run_id + chr(10) + body',
    ),
    (
        NOTIFY,
        "**再送キーを毎回ばらばらにする**（送り直しが二重送信になる）",
        "    return str(uuid.uuid5(RETRY_NAMESPACE,",
        "    return str(uuid.uuid4()) or str(uuid.uuid5(RETRY_NAMESPACE,",
    ),
    (
        NOTIFY,
        "**再送キーを付けない**",
        '        headers={"X-Line-Retry-Key": retry_key(run_id, body)},',
        "        headers={},",
    ),
    # ---------------------------------------------------------------- 送る
    (
        NOTIFY,
        "**通数が読めないと送らない**（無音になる）",
        "        return None\n\n\ndef _header",
        "        raise\n\n\ndef _header",
    ),
    (
        NOTIFY,
        "**409 を全部「送ってあった」にする**",
        "    duplicate = response.status_code == 409 and bool(accepted)",
        "    duplicate = response.status_code == 409",
    ),
    (
        NOTIFY,
        "**409 を失敗にする**（送ってあったのに異常で終わる）",
        "    duplicate = response.status_code == 409 and bool(accepted)",
        "    duplicate = False",
    ),
    (
        NOTIFY,
        "**エラーの応答を見ない**",
        "    if not duplicate:\n        line_auth.raise_for_line_error(response, *secrets)",
        "    if False:\n        line_auth.raise_for_line_error(response, *secrets)",
    ),
    (
        NOTIFY,
        "**秘密を伏せずにエラーにする**",
        "        line_auth.raise_for_line_error(response, *secrets)",
        "        line_auth.raise_for_line_error(response)",
    ),
    (
        NOTIFY,
        "ヘッダ名の大小を区別する",
        "        if str(key).lower() == name:",
        "        if str(key) == name.title():",
    ),
    (
        NOTIFY,
        "受け付け済みの ID を記録しない",
        "        accepted_request_id=accepted,",
        '        accepted_request_id="",',
    ),
    (
        NOTIFY,
        "**宛先を伏せずに記録する**",
        "        to_masked=line_send.mask_destination(to),",
        "        to_masked=to,",
    ),
    (
        NOTIFY,
        "送った後の通数を、送る前の値にする",
        "        usage_after=usage_after,",
        "        usage_after=usage_before,",
    ),
    (
        NOTIFY,
        "**読めなかった通数を 0 にする**",
        '    return "読めず" if value is None else str(value)',
        '    return "0" if value is None else str(value)',
    ),
    (
        NOTIFY,
        "**知らない状態を正常に倒す**",
        "        elif result.status not in (OK, EMPTY):",
        "        elif False:",
    ),
    (
        NOTIFY,
        "**照合が要約を覆っていなくても見ない**",
        "    if len(audit.checks) != len(digest.done):",
        "    if False:",
    ),
    (
        NOTIFY,
        "**本文の鍵を伏せずに送る**",
        "    body = line_auth.redact(body, *secrets)\n",
        "",
    ),
    (
        NOTIFY,
        "**push の通信例外を素のまま出す**（呼び出し側は LineError だけを待っている）",
        "    except requests.RequestException as e:\n        # 応答が返らない失敗。",
        "    except ZeroDivisionError as e:\n        # 応答が返らない失敗。",
    ),
    (
        NOTIFY,
        "push の通信例外の文言を伏せない",
        '            line_auth.redact(f"LINE へ送れなかった（通信）: {type(e).__name__}: {e}", *secrets)',
        '            f"LINE へ送れなかった（通信）: {type(e).__name__}: {e}"',
    ),
    (
        NOTIFY,
        "**通数の取得で通信例外を握らない**（送らないまま／送ったのに失敗で終わる）",
        "    except (line_send.SendError, line_auth.LineError, requests.RequestException):",
        "    except (line_send.SendError, line_auth.LineError):",
    ),
    (
        NOTIFY,
        "送ってあったと送ったを取り違える",
        "            if self.duplicate\n",
        "            if not self.duplicate\n",
    ),
    # ================================================================ cli
    # 狙うのは **「外へ出てから設定の誤りに気づく」「台帳が嘘をつく」「失敗が無音になる」**。
    # ---------------------------------------------------------------- 設定
    (
        CLI,
        "**知らない鍵を黙って捨てる**",
        "    if unknown:\n        raise ConfigError",
        "    if False:\n        raise ConfigError",
    ),
    (
        CLI,
        "summarizable が空でも通す",
        "    if not summarizable:\n",
        "    if False:\n",
    ),
    (
        CLI,
        "**summarizable に取得元に無い名前があっても通す**",
        "    if not summarizable <= names:",
        "    if False:",
    ),
    (
        CLI,
        "model が空でも通す",
        "    if not isinstance(model, str) or not model.strip():",
        "    if False:",
    ),
    (
        CLI,
        "**Inbox が無くても通す**",
        "    if not where.is_dir():",
        "    if False:",
    ),
    (
        CLI,
        "--inbox の差し替えを無視する",
        "    where = inbox if inbox is not None else _relative(path, data.get(\"inbox\"))",
        "    where = _relative(path, data.get(\"inbox\"))",
    ),
    (
        CLI,
        "Inbox を実行した場所から解決する",
        "(config_path.parent / raw).resolve()",
        "raw.resolve()",
    ),
    (
        CLI,
        "**max_calls が cap より小さくても通す**（途中で落ちて通知が届かない）",
        "    if max_calls < profile.cap:",
        "    if False:",
    ),
    (
        CLI,
        "取得元が0件でも通す",
        "    if not isinstance(value, list) or not value:\n        raise ConfigError(\"[[sources]]",
        "    if not isinstance(value, list):\n        raise ConfigError(\"[[sources]]",
    ),
    (
        CLI,
        "知らない種類の取得元を通す",
        "        if kind not in _KINDS:",
        "        if False:",
    ),
    (
        CLI,
        "**Qiita の limit が上限を超えても通す**（fetch が黙って丸める）",
        "        if kind == fetch.QIITA and limit > fetch.QIITA_MAX_PER_PAGE:",
        "        if False:",
    ),
    (
        CLI,
        "取得元の名前の重複を通す",
        "    if len(set(names)) != len(names):",
        "    if False:",
    ),
    (
        CLI,
        "一覧でない値を通す",
        "    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):",
        "    if not isinstance(value, (list, str)):",
    ),
    (
        CLI,
        "空の文字列を通す",
        "    if not isinstance(value, str) or not value.strip():\n        raise ConfigError(f\"{where} が空\")",
        "    if not isinstance(value, str):\n        raise ConfigError(f\"{where} が空\")",
    ),
    (
        CLI,
        "**bool を正の整数として通す**",
        "    if isinstance(value, bool) or not isinstance(value, int) or value < 1:",
        "    if not isinstance(value, int) or value < 1:",
    ),
    (
        CLI,
        "0 を正の整数として通す",
        "    if isinstance(value, bool) or not isinstance(value, int) or value < 1:",
        "    if isinstance(value, bool) or not isinstance(value, int) or value < 0:",
    ),
    (
        CLI,
        "壊れた TOML を素の例外で出す",
        "    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as e:",
        "    except (OSError, UnicodeDecodeError) as e:",
    ),
    (
        CLI,
        "BOM 付きの設定を読めない",
        "tomllib.loads(path.read_text(encoding=\"utf-8-sig\"))",
        "tomllib.loads(path.read_text(encoding=\"utf-8\"))",
    ),
    # ---------------------------------------------------------------- 台帳
    (
        CLI,
        "**壊れた台帳を空として続ける**（見た記事を全部もう一度送る）",
        "        raise StateError(f\"台帳を読めない（{path}）: {e}\") from e",
        "        return State(seen=frozenset(), last_run=None)",
    ),
    (
        CLI,
        "台帳の seen の形を見ない",
        "    if not isinstance(seen, list) or not all(isinstance(k, str) for k in seen):",
        "    if False:",
    ),
    (
        CLI,
        "台帳が表でなくても通す",
        "    if not isinstance(data, dict):\n        raise StateError",
        "    if False:\n        raise StateError",
    ),
    (
        CLI,
        "BOM 付きの台帳を読めない",
        "json.loads(path.read_text(encoding=\"utf-8-sig\"))",
        "json.loads(path.read_text(encoding=\"utf-8\"))",
    ),
    (
        CLI,
        "**一時ファイルを残す**",
        "        if temporary.exists():\n            temporary.unlink()",
        "        if False:\n            temporary.unlink()",
    ),
    (
        CLI,
        "**書く直前に読み直さない**（その間に他の実行が書いた分を消す）",
        "    seen = state.seen | (load_state(path).seen if path.is_file() else frozenset())",
        "    seen = state.seen",
    ),
    # ---------------------------------------------------------------- 外へ出る前に止める
    (
        CLI,
        "**初回に --since が無くても進む**",
        "    if since is None:\n        _say(",
        "    if False:\n        _say(",
    ),
    (
        CLI,
        "**--since より台帳を優先する**",
        "        since = date.fromisoformat(args.since) if args.since is not None else state.last_run",
        "        since = state.last_run or date.fromisoformat(args.since)",
    ),
    (
        CLI,
        "未来の --since を通す（毎回0件で正常になる）",
        "    if since > at.date():",
        "    if False:",
    ),
    (
        CLI,
        "**時刻を2回取る**（送り直しで時刻が変わり2通になる）",
        '    run_id = f"{at:%Y%m%d-%H%M%S}"',
        '    run_id = f"{now():%Y%m%d-%H%M%S}"',
    ),
    (
        CLI,
        "**--dry-run でも鍵を読んで接続する**",
        "(config, remote=not args.dry_run)",
        "(config, remote=True)",
    ),
    (
        CLI,
        "**.env が無いと traceback で落ちる**（終了コード 2 にならない）",
        "env_file.EnvFileError, OSError, ValueError) as e:",
        "OSError) as e:",
    ),
    (
        CLI,
        "**--dry-run でも先へ進む**",
        "    if args.dry_run:\n        _say(",
        "    if False:\n        _say(",
    ),
    # ---------------------------------------------------------------- 台帳を進める条件
    (
        CLI,
        "**--inbox の試しで本番の台帳を進める**",
        "    if args.inbox is not None:",
        "    if False:",
    ),
    (
        CLI,
        "**書けなかった回も記事を「見た」にする**（M4）",
        "    elif emitted is None or not emitted.ok:",
        "    elif False:",
    ),
    (
        CLI,
        "**読み戻しに問題があっても「見た」にする**",
        "    elif emitted is None or not emitted.ok:",
        "    elif emitted is None:",
    ),
    (
        CLI,
        "**取れなかった取得元がある回も日付を進める**（その日を飛ばす）",
        "        advance = harvest.status != fetch.FAILED and not",
        "        advance = True and not",
    ),
    (
        CLI,
        "**まだ先がある回も日付を進める**（残りを二度と取れない）",
        " and not any(r.more for r in harvest.results)",
        "",
    ),
    (
        CLI,
        "見出しだけの記事を「見た」にしない",
        " | {h.kept.article.url for h in parts.headline}",
        "",
    ),
    (
        CLI,
        "**正規化した値を台帳に残す**（`%26` の URL が既読にならない）",
        "{s.kept.article.url for s in parts.summarize}",
        "{s.kept.key for s in parts.summarize}",
    ),
    (
        CLI,
        "**台帳の保存の失敗を黙る**",
        "            ledger_failed = True\n",
        "",
    ),
    # ---------------------------------------------------------------- 秘密・通知・終了コード
    (
        CLI,
        "**段の行を伏せずに足す**（鍵が Inbox のノートに入る）",
        "        self.lines.append(self.hide(line))",
        "        self.lines.append(line)",
    ),
    (
        CLI,
        "**画面に出す本文を伏せない**（理由の行は伏せる前のまま入る）",
        "    _say(stages.hide(body))",
        "    _say(body)",
    ),
    (
        CLI,
        "**emit の例外を外側に任せる**（具体的な理由が通知に届かない）",
        "    except (ValueError, OSError) as e:\n        # **書けなかった回も送る。**",
        "    except ZeroDivisionError as e:\n        # **書けなかった回も送る。**",
    ),
    (
        CLI,
        "**想定外の例外で通知まで届かない**",
        "    except Exception as e:  # noqa: BLE001 - **想定外の例外でも無音にしない**（M7）",
        "    except ZeroDivisionError as e:  # noqa: BLE001",
    ),
    (
        CLI,
        "**--no-notify でも送る**",
        "    if not args.no_notify:",
        "    if True:",
    ),
    (
        CLI,
        "**LINE に送れなかったのに 0 で終わる**",
        "            sent = False\n",
        "",
    ),
    (
        CLI,
        "**異常でも 0 で終わる**",
        "    if result.health.level == notify.ABNORMAL or not sent or result.ledger_failed:",
        "    if not sent or result.ledger_failed:",
    ),
    (
        CLI,
        "**注意でも 1 で終わる**（毎日赤くなる）",
        "    if result.health.level == notify.ABNORMAL or not sent or result.ledger_failed:",
        "    if result.health.level != notify.NORMAL or not sent or result.ledger_failed:",
    ),
    (
        CLI,
        "**画面に書けない字で落ちる**（送信や終了コードまで届かない）",
        "    except UnicodeEncodeError:\n        encoding =",
        "    except UnicodeEncodeError:\n        raise\n        encoding =",
    ),
    # ---------------------------------------------------------------- LINE の上限
    (
        CLI,
        "**長い本文を切らない**（いちばん要る異常のときに 400 で落ちる）",
        "    if _utf16(text) <= limit:\n        return text",
        "    if True:\n        return text",
    ),
    (
        CLI,
        "**字数を UTF-16 で数えない**（サロゲートペアで上限を超える）",
        "        used += _utf16(ch)",
        "        used += 1",
    ),
    (
        CLI,
        "省いたことを書かない",
        "    return head + note.format(len(text) - len(head))",
        "    return head",
    ),
    # ---------------------------------------------------------------- 本物の接続の部品
    (
        CLI,
        "**timeout を足さない**",
        "    def get(self, url: str, **kwargs: Any) -> Any:\n        kwargs.setdefault(\"timeout\", self._timeout)",
        "    def get(self, url: str, **kwargs: Any) -> Any:\n        pass",
    ),
    (
        CLI,
        "post に timeout を足さない",
        "    def post(self, url: str, **kwargs: Any) -> Any:\n        kwargs.setdefault(\"timeout\", self._timeout)",
        "    def post(self, url: str, **kwargs: Any) -> Any:\n        pass",
    ),
    (
        CLI,
        "明示した timeout を上書きする",
        "    def get(self, url: str, **kwargs: Any) -> Any:\n        kwargs.setdefault(\"timeout\", self._timeout)",
        "    def get(self, url: str, **kwargs: Any) -> Any:\n        kwargs[\"timeout\"] = self._timeout",
    ),
    (
        CLI,
        "**取得に timeout を付けない**",
        "    reply = requests.get(url, timeout=TIMEOUT, headers=",
        "    reply = requests.get(url, headers=",
    ),
    (
        CLI,
        "**--dry-run でない回に鍵を読まない**",
        "    if not remote:\n        return Connections(",
        "    if True:\n        return Connections(",
    ),
    (
        CLI,
        "LINE の session を timeout で包まない",
        "        session=TimeoutSession(line_auth.build_session(token), timeout=TIMEOUT),",
        "        session=line_auth.build_session(token),",
    ),
    (
        CLI,
        "鍵を伏せ字の一覧に入れない",
        "        secrets=(token, api_key),",
        "        secrets=(token,),",
    ),
    (
        CLI,
        "要約の型を渡さない",
        "client, prompt=prompt, schema=summarize.SCHEMA, model=model, api_key=api_key",
        "client, prompt=prompt, schema=None, model=model, api_key=api_key",
    ),
    (
        CLI,
        "設定のモデルを使わない",
        "client, prompt=prompt, schema=summarize.SCHEMA, model=model, api_key=api_key",
        "client, prompt=prompt, schema=summarize.SCHEMA, api_key=api_key",
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
