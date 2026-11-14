# 実装計画（案）

対象: [REQUIREMENTS.md](REQUIREMENTS.md) v3
状態: **承認済み**（2026-11-15。A〜E は推奨案、追加指示を 3・5・7 章に反映）

---

## 0. 先に決めてほしいこと

推奨案で進めてよければ「承認」だけで大丈夫です。

| # | 論点 | 推奨案 |
|---|---|---|
| A | 価格の通貨 | Steamは `cc=jp` で円価格を取得します。itchはUSDなので、configの固定レート（例: 150）で円に換算して条件を判定します |
| B | `without permission` / `要許可` の扱い | **⚠️ 条件付き**にします（事前申請の一種とみなす）。`prohibited` / `not allowed` / `禁止` は ❌ NG |
| C | 「Atmospheric」「Short」のような広いタグ | これらだけで該当にすると、ホラー以外も大量に入ります。そこでタグを3種類に分けます。**候補収集用 `collect`**、**必須（どれか1つ）`require_any`**、**加点用 `bonus`** |
| D | robots.txt | プロジェクトPages（`user.github.io/repo/`）では、サブパスの robots.txt はクローラーに無視されます。配置はしますが、効くのは実質 `noindex` メタだけです |
| E | パッケージ名 | `yamo`（実行は `python -m yamo ...`）。変更できます |

---

## 1. ディレクトリ構成

```
.
├── pyproject.toml            # 依存とパッケージ定義（pip install . / pip install -e .[dev]）
├── config.yaml
├── overrides.yaml
├── README.md
├── src/yamo/
│   ├── __main__.py           # CLI: run / check-tags / render
│   ├── config.py             # pydantic で config・overrides を検証
│   ├── http.py               # 共通HTTP: UA付与、ホスト別レート制御、429/5xx指数バックオフ
│   ├── state.py              # state/ 配下JSONの読み書き、TTL判定
│   ├── models.py             # Game, Verdict, SourceStatus
│   ├── sources/
│   │   ├── steamspy.py       # タグ別一覧（1req/分）
│   │   ├── steam_store.py    # GetItems（バッチ）/ appdetails（公式サイトのみ）/ タグ一覧
│   │   └── itch.py           # タグ別RSS
│   ├── merge.py              # タイトル正規化・Steam/itch統合
│   ├── filters.py
│   ├── scoring.py
│   ├── streaming/
│   │   ├── fetch.py          # 探索範囲のページ取得→テキスト化（robots.txt尊重、サイズ/時間制限）
│   │   ├── classify.py       # 文言→判定（純関数・単体テスト対象）
│   │   └── overrides.py
│   ├── render.py             # Jinja2でHTML、data.json出力
│   └── web/                  # index.html.j2, app.js, style.css
├── tests/
│   ├── fixtures/             # steamspy/GetItems/appdetails JSON、itch RSS、配信規約文言サンプル
│   └── test_*.py
└── .github/workflows/
    ├── update.yml            # 定期実行＋Pagesデプロイ
    └── ci.yml                # push/PR時のpytest（任意）
```

`state/` は data ブランチの worktree、`site/` は生成物です。どちらも main では `.gitignore` に入れます。

ローカルでも同じ `python -m yamo run --state-dir state --out site` で動かします。オプションとして `--max-new 20` と `--sources steam` を用意します。

## 2. ライブラリ

| 用途 | ライブラリ |
|---|---|
| HTTP | `requests`（バックオフは自前で数十行。依存を増やさない） |
| 設定 | `PyYAML` + `pydantic` v2（設定ミスのエラーメッセージが分かりやすい） |
| HTML生成 | `Jinja2` |
| HTML→テキスト | `beautifulsoup4` + `lxml`（説明文、公式サイト、itchページ） |
| RSS | 標準 `xml.etree`（itch独自タグ `<price>`, `<imageurl>` を読むため。入力は外部XMLなので `defusedxml` 経由） |
| テスト | `pytest` + `responses`（requestsのモック） |
| フロントJS | 素のJSのみ（ビルド不要） |

## 3. 処理フロー（1回の実行）

1. config読込 → タグ実在検証
   - Steam: `store.steampowered.com/tagdata/populartags/english` と照合します
   - itch: RSSの404または0件を検出します
   - 実在しないタグは WARN を出し、ページにも表示します
2. **SteamSpy**: `collect` + `exclude` + `bonus` の各タグ一覧を取得します（1req/分）。各作品のタグは「どのタグ一覧に現れたか」から決めます。こうすると作品ごとのSteamSpy呼び出しが不要になります。
3. SteamSpyの値（好評/不評数）で**事前フィルタと仮スコア**を出し、上位から **IStoreBrowseService/GetItems**（キー不要）でまとめて取得します。
   - context は `language=english`, `country_code=JP`。1回のバッチは `getitems_batch_size`（既定100）件です
   - 名前、価格（円）、対応言語、デモ有無、発売日、レビュー、開発者、説明文、ヘッダー画像はすべて GetItems から取ります
   - レビューは `reviews.summary_filtered` の `review_count` と `percent_positive` を使います。好評数は「review_count × percent_positive」から推定します（丸め誤差は許容）。両方0ならレビューなしです
   - 言語は `supported_languages[].elanguage` で判定します（0=英語、10=日本語。appdetails と10作品で一致を確認済み）
   - 対象は新規または期限切れ（7日）だけで、上限は `max_new_fetch` 件です。取得できなかった分は、次回の実行で自然に取得されます
   - 表示用の日本語説明文は、`language=japanese` の GetItems で別に取得します（段階2）
4. **appdetails** は、GetItems では取れない**公式サイトURLだけ**のために使います（GetItems の `links` は SNS しか含まないため）。対象は本フィルタを通過した作品だけで、1件ずつ・7日キャッシュです。
   - appreviews は 2026-10-22 頃からストアのトップへ 302 されるようになったため、廃止しました
5. **itch RSS**: カテゴリタグごとに3種類のフィードを取得します。
   - 新着: `https://itch.io/games/newest/tag-<tag>.xml`
   - 人気順: `https://itch.io/games/tag-<tag>.xml`
   - 高評価: `https://itch.io/games/top-rated/tag-<tag>.xml`
   - 取得した項目は `itch_items.json` に蓄積し、`retention_days` を過ぎたら捨てます
   - 価格は RSS の `<currency>` を見て、config の `currency.to_jpy` 表で円に換算します。表にない通貨は WARN を出し、価格フィルタを「不明」として扱います
6. 統合 → 本フィルタ → スコア計算。
   - Steam と itch を統合するのは、**正規化タイトルが一致し、かつ開発者名と作者名が近い**場合だけです
   - 作者名は itch URL のサブドメインと RSS の作者表記、開発者名は GetItems の `basic_info.developers` から取ります
   - 「近い」の判定は、両方を正規化したうえで包含関係か類似度（difflib、既定0.8）で見ます
7. 配信可否: 未判定または30日超の作品を、上限件数まで判定します。そのあと overrides を適用します。
8. `site/` にHTML、data.json、robots.txt を出力します。`state/` を更新します。
9. 終了コードは次のとおりです。
   - 全ソース失敗、または例外: 1
   - 一部だけ失敗: 0。失敗したソースは前回のスナップショットの作品を「前回データ」として残し、ページに表示します

## 4. 状態データのスキーマ（data ブランチ、JSON）

SQLiteではなくJSONにします。理由は、GitHub上で差分が読めて手で直せるからです。数千件で数MB程度の想定です。

```
state/
├── steam_items.json      # Steam詳細キャッシュ（GetItems 7日、公式サイトURL 7日）
├── itch_items.json       # itch RSS蓄積
├── streaming.json        # 配信可否 最新判定（30日）
├── verdict_history.jsonl # 判定変化の履歴（変化時のみ追記）
├── last_output.json      # 前回出力（NEW判定・失敗時の代替）
└── run_status.json       # 毎回更新 → 毎回コミットが発生（60日停止回避）
```

```jsonc
// steam_items.json
{ "steam:620": { "fetched_at": "2026-11-15T03:20:00Z",   // GetItems
  "type": "game", "name": "...", "price_jpy": 980, "is_free": false,
  "languages": {"japanese": true, "english": true},
  "has_demo": true, "release_date": "2024-03-12", "coming_soon": false, "early_access": false,
  "header_image": "https://...", "developers": ["..."],
  "positive": 950, "negative": 50,                       // summary_filtered から推定
  "short_description": "...",                            // english
  "short_description_ja": "...",                         // japanese（段階2、本フィルタ通過作品のみ）
  "website": "https://...", "website_fetched_at": "..." } }  // appdetails

// itch_items.json
{ "itch:author/game-slug": { "first_seen": "...", "last_seen": "...",
  "title": "...", "url": "...", "author": "...",
  "price": 4.99, "currency": "USD", "price_jpy": 749, "image": "...",
  "feeds": ["newest", "top_rated"],
  "published": "...", "tags_matched": ["horror"] } }

// streaming.json
{ "steam:620": { "verdict": "ok|conditional|ng|unknown", "method": "auto",
  "checked_at": "...", "checked_urls": ["..."],
  "evidence": [{ "url": "...", "excerpt": "…200字以内…", "matched": ["stream","allowed"] }] } }

// verdict_history.jsonl（1行1件）
{"id":"steam:620","at":"...","from":"unknown","to":"conditional","method":"auto"}

// last_output.json
{ "generated_at": "...", "items": [ { "id": "steam:620", "aliases": ["itch:..."], "categories": ["horror"], "score": 82.1, "...": "..." } ] }
```

作品IDは `steam:{appid}` と `itch:{作者/スラッグ}` です。統合した作品はSteam IDを主IDにし、itch IDは `aliases` に入れます。

## 5. config例

```yaml
user_agent: "yamo/0.1 (+https://github.com/<owner>/<repo>)"
fetch:
  steamspy_interval_sec: 1.0
  steamspy_list_interval_sec: 60
  appdetails_interval_sec: 2.0   # Steam ストアAPI（GetItems / appdetails）
  getitems_batch_size: 100
  web_interval_sec: 2.0           # 公式サイト/itchページ
  max_new_fetch: 300
  max_streaming_checks: 150
  retry: { max_attempts: 5, base_sec: 2, max_sec: 120 }
cache_ttl_days: { appdetails: 7, streaming: 30 }
currency:
  to_jpy: { USD: 150, EUR: 165, GBP: 195, JPY: 1 }
itch:
  retention_days: 120
  feeds: [newest, popular, top_rated]
merge:
  developer_similarity: 0.8
streaming:
  # 公式サイト巡回から除外するホスト（サブドメイン含む）
  excluded_hosts: [x.com, twitter.com, linktr.ee, discord.gg, discord.com,
                   youtube.com, youtu.be, facebook.com, instagram.com, tiktok.com,
                   twitch.tv, reddit.com, patreon.com, bsky.app]

defaults:
  languages_any: [japanese, english]
  release_after: "2015-01-01"
  include_coming_soon: false

categories:
  horror:
    label: ホラー
    steam:
      collect:     [Horror, Psychological Horror, Survival Horror]
      require_any: [Horror, Psychological Horror, Survival Horror]
      exclude:     [Sexual Content, Nudity]
      bonus:       { Short: 5, Atmospheric: 3, Online Co-Op: 5 }
    itch: { tags: [horror] }
    filters:
      max_price_jpy: 2000
      min_positive_ratio: 0.80
      reviews: { min: 30, max: 20000 }
      require_demo: false
    weights:
      rating: 0.5          # 好評率（Wilson下限）
      volume: 0.3          # レビュー数の「ちょうど良さ」
      recency: 0.2
      volume_sweet_spot: 500
  bakage:
    label: バカゲー
    steam:
      collect:     [Funny, Comedy, Memes, Parody, Dark Humor, Physics, Party Game]
      require_any: [Funny, Comedy, Memes, Parody, Dark Humor]
      exclude:     [Sexual Content]
      bonus:       { Short: 5, Online Co-Op: 8, Physics: 3 }
    itch: { tags: [funny, comedy, meme, parody] }
    filters: { max_price_jpy: 1500, min_positive_ratio: 0.80, reviews: { min: 30, max: 30000 } }
    weights: { rating: 0.5, volume: 0.3, recency: 0.2, volume_sweet_spot: 800 }

itch_scoring: { recency: 0.5, price: 0.2, tag_match: 0.3 }
```

### overrides例

```yaml
- id: steam:620
  verdict: conditional        # ok | conditional | ng | unknown
  note: 収益化はクレジット表記が条件
  evidence_url: https://example.com/streaming-policy
  checked_on: 2026-11-10
```

## 6. スコア式

- **Steam**: 100 × (rating × Wilson下限(好評率) + volume × V + recency × R) + bonus
  - V は、log10(レビュー数) が sweet_spot で最大になる釣鐘型です
  - R は、発売からの経過で減衰します
- **itch**: 新しさ、価格（安いほど高い）、カテゴリタグの一致数を正規化して重み付けします
  - Steamとは別基準なので、ページでは「itch基準」と明示します
  - 統合した作品は、Steam側のスコアを使います

## 7. 配信可否判定

- **入力**: 探索範囲（Steam説明文、公式サイトと決め打ちパス、itchページ本文）をテキスト化して文に分割します。
  - 公式サイトのURLが `streaming.excluded_hosts`（SNS・リンク集系）のホストなら、巡回しません
- **トリガー語（配信系の語）**: この語を含む文だけを判定対象にします。
  - 英語は単語境界つきの正規表現です（大文字小文字は区別しません）: `\bstream(s|ing|ed|er|ers)?\b`, `\blet'?s ?plays?\b`, `\bcontent creators?\b`, `\bmonetiz(e|ed|ing|ation)\b`, `\bmonetis(e|ed|ing|ation)\b`, `\bvideos? (of|about) (the|this|our) game\b`
    - `mainstream`、`streamline`、`livestreamline` のような語は境界で除外されます
  - 日本語: `配信`, `実況`, `収益化`, `ゲーム実況`, `生配信`
    - ただし `配信日`, `配信開始`, `配信予定`, `DL配信`, `ダウンロード配信`, `配信中`（販売の意味）の中の `配信` は、先に取り除いてからトリガーを判定します
  - `YouTube` / `Twitch` 単独ではトリガーにしません（SNSリンクや Twitch 連携機能の誤検知を防ぐため）
- **判定ルール**
  1. **NG**: トリガー文とその前後1文の窓に禁止語がある
     - 例: `not allowed`, `prohibited`, `forbidden`, `do not (stream|record|upload)`, `may not`, `禁止`, `ご遠慮ください`, `お控えください`
  2. **条件付き**: 窓に条件語がある
     - 例: `without (prior )?permission`, `ask (for )?permission`, `contact us (first|before)`, `non-?commercial`, `not monetiz…`, `credit`, `link to (the|our) (store|page)`, `要許可`, `事前(に)?(連絡|申請|許可)`, `収益化(は)?(不可|できません|NG)`, `クレジット表記`
     - 例外として、条件語が否定されている場合（`without asking (for )?permission`, `no need to ask`, `許可(は)?不要`）は条件語として数えません
  3. **OK**: トリガー語と許可語が**同じ文の中に**ある。前後1文の窓は使いません
     - 許可語は複合表現だけです: `allowed to (stream|record|monetiz…)`, `free to (stream|record|make videos)`, `feel free to (stream|record|make (videos|content))`, `(streaming|let's plays?|videos?) (are|is) (welcome|allowed|encouraged|permitted)`, `you (can|may) (stream|monetiz…)`, `配信OK`, `実況OK`, `配信・実況OK`, `配信歓迎`, `実況歓迎`, `配信(して)?(いただいて)?(も)?(大丈夫|構いません|かまいません)`, `収益化OK`, `収益化(も)?可`
     - 単独の `OK` や `welcome` は許可語にしません
  4. **不明**: それ以外のすべて
- **NG/条件付きと許可語が両方ある場合**: NG > 条件付き > OK の順で厳しいほうを採用します。
  - 例: `feel free to stream without asking permission` は条件語の否定なので **OK**
  - 例: `do not stream without permission` は `do not stream` が禁止語ですが、`without permission` があるので許可を取れば配信できる意味です。この組み合わせは **条件付き** に寄せます。規則としては「禁止語の直後（同じ文内）に `without (prior )?permission` / `unless` / `許可なく` が続く場合、NGではなく条件付き」とします
- **複数ページに根拠がある場合**: 最も厳しい判定を採用します。根拠がなければ必ず不明にします。
- **保存するもの**: URL、200字以内の抜粋、一致した語、判定日時だけです。ページ本文は保存しません。
- **表示**: 自動判定には「自動判定」バッジを付けます。手動の上書きには「手動確認済み」を付けます。30日を超えた判定には「要再確認」を付けます。不明の作品には、ストアページと公式サイトへのリンクを出します。
- **テスト**: 日英の文言サンプルを30件程度、YAMLにまとめて正解ラベル付きで用意し、パラメータ化テストにします。必ず次のサンプルを含めます。
  - `feel free to stream without asking permission` → OK
  - `do not stream without permission` → 条件付き
  - SNSリンクだけの説明文（`Follow us on Twitter / YouTube / Twitch`）→ 不明
  - `mainstream` / `streamline` を含む文 → 不明
  - `配信日: 2026年3月` / `DL配信開始` → 不明
  - `Streaming is OK` のように、許可が単独の `OK` で書かれた文 → 不明（取りこぼしは overrides で補う方針）
  - 許可語が前後の別の文にある場合 → 不明（OKの判定は同じ文の中だけ）

## 8. workflow例（update.yml）

```yaml
name: update
on:
  schedule:
    - cron: "17 3 * * 1,4"   # 月・木 12:17 JST
  workflow_dispatch:
permissions:
  contents: write
  pages: write
  id-token: write
concurrency:
  group: update
  cancel-in-progress: false
jobs:
  build:
    runs-on: ubuntu-latest
    timeout-minutes: 120
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: "3.12", cache: pip }
      - run: pip install .
      - name: Prepare data branch worktree
        run: |
          if git fetch origin data; then
            git worktree add state origin/data
          else
            git worktree add --orphan -b data state
          fi
      - run: python -m yamo run --state-dir state --out site
      - name: Commit state
        if: always()
        working-directory: state
        run: |
          git config user.name "github-actions[bot]"
          git config user.email "41898282+github-actions[bot]@users.noreply.github.com"
          git add -A
          git commit -m "state: run ${{ github.run_id }}" || echo "no changes"
          git push origin HEAD:data
      - uses: actions/configure-pages@v5
      - uses: actions/upload-pages-artifact@v3
        with: { path: site }
  deploy:
    needs: build
    runs-on: ubuntu-latest
    environment:
      name: github-pages
      url: ${{ steps.deploy.outputs.page_url }}
    steps:
      - id: deploy
        uses: actions/deploy-pages@v4
```

アクションのバージョンは、実装時に最新のメジャー版を確認します。state のコミットは `if: always()` なので、本処理が失敗しても取得済みのキャッシュは残ります。

## 9. 段階実装とテスト

各段階が終わるごとに動作とテスト結果を報告し、区切りごとにコミットします（コミットしてよいかは確認します）。

| 段階 | 内容 | テスト |
|---|---|---|
| 1 | config/state/http基盤、SteamSpy + GetItems（appdetails は公式サイトのみ）。CLIでJSONを出力 | 取得部は`responses`でモック（正常、429リトライ、5xxでスキップ、上限件数）。フィルタとスコアの単体テスト |
| 2 | HTML出力（タブ、並べ替え、絞り込み、NEW、ソース状態、スマホ対応、noindex、robots.txt） | render出力のスナップショット的な検証（meta noindex、NEW付与、列） |
| 3 | update.yml と Pages、data ブランチでの持ち越し | ローカルで`act`は使わず、手動の workflow_dispatch で確認（手順はREADMEに書きます） |
| 4 | itch RSS、蓄積、タイトル正規化による統合 | RSSフィクスチャ、正規化（™/®、記号、全角半角、Demo接尾辞など）のテスト |
| 5 | 配信可否の取得と自動判定、要再確認 | 判定部は文言サンプルで単体テスト。fetchはモック（robots.txt拒否、サイズ超過） |
| 6 | overrides.yaml、手動確認済み表示、README仕上げ | override優先、不正な行の警告 |

## 10. 既知のリスク・注意点

- **itchの一覧**: RSSは件数が少ないので、itchの一覧は数回実行してから充実していきます。
- **SteamSpyのタグ一覧**: 非公式で、遅延や欠損があります。失敗したときは前回データで代替します。
  2026-11-15時点で、ローカル回線からは Cloudflare に止められて 403 になります。Actions 上での可否を疎通確認中です。代替は IStoreQueryService/Query（[STEAM_API_SURVEY.md](STEAM_API_SURVEY.md)）。
- **公式サイトの巡回**: robots.txtを尊重し、同じホストだけ、1ページ1MBまで、タイムアウト10秒にします。
- **自動判定の精度**: 根拠がなければ「不明」にする方針なので、不明が多めになる想定です。overridesで少しずつ補っていく運用になります。
