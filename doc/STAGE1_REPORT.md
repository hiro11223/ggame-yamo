# 段階1 報告（SteamSpy + appdetails 最小版）

日付: 2026-11-15
状態: **実装完了・テスト36件すべて合格。ただし実APIで前提が崩れている点があり、判断待ち**
コミット: 未実施

---

## 1. 判断が必要な事項

### 1-1. SteamSpy が使えない（候補収集の要）

- `https://steamspy.com/api.php` へのアクセスが、User-Agent に関係なく **403** になる。
  - 返ってくるのは Cloudflare のボット確認ページ（`Just a moment...`）。
  - 試した User-Agent は、本ツールのもの、`python-requests`、`curl` の3つ。
- この端末の回線からは、機械的なアクセスが一律に止められている状態。回避の細工はしていない。
- GitHub Actions 上でも同じかは**未確認**。データセンターのIPなので、むしろ止められやすい可能性がある。
- 現在のコードの動き: ソース失敗として扱い、前回データを「前回データ」として残して終了コード1で終わる（要件どおり）。

**対応案**

| # | 案 | メモ |
|---|---|---|
| 1 | **GitHub Actions 上で通るか先に確かめる（推奨）** | 段階3を一部前倒しし、疎通確認だけの workflow を手動で1回実行する。通ればそのまま進められる |
| 2 | Steam公式 Web API（IStoreQueryService 等）に切り替える | タグでの絞り込みとレビュー数を公式APIで取れるかは要調査。APIキーが必要になるため GitHub Secrets を使う |
| 3 | Steamストアの検索結果ページを解析する | 要件の「HTMLスクレイピングは原則しない」に反するので非推奨 |

### 1-2. Steam appreviews が JSON を返さない

- `https://store.steampowered.com/appreviews/<appid>?json=1...` が、ストアのトップページへ **302 リダイレクト**される。User-Agent やパラメータを変えても同じ。
- 今はこのAPIが使えない状態と思われる。
- 対策済み: JSON以外が返ったら、その回は appreviews の呼び出しを打ち切る（取得上限の件数まで無駄に呼び続けるのを防ぐ）。
- 影響: SteamSpy にレビュー数がない作品だけ、レビュー数を補えない。その作品はレビュー数の条件で絞り込みから落ちる。

### 1-3. Steamタグ `Parody` が実在しない

- タグの実在検証が期待どおり検知した。Steamのタグ一覧（429件）に `Parody` がない。
- bakage の設定から外すか、別のタグに替える必要がある。

### 1-4. コミットしてよいか

- 段階1の成果物は、まだコミットしていない。

---

## 2. 実APIで正常に確認できたこと

- **appdetails**（`cc=jp`, `l=english`）: 実データ（OMORI、Quake）で、円価格、日本語/英語の対応、発売日、開発者、公式サイトURLが正しく取れた。
- **ストアのタグ一覧**（`tagdata/populartags/english`）: 取得できている。

---

## 3. 実装したもの

| ファイル | 内容 |
|---|---|
| [pyproject.toml](../pyproject.toml) | 依存: requests / PyYAML / pydantic。開発用: pytest / responses |
| [config.yaml](../config.yaml) | 計画の config 例どおり |
| [overrides.yaml](../overrides.yaml) | 空（反映処理は段階6） |
| [src/yamo/config.py](../src/yamo/config.py) | pydantic で検証。未知のキーはエラー。SteamSpy の間隔は 1秒/60秒 未満を設定できない |
| [src/yamo/http.py](../src/yamo/http.py) | User-Agent 付与、キーごとの最小間隔、429/5xx・接続エラーの指数バックオフ（Retry-After 対応）、上限超過で `SourceUnavailable` |
| [src/yamo/state.py](../src/yamo/state.py) | state の JSON 読み書き（キー順固定・原子的書き込み）、TTL 判定 |
| [src/yamo/sources/steamspy.py](../src/yamo/sources/steamspy.py) | タグ別一覧の取得と、作品ごとのタグ集約。1タグ失敗は partial、全滅は failed |
| [src/yamo/sources/steam_store.py](../src/yamo/sources/steam_store.py) | appdetails / appreviews / タグ一覧の取得とパース |
| [src/yamo/filters.py](../src/yamo/filters.py) | 事前フィルタ（タグ・レビュー数・好評率）と本フィルタ（価格・言語・デモ・発売日・種別） |
| [src/yamo/scoring.py](../src/yamo/scoring.py) | Wilson 下限 × レビュー数の釣鐘型 × 新しさ ＋ 加点タグ |
| [src/yamo/pipeline.py](../src/yamo/pipeline.py) | 事前フィルタ → 仮スコア順に上限まで appdetails 取得 → 本フィルタ → `site/data.json` 出力。state は appdetails / reviews / last_output / run_status |
| [src/yamo/\_\_main\_\_.py](../src/yamo/__main__.py) | `python -m yamo run` と `python -m yamo check-tags` |

### テスト（36件、外部APIはすべてモック）

- HTTP: User-Agent、間隔、バックオフ、Retry-After、再試行上限、404は再試行しない
- 設定: リポジトリの config.yaml の検証、typo 検出、間隔の下限
- パーサ: SteamSpy、appdetails（言語・価格・日付）、appreviews
- スコアとフィルタ
- 結合
  - キャッシュTTL（7日以内は再取得しない）
  - 取得上限と、残りを次回に持ち越す動き
  - 存在しないタグの警告
  - appreviews が JSON 以外を返したときの打ち切り
  - SteamSpy 停止時に前回データを表示し、終了コード1で終わること

### 途中で直したバグ

- appdetails と appreviews が同じ取得上限を共有していた。そのため appdetails で上限を使い切ると、レビュー数を補えない作品が絞り込みで落ちていた。上限を別枠に分けて修正した。

---

## 4. 補足

- この端末には Python 3.12 がないため、ローカルのテストは 3.11 の `.venv` で実行している。`requires-python >=3.11` にしてあり、CI は 3.12 で動かす。
- 計画書 [PLAN.md](PLAN.md) には、追加指示（配信可否判定の規則、itch の3種フィード、通貨換算、統合条件、巡回除外ホスト）をすべて反映済み。
