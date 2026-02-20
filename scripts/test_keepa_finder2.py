"""Step 2: Fetch product details for ASINs found by product finder."""

import asyncio
import json

import httpx

KEEPA_API_KEY = "umbmhsqifsmd7i9vi8scp3o0312dnc04a9bmf9mar9nmn550ne4c7ke03vavhoem"
KEEPA_API_BASE = "https://api.keepa.com"
DOMAIN_JP = 5

# ASINs from previous finder result
ASINS = ["B09M6SKBHC", "B0CS3M9LM6", "B0DP326PZP", "B08XKKG7WK", "B0DR85FPYT",
         "B09M6SKBHC", "B0CS3M9LM6", "B0DP326PZP", "B08XKKG7WK", "B0DR85FPYT"]
# Deduplicate
ASINS = list(dict.fromkeys(ASINS))[:10]


async def fetch_details():
    async with httpx.AsyncClient(timeout=30.0) as client:
        # Query product details for the ASINs
        params = {
            "key": KEEPA_API_KEY,
            "domain": DOMAIN_JP,
            "asin": ",".join(ASINS[:10]),
            "stats": 90,
            "history": 0,
        }
        resp = await client.get(f"{KEEPA_API_BASE}/product", params=params)
        data = resp.json()
        print(f"Status: {resp.status_code}")
        print(f"Tokens left: {data.get('tokensLeft')}")
        print()

        products = data.get("products", [])
        print(f"Products returned: {len(products)}")
        print()

        with_model = 0
        total = len(products)

        for p in products:
            asin = p.get("asin", "")
            title = p.get("title", "N/A")
            model = p.get("model", "")
            brand = p.get("brand", "")
            category = p.get("categoryTree", [])
            cat_name = category[0].get("name", "") if category else ""

            stats = p.get("stats", {})
            drops30 = -1
            drops90 = -1
            used_price = -1
            rank = -1
            if isinstance(stats, dict):
                drops30 = stats.get("salesRankDrops30", -1)
                drops90 = stats.get("salesRankDrops90", -1)
                current = stats.get("current", [])
                used_price = current[2] if len(current) > 2 else -1
                rank = current[3] if len(current) > 3 else -1

            if model:
                with_model += 1

            print(f"ASIN: {asin}")
            print(f"  Title: {title[:80]}")
            print(f"  Brand: {brand} | Model: [{model}]")
            print(f"  Category: {cat_name}")
            print(f"  Used Price: ¥{used_price:,}" if used_price > 0 else f"  Used Price: N/A")
            print(f"  Rank: {rank:,}" if rank > 0 else f"  Rank: N/A")
            print(f"  Drops30: {drops30} | Drops90: {drops90}")
            print()

        print(f"=== Summary ===")
        print(f"Total: {total}")
        print(f"With model number: {with_model}/{total} ({with_model*100//max(total,1)}%)")


asyncio.run(fetch_details())
