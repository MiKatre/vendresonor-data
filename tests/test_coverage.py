from scripts.check_coverage import coverage


def test_coverage_excludes_retained_failed_formulas_and_mismatches():
    def buyer(ident, **fields):
        return {
            "buyer_id": ident,
            "status": "ok",
            "rates": [{"price_eur_per_gram": "80"}],
            **fields,
        }

    data = {
        "buyers": [
            buyer("valid"),
            buyer("failed", status="failed"),
            buyer("retained", retained_previous=True),
            buyer("formula", rates=[{"price_eur_per_gram": None}]),
            buyer("quarantine", status="quarantined"),
            buyer(
                "mismatch",
                rates=[
                    {"price_eur_per_gram": "80", "warnings": ["Source purity mismatch"]}
                ],
            ),
        ]
    }
    assert coverage(data) == ["valid"]
