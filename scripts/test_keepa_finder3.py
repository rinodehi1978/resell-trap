"""Step 2b: Fetch just 2 ASINs to minimize token cost."""

import asyncio
import httpx

KEEPA_API_KEY = "umbmhsqifsmd7i9vi8scp3o0312dnc04a9bmf9mar9nmn550ne4c7ke03vavhoem"
KEEPA_API_BASE = "https://api.keepa.com"
DOMAIN_JP = 5

ASINS = ["B09M6SKBHC", "B0CS3M9LM6"]


async def fetch():
    async with httpx.AsyncClient(timeout=30.0) as client:
        params = {
            "key": KEEPA_API_KEY,
            "domain": DOMAIN_JP,
            "asin": ",".join(ASINS),
            "stats": 90,
            "history": 0,
        }
        resp = await client.get(f"{KEEPA_API_BASE}/product", params=params)
        data = resp.json()
        print(f"Status: {resp.status_code} | Tokens left: {data.get('tokensLeft')}")

        if resp.status_code != 200:
            print("Waiting for tokens...")
            return

        products = data.get("products", [])
        for p in products:
            asin = p.get("asin", "")
            title = p.get("title", "N/A")
            model = p.get("model", "")
            brand = p.get("brand", "")
            stats = p.get("stats", {})
            drops30 = stats.get("salesRankDrops30", -1) if isinstance(stats, dict) else -1
            drops90 = stats.get("salesRankDrops90", -1) if isinstance(stats, dict) else -1
            current = stats.get("current", []) if isinstance(stats, dict) else []
            used_price = current[2] if len(current) > 2 else -1
            rank = current[3] if len(current) > 3 else -1

            print(f"\nASIN: {asin}")
            print(f"  Title: {title[:80]}")
            print(f"  Brand: {brand} | Model: [{model}]")
            print(f"  Used: {used_price:,} yen" if used_price > 0 else "  Used: N/A")
            print(f"  Rank: {rank:,}" if rank > 0 else "  Rank: N/A")
            print(f"  Drops30: {drops30} | Drops90: {drops90}")


asyncio.run(fetch())
