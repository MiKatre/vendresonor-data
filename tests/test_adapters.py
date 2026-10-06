import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from buyers import adapters
from buyers.models import Observation

FIXTURES = Path(__file__).parent / "fixtures"
CATALOG = {
    b["buyer_id"]: b
    for b in json.loads((FIXTURES.parents[1] / "buyers/catalog.json").read_text())[
        "buyers"
    ]
}
AT = datetime(2026, 10, 5, 21, 50, tzinfo=timezone.utc)


def parse(ident, *, html=None, js=None):
    html = html if html is not None else (FIXTURES / f"{ident}.html").read_text()
    args = [CATALOG[ident], AT, html]
    if ident == "godot":
        args = [CATALOG[ident], AT, (FIXTURES / "godot.json").read_text(), html]
    elif ident in {"or_heritage", "orencash", "lesmonnaiesdelyon"}:
        args.append(js if js is not None else (FIXTURES / f"{ident}.js").read_text())
        if ident == "or_heritage":
            args.append((FIXTURES / "or_heritage.json").read_text())
    return getattr(adapters, "heritage" if ident == "or_heritage" else ident)(*args)


@pytest.mark.parametrize(
    "ident,count",
    [
        ("goldson", 5),
        ("change_vivienne", 5),
        ("godot", 5),
        ("epo", 2),
        ("or_heritage", 5),
        ("antheor", 6),
        ("abacor", 2),
        ("gold_fr", 3),
        ("orencash", 6),
        ("cuo", 1),
        ("comptoircentral", 4),
        ("lesmonnaiesdelyon", 3),
    ],
)
def test_captured_sources(ident, count):
    obs = parse(ident)
    assert len(obs.rates) == count
    assert all(not r.comparison_eligible for r in obs.rates)
    Observation.model_validate_json(obs.model_dump_json())


def test_goldson_product_tables_stay_separate():
    rates = parse("goldson").rates
    assert next(
        r for r in rates if r.purity_carats == 18
    ).price_eur_per_gram == Decimal("65.44")
    assert {r.product_kind for r in rates} == {"jewellery_for_melting"}
    assert not any(r.purity_carats == 24 for r in rates)


def test_vivienne_displayed_price_and_unrounded_total_input():
    r = next(r for r in parse("change_vivienne").rates if r.purity_carats == 18)
    assert r.price_eur_per_gram == Decimal("81.84")
    assert r.source_fields["unrounded_eur_per_gram"] == "81.843425901138"


def test_godot_silver_excluded_and_conditions_retained():
    obs = parse("godot")
    assert {r.purity_carats for r in obs.rates} == {9, 14, 18, 22, 24}
    assert any("100€" in condition for condition in obs.conditions)
    assert (
        next(r for r in obs.rates if r.purity_carats == 18).source_fields["commission"]
        == 11.5
    )


def test_buyer_calculators_use_source_formulas():
    obs = parse("or_heritage")
    r = next(r for r in obs.rates if r.purity_carats == 18)
    expected = (
        Decimal("3697.12") / Decimal("31.1035") * Decimal("0.6565858231627923")
    ).quantize(Decimal("0.01"))
    assert r.price_eur_per_gram == expected
    assert r.source_updated_at is None
    assert "reference_updated_at" in r.source_fields
    oec = parse("orencash")
    r = next(r for r in oec.rates if r.purity_carats == 18)
    assert r.price_eur_per_gram == Decimal("118.614") * Decimal(".750") * Decimal(
        ".993"
    ) * Decimal(".985") - Decimal("13.5")
    assert all(r.origin == "buyer_calculator" for r in oec.rates)


def test_antheor_timestamp_minimum_and_distinct_bullion():
    obs = parse("antheor")
    assert all(r.min_weight_g == 10 for r in obs.rates)
    assert obs.rates[0].source_updated_at == datetime(
        2026, 10, 5, 21, 7, tzinfo=timezone.utc
    )
    assert (
        next(r for r in obs.rates if r.purity_carats == 24).product_kind
        == "investment_gold"
    )


def test_upper_limits_formulas_and_future_dates():
    assert {r.quote_kind for r in parse("abacor").rates} == {"up_to"}
    formula = parse("gold_fr").rates
    assert all(r.price_eur_per_gram is None for r in formula)
    assert next(r for r in formula if r.purity_carats == 9).warnings
    assert parse("cuo").rates[0].warnings == ["Future source date; quarantined."]


def test_comptoircentral_active_calculator_inputs_and_mismatch():
    rates = parse("comptoircentral").rates
    assert next(r for r in rates if r.purity_carats == 18).price_eur_per_gram == 80
    assert any(
        "mismatch" in w for r in rates if r.purity_carats == 21 for w in r.warnings
    )


def test_lyon_buyback_table_and_current_asset_discovery():
    obs = parse("lesmonnaiesdelyon")
    assert {r.purity_carats: r.price_eur_per_gram for r in obs.rates} == {
        24: 101,
        22: 92,
        18: 79,
    }
    assert {r.product_kind for r in obs.rates} == {"jewellery_for_melting"}

    class Session:
        last_observed_at = AT

        def __init__(self):
            self.documents = []

        def get(self, url):
            if url == CATALOG["lesmonnaiesdelyon"]["url"]:
                return (
                    (FIXTURES / "lesmonnaiesdelyon.html")
                    .read_text()
                    .replace("BQTQWX11", "next")
                )
            if url.endswith("index-next.js"):
                return (
                    (FIXTURES / "lesmonnaiesdelyon-entry.js")
                    .read_text()
                    .replace("CXa2WalY", "changed")
                )
            assert url.endswith("/assets/GoldSection-changed.js")
            return (FIXTURES / "lesmonnaiesdelyon.js").read_text()

    assert (
        adapters.collect_source(CATALOG["lesmonnaiesdelyon"], Session()).rates
        == obs.rates
    )


@pytest.mark.parametrize(
    "ident",
    [
        "goldson",
        "change_vivienne",
        "epo",
        "antheor",
        "abacor",
        "gold_fr",
        "orencash",
        "cuo",
    ],
)
def test_missing_markup_is_failure_not_zero(ident):
    with pytest.raises((ValueError, KeyError)):
        parse(ident, html="<html><p>Unavailable</p></html>")


@pytest.mark.parametrize("ident", ["or_heritage", "orencash", "lesmonnaiesdelyon"])
def test_changed_calculator_formula_fails(ident):
    with pytest.raises(adapters.SourceChanged):
        parse(ident, js="function newCalculator() {}")


@pytest.mark.parametrize(
    "text,expected",
    [("1\u202f234,56", "1234.56"), ("81.84", "81.84"), ("1\u00a0234,56", "1234.56")],
)
def test_french_decimal_formats(text, expected):
    assert adapters.decimal(text) == Decimal(expected)


def test_zero_and_duplicate_rates_rejected():
    obs = parse("goldson")
    data = obs.model_dump()
    data["rates"][0]["price_eur_per_gram"] = "0"
    with pytest.raises(ValidationError):
        Observation.model_validate(data)
    data = obs.model_dump()
    data["rates"].append(data["rates"][0])
    with pytest.raises(ValidationError):
        Observation.model_validate(data)
