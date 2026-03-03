#!/bin/bash
# ヤフアマ デプロイスクリプト
# 使い方: bash deploy.sh "変更内容の説明"

set -e

cd "$(dirname "$0")"

MSG="${1:-Auto deploy}"

echo "=== ヤフアマ デプロイ ==="

# 1. 未コミットの変更を確認
if [ -n "$(git status --porcelain)" ]; then
    echo "[1/4] 変更をコミット中..."
    git add -A
    git commit -m "$MSG"
else
    echo "[1/4] 未コミットの変更なし"
fi

# 2. pushs
echo "[2/4] GitHubにpush中..."
git push origin master

# 3. デプロイ
echo "[3/4] Fly.ioにデプロイ中..."
flyctl deploy --remote-only

# 4. 確認
echo "[4/4] ヘルスチェック..."
sleep 5
HEALTH=$(curl -s https://yafuama.fly.dev/partials/health)
echo "$HEALTH" | grep -o '[0-9]*件 / [0-9]*件 稼働中' || echo "ヘルスチェック応答: $HEALTH"

echo ""
echo "=== デプロイ完了 ==="
echo "URL: https://yafuama.fly.dev/"
