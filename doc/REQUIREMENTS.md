# 実況ネタ探索サイト 要件定義 v3

## 目的
実況向けのゲーム(ホラー/バカゲー等)を Steam / itch.io から横断収集し、
おすすめ順+配信可否つきの一覧ページを週2回自動更新で公開する。

## 構成
- 言語: Python 3.12。ローカルでも同じコマンドで実行可能にする。
- 実行: GitHub Actions(ubuntu-latest)。
- 公開: GitHub Pages(Actionsデプロイ)。静的HTML+JSのみ、サーバー・外部DBなし。
- リポジトリ: public。
- 検索避け: 全ページに `<meta name="robots" content="noindex, nofollow">`、robots.txt で全Disallow。
  READMEにもPagesのURLは書かない。
- Twitch連携は対象外。

## カテゴリ(config.yaml で追加可能)
- horror: Steamタグ例 Horror, Psychological Horror, Survival Horror, Atmospheric, Short / itchタグ例 horror
- bakage: Steamタグ例 Funny, Comedy, Memes, Parody, Dark Humor, Physics, Party Game / itchタグ例 funny, comedy, meme, parody
- カテゴリごとに: 含めるタグ、除外タグ、価格上限、好評率下限、レビュー数範囲、スコア重み
- 1作品が複数カテゴリに属してよい
- 起動時にタグ名の実在を検証し、存在しないタグは警告(0件の見逃し防止)

## データソース(独立モジュール。1つ失敗しても他は継続)
- SteamSpy: タグ別一覧で候補収集(所有者数推定、好評/不評数)。1req/秒、一覧系は1req/分を厳守。
- Steam appdetails: 価格、対応言語、デモ有無、発売日、公式サイトURL、説明文。間隔は設定可能(既定2秒)。
- Steam appreviews: 好評率・レビュー数(必要時のみ)。
- itch.io: タグ別RSS(例 https://itch.io/games/tag-horror.xml)。HTMLスクレイピングは原則しない。
  - itchはレビュー数がないため、スコアは「新しさ・価格・タグ一致度」の別基準。
- 重複統合: タイトル正規化で Steam と itch の同一作品を統合。
- キャッシュ: appdetailsは7日、配信可否は30日で再取得。差分更新(新規・期限切れのみ取得)。
- 1回の新規取得件数に上限(例 300件)、残りは次回へ。
- 429/5xxは指数バックオフで再試行、上限超過でそのソースをスキップ。
- User-Agentにツール名とリポジトリURLを明示。

## 絞り込み・スコア
- 条件: 価格、好評率、レビュー数、発売日、デモ有無、対応言語(日本語 or 英語)。
- Steamスコア: 好評率が高く、レビュー数が中程度(多すぎ=実況され尽くし)を高得点。重みはカテゴリ別。
- 「短い」タグや協力プレイは加点項目としてconfigで設定可能。

## 配信可否
- 表示4段階:
  - ✅ OK: 配信・実況・収益化の許可が明記
  - ⚠️ 条件付き: 許可あり、ただし条件(収益化不可、クレジット表記、事前申請 等)
  - ❌ NG: 禁止が明記
  - ❓ 不明: 上記以外すべて。根拠がなければOKにしない
- 探索範囲(限定):
  - Steamストアページ説明文
  - Steamに登録された公式サイト + /press /presskit /streaming /faq 等の決め打ちパス
  - itch.ioのゲームページ本文
- 自動判定: キーワード検出(streaming, stream, let's play, content creator, monetiz, YouTube, 配信, 実況, 収益化)
  + 否定語・条件語の検出(not allowed, prohibited, without permission, 禁止, 要許可 等)。
  - 自動判定は「候補」扱い。ページ上で「自動判定」と明示。
- 根拠: 根拠URL + 該当箇所の短い抜粋(ページ全文は保存しない) + 判定日時。
- 手動上書き: overrides.yaml(作品ID・判定・根拠URL・確認日)。自動判定より優先し「手動確認済み」と表示。
  GitHubのWeb UIから編集→次回実行で反映。
- 不明の場合: ストアページと公式サイトへの確認リンクを表示。
- 30日経過した判定は「要再確認」表示。

## 出力ページ
- カテゴリのタブ(horror / bakage / 全部)。
- 列: タイトル、サムネ、ソース、価格、好評率、レビュー数、タグ、デモ、対応言語、配信可否(+根拠)、スコア。
- 並べ替え、配信可否・タグ・言語での絞り込み(クライアントサイドJS)。
- NEW: 前回の出力に存在しなかった作品に付与。
- 生成日時、各ソースの取得成否を表示。
- スマホでも見やすいレイアウト。

## 定期実行
- .github/workflows/update.yml
  - on: schedule `17 3 * * 1,4`(月木 12:17 JST、毎時0分は避ける)+ workflow_dispatch
  - timeout-minutes: 120
  - concurrency 設定で多重実行防止
- 状態(キャッシュ、前回一覧、判定履歴)は data ブランチにJSON/SQLiteでコミットして持ち越す
  - 毎回のコミットで、60日無活動による定期実行の自動停止を回避
- 失敗時: ジョブ失敗扱い→GitHubの通知メールで気付ける。一部ソースのみ失敗ならページに表示しジョブは成功扱い。
- 実行ログはActionsのログに出力。

## 設定ファイル
- config.yaml: カテゴリ、条件、重み、リクエスト間隔、取得上限
- overrides.yaml: 配信可否の手動確定
- 秘密情報は現状なし(必要になればGitHub Secrets)

## 進め方(Claude Codeへの指示)
1. 実装計画(ディレクトリ構成、ライブラリ、状態データのスキーマ、config例、workflow例)を提示し、承認後に実装。
2. 段階実装: SteamSpy+appdetails最小版 → HTML出力 → GitHub Actions+Pages → itch RSS → 配信可否判定 → 手動上書き。
3. 各段階でテスト(取得部はモック、判定部は文言サンプルで単体テスト)。
4. READMEにセットアップ手順(Pages有効化、Actions権限設定、手動実行方法、config/overrides の書き方)を記載。