# lesson-5-1

Section 5-1「自分のためのツール」／5-1-1 実務プロジェクト（実践編）のリポジトリ。

Section 4-3 の [`lesson-4-3-2`](https://github.com/qwerrin/lesson-4-3-2) とは別リポジトリにしてある。
あちらは提出済みなので触らない。

| 課題 | 場所 | 状態 |
|---|---|---|
| 1 Slack の情報を取得・要約して LINE に送るツール | [`task1/`](task1/README.md) | **2026-09-06 合格。** 実装・テスト・実機確認・発展（スレッドの返信も読む）まで完了 |
| 2 EC サイト → Google スプレッドシート | [`task2/DESIGN.md`](task2/DESIGN.md) | **設計中。** 実装前に「何を見ていないか」を出した段階。コードはまだ0行。次は楽天APIの疎通確認1発 |
| 3 Google Meet 録音 → 議事録 → Google ドキュメント | — | 未着手 |

## 構成

```
common/          課題をまたいで使う部品
  env_file.py      .env を読む（os.environ とは混ぜない）
  slack_auth.py    Slack Bot Token
  line_auth.py     LINE チャネルアクセストークン＋送信前の確認
  line_send.py     LINE への送信（課題9から格上げ）
  gemini_client.py Gemini（この Section で新規）
task1/           課題1
```

### `common/line_send.py` は課題9からの格上げ

Section 4-3 の課題10 では `import send_push` と書いて、課題9の実装を1行も変えずに使った。
提出済みのコードに手を入れずに機能を足せるうえ、課題9側が直れば自動で効く。

Section 5-1 はリポジトリが分かれるのでその import は使えない。そこで**送信のコアだけ**を
`common/` に置き、CLI の皮（引数解析・記録の組み立て・画面表示）は課題ごとに書く。
皮は課題ごとに違うが、`build_payload` / `read_send_result` / `push` / `fetch_usage` /
`mask_destination` はどの課題でも同じだからである。

**テストも課題9から移した。** 期待値を新しい実装から作り直すと、
「実装がこうなっているから、こう期待する」になり、実機で1度通った知識が失われる。

## 資格情報

**3つとも `.env`**（`SLACK_BOT_TOKEN` / `GEMINI_API_KEY` / `LINE_CHANNEL_ACCESS_TOKEN` / `LINE_USER_ID`）。
環境変数は使わない。理由は [`.env.example`](.env.example) の冒頭に書いてある——
要約すると、**環境変数は共有の名前空間**で、課題用に置いた1本が
同じ PC の別プロセスの起動条件に化けて2日間そのシステムを止めた実績があるため。

```powershell
copy .env.example .env
# 値を埋める
```

## セットアップ

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

## テスト

```powershell
.venv\Scripts\python.exe -m pytest task1\tests common\tests -q
```
