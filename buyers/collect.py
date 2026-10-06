"""Run with uv run python -m buyers.collect. Logs on stderr, JSON on disk/stdout."""

import argparse
import hashlib
import json
import os
import sys
import tempfile
import time
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

import httpx

from .adapters import collect_source
from .models import Observation

ROOT = Path(__file__).resolve().parents[1]
USER_AGENT = "VendreSonOr/0.1 (+https://vendresonor.net; public buyer-rate collection)"


def now():
    return datetime.now(timezone.utc)


class Fetcher:
    def __init__(self, client, *, delay=0.3):
        self.client = client
        self.delay = delay
        self.robots = {}
        self.documents = []
        self.last_observed_at = None
        self.last_request = 0

    def allowed(self, url):
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        if origin not in self.robots:
            response = self.request(
                "GET", origin + "/robots.txt", check_robots=False, permit_404=True
            )
            parser = RobotFileParser()
            parser.parse(
                response.text.splitlines() if response.status_code != 404 else []
            )
            self.robots[origin] = parser
        if not self.robots[origin].can_fetch("VendreSonOr", url):
            raise PermissionError("robots.txt disallows collection of this URL")

    def request(self, method, url, *, check_robots=True, permit_404=False, data=None):
        if check_robots:
            self.allowed(url)
        for attempt in range(3):
            time.sleep(max(0, self.delay - (time.monotonic() - self.last_request)))
            try:
                # Explicit redirects ensure every destination passes its own crawl rules.
                response = self.client.request(
                    method, url, data=data, follow_redirects=False
                )
                self.last_request = time.monotonic()
                if response.is_redirect:
                    target = str(response.url.join(response.headers["location"]))
                    if target == url or attempt == 2:
                        raise ValueError("redirect loop or too many redirects")
                    url = target
                    if check_robots:
                        self.allowed(url)
                    continue
                if response.status_code in {429, 500, 502, 503, 504} and attempt < 2:
                    time.sleep(1 + attempt)
                    continue
                if not (permit_404 and response.status_code == 404):
                    response.raise_for_status()
                self.last_observed_at = now()
                self.documents.append(
                    {
                        "url": str(response.url),
                        "method": method,
                        "http_status": response.status_code,
                        "observed_at": self.last_observed_at.isoformat(),
                        "sha256": hashlib.sha256(response.content).hexdigest(),
                    }
                )
                return response
            except (httpx.TimeoutException, httpx.NetworkError):
                self.last_request = time.monotonic()
                if attempt == 2:
                    raise
                time.sleep(1 + attempt)
        raise ValueError("request did not complete")

    def get(self, url):
        return self.request("GET", url).text

    def post(self, url, data):
        # Only the catalogued read-only calculator API uses POST.
        return self.request("POST", url, data=data).text


def load_previous(path):
    if not path.exists():
        return None
    previous = json.loads(path.read_text())
    if previous.get("schema_version") != 1 or not isinstance(
        previous.get("buyers"), list
    ):
        raise ValueError("invalid previous snapshot; refusing to overwrite")
    ids = [b["buyer_id"] for b in previous["buyers"]]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate buyers in previous snapshot")
    for buyer in previous["buyers"]:
        Observation(
            buyer_id=buyer["buyer_id"],
            observed_at=buyer["observed_at"] or previous["generated_at"],
            rates=buyer["rates"],
        )
    return previous


def collect(sources, fetcher):
    attempts = []
    for source in sources:
        started = now()
        fetcher.documents = []
        try:
            obs = collect_source(source, fetcher)
            numeric = [r for r in obs.rates if r.price_eur_per_gram is not None]
            status = "ok" if numeric else ("formula_only" if obs.rates else "no_rates")
            if any(
                "Future source date" in warning
                for r in obs.rates
                for warning in r.warnings
            ):
                status = "quarantined"
            attempts.append(
                {
                    "buyer_id": source["buyer_id"],
                    "status": status,
                    "attempted_at": started.isoformat(),
                    "observation": obs.model_dump(mode="json"),
                    "error": None,
                }
            )
        except Exception as exc:  # noqa: BLE001 - isolate arbitrary source/parser failures
            # Isolate source failure; preserve other buyers and publish an explicit failure.
            attempts.append(
                {
                    "buyer_id": source["buyer_id"],
                    "status": "failed",
                    "attempted_at": started.isoformat(),
                    "observation": None,
                    "error": {"type": type(exc).__name__, "message": str(exc)[:700]},
                    "documents": fetcher.documents.copy(),
                }
            )
        print(
            f"{source['buyer_id']}: {attempts[-1]['status']}"
            + (
                f" ({attempts[-1]['error']['message']})"
                if attempts[-1]["error"]
                else ""
            ),
            file=sys.stderr,
        )
    return attempts


def merge_latest(previous, sources, attempts, generated_at):
    old = {b["buyer_id"]: b for b in (previous or {}).get("buyers", [])}
    by_id = {a["buyer_id"]: a for a in attempts}
    buyers = []
    for source in sources:
        ident = source["buyer_id"]
        prior = old.get(ident, {})
        attempt = by_id.get(ident)
        if attempt is None and prior:
            buyers.append(prior)
            continue
        record = {
            "buyer_id": ident,
            "name": source["name"],
            "source_url": source["url"],
            "transaction_channel": source["transaction_channel"],
            "reviewed_conditions": source.get("reviewed_conditions", []),
            "review_notes": source.get("review_notes", []),
            "conditions_reviewed_at": source.get("conditions_reviewed_at"),
            "rates": prior.get("rates", []),
            "conditions": prior.get("conditions", []),
            "documents": prior.get("documents", []),
            "observed_at": prior.get("observed_at"),
            "last_success_at": prior.get("last_success_at"),
            "last_attempt_at": attempt["attempted_at"] if attempt else None,
            "status": attempt["status"] if attempt else "not_attempted",
            "error": attempt["error"] if attempt else None,
            "retained_previous": False,
            "warnings": [],
            "quarantined_rates": [],
        }
        if attempt and attempt["observation"]:
            observation = attempt["observation"]
            # Large changes require review; only compare the same rate ID.
            prior_rates = {r["rate_id"]: r for r in prior.get("rates", [])}
            for rate in observation["rates"]:
                previous_rate = prior_rates.get(rate["rate_id"])
                if (
                    previous_rate
                    and rate["price_eur_per_gram"]
                    and previous_rate["price_eur_per_gram"]
                ):
                    change = abs(
                        Decimal(rate["price_eur_per_gram"])
                        / Decimal(previous_rate["price_eur_per_gram"])
                        - 1
                    )
                    if change > Decimal("0.30"):
                        record["status"] = "quarantined"
                        record["warnings"].append(
                            f"Large price change for {rate['rate_id']}; review required."
                        )
            if record["status"] == "quarantined":
                record["quarantined_rates"] = observation["rates"]
                record["warnings"].append(
                    "New observations quarantined; inspect immutable history."
                )
                record["retained_previous"] = bool(prior.get("rates"))
            else:
                for key in [
                    "rates",
                    "conditions",
                    "documents",
                    "observed_at",
                    "warnings",
                ]:
                    record[key] = observation[key]
                if observation["rates"]:
                    record["last_success_at"] = observation["observed_at"]
        elif attempt:
            record["retained_previous"] = bool(prior.get("rates"))
        buyers.append(record)
    return {
        "schema_version": 1,
        "generated_at": generated_at,
        "currency": "EUR",
        "unit": "per_gram_of_item_metal",
        "comparison_review_complete": False,
        "buyers": buyers,
    }


def encode(value):
    return json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n"


def atomic_write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, suffix=".tmp", delete=False
        ) as f:
            temp = Path(f.name)
            f.write(encode(value))
            f.flush()
            os.fsync(f.fileno())
        temp.replace(path)
    finally:
        if temp is not None:
            temp.unlink(missing_ok=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--only", nargs="+", help="Buyer IDs; otherwise attempt every catalogued buyer"
    )
    parser.add_argument("--catalog", type=Path, default=ROOT / "buyers/catalog.json")
    parser.add_argument(
        "--output",
        default=str(ROOT / "data/buyers/latest.json"),
        help="JSON path, or - for stdout",
    )
    parser.add_argument(
        "--history-dir", type=Path, default=ROOT / "data/buyers/history"
    )
    parser.add_argument("--no-history", action="store_true")
    parser.add_argument("--timeout", type=float, default=20)
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Exit 2 on any failed, quarantined or unsupported source",
    )
    args = parser.parse_args(argv)
    catalog = json.loads(args.catalog.read_text())["buyers"]
    identifiers = {s["buyer_id"] for s in catalog}
    if args.only and set(args.only) - identifiers:
        parser.error(
            "Unknown buyer IDs: " + ", ".join(sorted(set(args.only) - identifiers))
        )
    selected = [s for s in catalog if not args.only or s["buyer_id"] in args.only]
    output = None if args.output == "-" else Path(args.output)
    previous = load_previous(output) if output else None
    with httpx.Client(
        timeout=args.timeout, headers={"User-Agent": USER_AGENT}
    ) as client:
        attempts = collect(selected, Fetcher(client))
    timestamp = now()
    latest = merge_latest(previous, catalog, attempts, timestamp.isoformat())
    latest["collection"] = {
        "attempted": len(attempts),
        "outcomes": {
            a["buyer_id"]: next(
                b["status"] for b in latest["buyers"] if b["buyer_id"] == a["buyer_id"]
            )
            for a in attempts
        },
    }
    if not args.no_history:
        history = (
            args.history_dir
            / timestamp.strftime("%Y-%m-%d")
            / (timestamp.strftime("%H%M%S%f") + "-" + uuid.uuid4().hex[:8] + ".json")
        )
        history.parent.mkdir(parents=True, exist_ok=True)
        with history.open("x", encoding="utf-8") as f:
            f.write(
                encode(
                    {
                        "schema_version": 1,
                        "collected_at": timestamp.isoformat(),
                        "attempts": attempts,
                    }
                )
            )
    if output:
        atomic_write(output, latest)
    else:
        print(encode(latest), end="")
    numeric_buyers = sum(
        b["status"] == "ok" and b["buyer_id"] in {s["buyer_id"] for s in selected}
        for b in latest["buyers"]
    )
    count = sum(len(a["observation"]["rates"]) for a in attempts if a["observation"])
    print(
        f"Collected {count} records; {numeric_buyers}/{len(selected)} buyers with usable numeric observations.",
        file=sys.stderr,
    )
    if not numeric_buyers:
        return 1
    return (
        2
        if args.strict
        and any(
            s in {"failed", "quarantined", "no_rates"}
            for s in latest["collection"]["outcomes"].values()
        )
        else 0
    )


if __name__ == "__main__":
    raise SystemExit(main())
