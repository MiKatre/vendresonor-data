"""Expected source outages warn; parser/programming errors must stay fatal."""

import httpx
import pytest

from buyers.adapters import SourceChanged, SourceUnavailable
from buyers.health import annotate, error_details, report_health


@pytest.mark.parametrize(
    "exc",
    [
        SourceUnavailable("zero quote"),
        httpx.ReadTimeout("timeout"),
        httpx.ConnectError("offline"),
        httpx.RemoteProtocolError("server disconnected"),
        PermissionError("robots.txt disallows collection"),
    ],
)
def test_expected_source_unavailability_is_a_warning(exc):
    assert error_details(exc)["severity"] == "warning"


@pytest.mark.parametrize(
    "status,expected",
    [
        (403, "warning"),
        (404, "warning"),
        (410, "warning"),
        (429, "warning"),
        (500, "warning"),
        (503, "warning"),
        (400, "error"),
        (401, "error"),
    ],
)
def test_http_error_classification(status, expected):
    response = httpx.Response(
        status, request=httpx.Request("GET", "https://buyer.test")
    )
    exc = httpx.HTTPStatusError("blocked", request=response.request, response=response)
    assert error_details(exc)["severity"] == expected


@pytest.mark.parametrize(
    "exc",
    [
        SourceChanged("formula changed"),
        ValueError("invalid output"),
        KeyError("missing field"),
        RuntimeError("bug"),
        httpx.LocalProtocolError("invalid client request"),
    ],
)
def test_unknown_parser_and_client_errors_fail_closed(exc):
    assert error_details(exc)["severity"] == "error"


def failed(exc):
    return {"buyer_id": "source", "status": "failed", "error": error_details(exc)}


def test_low_coverage_and_expected_source_failure_are_not_execution_failures(capsys):
    snapshot = {"buyers": [failed(SourceUnavailable("zero quote"))]}
    assert report_health(snapshot, 4, target=5) == 0
    assert "4/5" in capsys.readouterr().err
    assert report_health(snapshot, 0, target=5) == 1


def test_partial_parser_failure_and_legacy_unknown_errors_still_fail():
    assert report_health({"buyers": [failed(SourceChanged("markup"))]}, 24) == 1
    assert (
        report_health({"buyers": [{"buyer_id": "legacy", "status": "failed"}]}, 24) == 1
    )


def test_unattempted_sources_do_not_fail_selected_collection():
    snapshot = {"buyers": [failed(SourceChanged("markup"))]}
    assert report_health(snapshot, 1, attempted_ids={"other"}) == 0


def test_unavailable_fx_warns_but_broken_fx_parser_fails():
    snapshot = {
        "buyers": [],
        "fx": {
            "status": "failed",
            "error": "offline",
            "error_severity": "warning",
        },
    }
    assert report_health(snapshot, 4, target=5) == 0
    snapshot["fx"]["error_severity"] = "error"
    assert report_health(snapshot, 4, target=5) == 1


def test_annotations_escape_source_text(monkeypatch, capsys):
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    annotate("warning", "unavailable%\n::error::injected\r")
    assert capsys.readouterr().err == (
        "::warning::unavailable%25%0A::error::injected%0D\n"
    )
