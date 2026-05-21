import httpx
r = httpx.get("https://recherche-entreprises.api.gouv.fr/search", params={
    "activite_principale": "70.22Z,78.10Z",
    "departement": "31,34",
    "tranche_effectif_salarie": "21",
    "per_page": 3,
})
data = r.json()
print(f"status={r.status_code} total={data.get('total_results')}")
for c in data.get("results", [])[:3]:
    siege = c.get("siege") or {}
    print(f"  {c.get('nom_complet')} | NAF={c.get('activite_principale')} | {siege.get('libelle_commune')}")
