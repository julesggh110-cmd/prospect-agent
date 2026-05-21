"""Debug BODACC API query."""
import httpx
import sys
sys.path.insert(0, "/home/jules/prospect-agent")

url = "https://bodacc-datadila.opendatasoft.com/api/explore/v2.1/catalog/datasets/annonces-commerciales/records"

# 1. No date filter at all → see all MOBIVIA announcements
r = httpx.get(url, params={"where": 'registre="470501545"', "limit": 50, "order_by": "dateparution DESC"})
results = r.json().get("results", [])
print(f"Without date filter: {len(results)} announcements")
for x in results[:10]:
    famille = x.get("familleavis_lib") or x.get("typeavis_lib")
    print(f"  {x.get('dateparution')} | {famille}")

# 2. Try date filter as plain ISO string
print()
print("Test date >= '2022-01-01':")
r = httpx.get(url, params={"where": 'registre="470501545" AND dateparution>="2022-01-01"', "limit": 50})
results = r.json().get("results", [])
print(f"  → {len(results)} announcements")

# 3. Try with date() function
print("Test date >= date'2022-01-01':")
r = httpx.get(url, params={"where": "registre=\"470501545\" AND dateparution>=date'2022-01-01'", "limit": 50})
results = r.json().get("results", [])
print(f"  → {len(results)} announcements")
