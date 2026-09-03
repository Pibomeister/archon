# ENG-3060 Synthetic Bug Report

Canonical URL: https://linear.app/goodword/issue/ENG-3060/synthetic
Fetched at: 2026-09-01T00:00:00Z

## Reporter observations

1. The group shows **Granola** as a participant/provider even though Granola should not be present in the visible people list.
2. **Sahiba** is missing from the visible participants.
3. **Outputs** shows only one contact, even though the reporter expected multiple Outputs contacts in the group.

## Repro

```bash
python3 scripts/repro_eng3060_synthetic.py --fixture eng3060
```

Observed failure output:

```text
visible participants: Granola, Outputs
missing expected participant: Sahiba
Outputs contacts shown: 1
```
