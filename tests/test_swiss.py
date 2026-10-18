"""Captured real buyer structures: buying/spot, restrictions, staleness, API fallbacks."""

import json
import re
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest

from buyers import collect_swiss as module
from buyers.adapters import SourceChanged, SourceUnavailable
from buyers.collect_swiss import collect_snapshot, coverage
from buyers.health import error_details
from buyers.swiss import parse, parse_fx, quote_status

FIXTURES = Path(__file__).parent / "fixtures/swiss"
SOURCES = {
    s["buyer_id"]: s
    for s in json.loads(Path("buyers/swiss_catalog.json").read_text())["buyers"]
}
AT = datetime(2026, 10, 6, 13, tzinfo=timezone.utc)


def captured(ident, html=None, payload=None):
    if ident == "goldencash" and payload is None:
        payload = json.loads((FIXTURES / "goldencash-api.json").read_text())
        payload["_endpoint"] = "https://www.goldencash.ch/api/prices"
    if ident == "geiger" and payload is None:
        payload = (FIXTURES / "geiger-api.txt").read_text()
    return parse(
        SOURCES[ident], html or (FIXTURES / f"{ident}.html").read_text(), payload
    )


@pytest.mark.parametrize(
    "ident,count",
    [
        ("swissgoldexchange", 5),
        ("goldencash", 4),
        ("bijouxor", 3),
        ("geiger", 7),
        ("hausgold", 7),
        ("orcash", 5),
        ("valorum", 6),
    ],
)
def test_real_buying_rates_and_no_market_fallback(ident, count):
    obs = captured(ident)
    assert len(obs["rates"]) == count
    assert all(float(r["price_chf_per_gram"]) > 0 for r in obs["rates"])
    with pytest.raises((SourceChanged, ValueError)):
        captured(ident, html="<p>Or spot 18K 750 : 99 CHF/g</p>")


def test_spot_and_coin_prices_never_become_buying_offers():
    rates = captured("goldencash")["rates"]
    assert {r["purity_carats"] for r in rates} == {9, 14, 18, 22}
    # API 24K is the spot ticker. Only the advertised buying table is eligible.
    assert (
        next(r for r in rates if r["purity_carats"] == 18)["price_chf_per_gram"]
        == "76.07"
    )
    assert (
        next(
            r
            for r in captured("swissgoldexchange")["rates"]
            if r["purity_carats"] == 24
        )["price_chf_per_gram"]
        == "107.63"
    )
    assert len(captured("bijouxor")["rates"]) == 3  # excludes 640 CHF/coin


@pytest.mark.parametrize("carats", [9, 14, 18, 22, 24])
def test_zero_price_is_unavailable_for_any_buyer_not_a_closure(carats):
    from buyers.swiss import rate

    with pytest.raises(SourceUnavailable, match="zero"):
        rate(carats, "00")
    with pytest.raises(SourceChanged, match="invalid CHF"):
        rate(carats, "1000")


def test_zero_price_does_not_hide_changed_purity():
    from buyers.swiss import rate

    for carats, permille in [(19, 750), (18, 900)]:
        with pytest.raises(SourceChanged) as error:
            rate(carats, "00", permille=permille)
        assert error_details(error.value)["severity"] == "error"


def test_zero_prices_retain_original_provenance_and_recover():
    original = {
        "buyer_id": "bijouxor",
        **captured("bijouxor"),
        "status": "ok",
        "observed_at": "2026-10-05T10:00:00+00:00",
        "documents": [],
    }

    class Fetcher:
        last_observed_at = AT
        zero = True

        def __init__(self):
            self.documents = []

        def get(self, url):
            if url.endswith("eurofxref-daily.xml"):
                return '<root><Cube time="2026-10-05"><Cube currency="CHF" rate="0.9311"/></Cube></root>'
            html = (FIXTURES / "bijouxor.html").read_text()
            if self.zero:
                html = re.sub(r"[\d.,]+ CHF par gramme", "00 CHF par gramme", html)
            return html

    fetcher = Fetcher()
    snapshot = collect_snapshot(
        [SOURCES["bijouxor"]],
        fetcher,
        {"buyers": [original]},
        at=AT,
    )
    buyer = snapshot["buyers"][0]
    assert buyer["status"] == "failed" and buyer["error"]["severity"] == "warning"
    assert buyer["rates"] == original["rates"]
    assert buyer["observed_at"] == original["observed_at"]
    assert coverage(snapshot) == 0
    fetcher.zero = False
    recovered = collect_snapshot([SOURCES["bijouxor"]], fetcher, snapshot, at=AT)
    assert recovered["buyers"][0]["status"] == "ok" and coverage(recovered) == 1


@pytest.mark.parametrize(
    "exc,usable,fx_error,expected",
    [
        (SourceUnavailable("zero quote"), True, None, 0),
        (SourceChanged("formula changed"), True, None, 1),
        (SourceUnavailable("zero quote"), False, None, 1),
        (SourceUnavailable("zero quote"), True, httpx.ReadTimeout("offline"), 0),
        (SourceUnavailable("zero quote"), True, SourceChanged("bad XML"), 1),
    ],
)
def test_ci_coverage_warning_and_execution_errors(
    tmp_path,
    monkeypatch,
    exc,
    usable,
    fx_error,
    expected,
):
    snapshot = {
        "schema_version": 1,
        "market": "CH",
        "buyers": [
            {
                "buyer_id": "outage",
                "status": "failed",
                "error": error_details(exc),
                "rates": [],
                "eligibility": {"status": "unknown"},
            }
        ],
        "fx": {"status": "ok"},
    }
    if usable:
        snapshot["buyers"].append(
            {
                "buyer_id": "available",
                "status": "ok",
                "rates": [{"price": "80"}],
                "eligibility": {"status": "unknown"},
            }
        )
    if fx_error:
        snapshot["fx"] = {
            "status": "failed",
            "error": str(fx_error),
            "error_severity": error_details(fx_error)["severity"],
        }
    monkeypatch.setattr(module, "collect_snapshot", lambda *a, **kw: snapshot)
    args = ["--output", str(tmp_path / "latest.json"), "--no-history", "--target", "5"]
    assert module.main(args) == 1  # Strict coverage remains available outside CI.
    assert module.main(args + ["--ci"]) == expected


def test_foreign_customer_conditions_and_minimum_are_preserved():
    golden = captured("goldencash")
    assert all(r["min_weight_g"] == 10 for r in golden["rates"])
    assert golden["eligibility"]["status"] == "conditional"
    assert "douane" in golden["eligibility"]["statement"]
    assert captured("geiger")["eligibility"]["status"] == "conditional"
    assert captured("orcash")["eligibility"]["status"] == "swiss_residents_only"
    html = (
        (FIXTURES / "orcash.html")
        .read_text()
        .replace(
            "limiter ses achats aux personnes domiciliées en Suisse",
            "accueillir tous les clients",
        )
    )
    with pytest.raises(SourceChanged, match="policy changed"):
        captured("orcash", html)
    assert captured("bijouxor")["eligibility"]["status"] == "unknown"


def test_live_api_failure_must_not_use_static_fallback():
    payload = json.loads((FIXTURES / "goldencash-api.json").read_text())
    payload.update(mode="fallback", _endpoint="https://www.goldencash.ch/api/prices")
    with pytest.raises(SourceChanged, match="fallback excluded"):
        captured("goldencash", payload=payload)
    with pytest.raises(SourceChanged, match="identifier changed"):
        captured("geiger", payload="unknown\n99.34")
    r = next(r for r in captured("geiger")["rates"] if r["purity_carats"] == 18)
    assert r["price_chf_per_gram"] == "74.51"  # buyer's published 99.34 base * 750/1000


def test_old_source_future_date_and_large_price_jump_are_quarantined():
    assert quote_status(captured("valorum"), AT) == "stale_source"
    obs = captured("swissgoldexchange")
    assert quote_status(obs, AT) == "ok"
    assert (
        quote_status({**obs, "source_updated_at": "2027-01-01T00:00:00+01:00"}, AT)
        == "quarantined"
    )
    old = {**obs, "rates": [{**r, "price_chf_per_gram": "1"} for r in obs["rates"]]}
    assert quote_status(obs, AT, old) == "quarantined"


def test_conflicting_displayed_and_calculator_prices_fail():
    html = (
        (FIXTURES / "swissgoldexchange.html")
        .read_text()
        .replace('data-preis="107.63"', 'data-preis="999"')
    )
    with pytest.raises(SourceChanged, match="conflicts"):
        captured("swissgoldexchange", html)


def test_ecb_base_direction_date_and_missing_currency():
    xml = '<root><Cube time="2026-10-05"><Cube currency="CHF" rate="0.9311"/></Cube></root>'
    fx = parse_fx(xml, AT)
    assert fx["base_currency"] == "EUR" and fx["quote_currency"] == "CHF"
    assert fx["chf_per_eur"] == "0.9311"
    for broken in [
        xml.replace("2026-10-05", "2026-10-07"),
        xml.replace("2026-10-05", "2026-09-01"),
        xml.replace("CHF", "USD"),
        xml.replace("0.9311", "0"),
    ]:
        with pytest.raises(SourceChanged):
            parse_fx(broken, AT)


def test_failed_collection_retains_original_time_but_excludes_coverage():
    previous = {
        "buyers": [
            {
                "buyer_id": "bijouxor",
                **captured("bijouxor"),
                "status": "ok",
                "observed_at": "2026-10-05T10:00:00+00:00",
                "documents": [
                    {"url": SOURCES["bijouxor"]["url"], "sha256": "original"}
                ],
            }
        ]
    }

    class Broken:
        documents = []

        def get(self, url):
            self.documents.append({"url": url, "status_code": 503})
            raise RuntimeError("network down")

    snapshot = collect_snapshot([SOURCES["bijouxor"]], Broken(), previous, at=AT)
    buyer = snapshot["buyers"][0]
    assert buyer["rates"] == previous["buyers"][0]["rates"]
    assert buyer["observed_at"] == "2026-10-05T10:00:00+00:00"
    assert buyer["status"] == "failed" and coverage(snapshot) == 0
    assert buyer["documents"] == previous["buyers"][0]["documents"]
    assert buyer["attempt_documents"] == [
        {"url": SOURCES["bijouxor"]["url"], "status_code": 503}
    ]
    assert snapshot["fx"]["status"] == "failed"


def test_quarantine_does_not_accept_the_same_bad_jump_next_run():
    obs = captured("swissgoldexchange")
    original = [{**r, "price_chf_per_gram": "1"} for r in obs["rates"]]
    previous = {**obs, "status": "quarantined", "accepted_rates": original}
    assert quote_status(obs, AT, previous) == "quarantined"
    html = (
        (FIXTURES / "swissgoldexchange.html")
        .read_text()
        .replace('data-feinheit="0.750"', 'data-feinheit="0.999"')
    )
    with pytest.raises(SourceChanged, match="fineness disagree"):
        captured("swissgoldexchange", html)


@pytest.mark.parametrize("change", ["divisor", "discount"])
def test_changed_gold_formula_cannot_be_validated_by_unchanged_silver(change):
    html = (FIXTURES / "geiger.html").read_text()
    if change == "divisor":
        html = html.replace("/1000", "/10000", 1)
    else:
        html = html.replace("(price *", "(price * .9 *", 1)
    with pytest.raises(SourceChanged, match="formula changed"):
        captured("geiger", html)


def test_repeated_failed_buyer_recovers_and_legacy_null_baseline_is_repaired():
    class Fetcher:
        documents = []
        last_observed_at = AT
        broken = True

        def get(self, url):
            if self.broken:
                raise RuntimeError("network down")
            if url.endswith("eurofxref-daily.xml"):
                return '<root><Cube time="2026-10-05"><Cube currency="CHF" rate="0.9311"/></Cube></root>'
            return (FIXTURES / "bijouxor.html").read_text()

    fetcher = Fetcher()
    snapshot = None
    for _ in range(2):
        snapshot = collect_snapshot([SOURCES["bijouxor"]], fetcher, snapshot, at=AT)
        assert snapshot["buyers"][0]["accepted_rates"] == []
    # Older snapshots emitted null after repeated network failures.
    snapshot["buyers"][0]["accepted_rates"] = None
    fetcher.broken = False
    for _ in range(2):
        snapshot = collect_snapshot([SOURCES["bijouxor"]], fetcher, snapshot, at=AT)
        buyer = snapshot["buyers"][0]
        assert buyer["status"] == "ok" and len(buyer["accepted_rates"]) == 3
