# VendreSonOr data

Public French gold-buying prices used by https://vendresonor.netlify.app. Website source stays private. Default branch: `master`.

## Collection

Python 3.12, uv. No buyer API keys.

```sh
uv sync --locked --group dev
uv run --locked pytest -q
uv run --locked python -m buyers.collect
```

- Latest JSON: [`data/buyers/latest.json`](data/buyers/latest.json).
- Immutable run observations: `data/buyers/history/YYYY-MM-DD/`.
- Selected buyers: `uv run --locked python -m buyers.collect --only godot goldson`.
- JSON stdout: `uv run --locked python -m buyers.collect --output - --no-history`.
- Options: `--catalog`, `--output`, `--history-dir`, `--timeout`, `--strict`.

15 catalogued buyers. Rates carry purity, decimal EUR/g, quote type, product, channel, source URL, observation time and conditions. Source publication dates are included when available. Some buyers publish formulas or block automated access; gaps are explicit.

Failures preserve previous rates and their original timestamps. Changes above 30% are quarantined. Snapshot generation time does not refresh individual rates. The website suspends estimates after 24 hours.

`comparison_eligible: false`: published prices are not verified net payouts. Fees, taxes and product eligibility require review before claiming a best offer. Consult each buyer's linked source and conditions.

## Automation

**Collect buyer rates** runs at 00:17, 06:17, 12:17 and 18:17 UTC, plus manual dispatch. GitHub may delay scheduled runs. Tests precede collection; observations are uploaded and generated JSON is committed to `master`.

After a successful collection, the workflow requests a Netlify rebuild via the `NETLIFY_BUILD_HOOK` repository secret. Its payload pins the website build to the exact public data commit. Failed collection runs record outcomes without triggering a rebuild. No website repository token is needed.

Exit codes: 0 = accepted numeric rates from at least one attempted buyer; 1 = none; 2 = partial failures with `--strict`. Collection respects robots rules, request spacing and bounded retries; blocked access is not bypassed.

