"""Quick experiment: test Keepa product finder for demand-based keyword discovery."""

import asyncio
import json

import httpx

KEEPA_API_KEY = "umbmhsqifsmd7i9vi8scp3o0312dnc04a9bmf9mar9nmn550ne4c7ke03vavhoem"
KEEPA_API_BASE = "https://api.keepa.com"
DOMAIN_JP = 5


async def test_finder():
    async with httpx.AsyncClient(timeout=30.0) as client:
        # Step 1: Check token balance
        params = {
            "key": KEEPA_API_KEY,
            "domain": DOMAIN_JP,
            "type": "product",
            "term": "test",
            "stats": 90,
        }
        resp = await client.get(f"{KEEPA_API_BASE}/search", params=params)
        data = resp.json()
        print(f"Current tokens: {data.get('tokensLeft')}")
        print(f"Status: {resp.status_code}")
        print()

        if resp.status_code != 200:
            print("Token not available yet.")
            return

        # Step 2: Product Finder - salesRankDrops30 >= 5, used price >= 10000 yen
        # Keepa JP: prices stored in yen (integer)
        for label, selection in [
            ("USED >= 10000 (raw yen)", {
                "salesRankDrops30_gte": 5,
                "current_USED_gte": 10000,
                "perPage": 50,
            }),
            ("USED >= 1000000 (cents?)", {
                "salesRankDrops30_gte": 5,
                "current_USED_gte": 1000000,
                "perPage": 50,
            }),
            ("Minimal: drops30 >= 5 only", {
                "salesRankDrops30_gte": 5,
                "perPage": 10,
            }),
        ]:
            print(f"=== Test: {label} ===")
            params2 = {
                "key": KEEPA_API_KEY,
                "domain": DOMAIN_JP,
                "selection": json.dumps(selection),
            }
            resp2 = await client.get(f"{KEEPA_API_BASE}/query", params=params2)
            data2 = resp2.json()
            print(f"  Status: {resp2.status_code}")
            print(f"  Tokens left: {data2.get('tokensLeft')}")
            if "error" in data2:
                print(f"  Error: {data2['error']}")
            products = data2.get("products", [])
            asin_list = data2.get("asinList", [])
            print(f"  Products: {len(products)}, ASIN list: {len(asin_list)}")

            if products:
                print()
                print("  --- Top results ---")
                with_model = 0
                for p in products[:10]:
                    title = p.get("title", "N/A")
                    model = p.get("model", "")
                    asin = p.get("asin", "")
                    stats = p.get("stats", {})
                    drops30 = -1
                    used_price = -1
                    if isinstance(stats, dict):
                        drops30 = stats.get("salesRankDrops30", -1)
                        current = stats.get("current", [])
                        used_price = current[2] if len(current) > 2 else -1
                    if model:
                        with_model += 1
                    print(f"    ASIN:{asin} Model:[{model}] Drops30:{drops30} Used:¥{used_price} | {title[:55]}")
                print(f"  Model rate: {with_model}/{min(len(products),10)}")
                break  # Found results, stop trying
            elif asin_list:
                print(f"  ASIN list sample: {asin_list[:5]}")
                break
            print()


asyncio.run(test_finder())
