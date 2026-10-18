import json

from scripts.check_coverage import coverage, main


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


def test_warn_only_preserves_strict_default_and_coverage_target(tmp_path, capsys):
    path = tmp_path / "snapshot.json"
    path.write_text(json.dumps({"buyers": []}))
    args = ["--snapshot", str(path), "--target", "25"]
    assert main(args) == 1
    assert main(args + ["--warn-only"]) == 0
    output = capsys.readouterr()
    assert "0/25" in output.out and "0/25" in output.err
