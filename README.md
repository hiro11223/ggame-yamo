# yamo — 実況ネタ探索サイト ジェネレータ

Steam と itch.io から実況向けのゲーム（ホラー / バカゲー など）を集め、
おすすめ順と配信可否つきの一覧ページを作ります。GitHub Actions で週2回自動更新し、GitHub Pages で公開します。

- サーバーや外部DBは使いません。静的な HTML と JS だけです
- 検索エンジンに載らないよう、全ページに `noindex, nofollow` を入れ、`robots.txt` で全体を Disallow にしています
- 要件は [doc/REQUIREMENTS.md](doc/REQUIREMENTS.md)、設計は [doc/PLAN.md](doc/PLAN.md) を参照してください

## セットアップ

### 1. GitHub Pages を有効にする

リポジトリの **Settings → Pages** で、**Build and deployment → Source** を **GitHub Actions** にします。

### 2. Actions の権限を設定する

**Settings → Actions → General → Workflow permissions** で **Read and write permissions** を選んで保存します。
状態（キャッシュや前回の一覧）を `data` ブランチにコミットするために必要です。

### 3. 初回を手動で実行する

**Actions** タブ → 左の **update** → **Run workflow** を押します。

- 実行には数分かかります（Steam から各カテゴリを全件取得します）
- 初回は `data` ブランチが自動で作られます
- 完了すると、実行結果の **deploy** ジョブにページの URL が表示されます

以降は、月曜と木曜の 12:17（日本時間）に自動で実行されます。

## 手動実行のしかた

- **Actions → update → Run workflow** を押します
- `max_new` には、公式サイトURL など作品ごとに取得する件数の上限を指定できます。空欄なら config.yaml の値です
- コマンドラインからは `gh workflow run update.yml` でも実行できます

## 失敗したとき

| 状況 | ジョブ | ページ |
|---|---|---|
| すべてのソースで取得に失敗 | **失敗**（GitHub から通知メールが届きます） | 前回のデータで表示し、「前回データ」と注記 |
| 一部のソースだけ失敗 | 成功 | ヘッダーに「一部失敗」と詳細を表示 |
| 正常 | 成功 | — |

実行ログは Actions の各実行ページで確認できます。

## config.yaml の書き方

GitHub の Web 画面で編集し、コミットすれば、次回の実行から反映されます。
設定ミス（未知のキー、範囲外の値）があると、実行時にエラーで止まります。

### カテゴリを追加する

`categories:` の下に追加します。キー（例 `horror`）は半角英小文字・数字・`_`・`-` のみ使えます。`all` は予約語なので使えません。

```yaml
categories:
  horror:
    label: ホラー                     # タブに表示する名前
    steam:
      require_any: [Horror, Psychological Horror, Survival Horror]  # どれか1つを持つ作品を集める
      exclude:     [Sexual Content, Nudity]                         # 1つでも持つ作品は除外
      bonus:       { Short: 5, Online Co-Op: 5 }                    # 持っていれば加点
    itch: { tags: [horror] }         # itch.io のタグ（URL の tag-○○ の部分）
    filters:
      max_price_jpy: 2000            # 価格の上限（円）
      min_positive_ratio: 0.80       # 好評率の下限
      reviews: { min: 30, max: 5000 }   # レビュー数の範囲（多すぎ＝もう知られている）
      require_demo: false            # true ならデモのある作品だけ
    weights:
      rating: 0.4                    # 好評率の重み
      volume: 0.3                    # レビュー数の「ちょうど良さ」の重み
      recency: 0.3                   # 新しさの重み
      volume_sweet_spot: 150         # このレビュー数で volume が最大になる（少なめ＝出たばかりの作品が有利）
      recency_half_life_years: 1.0   # 発売からこの年数で「新しさ」が半分になる（省略時 2.0）
    max_items: 1000                  # スコア上位この件数までを掲載
```

- 1つの作品が複数のカテゴリに入ることがあります
- タグ名は Steam のストアに表示される英語名です。実在しないタグを書くと、ページの「警告」に表示されます
- タグが実在するかは、手元で `python -m yamo check-tags` を実行しても確かめられます

### itch.io の設定

```yaml
itch:
  retention_days: 120                  # 最後に RSS で見かけてからこの日数で一覧から外す
  feeds: [newest, popular, top_rated]  # 取得するフィード（新着 / 人気 / 高評価）
  pages: 1                             # フィードごとのページ数（1ページ36件）
  max_items: 100                       # カテゴリごとに掲載する itch 作品の上限（Steam とは別枠）

itch_scoring:                          # itch はレビュー数がないので別基準
  recency: 0.5                         # 新しさの重み
  price: 0.2                           # 安さの重み
  tag_match: 0.3                       # カテゴリのタグにいくつ当てはまるか
  recency_half_life_days: 180          # この日数で「新しさ」が半分になる
  feed_bonus: { top_rated: 25, popular: 15 } # 人気・高評価フィードに載っていれば加点
```

- RSS は新しい作品から36件ずつしか返さないので、実行のたびに見つけた作品を蓄積していきます
- itch の作品は対応言語が分からないため、言語の条件（`languages_any`）は適用しません
- 価格は RSS の通貨を見て、`currency.to_jpy` の換算レートで円にします。レートのない通貨は「価格不明」として扱い、警告を出します
- Steam にも同じ作品がある場合（タイトルが一致し、開発者名と作者名が近い場合）は、Steam 側の行にまとめ、itch.io へのリンクを付けます

### 共通の条件

```yaml
defaults:
  languages_any: [japanese, english]   # どれかに対応している作品だけ
  release_after: "2015-01-01"          # これより前に発売された作品は除外
  include_coming_soon: false           # 未発売の作品を含めるか
```

### 取得の設定

```yaml
fetch:
  steam_interval_sec: 2.0   # Steam API へのリクエスト間隔（秒）
  max_new_fetch: 300        # 1回の実行で作品ごとに取得する件数の上限
```

## 配信可否の自動判定

掲載作品について、次のページの文言から配信・実況・収益化の可否を推定します。

- Steam のストア説明文と法的表記
- Steam に登録された公式サイトと、その `/press` `/presskit` `/press-kit` `/streaming` `/faq`
- itch.io のゲームページ本文

| 表示 | 意味 |
|---|---|
| ✅ OK | 配信系の語と許可の表現が**同じ文の中に**ある（例: 「Feel free to stream」「配信OK」） |
| ⚠️ 条件付き | 許可を取る必要がある、収益化は不可、クレジット表記が必要、など |
| ❌ NG | 禁止が明記されている |
| ❓ 不明 | 上記以外すべて。根拠が見つからなければ OK にはしません |

- 自動判定はあくまで**候補**です。ページ上で「自動判定」と表示し、根拠（URL・200字以内の抜粋・判定日時）を出します
- ページの全文は保存しません
- 判定から30日たつと再判定します。再判定が追いついていない作品には「要再確認」と表示します
- 1回の実行で判定するのは、スコアの高い順に `fetch.max_streaming_checks` 件（既定150件）までです
- 公式サイトの robots.txt を守り、SNS やリンク集のホスト（`streaming.excluded_hosts`）には行きません
- 配信可否について書いていない作品が多いため、「不明」が多くなります。確認できた作品は overrides.yaml で確定させてください
- 判定の変化は data ブランチの `verdict_history.jsonl` に記録されます

## overrides.yaml の書き方

配信可否を手動で確定させるファイルです。自動判定より優先され、ページには「手動確認済み」と表示されます。
手動で確定した作品は、自動判定の巡回もしなくなります。

### 編集のしかた

1. ページで作品を探し、タイトルの下に小さく出ている **作品ID**（例 `steam:620`、`itch:作者/スラッグ`）をコピーします
2. GitHub でリポジトリの `overrides.yaml` を開き、鉛筆アイコン（Edit）を押します
3. 次の形式で追記して、**Commit changes** を押します
4. 次回の定期実行（または手動実行）でページに反映されます

```yaml
- id: steam:620                  # 作品ID（ページに表示されているもの）
  verdict: conditional           # ok | conditional | ng | unknown
  note: 収益化はクレジット表記が条件   # 任意。ページの「根拠」に表示される（500字まで）
  evidence_url: https://example.com/streaming-policy   # 任意。http(s) の URL
  checked_on: 2026-11-10         # 確認した日（YYYY-MM-DD）
```

| verdict | 表示 |
|---|---|
| `ok` | ✅ OK |
| `conditional` | ⚠️ 条件付き |
| `ng` | ❌ NG |
| `unknown` | ❓ 不明 |

- 書き間違いのある項目は**その項目だけ無視**され、理由がページの「警告」に表示されます（更新全体は止まりません）
- 同じ ID を2回書いた場合は、後に書いた方が使われます
- 確認日から30日たつと「要再確認」と表示されます。確認し直したら `checked_on` を更新してください
- Steam と itch.io で統合された作品は、どちらの ID で書いても反映されます
- 掲載中の作品に当たらない ID は、ページの「配信可否チェック → 詳細」に一覧されます（掲載されれば反映されます）

## 手元で動かす

Python 3.12（3.11 でも動きます）が必要です。

```bash
python -m venv .venv
.venv/Scripts/activate        # macOS / Linux は source .venv/bin/activate
pip install -e ".[dev]"

python -m yamo run --state-dir state --out site --max-new 20   # site/index.html ができる
python -m yamo check-tags                                      # config のタグが実在するか
python -m pytest                                               # テスト（JS のテストには Node が必要）
```

`state/` は手元用の状態ディレクトリです。GitHub Actions では `data` ブランチがこの役割を持ちます。

## データソース

| ソース | 用途 |
|---|---|
| Steam IStoreQueryService/Query | タグでの候補収集と詳細（価格・言語・レビュー・デモ・タグ） |
| Steam IStoreBrowseService/GetItems | 日本語の説明文 |
| Steam appdetails | 公式サイトURL |
| itch.io タグ別RSS（新着 / 人気 / 高評価） | 候補収集（タイトル・価格・画像・説明・公開日） |

いずれも API キーは不要です。リクエストには、ツール名とこのリポジトリの URL を User-Agent として付けています。
