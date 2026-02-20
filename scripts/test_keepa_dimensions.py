"""Check if Keepa returns package dimensions for products."""

import asyncio
import json
import sys

import httpx

sys.stdout.reconfigure(encoding="utf-8")

KEEPA_API_KEY = "umbmhsqifsmd7i9vi8scp3o0312dnc04a9bmf9mar9nmn550ne4c7ke03vavhoem"
KEEPA_API_BASE = "https://api.keepa.com"
DOMAIN_JP = 5

# The refrigerator ASIN from earlier
ASINS = ["B09M6SKBHC", "B0FL2627Y3"]  # Fridge + Earbuds


async def run():
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
        print(f"Status: {resp.status_code}, Tokens: {data.get('tokensLeft')}")
        print()

        products = data.get("products", [])
        for p in products:
            asin = p.get("asin", "")
            title = p.get("title", "N/A")[:60]
            print(f"=== {asin}: {title} ===")
            print()

            # Check ALL dimension-related fields
            for key in ["packageHeight", "packageLength", "packageWidth", "packageWeight",
                        "itemHeight", "itemLength", "itemWidth", "itemWeight",
                        "packageDimension", "size", "format",
                        "productGroup", "categoryTree", "rootCategory",
                        "features", "description"]:
                val = p.get(key)
                if val is not None:
                    if isinstance(val, list) and len(val) > 3:
                        print(f"  {key}: {val[:3]}... ({len(val)} items)")
                    else:
                        print(f"  {key}: {val}")

            # Also dump all top-level keys to find dimension fields
            print()
            print(f"  All keys: {sorted(p.keys())}")
            print()


asyncio.run(run())
