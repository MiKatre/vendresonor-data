"""Additional buyer parsers: explicit buyback structures, never generic spot tables."""

import re
from decimal import Decimal, ROUND_HALF_UP

from .adapters import SourceChanged, clean_text, decimal, make, result, soup

NUMBER = r"[\d.,\s\u00a0\u202f]+"
CARATS = {375: 9, 585: 14, 750: 18, 875: 21, 916: 22, 999: 24, 900: Decimal("21.6")}


def unit_price(text):
    match = re.fullmatch(rf"\s*({NUMBER})\s*€\s*/\s*g\s*", text)
    if not match:
        raise SourceChanged("Expected a euro-per-gram buying price")
    return decimal(match[1])


def artnumor(source, at, html):
    rates = []
    for row in soup(html).select("tr"):
        cells = row.find_all("td", recursive=False)
        if len(cells) != 2:
            continue
        label = re.fullmatch(
            r"(\d+) Carats \((\d+)/1000\)", cells[0].get_text(" ", strip=True)
        )
        if label:
            rates.append(
                make(
                    source,
                    at,
                    label[1],
                    unit_price(cells[1].get_text(" ", strip=True)),
                    fineness_per_mille=label[2],
                    product_kind="jewellery_for_melting",
                    warnings=["Source carats/fineness mismatch; review required."]
                    if abs(Decimal(label[1]) / 24 * 1000 - Decimal(label[2])) > 3
                    else [],
                )
            )
    return result(source, at, rates, html)


def ag_gold(source, at, html):
    rates = []
    for row in soup(html).select("tr"):
        cells = row.find_all("td", recursive=False)
        if len(cells) < 2:
            continue
        label = re.fullmatch(r"(\d+) carats", cells[0].get_text(strip=True))
        if label:
            rates.append(
                make(
                    source,
                    at,
                    label[1],
                    unit_price(cells[1].get_text(strip=True)),
                    price_basis="source_claims_tax_deducted",
                )
            )
    return result(source, at, rates, html)


def rafinor(source, at, html):
    rates = {}
    for row in soup(html).select("tr"):
        cells = row.find_all("td", recursive=False)
        label = (
            re.fullmatch(r"Bijoux Or (\d+) carats", cells[0].get_text(strip=True))
            if cells
            else None
        )
        if not label:
            continue
        cell = row.select_one(".se-price[data-price]")
        if not cell or decimal(cell["data-price"]) != unit_price(
            cell.get_text(" ", strip=True)
        ):
            raise SourceChanged("Rafinor displayed and calculator prices disagree")
        rate = make(
            source,
            at,
            label[1],
            decimal(cell["data-price"]),
            product_kind="jewellery_for_melting",
            price_basis="before_commissions_and_taxes",
        )
        if (
            label[1] in rates
            and rates[label[1]].price_eur_per_gram != rate.price_eur_per_gram
        ):
            raise SourceChanged("Rafinor duplicate purity prices disagree")
        rates[label[1]] = rate
    return result(source, at, list(rates.values()), html)


def monex(source, at, html):
    if "nous achetons le gramme" not in clean_text(html):
        raise SourceChanged("Monex buying-price context missing")
    rates = {}
    for node in soup(html).select("span.inline-flex"):
        text = node.get_text(" ", strip=True)
        match = re.fullmatch(rf"OR (\d+) CT\s+({NUMBER})\s*€/g\s*\|", text)
        if match:
            amount = decimal(match[2])
            if match[1] in rates and rates[match[1]].price_eur_per_gram != amount:
                raise SourceChanged("Monex duplicate prices disagree")
            rates[match[1]] = make(
                source, at, match[1], amount, price_basis="source_labels_net"
            )
    return result(source, at, list(rates.values()), html)


def or_avenir(source, at, html):
    rates = []
    for card in soup(html).select("div.glass-card"):
        text = card.get_text(" ", strip=True)
        match = re.fullmatch(
            rf"Or (\d+) carats\s+Pureté\s+({NUMBER})\s*%\s+({NUMBER})\s*€\s*/g", text
        )
        if match:
            rates.append(
                make(
                    source,
                    at,
                    match[1],
                    decimal(match[3]),
                    fineness_per_mille=decimal(match[2]) * 10,
                    price_basis="after_buyer_margin_before_unverified_costs",
                )
            )
    return result(source, at, rates, html)


def or_et_change(source, at, html):
    doc = soup(html)
    scripts = "\n".join(n.get_text() for n in doc.find_all("script"))
    commission = re.search(r"const COMMISSION_GOLD\s*=\s*([\d.]+)", scripts)
    if (
        not commission
        or "goldPerGram * g.purity * (1 - COMMISSION_GOLD)" not in scripts
    ):
        raise SourceChanged("Or & Change jewellery calculator changed")
    banner = doc.select_one(".gpe-metal-price-bar")
    reference = re.search(
        r"Or\s*€\s*([\d.\s]+,\d+)\s*/\s*kg",
        banner.get_text(" ", strip=True) if banner else "",
    )
    grades = re.search(r"const goldGrades\s*=\s*\[([^]]+)\]", scripts)
    if not reference or not grades:
        raise SourceChanged("Or & Change buyer reference or purity table missing")
    price = decimal(reference[1].replace(".", "")) / 1000
    fee = decimal(commission[1])
    if not 0 < fee < 1:
        raise SourceChanged("Invalid buyer commission")
    rates = []
    for carats, milli, purity in re.findall(
        r"label:'(\d+)K \((\d+)‰\)',\s*purity:([\d.]+)", grades[1]
    ):
        factor = decimal(purity)
        if factor * 1000 != Decimal(milli):
            raise SourceChanged("Buyer fineness mapping changed")
        amount = (price * factor * (1 - fee)).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
        rates.append(
            make(
                source,
                at,
                carats,
                amount,
                fineness_per_mille=milli,
                origin="buyer_calculator",
                price_basis="after_buyer_commission",
                source_fields={
                    "buyer_reference_eur_per_gram": str(price),
                    "buyer_commission": str(fee),
                    "buyer_purity_factor": purity,
                },
            )
        )
    return result(source, at, rates, html)


def lepage(source, at, html):
    text = clean_text(html)
    match = re.search(rf"Or 750/1000e \(18ct\)\s*=\s*({NUMBER})€ le gramme", text)
    if not match:
        raise SourceChanged("Lepage jewellery buying-price statement missing")
    return result(
        source,
        at,
        [make(source, at, 18, decimal(match[1]), product_kind="jewellery_for_melting")],
        html,
    )


def oceanor(source, at, html):
    from datetime import datetime
    from zoneinfo import ZoneInfo

    validity = re.search(
        r"Offre valable du (\d{2}/\d{2}/\d{4}) au (\d{2}/\d{2}/\d{4})", clean_text(html)
    )
    if not validity:
        raise SourceChanged("Oceanor offer validity dates missing")
    today = at.astimezone(ZoneInfo("Europe/Paris")).date()
    if (
        not datetime.strptime(validity[1], "%d/%m/%Y").date()
        <= today
        <= datetime.strptime(validity[2], "%d/%m/%Y").date()
    ):
        raise SourceChanged("Oceanor published offer is outside its validity period")
    rates = [
        make(
            source,
            at,
            k,
            decimal(price),
            fineness_per_mille=milli,
            product_kind="jewellery_for_melting",
        )
        for k, milli, price in re.findall(
            rf"votre OR (\d+) carats \((\d+)mil\) au prix de ({NUMBER})€ le gramme",
            clean_text(html),
        )
    ]
    return result(source, at, rates, html)


def feuillatre(source, at, html):
    text = clean_text(html)
    section = re.search(
        r"Nos tarifs de rachat \(au gramme\)(.*?)Taxe sur le rachat", text
    )
    if not section:
        raise SourceChanged("Feuillatre buying-price section missing")
    rates = []
    for node in re.split(r"- Or ", section[1])[1:]:
        match = re.fullmatch(
            rf"(\d+)/1000\s*:\s*({NUMBER})€(?:\s*à\s*({NUMBER})€ selon les articles)?\s*",
            node,
        )
        if not match or int(match[1]) not in CARATS:
            raise SourceChanged("Feuillatre price or product condition changed")
        rates.append(
            make(
                source,
                at,
                CARATS[int(match[1])],
                decimal(match[2]),
                fineness_per_mille=match[1],
                product_kind="jewellery_for_melting",
                quote_kind="range" if match[3] else "indicative",
                max_price_eur_per_gram=decimal(match[3]) if match[3] else None,
                conditions=["Le tarif dépend des articles après expertise."]
                if match[3]
                else [],
            )
        )
    return result(source, at, rates, html)


def maty(source, at, html):
    doc = soup(html)
    table = doc.select_one("table.prices")
    if not table or "EN CHÈQUE BANCAIRE" not in table.get_text():
        raise SourceChanged("MATY cash-payment column missing")
    rates = []
    for row in table.select("tr.metal"):
        label = re.fullmatch(r"OR (\d+)", row.find("td").get_text(strip=True))
        if not label or int(label[1]) not in CARATS:
            continue
        cell = row.select_one("td.remboursement")
        match = re.fullmatch(
            rf"({NUMBER})€ le gramme", cell.get_text(" ", strip=True) if cell else ""
        )
        if not match:
            raise SourceChanged("MATY cash rate unit changed")
        rates.append(
            make(
                source,
                at,
                CARATS[int(label[1])],
                decimal(match[1]),
                fineness_per_mille=label[1],
                product_kind="jewellery_for_melting",
                source_fields={
                    "payment": "cheque_bancaire",
                    "gift_vouchers_excluded": True,
                },
            )
        )
    return result(source, at, rates, html)


def mantes(source, at, html):
    doc = soup(html)
    tables = [t for t in doc.find_all("table") if "Or 750/00" in t.get_text()]
    if len(tables) != 1:
        raise SourceChanged("Mantes scrap-gold table missing or ambiguous")
    table = tables[0]
    text = table.get_text(" ", strip=True)
    if not all(label in text for label in ["1 à 49gr", "50 à 99gr", "+100gr"]):
        raise SourceChanged("Mantes weight brackets changed")
    rates = []
    for row in table.find_all("tr"):
        cells = row.find_all("td", recursive=False)
        if not cells:
            continue
        label = re.fullmatch(r"Or (\d+)/00", cells[0].get_text(strip=True))
        if not label:
            continue
        if len(cells) != 4 or int(label[1]) not in CARATS:
            raise SourceChanged("Mantes table structure changed")
        for i, (minimum, maximum) in enumerate([(1, 49), (50, 99), (100, None)]):
            match = re.fullmatch(rf"({NUMBER})\s*€", cells[i + 1].get_text(strip=True))
            if not match:
                raise SourceChanged("Mantes buying-price currency changed")
            rates.append(
                make(
                    source,
                    at,
                    CARATS[int(label[1])],
                    decimal(match[1]),
                    fineness_per_mille=label[1],
                    tag=f"tier-{minimum}",
                    min_weight_g=minimum,
                    max_weight_g=maximum,
                    product_kind="jewellery_for_melting",
                    conditions=[
                        "Tarif selon le poids total du lot; tranche confirmée à l'expertise."
                    ],
                )
            )
    return result(source, at, rates, html)


def kolidor(source, at, html):
    doc = soup(html)
    section = doc.select_one("#simulation")
    if not section or "Estimation indicative" not in section.get_text(" ", strip=True):
        raise SourceChanged("Kolidor buying estimator missing")
    rates = []
    for card in section.select(".metal-card[data-metal]"):
        match = re.fullmatch(r"or(\d+)", card["data-metal"])
        if not match:
            continue
        text = card.get_text(" ", strip=True)
        price = re.search(rf"({NUMBER})€/g", text)
        milli = re.search(r"\(([\d,.]+)‰\)", text)
        if not price or not milli:
            raise SourceChanged("Kolidor purity or gram quote missing")
        rates.append(
            make(
                source,
                at,
                match[1],
                decimal(price[1]),
                fineness_per_mille=decimal(milli[1]),
                product_kind="jewellery_for_melting",
            )
        )
    return result(source, at, rates, html)


def eurogold(source, at, html):
    from datetime import datetime

    text = clean_text(html)
    dated = re.search(r"COURS DE L OR .*?-le (\d{2}/\d{2}/\d{4})", text)
    if not dated:
        raise SourceChanged("Euro Gold tariff date missing")
    source_date = datetime.strptime(dated[1], "%d/%m/%Y").date().isoformat()
    rates = []
    for k, pattern in [
        (18, rf"rachat du 18K a ({NUMBER})€\s*net le gramme"),
        (14, rf"rachat or 14 K a ({NUMBER})€ net le gramme"),
        (9, rf"9 K a ({NUMBER})€ net le grammes"),
    ]:
        match = re.search(pattern, text, re.I)
        if not match:
            raise SourceChanged("Euro Gold jewellery price statement changed")
        rates.append(
            make(
                source,
                at,
                k,
                decimal(match[1]),
                source_date=source_date,
                product_kind="jewellery_for_melting",
                price_basis="source_labels_net",
                conditions=[
                    f"Tarif affiché daté du {dated[1]}; à confirmer avant déplacement."
                ],
            )
        )
    return result(source, at, rates, html)


def aurum_silver(source, at, html):
    return ag_gold(source, at, html)


def bureau_national(source, at, html, quotes):
    import json

    rates = []
    for weight, payload in quotes:
        records = []
        for line in payload.splitlines():
            if re.match(r"^[0-9a-f]+:\{", line):
                value = json.loads(line.split(":", 1)[1])
                if "disponible" in value:
                    records.append(value)
        if (
            len(records) != 1
            or records[0].get("disponible") is not True
            or records[0].get("metal") != "or"
        ):
            raise SourceChanged("Bureau National buying estimate unavailable")
        quote = records[0]
        low, high = decimal(quote["basse"]), decimal(quote["haute"])
        if low > high:
            raise SourceChanged("Bureau National buying range reversed")
        rates.append(
            make(
                source,
                at,
                18,
                low / weight,
                quote_kind="range",
                tag=f"weight-{weight}",
                max_price_eur_per_gram=high / weight,
                min_weight_g=weight,
                max_weight_g=weight,
                product_kind="jewellery_for_melting",
                origin="buyer_calculator",
                source_fields={
                    "queried_weight_g": weight,
                    "total_low_eur": str(low),
                    "total_high_eur": str(high),
                },
                conditions=[
                    f"Estimation du simulateur pour exactement {weight} g, 18 carats; total arrondi par l'acheteur. Offre finale après expertise."
                ],
            )
        )
    return result(source, at, rates, html)


def sophie_lamblin(source, at, html):
    from datetime import datetime

    text = clean_text(html)
    quote = re.search(
        rf"Tarif indicatif de reprise Cours enregistré le (\d{{2}}/\d{{2}}/\d{{4}})\s+({NUMBER})€ le gramme pour l’or 18 carats — 750 ‰",
        text,
    )
    if not quote:
        raise SourceChanged("Sophie Lamblin dated buying quote missing")
    dated = datetime.strptime(quote[1], "%d/%m/%Y").date().isoformat()
    return result(
        source,
        at,
        [
            make(
                source,
                at,
                18,
                decimal(quote[2]),
                fineness_per_mille=750,
                source_date=dated,
                product_kind="jewellery_for_melting",
                conditions=[
                    "Estimation sur rendez-vous; poids net et titre contrôlés à l'atelier. Proposition définitive après expertise."
                ],
            )
        ],
        html,
    )
