"""Coverage gate: count current accepted buying observations, not catalog entries."""

import argparse
import json
from pathlib import Path


def coverage(snapshot):
    usable = []
    for buyer in snapshot["buyers"]:
        if buyer["status"] != "ok" or buyer.get("retained_previous"):
            continue
        if any(
            r.get("price_eur_per_gram") is not None
            and not any(
                "mismatch" in w.lower() or "future source date" in w.lower()
                for w in r.get("warnings", [])
            )
            for r in buyer["rates"]
        ):
            usable.append(buyer["buyer_id"])
    return usable


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--snapshot", type=Path, default=Path("data/buyers/latest.json")
    )
    parser.add_argument("--target", type=int, default=25)
    args = parser.parse_args()
    data = json.loads(args.snapshot.read_text())
    usable = coverage(data)
    print(f"## Buyer coverage: {len(usable)}/{args.target}")
    print(
        "Accepted numeric buying observations in this snapshot; failed, retained, quarantined and formula-only buyers excluded."
    )
    for b in data["buyers"]:
        print(f"- {b['name']}: {b['status']}")
    return 0 if len(usable) >= args.target else 1


if __name__ == "__main__":
    raise SystemExit(main())
