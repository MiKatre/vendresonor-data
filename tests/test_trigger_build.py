import json
import urllib.error
from scripts import trigger_build as module


def test_hook_sends_exact_commit_without_printing_secret(monkeypatch, capsys):
    monkeypatch.setenv("NETLIFY_BUILD_HOOK", "https://example.test/secret")
    monkeypatch.setattr(module.subprocess, "check_output", lambda *a, **kw: "a" * 40 + "\n")
    requests = []

    class Response:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *args): pass

    def send(request, **kwargs):
        requests.append(request)
        return Response()

    monkeypatch.setattr(module.urllib.request, "urlopen", send)
    assert module.main() == 0
    assert json.loads(requests[0].data) == {"buyer_data_commit": "a" * 40}
    assert "secret" not in capsys.readouterr().out


def test_failed_hook_retries_without_disclosing_url(monkeypatch, capsys):
    monkeypatch.setenv("NETLIFY_BUILD_HOOK", "https://example.test/secret")
    monkeypatch.setattr(module.subprocess, "check_output", lambda *a, **kw: "a" * 40)
    attempts = []

    def fail(*args, **kwargs):
        attempts.append(1)
        raise urllib.error.URLError("https://example.test/secret unreachable")

    monkeypatch.setattr(module.urllib.request, "urlopen", fail)
    monkeypatch.setattr(module.time, "sleep", lambda *args: None)
    assert module.main() == 1
    assert len(attempts) == 3
    assert "secret" not in capsys.readouterr().err
