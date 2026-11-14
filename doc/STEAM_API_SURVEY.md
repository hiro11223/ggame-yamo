# Steam API 調査メモ

調査日: 2026-11-15（ローカル回線から実APIを呼んで確認）

---

## 1. IStoreBrowseService/GetItems（実装済み）

`https://api.steampowered.com/IStoreBrowseService/GetItems/v1/?input_json=...`
キーは不要。context は `language=english`, `country_code=JP`。

### appdetails の項目をどこまで代替できるか

10作品（OMORI、Quake、Balatro、DDLC、Lethal Company、Phasmophobia、Vampire Survivors、R.E.P.O.、Inscryption、Content Warning）で、appdetails の結果と突き合わせた。

| 項目 | GetItems での取り方 | 代替 |
|---|---|---|
| 価格 | `best_purchase_option.final_price_in_cents` ÷ 100（JPなので円）。無料作品はトップレベルの `is_free: true` で、`best_purchase_option` がない | ✅ 全作品で appdetails と一致 |
| 対応言語 | `supported_languages[].elanguage`（0=英語、10=日本語） | ✅ 日本語対応の有無が10作品すべてで一致 |
| デモ有無 | `related_items.demo_appid`（data_request の指定に関係なく返る） | ✅ Inscryption のみデモあり、appdetails と一致 |
| 発売日 | `release.steam_release_date`（UNIX秒） | ✅ |
| 未発売 | 発売日がない、または未来の日付。`is_coming_soon` も見る | ⚠️ 実際の未発売作品ではまだ未確認 |
| アーリーアクセス | トップレベルの `is_early_access` | ✅（appdetails にはない項目） |
| 種別 | `type`（0=ゲーム、1=デモ、11=サウンドトラックを確認済み） | ✅ |
| 開発者 | `basic_info.developers[].name` | ✅ |
| 説明文 | `basic_info.short_description`。`language=japanese` にすると日本語で返る | ✅ |
| ヘッダー画像 | `https://shared.akamai.steamstatic.com/store_item_assets/` + `assets.asset_url_format`（`${FILENAME}` を `assets.header` で置換） | ✅ appdetails の URL と一致 |
| レビュー | `reviews.summary_filtered.review_count` / `percent_positive` | ✅（appreviews の代替） |
| タグ | `tagids`（`include_tag_count` 指定時は重み付き `tags`） | ✅（今は未使用。後述） |
| **公式サイト** | `links` に入るのは SNS（YouTube、X、Discord、TikTok 等）だけで、公式サイトは入らない | ❌ **appdetails に残した** |

### 動作

- 100件のバッチでも 200 で、100件すべて返った（URL長は約3.4KB）。
- 存在しない appid は `success: 15`、`appid: 0` で返る。appid の対応づけには `id` を使う。
- 実装では、GetItems が JSON 以外を返したらその実行では打ち切る。

### 実装への反映

- appdetails の呼び出しは、**掲載が決まった作品の公式サイトURLだけ**に減った（1件ずつ、7日キャッシュ）。
- appreviews は廃止した（2026-10-22 頃からストアのトップへ 302）。
- キャッシュファイル名は `appdetails.json` から `steam_items.json` に変えた。

---

## 2. IStoreQueryService/Query（SteamSpy の代替候補・未実装）

`https://api.steampowered.com/IStoreQueryService/Query/v1/?input_json=...`

### 報告を求められた4点

| 観点 | 結果 |
|---|---|
| **キーの要否** | **不要**。キーなしで 200 が返り、結果も返る |
| **タグ絞り込み** | **できる**。`filters.tagids_must_match` は「グループの配列」で、**グループ内は OR、グループ同士は AND**。除外は `filters.tagids_exclude` |
| **ページング** | `query.start`（オフセット）と `query.count`。応答の `metadata.total_matching_records` で総件数が分かる。**`sort` を省略すると毎回ランダム順になり、ページングが成立しない**。`sort` を指定すると安定し、`sort: 2` は appid の昇順と推定（全件を順に取るのに向く） |
| **1回の取得件数** | **最大1000件**。`count: 2000` を指定しても1000件で切られる |

### タグ絞り込みの検証（Horror=1667、Psychological Horror=1721、発売済みゲームのみ）

| 条件 | 件数 | 確認 |
|---|---|---|
| Horror | 14,515 | |
| Psychological Horror | 9,369 | |
| 1グループ [Horror, Psych] | 17,005 | = 14515 + 9369 − 6879 → **OR** |
| 2グループ [Horror], [Psych] | 6,879 | 両方を持つ作品 → **AND** |
| Horror かつ Psych を除外 | 7,636 | = 14515 − 6879 → **除外が効く** |

### 使えるフィルタ

- `released_only: true`（発売済みのみ）
- `type_filters.include_games: true`（ゲームのみ）

### 見積もり（採用する場合）

- Horror 系3タグの OR で約2万件なので、1000件ずつ約20回。カテゴリ2つでも50回前後になる。
- SteamSpy の「1タグ1回・1回ごとに1分待つ」に比べて、待ち時間は大幅に短い。
- `data_request`（`include_reviews`、`include_tag_count` 等）を付けると、候補収集と同時にレビュー数やタグも取れる。3件では確認済みだが、1000件まとめて付けた場合の応答サイズと速度は未確認。
- うまくいけば、SteamSpy と GetItems の役割の大部分を Query 1本で置き換えられる可能性がある。

### 未確認・リスク

- レート制限は公開されていない。今回の調査では2秒間隔で約30回呼び、制限には当たらなかった。
- `sort` の値の意味は公式文書がない。2以外は意味が推定できていない。
- 非公開APIに近い扱いのため、予告なく仕様が変わる可能性がある（appreviews と同じリスク）。

### タグIDの取得

- タグ名→ID の対応は `tagdata/populartags/english`（実在検証で既に取得している一覧）から作れる。

---

## 3. bakage 向けの実在タグ候補（採用はユーザー判断）

`tagdata/populartags/english` の全429タグから、バカゲーに近そうなものを抜き出した。

| tagid | タグ | メモ |
|---|---|---|
| 1651 | Satire | 風刺。Parody に最も近い |
| 19995 | Dark Comedy | Dark Humor と近い |
| 1710 | Surreal | シュール系 |
| 1714 | Psychedelic | 奇抜な見た目 |
| 13782 | Experimental | 実験作。範囲は広め |
| 5363 | Destruction | 破壊系（物理バカゲー） |
| 35079 | Job Simulator | ネタ系シミュレーター |
| 7108 | Party | Party Game とは別のタグ |
| 4195 | Cartoony / 4562 Cartoon | 見た目のタグ。加点向き |
| 4726 | Cute | 加点向き |
| 7368 | Local Multiplayer / 4840 4 Player Local / 3841 Local Co-Op | 協力・対戦の加点向き |
| 10437 | Trivia | クイズ系 |
| 3810 | Sandbox | 範囲は広い |

**補足: `Parody` は実在した（tagid 4878）。**
- Steam のタグ一覧では名前が `"Parody "`（末尾に空白）になっていた。前回の実在検証は空白を除かずに比べていたため、誤って「存在しない」と判定していた。
- 検証側は空白を除いて比べるように修正済み。
- いったん config から削除したが、実在が確認できたので戻した（下記）。

### 採用結果（2026-11-15、判断を任されたため Claude が決定）

- **必須（require_any）**: Funny, Comedy, Memes, **Parody**, **Satire**, Dark Humor, **Dark Comedy**
  - 笑い・ネタを直接表すタグに限った
- **収集（collect）**: 上記 + Physics, Party Game
- **加点（bonus）**: Short +5, Online Co-Op +8, **Local Multiplayer +3**, Physics +3, **Destruction +3**
- **採用しなかったもの**: Surreal, Experimental, Psychedelic
  - 範囲が広く、ホラーやアート系まで大量に拾うため
  - 必要なら加点タグとして後から足せる
- 注意: SteamSpy を使う限り、タグが1つ増えるごとに一覧の取得が1回（1分の待ち）増える
