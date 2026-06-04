# cf-proxyip-us

Canonical path: `/home/workspace/Projects/cf-proxyip-us`.

Purpose: Cloudflare Worker + KV + DNS-only single A-record ProxyIP distribution for a stable current IP plus standby pool.

Operational rules:
- Do not edit `/home/workspace/cf-proxyip-us`; it is an older duplicate copy.
- Do not run `wrangler deploy`, `scripts/sync_dns.py`, `scripts/sync_kv.py`, or push to GitHub without explicit live-operation confirmation.
- Keep Worker as a distribution layer only; candidate collection and validation belong in local/CI Python scripts.
- After code/config changes, run:
  - `python3 -m py_compile build_dataset.py scripts/*.py`
  - `node --check worker.js`
  - `python3 scripts/validate_outputs.py`

Key files:
- `build_dataset.py`: candidate collection, validation, ranking, failover, docs output generation.
- `worker.js`: auth, API routing, KV reads, public status page.
- `scripts/auto_update.py`: end-to-end CI automation for generate/validate/sync/deploy/audit/commit.
- `scripts/validate_outputs.py`: pure local docs consistency validation.
- `.github/workflows/proxyip-auto-update.yml`: scheduled GitHub Actions entrypoint.
- `docs/implementation-plan-2026-06-04.md`: staged improvement plan from the 2026-06-04 audit.
