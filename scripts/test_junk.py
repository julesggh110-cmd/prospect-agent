"""Quick smoke test for the junk-name detector."""
import sys
sys.path.insert(0, "/home/jules/prospect-agent")
from pipeline import is_junk_company_name

cases = [
    "UDIFE",
    "UNION DES INDEPENDANTS POUR UN FUNERAIRE ENGAGE",
    "GALIAN-SMABTP",
    "MOBIVIA",                              # not junk
    "EVO +",                                # not junk
    "AGENCE DE DEVELOPPEMENT ECONOMIQUE D OCCITANIE",
    "MUTUELLE GENERALE",
    "SYNDICAT DES TRAVAILLEURS",
    "FEDERATION FRANCAISE",
    "CHAMBRE DE COMMERCE DE TOULOUSE",
    "STEP CONSULTING",                      # not junk
    "EXECUTIVE RELOCATIONS",                # not junk
    "QUATERNAIRE",                          # not junk
    "ADEROC SAS",
    "PEPS SYNDICAT",
]
for n in cases:
    verdict = "JUNK" if is_junk_company_name(n) else "keep"
    print(f"  {n:55s} → {verdict}")
