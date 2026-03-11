# ヤフアマ (yafuama) - Claude Code 指示書

## プロジェクト概要
ヤフオク→Amazon無在庫転売の自動化プラットフォーム。
FastAPI + SQLite + APScheduler + Docker。Fly.ioにデプロイ。

---

## 絶対に守るべきルール

### 1. 既存機能を壊さない（最重要）
新機能の実装時、以下の既存機能が正常に動作し続けることを必ず確認すること：

- **ヤフオク監視ループ**: 60秒間隔で全アクティブ商品の価格・入札・状態をチェック
- **Yahoo→Amazon出品連動**: 出品・削除・再出品が正しく同期される
- **自動再出品**: Yahoo再出品検知→Amazon自動再出品（利益率・利益額チェック付き）
- **価格自動同期**: Yahoo即決価格変動→Amazon価格自動更新→30秒後に反映確認
- **利益計算の正確性**: `calculate_amazon_price()` と `score_deal()` の計算式を変更しない
- **注文監視**: 3分間隔でAmazon注文をポーリング→Discord通知
- **ディールスキャン**: Product Finder→型番抽出→Yahoo検索→マッチング→通知

### 2. 変更前の確認手順
コード修正前に以下を必ず実施：
1. 修正対象ファイルを `Read` で読む（推測で修正しない）
2. そのファイルを呼び出している箇所を `Grep` で確認
3. 関連テストを `pytest tests/` で実行
4. 影響範囲を把握してから修正する

### 3. サーバー構成
- **本番サーバーはFly.ioのみ**: https://yafuama.fly.dev/
- **ローカルサーバーは起動しない**: データの二重管理になるため禁止
- **DBはFly.ioの `/data/yafuama.db` のみ**

### 4. デプロイ手順
```bash
cd C:\Projects\yafuama
git add <修正ファイル>
git -c user.name="rinodehi1978" -c user.email="rinodehi1978@gmail.com" commit -m "変更内容"
git push origin master
flyctl deploy --remote-only
```

---

## システムアーキテクチャ

### ディールスキャンの仕組み（シンプルに理解すること）

**キーワード生成 = Product Finderのみ（Amazon起点）**
```
Keepa Product Finder API
  → フィルタ: 中古≥¥10,000、90日で1個以上売れ、中古<新品
  → 通過した商品から型番抽出（5文字以上のみ）
  → その型番でヤフオク検索
  → マッチング（型番完全一致必須、スコア≥0.40）
  → 利益計算（粗利率≥25%、粗利額≥¥3,000）
  → Discord通知
```

### 出品フロー（SP-API）
**全出品ルートは `amazon/listing.py` の `submit_to_amazon()` を使用**
- `api/amazon.py` の `create_listing`（手動出品）
- `api/amazon.py` の `relist_listing`（手動再出品）
- `api/deals.py` の `list_from_deal`（ディールから出品）
- `scheduler.py` の `_auto_relist_to_amazon()`（自動再出品）

フロー: PUT → 3秒待機 → condition_note PATCH → image PATCH → price PATCH → quantity PATCH → price Feed → inventory Feed

**修正が必要な場合は `amazon/listing.py` だけを変更すれば全ルートに反映される**

### SKU命名規則
- 通常: `YAHOO-{auction_id}`
- 再出品: `YAHOO-{auction_id}-R{YYMMDDHHmm}`

### 利益計算式（変更禁止）
```
Amazon出品価格 = (Yahoo即決 + 送料) / (1 - (マージン% + 手数料%) / 100)  ※10円切上
粗利 = 出品価格 - (Yahoo価格 + 送料 + 転送料 + システム料¥100) - Amazon手数料
粗利率 = (粗利 / 出品価格) × 100
転送料: 3辺合計→ 60cm:¥735, 80cm:¥840, 100cm:¥960 ... 200cm:¥3,810
```

### 自動連動の3本柱
1. **Yahoo終了→Amazon削除**: `amazon/notifier.py` AmazonNotifier が status_change="ended_*" で自動削除
2. **Yahoo再出品→Amazon自動再出品**: `scheduler.py` _check_relist_candidates() → _auto_relist_to_amazon()
3. **Yahoo価格変動→Amazon価格同期**: `scheduler.py` _check_item() → _auto_sync_amazon_price()

### 反映確認
自動再出品・価格同期の後、30秒後にSP-APIでAmazon側の価格を照合。
成功=緑Discord通知、失敗=赤Discord通知。

### 型番マッチングルール
- 最小5文字（4文字以下は誤マッチが多いため除外）
- 英字+数字の両方が必要（純文字・純数字は除外）
- ハイフン除去 + 小文字化後の**完全一致のみ**（WH-1000XM4 = WH1000XM4）
- カラーサフィックス一致は廃止（SV18 ≠ SV18FF、ABC100 ≠ ABC100PRO）
- 大小文字の区別なし
- 型番一致が必須（型番なしはリジェクト）
- 型番衝突は即リジェクト（XM4 vs XM5 = 別商品）

### 型番バリデーション鉄壁ルール（絶対に破らない）
**全てのコードパスで `is_valid_model()` を通すこと。**

1. **`is_valid_model()` は唯一の門番**: 型番を受け入れる全経路（Keepa model field、タイトル抽出、手動入力）で必ず呼ぶ
2. **`is_valid_model()` 内部チェック一覧**（全て必須、削除禁止）:
   - `_SPEC_UNIT_RE`: スペック値拒否（32bit, 192khz, 128gb, 100mm, 48fps等）
   - `_DIMENSION_RE`: 寸法値拒否（30x30cm, 100×200mm等）
   - `_MODEL_BLOCKLIST`: ブランド名等の誤型番拒否（52toys等）
   - `_WORD_VERSION_RE` + `COMMON_WORDS`: 一般語+バージョン拒否（switch2, bluetooth6等）
   - 5文字最小、英字+数字必須、日本語拒否
3. **Keepa model field**: `_extract_yahoo_keywords()` と `_match_yahoo_to_amazon()` の両方で `is_valid_model()` チェック必須
4. **短型番ガード**: 7文字以下の型番一致時、ノイズ語・色を除外した意味のある共通トークンが必要
5. **アクセサリー検出**: `_ACCESSORY_WORDS` に新パターンを追加したら `extract_accessory_signals_from_text()` で検出されることをテストで確認
6. **新しいフィルタを追加したら**: 必ず `tests/test_deal_scanner.py` と `tests/test_matcher.py` に回帰テストを追加

⚠️ **`_extract_model_numbers()` にもスペック値フィルタがあるが、`is_valid_model()` とは独立。両方に存在する必要がある（二重防御）**

---

## 重要な設定値（config.py）

| 設定 | 値 | 変更時の注意 |
|------|-----|------------|
| 型番最小文字数 | 5 | matcher.py 2箇所（is_valid_model, _extract_model_numbers） |
| deal_min_gross_margin_pct | 25.0% | 下げると低品質ディールが増える |
| deal_min_gross_profit | ¥3,000 | 下げると薄利案件が増える |
| demand_finder_min_used_price | ¥10,000 | Product Finderの中古最低価格 |
| demand_finder_min_drops90 | 1 | 90日で1個以上売れていればOK |
| sp_api_default_margin_pct | 15.0% | Amazon出品価格のマージン |
| deal_amazon_fee_pct | 10.0% | Amazon販売手数料 |
| relist_auto_enabled | True | 自動再出品ON |
| price_sync_enabled | True | 価格自動同期ON |
| verification_delay_seconds | 30 | 反映確認の待機秒数 |

---

## データベース
- SQLite WALモード + busy_timeout=30秒 + foreign_keys=ON
- マイグレーションは Alembic（起動時に自動実行）
- `alembic/env.py` に全モデルがimportされていること

## コード規約
- inline importは避ける（循環回避は例外）
- `asyncio.get_running_loop()` を使用（`get_event_loop()` は非推奨）
- Noneの可能性がある値のフォーマットにはガード付ける（例: `value or 0`）

## 既知の注意事項
- Fly.ioのDockerは非rootユーザー(appuser)。`/app/`は読み取り専用、`/data/`のみ書き込み可
- ログファイルは `/data/yafuama.log`（RotatingFileHandler, 5MB×3）
- NoCacheMiddleware でHTML応答にCache-Control付与済み
- Feeds API: XML系は403。`JSON_LISTINGS_FEED`を使用
