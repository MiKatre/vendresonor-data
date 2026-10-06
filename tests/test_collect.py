import json
from datetime import datetime, timezone

import httpx
import pytest

from buyers import collect as module
from buyers.models import Observation, Rate

AT = datetime(2026, 10, 5, 22, 0, tzinfo=timezone.utc)
SOURCE = {
    "buyer_id": "test",
    "name": "Test",
    "url": "https://buyer.test/prices",
    "transaction_channel": "postal",
}


def observation(price="80"):
    return Observation(
        buyer_id="test",
        observed_at=AT,
        rates=[
            Rate(
                rate_id="test:18",
                purity_carats=18,
                price_eur_per_gram=price,
                quote_kind="indicative",
                product_kind="jewellery",
                transaction_channel="postal",
                source_url=SOURCE["url"],
                observed_at=AT,
            )
        ],
    ).model_dump(mode="json")


def attempt(status="ok", price="80"):
    return {
        "buyer_id": "test",
        "status": status,
        "attempted_at": AT.isoformat(),
        "error": {"type": "Timeout", "message": "failed"}
        if status == "failed"
        else None,
        "observation": None if status == "failed" else observation(price),
    }


def test_failure_retains_prices_and_observation_time():
    previous = module.merge_latest(None, [SOURCE], [attempt()], AT.isoformat())
    latest = module.merge_latest(
        previous, [SOURCE], [attempt("failed")], "2026-10-06T22:00:00Z"
    )
    b = latest["buyers"][0]
    assert b["rates"] == previous["buyers"][0]["rates"]
    assert b["observed_at"] == previous["buyers"][0]["observed_at"]
    assert b["last_success_at"] == previous["buyers"][0]["last_success_at"]
    assert b["status"] == "failed" and b["retained_previous"]


def test_large_changes_quarantined():
    previous = module.merge_latest(None, [SOURCE], [attempt()], AT.isoformat())
    b = module.merge_latest(previous, [SOURCE], [attempt(price="200")], AT.isoformat())[
        "buyers"
    ][0]
    assert b["status"] == "quarantined"
    assert b["rates"][0]["price_eur_per_gram"] == "80"
    assert b["quarantined_rates"][0]["price_eur_per_gram"] == "200"


def test_future_source_quarantined_without_previous():
    b = module.merge_latest(None, [SOURCE], [attempt("quarantined")], AT.isoformat())[
        "buyers"
    ][0]
    assert b["rates"] == [] and b["quarantined_rates"]
    assert b["last_success_at"] is None


def test_source_failure_is_isolated(monkeypatch):
    other = {**SOURCE, "buyer_id": "other"}

    def adapter(source, fetcher):
        if source["buyer_id"] == "other":
            raise ValueError("markup changed")
        return Observation.model_validate(observation())

    monkeypatch.setattr(module, "collect_source", adapter)

    class Fake:
        def __init__(self):
            self.documents = []

    batch = module.collect([SOURCE, other], Fake())
    assert [a["status"] for a in batch] == ["ok", "failed"]


def test_history_is_immutable_and_atomic_latest(tmp_path, monkeypatch):
    catalog = tmp_path / "catalog.json"
    catalog.write_text(json.dumps({"buyers": [SOURCE]}))
    monkeypatch.setattr(module, "collect", lambda *args: [attempt()])
    output = tmp_path / "latest.json"
    history = tmp_path / "history"
    args = [
        "--catalog",
        str(catalog),
        "--output",
        str(output),
        "--history-dir",
        str(history),
    ]
    assert module.main(args) == 0
    first = next(history.rglob("*.json"))
    content = first.read_bytes()
    assert module.main(args) == 0
    assert len(list(history.rglob("*.json"))) == 2 and first.read_bytes() == content
    assert not list(tmp_path.glob("*.tmp"))
    module.load_previous(output)


def test_complete_failure_and_strict_partial(tmp_path, monkeypatch):
    catalog = tmp_path / "catalog.json"
    catalog.write_text(json.dumps({"buyers": [SOURCE]}))
    monkeypatch.setattr(module, "collect", lambda *args: [attempt("failed")])
    args = [
        "--catalog",
        str(catalog),
        "--output",
        str(tmp_path / "latest.json"),
        "--no-history",
    ]
    assert module.main(args) == 1
    assert json.loads((tmp_path / "latest.json").read_text())["buyers"][0]["error"]
    other = {**SOURCE, "buyer_id": "other"}
    catalog.write_text(json.dumps({"buyers": [SOURCE, other]}))
    bad = {**attempt("failed"), "buyer_id": "other"}
    monkeypatch.setattr(module, "collect", lambda *args: [attempt(), bad])
    assert module.main(args) == 0
    assert module.main(args + ["--strict"]) == 2


def test_invalid_previous_never_overwritten(tmp_path):
    f = tmp_path / "latest.json"
    f.write_text('{"schema_version":99}')
    with pytest.raises(ValueError):
        module.load_previous(f)
    assert f.read_text() == '{"schema_version":99}'


def test_stdout_is_json_and_logs_are_separate(tmp_path, monkeypatch, capsys):
    catalog = tmp_path / "catalog.json"
    catalog.write_text(json.dumps({"buyers": [SOURCE]}))
    monkeypatch.setattr(module, "collect", lambda *args: [attempt()])
    assert (
        module.main(["--catalog", str(catalog), "--output", "-", "--no-history"]) == 0
    )
    captured = capsys.readouterr()
    assert (
        json.loads(captured.out)["buyers"][0]["rates"][0]["price_eur_per_gram"] == "80"
    )
    assert "Collected" in captured.err


def test_only_preserves_unselected_buyer(tmp_path, monkeypatch):
    other = {**SOURCE, "buyer_id": "other"}
    catalog = tmp_path / "catalog.json"
    catalog.write_text(json.dumps({"buyers": [SOURCE, other]}))
    output = tmp_path / "latest.json"
    previous = module.merge_latest(None, [SOURCE, other], [attempt()], AT.isoformat())
    module.atomic_write(output, previous)

    def collect_selected(sources, fetcher):
        assert [s["buyer_id"] for s in sources] == ["test"]
        return [attempt(price="85")]

    monkeypatch.setattr(module, "collect", collect_selected)
    assert (
        module.main(
            [
                "--catalog",
                str(catalog),
                "--output",
                str(output),
                "--only",
                "test",
                "--no-history",
            ]
        )
        == 0
    )
    latest = json.loads(output.read_text())
    assert latest["buyers"][1] == previous["buyers"][1]
    assert latest["collection"]["attempted"] == 1


def test_http_retries_and_robots_no_bypass():
    hits = []

    def handler(request):
        hits.append(str(request.url))
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /private")
        if request.url.path == "/prices" and hits.count(str(request.url)) == 1:
            return httpx.Response(503)
        return httpx.Response(200, text="ok")

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        f = module.Fetcher(client, delay=0)
        assert f.get("https://buyer.test/prices") == "ok"
        assert hits.count("https://buyer.test/prices") == 2
        with pytest.raises(PermissionError):
            f.get("https://buyer.test/private")
        assert "https://buyer.test/private" not in hits
