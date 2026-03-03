# ヤフアマ (yafuama) - Claude Code 指示書

## プロジェクト概要
ヤフオク→Amazon無在庫転売の在庫連動ツール。
FastAPI + SQLite + APScheduler + Docker。Fly.ioにデプロイ。

## 重要ルール

### サーバー構成（必ず守ること）
- **本番サーバーはFly.ioのみ**: https://yafuama.fly.dev/
- **ローカルサーバーは起動しない**: データの二重管理になるため禁止
- **データベースはFly.ioの `/data/yafuama.db` のみ**: ローカルのyafuama.dbは使わない
- コード編集はローカルで行い、`flyctl deploy` でFly.ioに反映する

### デプロイ手順（コード修正後は必ず実行）
```bash
cd C:\Projects\yafuama
git add <修正ファイル>
git commit -m "変更内容"
git push origin master
flyctl deploy --remote-only
```

### SP-API出品ロジック
- **全出品ルートは `amazon/listing.py` の `submit_to_amazon()` を使用**
  - `api/amazon.py` の `create_listing` (ASIN有りの場合)
  - `api/amazon.py` の `relist_listing`
  - `api/keywords.py` の `list_from_deal`
- 出品フロー: PUT → 3秒待機 → condition_note PATCH → image PATCH → price PATCH → quantity PATCH → price Feed → inventory Feed
- 修正が必要な場合は `amazon/listing.py` だけを変更すれば全ルートに反映される

### データベース
- SQLite WALモード + busy_timeout=30秒 + foreign_keys=ON
- マイグレーションは Alembic（起動時に自動実行）
- `alembic/env.py` に全モデルがimportされていること

### コード規約
- inline importは避ける（`from ..main import app_state` のような循環回避は例外）
- `asyncio.get_running_loop()` を使用（`get_event_loop()` は非推奨）
- Noneの可能性がある値のフォーマットにはガード付ける（例: `value or 0`）

## ファイル構成
```
src/yafuama/
  main.py              # FastAPIアプリ、lifespan、ミドルウェア
  config.py            # 設定（pydantic-settings）
  database.py          # SQLAlchemy engine/session
  models.py            # 全テーブル定義
  schemas.py           # Pydantic スキーマ
  auth.py              # API Key ミドルウェア
  api/
    amazon.py          # Amazon出品API（create/relist/price更新等）
    items.py           # 監視アイテムCRUD
    keywords.py        # キーワード監視・ディールアラート
  amazon/
    client.py          # SP-API クライアント（async wrapper）
    listing.py         # 共通出品関数 submit_to_amazon()
    notifier.py        # Yahoo終了時のAmazon連携
    image_proxy.py     # S3画像アップロード
    order_monitor.py   # 注文監視
    listing_sync.py    # 出品同期チェック
  monitor/
    scheduler.py       # APScheduler ジョブ管理
    deal_scanner.py    # 価格差スキャナー
  ai/
    engine.py          # AI Discovery エンジン
  keepa/
    client.py          # Keepa API クライアント
  web/
    views.py           # Web UI（Jinja2テンプレート）
  templates/           # HTMLテンプレート
```

## 既知の注意事項
- Fly.ioのDockerは非rootユーザー(appuser)。`/app/`は読み取り専用、`/data/`のみ書き込み可
- ログファイルは `/data/yafuama.log`（RotatingFileHandler, 5MB×3）
- NoCacheMiddleware でHTML応答にCache-Control付与済み
