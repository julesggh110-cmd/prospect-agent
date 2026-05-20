"""Quick smoke test that quotas tracker is correctly wired."""
import sys
sys.path.insert(0, "/home/jules/prospect-agent")
import pipeline, run_campaign, quotas
from quotas import mark_used, summary, reset

# Reset to known state (current period only)
for svc in ("serper", "dropcontact", "here_maps"):
    reset(svc)

# Simulate
mark_used("serper", count=3)
mark_used("dropcontact")
mark_used("here_maps", count=2)

s = summary()
print(f"bottleneck: {s['bottleneck_service_label']} = {s['bottleneck_leads_remaining']} leads")
print()
for svc in s["services"]:
    if svc["used"] > 0:
        print(f"  {svc['label']}: used={svc['used']}/{svc['free_limit']}  → {svc['leads_capacity']} leads left")
print()
print("=== full table ===")
import subprocess
subprocess.run(["/home/jules/prospect-agent/.venv/bin/python", "/home/jules/prospect-agent/quotas.py"])
