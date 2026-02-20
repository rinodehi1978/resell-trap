"""Full analysis: fetch product finder results and analyze model number rate."""

import asyncio
import json
import sys

import httpx

# Force UTF-8 output
sys.stdout.reconfigure(encoding="utf-8")

KEEPA_API_KEY = "umbmhsqifsmd7i9vi8scp3o0312dnc04a9bmf9mar9nmn550ne4c7ke03vavhoem"
KEEPA_API_BASE = "https://api.keepa.com"
DOMAIN_JP = 5


async def run():
    async with httpx.AsyncClient(timeout=30.0) as client:
        # Step 1: Product Finder
        selection = {
            "salesRankDrops30_gte": 5,
            "current_USED_gte": 10000,
            "perPage": 50,
        }
        params = {
            "key": KEEPA_API_KEY,
            "domain": DOMAIN_JP,
            "selection": json.dumps(selection),
        }
        resp = await client.get(f"{KEEPA_API_BASE}/query", params=params)
        data = resp.json()
        print(f"Finder: status={resp.status_code}, tokens={data.get('tokensLeft')}")

        asin_list = data.get("asinList", [])
        print(f"ASINs found: {len(asin_list)}")
        print()

        if not asin_list:
            print("No results.")
            return

        # Step 2: Fetch details for first 10 ASINs only (save tokens)
        batch = asin_list[:10]
        params2 = {
            "key": KEEPA_API_KEY,
            "domain": DOMAIN_JP,
            "asin": ",".join(batch),
            "stats": 90,
            "history": 0,
        }
        resp2 = await client.get(f"{KEEPA_API_BASE}/product", params=params2)
        data2 = resp2.json()
        print(f"Product: status={resp2.status_code}, tokens={data2.get('tokensLeft')}")
        print()

        products = data2.get("products", [])
        with_model = 0
        with_used = 0

        for p in products:
            asin = p.get("asin", "")
            title = p.get("title", "N/A")
            model = p.get("model", "")
            brand = p.get("brand", "")
            stats = p.get("stats", {})
            drops30 = stats.get("salesRankDrops30", -1) if isinstance(stats, dict) else -1
            current = stats.get("current", []) if isinstance(stats, dict) else []
            used_price = current[2] if len(current) > 2 else -1
            rank = current[3] if len(current) > 3 else -1

            if model:
                with_model += 1
            if used_price > 0:
                with_used += 1

            print(f"ASIN: {asin}")
            print(f"  {title[:80]}")
            print(f"  Brand: {brand}")
            print(f"  Model: [{model}]")
            print(f"  Used: {used_price:,} yen | Rank: {rank:,} | Drops30: {drops30}")
            print()

        total = len(products)
        print("=" * 60)
        print(f"TOTAL: {total} products")
        print(f"With model number: {with_model}/{total} ({with_model*100//max(total,1)}%)")
        print(f"With used price: {with_used}/{total}")
        print()
        print("Conclusion:")
        if with_model >= total * 0.5:
            print("  -> High model number rate! Demand-based strategy is viable.")
        else:
            print("  -> Low model number rate. May need additional filtering.")


asyncio.run(run())
