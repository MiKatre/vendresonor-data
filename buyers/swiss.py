"""Swiss buying quotes. CHF is never passed to the French EUR models."""

import re
from datetime import datetime, timedelta, date
from decimal import Decimal, ROUND_HALF_UP
from urllib.parse import urljoin
from zoneinfo import ZoneInfo
from xml.etree import ElementTree

from .adapters import SourceChanged, clean_text, decimal, soup

FX_URL = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml"
SWISS = ZoneInfo("Europe/Zurich")
PERMILLE = {8: 333, 9: 375, 14: 585, 18: 750, 21: 875, 21.6: 900, 22: 916, 24: 999.9}


def rate(carats, price, *, permille=None, minimum=0, fields=None):
    value = decimal(price)
    if not value.is_finite() or not 0 < value < 1000:
        raise SourceChanged("invalid CHF buying price")
    if carats not in PERMILLE:
        raise SourceChanged("unrecognized gold purity")
    if permille is not None and abs(permille - PERMILLE[carats]) > 1.1:
        raise SourceChanged("carat and fineness disagree")
    return {
        "purity_carats": carats,
        "purity_permille": permille or PERMILLE[carats],
        "price_chf_per_gram": str(value),
        "min_weight_g": minimum,
        "quote_kind": "indicative",
        "product_kind": "gold_items",
        "source_fields": fields or {},
    }


def source_time(text, pattern, fmt):
    found = re.search(pattern, text)
    if not found:
        raise SourceChanged("expected source update timestamp missing")
    return datetime.strptime(found[1], fmt).replace(tzinfo=SWISS).isoformat()


def extract_conditions(html):
    statements = []
    for node in soup(html).select("p,li"):
        t = node.get_text(" ", strip=True)
        if (
            15 < len(t) <= 1600
            and re.search(
                r"indicatif|frais|commission|minimum|paiement|expertise|alliage|rendez-vous|domicili|résident|poste|douane|Ankauf|Gebühr|Ausweis|Termin|Einfuhr|Edelstein",
                t,
                re.I,
            )
            and t not in statements
        ):
            statements.append(t)
    return statements[:35]


def parse(source, html, payload=None):
    ident = source["buyer_id"]
    doc = soup(html)
    text = clean_text(html)
    conditions = extract_conditions(html)
    rates = []
    updated = None
    eligibility = {"status": "unknown", "statement": None, "source_url": source["url"]}
    warnings = []
    note = "Accueil des résidents français à confirmer avant déplacement."
    if ident == "valorum":
        table = next(
            (t for t in doc.find_all("table") if "Prix de rachat" in t.get_text()), None
        )
        if table is None or "par gramme" not in text:
            raise SourceChanged("buying table or per-gram unit missing")
        for row in table.select("tr"):
            cells = row.find_all("td")
            if len(cells) != 2:
                continue
            m = re.fullmatch(r"Or (\d+) (?:k|ct)", cells[0].get_text(" ", strip=True))
            if m:
                p = re.fullmatch(r"([\d.,]+) CHF", cells[1].get_text(" ", strip=True))
                if not p:
                    raise SourceChanged("CHF unit missing")
                rates.append(rate(int(m[1]), p[1]))
        updated = source_time(
            text,
            r"Dernière mise à jour\s*:\s*(\d{2}/\d{2}/\d{4} \d{2}:\d{2}:\d{2})",
            "%d/%m/%Y %H:%M:%S",
        )
    elif ident == "bijouxor":
        for m in re.finditer(
            r"Or (\d+) carats\s*[—–-]\s*poinçon (\d+)\s*:\s*([\d.,]+) CHF par gramme, net TTC",
            text,
        ):
            rates.append(rate(int(m[1]), m[3], permille=int(m[2])))
        note = "Sur rendez-vous, après vérification de l’alliage. Envoi sécurisé proposé ; départ depuis la France à confirmer."
    elif ident == "swissgoldexchange":
        if "Prix de rachat par alliage" not in text or "CHF le gramme" not in text:
            raise SourceChanged("buying table/unit missing")
        for row in doc.select('tr[data-gruppe="Or"][data-einheit="g"]'):
            m = re.fullmatch(r"(\d+) K", row["data-sorte"])
            if not m:
                raise SourceChanged("changed purity label")
            displayed = decimal(row.select_one("[data-kurs]").get_text(strip=True))
            if displayed != decimal(row["data-preis"]):
                raise SourceChanged("buying price conflicts with displayed value")
            rates.append(
                rate(
                    int(m[1]),
                    displayed,
                    permille=float(decimal(row["data-feinheit"]) * 1000),
                )
            )
        updated = source_time(
            text, r"Au (\d{2}\.\d{2}\.\d{4} \d{2}:\d{2})", "%d.%m.%Y %H:%M"
        )
        note = "Titre vérifié et pesée sur place. Envoi postal annoncé uniquement depuis la Suisse."
    elif ident == "orcash":
        if "CHF par gramme" not in text or "Taux de rachat" not in text:
            raise SourceChanged("buying heading/unit missing")
        for cell in doc.select(".metals-buy__offer--gold"):
            cells = cell.parent.find_all("td")
            carats = float(cells[0].get_text(strip=True).replace("K", ""))
            rates.append(
                rate(
                    carats,
                    cell.get_text(strip=True),
                    permille=int(cells[1].get_text(strip=True)),
                )
            )
        statement = next(
            (
                c
                for c in conditions
                if re.search(
                    r"limiter ses achats aux personnes domiciliées en Suisse", c
                )
            ),
            None,
        )
        if not statement:
            raise SourceChanged(
                "Swiss-only eligibility policy changed; review required"
            )
        eligibility.update(status="swiss_residents_only", statement=statement)
        note = "Réservé aux personnes domiciliées en Suisse."
        updated = source_time(
            text, r"au (\d{2}:\d{2} · \d{4}-\d{2}-\d{2})", "%H:%M · %Y-%m-%d"
        )
    elif ident == "goldencash":
        if not payload or payload.get("mode") != "api":
            raise SourceChanged("live buyer API unavailable; static fallback excluded")
        if "/api/prices" not in payload.get("_endpoint", ""):
            raise SourceChanged("missing API provenance")
        minimum = re.search(r"Minimum (\d+) grammes", text)
        statement = next(
            (c for c in conditions if "Non-résidents CH" in c and "douane" in c),
            None,
        )
        if not minimum or not statement:
            raise SourceChanged("minimum/foreign-customer conditions missing")
        for cell in doc.select('#prices-table-body [id^="table-price-"]'):
            key = cell["id"].replace("table-price-", "")
            if not re.fullmatch(r"(9|14|18|22)k", key):
                continue  # spot/kg rows are not buying offers
            rates.append(
                rate(
                    int(key.replace("k", "")),
                    payload["prices"][key]["chf"],
                    minimum=int(minimum[1]),
                    fields={"endpoint": payload["_endpoint"], "mode": "api"},
                )
            )
        updated = payload["lastUpdated"]
        eligibility.update(status="conditional", statement=statement)
        note = f"Minimum {minimum[1]} g. Virement uniquement. Démarche douanière pour les objets non achetés en Suisse."
    elif ident == "hausgold":
        if "Ankauf" not in text:
            raise SourceChanged("buying context missing")
        table = doc.select_one('table.gssp-metal-table[data-currency="CHF"]')
        if table is None or "CHF/g" not in table.get_text():
            raise SourceChanged("CHF buying table missing")
        for row in table.select("tbody tr[data-price]"):
            c = row.find_all("td")
            if c[1].get_text(strip=True) != "Gold":
                continue
            karat = float(
                c[2].get_text(strip=True).replace(" Karat", "").replace(",", ".")
            )
            if decimal(c[4].get_text(strip=True)) != decimal(row["data-price"]):
                raise SourceChanged("displayed and input prices disagree")
            rates.append(
                rate(
                    karat,
                    row["data-price"],
                    permille=float(decimal(c[3].get_text(strip=True))),
                )
            )
    elif ident == "geiger":
        script = next(
            (
                n.get_text()
                for n in doc.find_all("script")
                if "altgoldankauf.csv" in n.get_text()
            ),
            "",
        )
        gold = re.search(
            r'if\s*\(\s*response\[0\]\s*===\s*"2230003"\s*\)(.*?)(?=if\s*\(\s*response\[3\]|$)',
            script,
            re.S,
        )
        expected = "(price * (parseFloat(content.replace(/[^0-9,\\s]/g, '').replace(/,/, '.'))/1000))"
        expression = (
            re.search(r"let\s+dividedPrice\s*=\s*(.*?)\.toLocaleString", gold[1], re.S)
            if gold
            else None
        )
        if not expression or re.sub(r"\s+", "", expression[1]) != re.sub(
            r"\s+", "", expected
        ):
            raise SourceChanged("buyer calculator formula changed")
        block = re.search(r"\[([^\]]+)\]\.forEach", gold[1])
        labels = re.findall(r"'([^']+)'", block[1]) if block else []
        raw = payload.splitlines() if isinstance(payload, str) else []
        if len(raw) < 2 or raw[0] != "2230003":
            raise SourceChanged("gold export identifier changed")
        base = decimal(raw[1])
        mapping = {v: k for k, v in PERMILLE.items()}
        for label in labels:
            pm = float(decimal(re.match(r"[\d,]+", label)[0]))
            if pm not in mapping:
                raise SourceChanged("unknown calculator alloy")
            p = (base * Decimal(str(pm)) / 1000).quantize(
                Decimal(".01"), rounding=ROUND_HALF_UP
            )
            rates.append(
                rate(
                    mapping[pm],
                    p,
                    permille=pm,
                    fields={
                        "pure_gold_buying_base_chf_per_gram": str(base),
                        "calculator_alloy": label,
                    },
                )
            )
        statement = next((c for c in conditions if "Einfuhrzollnachweises" in c), None)
        if not statement:
            raise SourceChanged("foreign import conditions missing")
        eligibility.update(status="conditional", statement=statement)
        note = "Niederglatt uniquement, sur rendez-vous. Justificatif officiel d’importation demandé pour les bijoux venant de l’étranger."
    else:
        raise SourceChanged("unknown Swiss adapter")
    if not rates:
        raise SourceChanged("no buying rates in expected structure")
    purities = [r["purity_carats"] for r in rates]
    if len(set(purities)) != len(purities):
        # Some legacy HTML repeats its entire buying calculator.
        unique = {r["purity_carats"]: r for r in rates}
        if any(r != unique[r["purity_carats"]] for r in rates):
            raise SourceChanged("conflicting duplicate buying quotes")
        rates = list(unique.values())
    if updated:
        dt = datetime.fromisoformat(updated)
        if dt.tzinfo is None:
            raise SourceChanged("unqualified source timestamp")
    else:
        warnings.append(
            "Le vendeur ne date pas ses tarifs ; seule la collecte est horodatée."
        )
    return {
        "rates": rates,
        "source_updated_at": updated,
        "eligibility": eligibility,
        "conditions": conditions,
        "condition_summary": note,
        "warnings": warnings,
    }


def parse_fx(xml, at):
    doc = ElementTree.fromstring(xml)
    cubes = [n for n in doc.iter() if "time" in n.attrib]
    if len(cubes) != 1:
        raise SourceChanged("ECB reference date missing/ambiguous")
    day = date.fromisoformat(cubes[0].attrib["time"])
    if not 0 <= (at.date() - day).days <= 7:
        raise SourceChanged("ECB rate is future-dated or older than seven days")
    chf = [n.attrib["rate"] for n in cubes[0] if n.attrib.get("currency") == "CHF"]
    if len(chf) != 1:
        raise SourceChanged("ECB CHF quote missing/ambiguous")
    value = decimal(chf[0])
    if not Decimal(".1") < value < Decimal("10"):
        raise SourceChanged("invalid ECB CHF/EUR rate")
    return {
        "base_currency": "EUR",
        "quote_currency": "CHF",
        "chf_per_eur": str(value),
        "date": day.isoformat(),
        "observed_at": at.isoformat(),
        "source_url": FX_URL,
    }


def trusted_rates(previous):
    rates = previous.get("accepted_rates")
    return rates if rates is not None else previous.get("rates", [])


def quote_status(observation, at, previous=None):
    updated = observation.get("source_updated_at")
    if updated:
        delta = at - datetime.fromisoformat(updated)
        if delta < -timedelta(minutes=10):
            return "quarantined"
        if delta > timedelta(days=3):
            return "stale_source"
    if previous:
        old = {
            r["purity_carats"]: decimal(r["price_chf_per_gram"])
            for r in trusted_rates(previous)
        }
        if any(
            r["purity_carats"] in old
            and abs(decimal(r["price_chf_per_gram"]) / old[r["purity_carats"]] - 1)
            > Decimal(".3")
            for r in observation["rates"]
        ):
            return "quarantined"
    return "ok"
