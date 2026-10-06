"""Captured buyer structures and failures that could publish misleading rates."""

import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from buyers import adapters, expanded

FIXTURES = Path(__file__).parent / "fixtures"
CATALOG = {
    b["buyer_id"]: b
    for b in json.loads(Path("buyers/catalog.json").read_text())["buyers"]
}
AT = datetime(2026, 10, 6, 12, tzinfo=timezone.utc)


def parse(ident, html=None):
    return getattr(expanded, ident)(
        CATALOG[ident], AT, html or (FIXTURES / f"{ident}.html").read_text()
    )


@pytest.mark.parametrize(
    "ident,count",
    [
        ("artnumor", 5),
        ("ag_gold", 5),
        ("rafinor", 3),
        ("monex", 4),
        ("or_avenir", 5),
        ("or_et_change", 5),
        ("lepage", 1),
        ("feuillatre", 4),
        ("maty", 4),
        ("mantes", 18),
        ("kolidor", 4),
        ("eurogold", 3),
        ("aurum_silver", 5),
    ],
)
def test_captured_buying_quotes(ident, count):
    obs = parse(ident)
    assert len(obs.rates) == count
    assert all(
        r.price_eur_per_gram > 0 and not r.comparison_eligible for r in obs.rates
    )
    with pytest.raises(adapters.SourceChanged):
        parse(ident, "<p>Cours du marché 18 carats 99 €/g</p>")


def test_cash_column_excludes_maty_gift_vouchers():
    rate = next(r for r in parse("maty").rates if r.purity_carats == 18)
    assert rate.price_eur_per_gram == 52
    assert rate.source_fields["payment"] == "cheque_bancaire"


def test_source_commission_and_reference_required():
    r = next(r for r in parse("or_et_change").rates if r.purity_carats == 18)
    assert r.price_eur_per_gram == Decimal("78.41")
    assert r.origin == "buyer_calculator"
    html = (FIXTURES / "or_et_change.html").read_text()
    with pytest.raises(adapters.SourceChanged):
        parse("or_et_change", html.replace("COMMISSION_GOLD", "COMMISSION_UNKNOWN"))


def test_duplicate_rafinor_calculator_inputs_must_agree():
    html = (FIXTURES / "rafinor.html").read_text()
    doc = adapters.soup(html)
    row = next(n for n in doc.find_all("tr") if "Bijoux Or 18 carats" in n.get_text())
    row.select_one(".se-price")["data-price"] = "1"
    changed = str(doc)
    with pytest.raises(adapters.SourceChanged):
        parse("rafinor", changed)


def test_weight_brackets_and_article_range_preserved():
    tiers = [r for r in parse("mantes").rates if r.purity_carats == 18]
    assert [(r.min_weight_g, r.max_weight_g, r.price_eur_per_gram) for r in tiers] == [
        (1, 49, 72),
        (50, 99, 73),
        (100, None, 74),
    ]
    r = next(r for r in parse("feuillatre").rates if r.purity_carats == 18)
    assert (r.quote_kind, r.price_eur_per_gram, r.max_price_eur_per_gram) == (
        "range",
        80,
        90,
    )
    assert r.conditions


def test_expired_offer_never_accepted_and_publication_date_retained():
    with pytest.raises(adapters.SourceChanged, match="validity period"):
        parse("oceanor")
    assert {r.source_date for r in parse("eurogold").rates} == {"2026-09-28"}


def test_bureau_national_exact_weight_ranges_and_asset_refresh():
    payload = (FIXTURES / "bureau_national.txt").read_text()
    obs = expanded.bureau_national(CATALOG["bureau_national"], AT, "", [(1, payload)])
    r = obs.rates[0]
    assert (
        r.price_eur_per_gram,
        r.max_price_eur_per_gram,
        r.min_weight_g,
        r.max_weight_g,
    ) == (74, 83, 1, 1)
    with pytest.raises(adapters.SourceChanged):
        expanded.bureau_national(
            CATALOG["bureau_national"], AT, "", [(1, '1:{"disponible":false}')]
        )

    class Session:
        last_observed_at = AT
        documents = []

        def get(self, url):
            return (
                FIXTURES
                / (
                    "bureau_national.js"
                    if url.endswith(".js")
                    else "bureau_national.html"
                )
            ).read_text()

        def request(self, method, url, **kwargs):
            assert method == "POST"
            args = json.loads(kwargs["content"])[0]
            assert args["purete"] == 750 and args["categorie"] == "bijou-or"
            return type("Response", (), {"text": payload})()

    actual = adapters.collect_source(CATALOG["bureau_national"], Session())
    assert len(actual.rates) == 4
    assert {r.min_weight_g for r in actual.rates} == {1, 10, 50, 100}


def test_official_icon_resolution():
    class Session:
        last_observed_at = AT
        documents = []

        def get(self, url):
            return (
                '<link rel="apple-touch-icon" href="/brand.png">'
                + (FIXTURES / "lepage.html").read_text()
            )

    obs = adapters.collect_source(CATALOG["lepage"], Session())
    assert obs.icon_url == "https://www.lepage.fr/brand.png"
