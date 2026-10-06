"""Reusable Swiss collection: uv run python -m buyers.collect_swiss."""

import argparse
import json
import sys
import uuid
from pathlib import Path
from urllib.parse import urljoin
import re

import httpx

from .collect import ROOT, USER_AGENT, Fetcher, atomic_write, now
from .adapters import discover_icon, soup
from .swiss import FX_URL, parse, parse_fx, quote_status


def collect_snapshot(sources, fetcher, previous=None, *, at=None):
    at = at or now()
    old = {b["buyer_id"]: b for b in (previous or {}).get("buyers", [])}
    buyers = []
    for source in sources:
        fetcher.documents = []
        ident = source["buyer_id"]
        buyer = {
            **source,
            "currency": "CHF",
            "status": "failed",
            "observed_at": None,
            "rates": [],
            "icon_url": None,
            "conditions": [],
            "eligibility": {
                "status": "unknown",
                "statement": None,
                "source_url": source["url"],
            },
            "condition_summary": "Tarifs indisponibles.",
            "warnings": [],
            "source_updated_at": None,
        }
        try:
            html = fetcher.get(source["url"])
            observed = fetcher.last_observed_at
            payload = None
            if ident == "goldencash":
                asset = soup(html).select_one('script[src*="_astro/estimation."]')
                if not asset:
                    raise ValueError("buyer calculator asset missing")
                script = fetcher.get(urljoin(source["url"], asset["src"]))
                if "/api/prices" not in script:
                    raise ValueError("buyer API reference changed")
                endpoint = urljoin(source["url"], "/api/prices")
                payload = json.loads(fetcher.get(endpoint))
                payload["_endpoint"] = endpoint
                observed = fetcher.last_observed_at
            elif ident == "geiger":
                script = next(
                    (
                        n.get_text()
                        for n in soup(html).find_all("script")
                        if "altgoldankauf.csv" in n.get_text()
                    ),
                    "",
                )
                endpoint = re.search(
                    r"xhr.open\('GET', '([^']+altgoldankauf\.csv)'", script
                )
                if not endpoint:
                    raise ValueError("buyer export reference missing")
                payload = fetcher.get(urljoin(source["url"], endpoint[1]))
                observed = fetcher.last_observed_at
            observation = parse(source, html, payload)
            status = quote_status(observation, at, old.get(ident))
            buyer.update(observation, status=status, observed_at=observed.isoformat())
            buyer["accepted_rates"] = (
                observation["rates"]
                if status == "ok"
                else old.get(ident, {}).get(
                    "accepted_rates", old.get(ident, {}).get("rates", [])
                )
            )
            buyer["icon_url"] = discover_icon(source["url"], html, fetcher)
            buyer["error"] = None
        except Exception as exc:
            if ident in old:
                # Keep provenance and original collection time; never call retained data fresh.
                for key in [
                    "rates",
                    "observed_at",
                    "source_updated_at",
                    "conditions",
                    "condition_summary",
                    "eligibility",
                    "icon_url",
                    "warnings",
                    "accepted_rates",
                ]:
                    buyer[key] = old[ident].get(key, buyer.get(key))
            buyer["error"] = {"type": type(exc).__name__, "message": str(exc)[:600]}
        buyer["attempted_at"] = at.isoformat()
        buyer["documents"] = fetcher.documents.copy()
        buyers.append(buyer)
        print(
            f"Swiss {ident}: {buyer['status']}"
            + (f" ({buyer['error']['message']})" if buyer.get("error") else ""),
            file=sys.stderr,
            flush=True,
        )
    fetcher.documents = []
    try:
        fx = parse_fx(fetcher.get(FX_URL), at)
        fx.update(status="ok", documents=fetcher.documents.copy())
    except Exception as exc:
        fx = {
            **((previous or {}).get("fx") or {}),
            "status": "failed",
            "error": str(exc)[:600],
            "attempted_at": at.isoformat(),
        }
    return {
        "schema_version": 1,
        "market": "CH",
        "currency": "CHF",
        "generated_at": at.isoformat(),
        "fx": fx,
        "buyers": buyers,
        "collection": {"outcomes": {b["buyer_id"]: b["status"] for b in buyers}},
    }


def coverage(snapshot):
    return sum(
        b["status"] == "ok"
        and bool(b["rates"])
        and b["eligibility"]["status"] != "swiss_residents_only"
        for b in snapshot["buyers"]
    )


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--catalog", type=Path, default=ROOT / "buyers/swiss_catalog.json")
    p.add_argument("--output", type=Path, default=ROOT / "data/swiss/latest.json")
    p.add_argument("--history-dir", type=Path, default=ROOT / "data/swiss/history")
    p.add_argument("--no-history", action="store_true")
    p.add_argument("--target", type=int, default=5)
    a = p.parse_args()
    sources = json.loads(a.catalog.read_text())["buyers"]
    if len({s["buyer_id"] for s in sources}) != len(sources):
        raise ValueError("duplicate Swiss buyer IDs")
    previous = json.loads(a.output.read_text()) if a.output.exists() else None
    if previous and (
        previous.get("schema_version") != 1 or previous.get("market") != "CH"
    ):
        raise ValueError("invalid previous Swiss snapshot")
    with httpx.Client(timeout=30, headers={"User-Agent": USER_AGENT}) as client:
        snapshot = collect_snapshot(sources, Fetcher(client), previous)
    atomic_write(a.output, snapshot)
    if not a.no_history:
        atomic_write(
            a.history_dir
            / f"{now().strftime('%Y-%m-%dT%H-%M-%SZ')}-{uuid.uuid4().hex[:8]}.json",
            snapshot,
        )
    count = coverage(snapshot)
    print(
        f"Swiss coverage: {count} usable buyers (excluding Swiss-only and outdated sources); ECB: {snapshot['fx']['status']}",
        file=sys.stderr,
    )
    return 0 if count >= a.target and snapshot["fx"]["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
