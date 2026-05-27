# CF ProxyIP US IPv4

Automatically checked IPv4-only Cloudflare ProxyIP candidates.

## Live outputs

After GitHub Pages is enabled for this repository, these files are published from `docs/`:

- `all.txt` — all valid IPv4 results
- `us.txt` — valid IPv4 results passing the configured US rule
- `best.txt` — best IPv4 results by latency
- `full.json` — full report
- `v2ray.txt` — Base64 encoded list

## Run locally

```bash
python3 checker.py --remote --no-country-filter --output result.json --publish-dir docs --best-count 20
```

Strict `cf-ipcountry=US` mode:

```bash
python3 checker.py --remote --country US --output result.json --publish-dir docs --best-count 20
```

FOFA is optional. Add repository secret `FOFA_KEY`, then run with `--fofa`:

```bash
python3 checker.py --remote --fofa --fofa-size 200 --no-country-filter --output result.json --publish-dir docs
```

The checker is IPv4-only by default. Use `--include-domains` only if you intentionally want domain candidates.
