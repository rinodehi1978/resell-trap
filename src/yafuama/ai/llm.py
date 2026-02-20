"""Optional Claude API integration for smarter keyword suggestions."""

from __future__ import annotations

import json
import logging

import httpx

from .analyzer import KeywordInsights
from .generator import CandidateProposal

logger = logging.getLogger(__name__)

ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
MODEL = "claude-haiku-4-5-20251001"


async def get_llm_suggestions(
    insights: KeywordInsights,
    api_key: str,
    max_suggestions: int = 10,
) -> list[CandidateProposal]:
    """Call Claude API for keyword suggestions based on deal history analysis.

    Returns empty list on any error (never blocks the discovery cycle).
    """
    if not api_key:
        return []

    # Build context from insights
    top_kws = "\n".join(
        f"- {kp.keyword}: {kp.total_deals}件Deal, 平均利益¥{kp.avg_gross_profit:,.0f}, スコア{kp.performance_score:.2f}"
        for kp in insights.top_keywords[:10]
        if kp.total_deals > 0
    )

    brands = ", ".join(
        f"{b.brand_name}({b.deal_count}件)"
        for b in insights.brand_patterns[:10]
    )

    product_types = ", ".join(
        p.product_type for p in insights.product_type_patterns[:10]
    )

    prompt = f"""あなたはヤフオク→Amazon転売（無在庫）のキーワードリサーチの専門家です。
以下のデータは過去の成功Deal（ヤフオクで安く仕入れてAmazonで利益が出た取引）の分析結果です。

## 実績のあるキーワード
{top_kws or "（まだ十分なデータがありません）"}

## 利益の出るブランド
{brands or "（未分析）"}

## よく出る商品種別
{product_types or "（未分析）"}

## 過去Deal総数: {insights.total_deals}件

この分析結果に基づき、新しい検索キーワードを{max_suggestions}件提案してください。
条件：
- ヤフオクで安く出品されていてAmazonで高く売れそうな商品のキーワード
- 既存キーワードと被らないこと
- 2〜4語の具体的なキーワード（ブランド名+商品種別など）

以下のJSON形式で回答してください：
[{{"keyword": "キーワード", "reasoning": "理由", "confidence": 0.5}}]

JSONのみ出力してください。"""

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                ANTHROPIC_API_URL,
                headers={
                    "x-api-key": api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": MODEL,
                    "max_tokens": 1024,
                    "messages": [{"role": "user", "content": prompt}],
                },
            )

        if resp.status_code != 200:
            logger.warning("Claude API returned %d: %s", resp.status_code, resp.text[:200])
            return []

        data = resp.json()
        content = data.get("content", [{}])[0].get("text", "")

        # Parse JSON from response
        suggestions = json.loads(content)
        if not isinstance(suggestions, list):
            return []

        candidates = []
        for s in suggestions[:max_suggestions]:
            if not isinstance(s, dict) or "keyword" not in s:
                continue
            candidates.append(CandidateProposal(
                keyword=s["keyword"],
                strategy="llm",
                confidence=min(float(s.get("confidence", 0.5)), 1.0),
                parent_keyword_id=None,
                reasoning=s.get("reasoning", "Claude API提案"),
            ))

        logger.info("Claude API suggested %d keywords", len(candidates))
        return candidates

    except Exception as e:
        logger.warning("Claude API call failed: %s", e)
        return []


# ---------------------------------------------------------------------------
# Cold-start: generate seed keywords for a brand-new system
# ---------------------------------------------------------------------------

_SEED_PROMPT = """\
あなたはヤフオク→Amazon中古転売の市場リサーチ専門家です。

日本のヤフオク（Yahoo!オークション）で安く仕入れて、Amazon.co.jpの中古品として出品し、
価格差で利益を得るビジネスモデルにおいて、初期キーワードを提案してください。

## 条件
- ヤフオクで頻繁に出品されていて、Amazon中古でも需要がある商品ジャンル
- 粗利率40%以上・粗利3,000円以上が狙えるもの
- 具体的な検索キーワード（ブランド名+商品種別の2〜4語）
- 以下のカテゴリをバランスよくカバー:
  ゲーム機・ゲームソフト、オーディオ、カメラ、家電、PC周辺機器、
  フィギュア・ホビー、トレーディングカード
- 【重要】アパレル・ファッション関連は完全に除外すること（靴、服、バッグ、財布、アクセサリー、Nike、Adidas、ヴィトン、グッチ等のファッションブランドすべて不可）

## 出力
{count}件のキーワードをJSON配列で出力してください。他のテキストは不要です。

[{{"keyword": "Nintendo Switch Pro コントローラー", "category": "ゲーム", "reasoning": "純正コントローラーは中古でも需要が高く、ヤフオクでは3000-4000円、Amazon中古では6000-7000円で取引される", "confidence": 0.7}}]
"""


async def get_seed_keywords(
    api_key: str,
    count: int = 40,
) -> list[dict]:
    """Generate initial seed keywords for a cold-start system.

    Returns a list of dicts with keys: keyword, category, reasoning, confidence.
    Returns empty list on any error.
    """
    if not api_key:
        return []

    prompt = _SEED_PROMPT.format(count=count)

    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(
                ANTHROPIC_API_URL,
                headers={
                    "x-api-key": api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": MODEL,
                    "max_tokens": 4096,
                    "messages": [{"role": "user", "content": prompt}],
                },
            )

        if resp.status_code != 200:
            logger.warning("Seed keywords: Claude API returned %d: %s", resp.status_code, resp.text[:200])
            return []

        data = resp.json()
        content = data.get("content", [{}])[0].get("text", "")

        # Strip markdown fences if present
        content = content.strip()
        if content.startswith("```"):
            content = content.split("\n", 1)[-1]
        if content.endswith("```"):
            content = content.rsplit("```", 1)[0]
        content = content.strip()

        suggestions = json.loads(content)
        if not isinstance(suggestions, list):
            return []

        results = []
        for s in suggestions[:count]:
            if not isinstance(s, dict) or "keyword" not in s:
                continue
            results.append({
                "keyword": str(s["keyword"]).strip(),
                "category": str(s.get("category", "")),
                "reasoning": str(s.get("reasoning", "")),
                "confidence": min(float(s.get("confidence", 0.5)), 1.0),
            })

        logger.info("Seed keywords: Claude suggested %d keywords", len(results))
        return results

    except Exception as e:
        logger.warning("Seed keywords: Claude API call failed: %s", e)
        return []
