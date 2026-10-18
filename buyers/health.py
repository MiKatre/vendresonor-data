"""Execution health is distinct from source availability and coverage targets."""

import os
import sys

import httpx

from .adapters import SourceUnavailable


def error_details(exc):
    expected = isinstance(
        exc,
        (
            SourceUnavailable,
            httpx.NetworkError,
            httpx.TimeoutException,
            httpx.RemoteProtocolError,
            httpx.ProxyError,
            PermissionError,
        ),
    )
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        expected = status in {403, 404, 410, 429} or 500 <= status < 600
    return {
        "type": type(exc).__name__,
        "message": str(exc)[:700],
        "severity": "warning" if expected else "error",
    }


def annotate(level, message):
    if os.environ.get("GITHUB_ACTIONS") == "true":
        # Source-controlled text must not inject additional workflow commands.
        message = message.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
        print(f"::{level}::{message}", file=sys.stderr)
    else:
        print(f"{level}: {message}", file=sys.stderr)


def report_health(snapshot, usable, *, target=None, attempted_ids=None):
    fatal = False
    for buyer in snapshot["buyers"]:
        if attempted_ids is not None and buyer["buyer_id"] not in attempted_ids:
            continue
        error = buyer.get("error")
        if buyer["status"] == "failed":
            # Unknown exceptions fail closed, even when other sources succeeded.
            severity = (error or {}).get("severity", "error")
            fatal |= severity != "warning"
            annotate(
                severity,
                f"{buyer['buyer_id']}: {(error or {}).get('message', 'collection failed')}",
            )
        elif buyer["status"] != "ok":
            annotate("warning", f"{buyer['buyer_id']}: {buyer['status']}")
    fx = snapshot.get("fx")
    if fx and fx["status"] != "ok":
        severity = fx.get("error_severity", "error")
        fatal |= severity != "warning"
        annotate(
            severity, f"ECB conversion unavailable: {fx.get('error', 'unknown error')}"
        )
    if not usable:
        annotate("error", "No current usable buyers in this market")
        fatal = True
    elif target is not None and usable < target:
        annotate("warning", f"Buyer coverage: {usable}/{target}; data is degraded")
    return 1 if fatal else 0
