"""Explicit source parsers. No fallback from missing buyer quotes to spot."""

import json
import re
from datetime import date as calendar_date
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup

from .models import Observation, Rate

PARIS = ZoneInfo("Europe/Paris")


class SourceChanged(ValueError):
    pass


def decimal(text):
    value = re.sub(r"[\s\u00a0\u202f]", "", str(text)).replace(",", ".")
    if value.startswith("."):
        value = "0" + value
    if not re.fullmatch(r"\d+(?:\.\d+)?", value):
        raise SourceChanged(f"invalid number: {text!r}")
    return Decimal(value)


def soup(html):
    return BeautifulSoup(html, "html.parser")


def clean_text(html):
    doc = soup(html)
    for node in doc(["script", "style", "nav", "header", "footer"]):
        node.decompose()
    return doc.get_text(" ", strip=True)


def conditions(html):
    """Keep source statements, separately from the registry's dated manual notes."""
    result = []
    for node in soup(html).select("p, li"):
        text = node.get_text(" ", strip=True)
        if (
            text not in result
            and 15 < len(text) <= 1600
            and re.search(
                r"indicatif|estimation|frais|commission|minimum|rétractation|rembours|taxe|tarifs.*ligne|paiement|chèque|expertise|poids|valable|frais.*envoi",
                text,
                re.IGNORECASE,
            )
        ):
            result.append(text)
    return result[:35]


def make(source, at, purity, price, *, tag="", **fields):
    product = fields.pop("product_kind", "gold_items_unspecified")
    kind = fields.pop("quote_kind", "indicative")
    return Rate(
        rate_id=f"{source['buyer_id']}:{product}:{purity}:{kind}:{tag}",
        purity_carats=purity,
        price_eur_per_gram=price,
        product_kind=product,
        quote_kind=kind,
        transaction_channel=source["transaction_channel"],
        source_url=source["url"],
        observed_at=at,
        **fields,
    )


def result(source, at, rates, html="", **fields):
    if not rates:
        raise SourceChanged("no buyer rates found in expected source structure")
    return Observation(
        buyer_id=source["buyer_id"],
        observed_at=at,
        rates=rates,
        conditions=conditions(html),
        **fields,
    )


def goldson(source, at, html):
    rates = []
    for row in soup(html).select(".estimation_produit"):
        label, cell = (
            row.select_one(".estimation_cellule_1"),
            row.select_one(".estimation_cellule_3"),
        )
        if label is None or cell is None:
            continue
        match = re.fullmatch(r"Bijoux Or (\d+) carats", label.get_text(strip=True))
        if match:
            unit = re.fullmatch(
                r"([\d ,\u00a0\u202f]+)\s*€/g", cell.get_text(strip=True)
            )
            if not unit:
                raise SourceChanged("Goldson jewellery rate unit changed")
            rates.append(
                make(
                    source,
                    at,
                    match[1],
                    decimal(unit[1]),
                    product_kind="jewellery_for_melting",
                    price_basis="source_claims_tax_deducted",
                    warnings=[
                        "Tax inclusion differs between price page and terms; net estimate unvalidated."
                    ],
                )
            )
    return result(source, at, rates, html)


def change_vivienne(source, at, html):
    doc = soup(html)
    if "Total estimé H.T" not in doc.get_text(" ", strip=True):
        raise SourceChanged("Change Vivienne price basis label changed")
    rates = []
    for row in doc.select("tr[data-carat]"):
        k = row["data-carat"]
        if not k.isdigit():
            continue
        cell = row.select_one("td[data-price]")
        if cell is None:
            raise SourceChanged("Change Vivienne price cell missing")
        match = re.fullmatch(r"([\d.,\s]+)\s*€", cell.get_text(strip=True))
        if not match:
            raise SourceChanged("Change Vivienne currency changed")
        rates.append(
            make(
                source,
                at,
                k,
                decimal(match[1]),
                price_basis="source_labels_total_ht",
                source_fields={
                    "unrounded_eur_per_gram": str(decimal(cell["data-price"]))
                },
            )
        )
    return result(source, at, rates, html)


def godot(source, at, payload, html):
    data = json.loads(payload)
    if data.get("success") is not True or not isinstance(data.get("carats"), list):
        raise SourceChanged("Godot API unsuccessful or schema changed")
    if "Total estimé H.T" not in clean_text(html):
        raise SourceChanged("Godot price basis label changed")
    rates = []
    for row in data["carats"]:
        match = re.fullmatch(r"(\d+) carats", row["label"])
        if match:
            fields = {k: row[k] for k in ["commission", "tax", "limitTaxe", "comment"]}
            fields["commission_inclusion"] = "do_not_deduct_again_without_review"
            rates.append(
                make(
                    source,
                    at,
                    match[1],
                    decimal(row["montantE"]),
                    price_basis="source_labels_total_ht",
                    source_fields=fields,
                )
            )
    obs = result(source, at, rates, html)
    obs.conditions.extend(
        clean_text(v) for v in data.get("textes", {}).values() if isinstance(v, str)
    )
    return obs


def epo(source, at, html):
    rates = []
    for node in soup(html).select(".max-gold-price[data-carat][data-price]"):
        k = re.fullmatch(r"(\d+)K", node["data-carat"])
        if not k or node.get("data-currency") != "€":
            raise SourceChanged("EPO rate attributes changed")
        rates.append(
            make(source, at, k[1], decimal(node["data-price"]), quote_kind="published")
        )
    return result(source, at, rates, html)


def heritage(source, at, html, js, payload):
    # Follow this buyer's public calculator, never our own market feed.
    margins = re.search(r"const\s+GOLD_MARGINS\s*=\s*\{([^}]+)\}", js)
    grams = re.search(
        r"LIVE_SPOT_24K\s*=\s*\(d\['pax-gold'\]\.eur\s*/\s*([\d.]+)\)", js
    )
    if (
        not margins
        or not grams
        or "LIVE_SPOT_24K * (GOLD_MARGINS[carats] || 1)" not in js
    ):
        raise SourceChanged("Or et Héritage calculator formula changed")
    data = json.loads(payload)["pax-gold"]
    price = decimal(data["eur"]) / decimal(grams[1])
    updated = datetime.fromtimestamp(data["last_updated_at"], tz=PARIS)
    if updated > at:
        raise SourceChanged("Or et Héritage reference timestamp is in the future")
    rates = []
    for k, factor in re.findall(r"(\d+)\s*:\s*([\d.]+)", margins[1]):
        if soup(html).select_one(f'[data-price="gold-{k}"]') is None:
            raise SourceChanged("Or et Héritage display purity missing")
        amount = (price * decimal(factor)).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
        rates.append(
            make(
                source,
                at,
                k,
                amount,
                origin="buyer_calculator",
                price_basis="before_final_tax_assessment",
                source_fields={
                    "buyer_factor": factor,
                    "reference": "buyer_PAXG_endpoint",
                    "paxg_eur_per_ounce": str(data["eur"]),
                    "reference_updated_at": updated.isoformat(),
                },
                warnings=["Reference timestamp is not buyer price publication time."],
            )
        )
    return result(source, at, rates, html)


def antheor(source, at, html):
    doc = soup(html)
    text = clean_text(html)
    if "Prix indicatif Anthéor" not in text:
        raise SourceChanged("Anthéor indicative table missing")
    date = re.search(r"Mise à jour\s*:\s*(\d{2}/\d{2}/\d{4})\s+à\s+(\d{2}:\d{2})", text)
    if not date:
        raise SourceChanged("Anthéor price timestamp missing")
    updated = datetime.strptime(date[1] + " " + date[2], "%d/%m/%Y %H:%M").replace(
        tzinfo=PARIS
    )
    minimum = re.search(
        r"rachetons votre or à partir de\s+(\d+)\s+grammes", text, re.IGNORECASE
    )
    if not minimum:
        raise SourceChanged("Anthéor minimum weight missing")
    rates = []
    for row in doc.select("table tr"):
        cells = row.find_all("td")
        if len(cells) < 3:
            continue
        k = re.fullmatch(r"(\d+) carats", cells[0].get_text(strip=True))
        if not k:
            continue
        match = re.fullmatch(r"([\d.,\s]+)\s*€/g\*?", cells[2].get_text(strip=True))
        if not match:
            raise SourceChanged("Anthéor rate cell changed")
        rates.append(
            make(
                source,
                at,
                k[1],
                decimal(match[1]),
                source_updated_at=updated,
                product_kind="investment_gold"
                if k[1] == "24"
                else "jewellery_for_melting",
                min_weight_g=minimum[1],
                warnings=[
                    "General table; weight-dependent examples are not firm tiers."
                ],
            )
        )
    return result(source, at, rates, html)


def abacor(source, at, html):
    paragraphs = [p.get_text(" ", strip=True) for p in soup(html).select("p")]
    rates = []
    # Explicit advertised upper-limit paragraphs; never the interbank spot table.
    for text in paragraphs:
        match = re.search(
            r"jusqu’à\s+([\d.,]+)\s*euros le gramme pour de l’or\s+(\d+)\s*carats et\s+([\d.,]+)\s*euros le gramme d’or\s+(\d+)\s*carats",
            text,
        )
        if match:
            for price, k in [(match[1], match[2]), (match[3], match[4])]:
                rates.append(
                    make(
                        source,
                        at,
                        k,
                        decimal(price),
                        quote_kind="up_to",
                        conditions=[text],
                        warnings=[
                            "Quantity-dependent upper limit; exact tiers unknown."
                        ],
                    )
                )
            break
    return result(source, at, rates, html)


def gold_fr(source, at, html):
    rates = []
    for node in soup(html).select("li"):
        text = node.get_text(" ", strip=True)
        match = re.search(
            r"Prix Garanti Minimum\s*:\s*(\d+)%.*?gramme de\s+(\d+)k\s*\((\d+)/1000",
            text,
        )
        if match:
            k, fineness = Decimal(match[2]), Decimal(match[3])
            warnings = []
            if abs(k / 24 * 1000 - fineness) > 3:
                warnings.append("Source carats/fineness mismatch; formula quarantined.")
            rates.append(
                make(
                    source,
                    at,
                    k,
                    None,
                    quote_kind="minimum_formula",
                    origin="formula",
                    fineness_per_mille=fineness,
                    source_fields={
                        "minimum_factor": str(Decimal(match[1]) / 100),
                        "reference": "London_second_fixing_adjusted_by_fineness",
                    },
                    warnings=warnings,
                    conditions=[text],
                )
            )
    return result(source, at, rates, html)


def orencash(source, at, html, js):
    doc = soup(html)
    section = doc.select_one('[data-controller="estimation-wizard"]')
    if section is None:
        raise SourceChanged("Or en Cash wizard missing")
    # Fail closed if the public calculator changes its deductions/formula.
    formula = re.search(
        r"s=e\*i\*([\d.]+)\*\(i<([\d.]+)\?([\d.]+):1\)-this\._lookupMarge\(\)", js
    )
    margins = re.search(r"g=\{or:\{([^}]+)\}", js)
    if not formula or not margins:
        raise SourceChanged("Or en Cash calculator formula changed")
    scale, cutoff, low_factor = map(decimal, formula.groups())
    margin = {int(k): decimal(v) for k, v in re.findall(r"(\d+):([\d.]+)", margins[1])}
    reference = decimal(section["data-estimation-wizard-gold-rate-value"]) / 1000
    if reference <= 0:
        raise SourceChanged("Or en Cash reference unavailable")
    rates = []
    for group in doc.select('[data-purity-key^="or_"]'):
        for button in group.select("[data-factor]"):
            if not button["data-factor"]:
                continue
            factor = decimal(button["data-factor"])
            milli = int(factor * 1000)
            eligible = sorted(k for k in margin if k <= milli)
            cost = margin[eligible[-1] if eligible else max(margin)]
            match = re.fullmatch(r"(\d+) carats", button["data-label"])
            k = match[1] if match else None
            amount = (
                reference * factor * scale * (low_factor if factor < cutoff else 1)
                - cost
            )
            # The source clamps negative estimates to zero; zero is unavailable here.
            rates.append(
                make(
                    source,
                    at,
                    k,
                    amount,
                    origin="buyer_calculator",
                    tag=group["data-purity-key"],
                    fineness_per_mille=factor * 1000,
                    product_kind=group["data-purity-key"],
                    source_fields={
                        "source_label": button["data-label"],
                        "buyer_reference_eur_per_gram": str(reference),
                        "purity_factor": str(factor),
                        "scale": str(scale),
                        "low_purity_cutoff": str(cutoff),
                        "low_purity_factor": str(low_factor),
                        "deduction_eur_per_gram": str(cost),
                    },
                    warnings=[
                        "Source calculator estimate; deductions already applied. Final tax basis unvalidated."
                    ],
                )
            )
    return result(source, at, rates, html)


MONTHS = {
    "JANVIER": 1,
    "FÉVRIER": 2,
    "FEVRIER": 2,
    "MARS": 3,
    "AVRIL": 4,
    "MAI": 5,
    "JUIN": 6,
    "JUILLET": 7,
    "AOÛT": 8,
    "AOUT": 8,
    "SEPTEMBRE": 9,
    "OCTOBRE": 10,
    "NOVEMBRE": 11,
    "DÉCEMBRE": 12,
    "DECEMBRE": 12,
}


def cuo(source, at, html):
    text = clean_text(html)
    dated = re.search(r"Fixing OR\s+(\d{1,2})\s+(\w+)\s+(\d{4})", text, re.IGNORECASE)
    if not dated or dated[2].upper() not in MONTHS:
        raise SourceChanged("CUO source date missing")
    date = calendar_date(int(dated[3]), MONTHS[dated[2].upper()], int(dated[1]))
    rates = []
    for node in soup(html).select("li"):
        match = re.search(
            r"Prix net 1 gramme d'or (\d+) carat\s+[-\s]+(\d+)\s*€\s*(\d{2})",
            node.get_text(" ", strip=True),
        )
        if match:
            rates.append(
                make(
                    source,
                    at,
                    match[1],
                    Decimal(match[2]) + Decimal(match[3]) / 100,
                    price_basis="source_labels_net",
                    source_date=date.isoformat(),
                    product_kind="jewellery_for_melting",
                    warnings=["Future source date; quarantined."]
                    if date > at.astimezone(PARIS).date()
                    else [],
                )
            )
    return result(source, at, rates, html)


def comptoircentral(source, at, html):
    doc = soup(html)
    # The inline gold calculator explicitly multiplies weight by these rates.
    scripts = "\n".join(s.get_text() for s in doc.find_all("script"))
    if "value=w*r" not in scripts or "option.getAttribute('data-rate')" not in scripts:
        raise SourceChanged("Comptoir Central gold calculator formula changed")
    rates = []
    for option in doc.select("select#cco-carat option[data-rate]"):
        match = re.fullmatch(
            r"(\d+) carats\s*\((\d+)\s*‰\)", option.get_text(strip=True)
        )
        if not match:
            raise SourceChanged("Comptoir Central purity label changed")
        warnings = [
            "Calculator's initial HTML total may differ; extracted active calculator inputs."
        ]
        if abs(Decimal(match[1]) / 24 * 1000 - Decimal(match[2])) > 3:
            warnings.append("Source carats/fineness mismatch; review required.")
        rates.append(
            make(
                source,
                at,
                match[1],
                decimal(option["data-rate"]),
                fineness_per_mille=match[2],
                origin="buyer_calculator",
                warnings=warnings,
            )
        )
    return result(source, at, rates, html)


def lesmonnaiesdelyon(source, at, html, js):
    # The jewellery buyback component renders these explicit quotes, not spot/coins.
    table = re.search(
        r'(\w+)=\[((?:\{carat:"[^"]+",pricePerGram:\w+\["[^"]+"\]\},?)+)\]', js
    )
    if (
        not table
        or "gold.jewelryBuyback" not in js
        or f"{table[1]}.map(" not in js
        or 'pricePerGram," €/g"' not in js
    ):
        raise SourceChanged("Les Monnaies de Lyon jewellery buyback component changed")
    rates = []
    for k, fineness, variable, key in re.findall(
        r'carat:"(\d+) carats \((\d+)‰\)",pricePerGram:(\w+)\["([^"]+)"\]', table[2]
    ):
        values = re.search(r"\b" + re.escape(variable) + r"=\{([^}]+)\}", js)
        amount = (
            re.search(r'"' + re.escape(key) + r'":([\d.]+)(?:,|$)', values[1])
            if values
            else None
        )
        if not amount or key != k + "k":
            raise SourceChanged("Les Monnaies de Lyon jewellery quote mapping changed")
        rates.append(
            make(
                source,
                at,
                k,
                decimal(amount[1]),
                fineness_per_mille=fineness,
                product_kind="jewellery_for_melting",
                source_fields={
                    "source_component": "GoldSection",
                    "source_label": f"{k} carats ({fineness}‰)",
                },
                warnings=[
                    "Quotes embedded in buyer's current JavaScript asset; publication timestamp unavailable."
                ],
            )
        )
    return result(source, at, rates, html)


def collect_source(source, session):
    html = session.get(source["url"])
    at = session.last_observed_at
    adapter = source["buyer_id"]
    if adapter == "godot":
        payload = session.post(
            "https://www.achat-or-et-argent.fr/workerApi",
            {"methode": "getRachatBijoux"},
        )
        obs = godot(source, session.last_observed_at, payload, html)
    elif adapter == "or_heritage":
        script = soup(html).find("script", src=re.compile(r"^script\.js"))
        if script is None:
            raise SourceChanged("Or et Héritage script missing")
        js = session.get(urljoin(source["url"], script["src"]))
        payload = session.get(
            "https://www.or-heritage.com/.netlify/functions/coingecko-paxg"
        )
        obs = heritage(source, session.last_observed_at, html, js, payload)
    elif adapter == "orencash":
        script = soup(html).find(
            "script", src=re.compile(r"/build/app/shop/app-shop-entry\..*\.js")
        )
        if script is None:
            raise SourceChanged("Or en Cash script missing")
        js = session.get(urljoin(source["url"], script["src"]))
        obs = orencash(source, session.last_observed_at, html, js)
    elif adapter == "lesmonnaiesdelyon":
        script = soup(html).find("script", src=re.compile(r"/assets/index-[^/]+\.js$"))
        if script is None:
            raise SourceChanged("Les Monnaies de Lyon entry asset missing")
        entry = session.get(urljoin(source["url"], script["src"]))
        chunks = set(re.findall(r"assets/GoldSection-[\w-]+\.js", entry))
        if len(chunks) != 1:
            raise SourceChanged(
                "Les Monnaies de Lyon buyback asset missing or ambiguous"
            )
        js = session.get(urljoin(source["url"], "/" + chunks.pop()))
        obs = lesmonnaiesdelyon(source, session.last_observed_at, html, js)
    elif adapter == "bureau_national":
        from .expanded import bureau_national

        scripts = soup(html).find_all(
            "script", src=re.compile(r"/app/estimation/page-[^/]+\.js")
        )
        if len(scripts) != 1:
            raise SourceChanged("Bureau National estimator asset missing or ambiguous")
        js = session.get(urljoin(source["url"], scripts[0]["src"]))
        action = re.search(
            r'createServerReference\)\("([a-f0-9]+)"[^;]+"estimerAction"', js
        )
        if not action or "await z({categorie:w.id,purete:D,poids:I})" not in js:
            raise SourceChanged("Bureau National public estimator call changed")
        quotes = []
        # The source rounds totals according to lot size. Do not infer a linear gram rate.
        for weight in [1, 10, 50, 100]:
            response = session.request(
                "POST",
                source["url"],
                headers={
                    "Next-Action": action[1],
                    "Content-Type": "text/plain;charset=UTF-8",
                    "Accept": "text/x-component",
                    "Origin": "https://bureaunationaldelor.fr",
                },
                content=json.dumps(
                    [{"categorie": "bijou-or", "purete": 750, "poids": weight}]
                ),
            )
            quotes.append((weight, response.text))
        obs = bureau_national(source, session.last_observed_at, html, quotes)
    elif source.get("adapter") == "discovery_only":
        obs = Observation(
            buyer_id=source["buyer_id"],
            observed_at=at,
            conditions=conditions(html),
            warnings=[
                "Page retrieved; no supported numeric buyer-rate structure identified. Adapter required."
            ],
        )
    else:
        from . import expanded

        parser = globals().get(adapter) or getattr(expanded, adapter, None)
        if not callable(parser) or adapter.startswith("_"):
            raise SourceChanged("Unsupported buyer adapter")
        obs = parser(source, at, html)
    doc = soup(html)
    icons = [
        n
        for n in doc.find_all("link", href=True)
        if any(
            rel in {"icon", "apple-touch-icon", "shortcut"} for rel in n.get("rel", [])
        )
    ]
    icon = next(
        (n for n in icons if "apple-touch-icon" in n.get("rel", [])),
        icons[0] if icons else None,
    )
    candidate = urljoin(source["url"], icon["href"] if icon else "/favicon.ico")
    if candidate.startswith("https://"):
        obs.icon_url = candidate
    obs.documents = session.documents.copy()
    return obs
