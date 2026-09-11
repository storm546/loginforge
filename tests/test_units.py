"""Offline unit tests: captcha classification, 2captcha wire format, target loading."""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import threading
from http.server import HTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agent.captcha import (  # noqa: E402
    Captcha,
    TwoCaptcha,
    TwoCaptchaError,
    arkose_params_from_urls,
    detect_sync_html,
)
from agent.config import load_target  # noqa: E402
from agent.llm import parse_args  # noqa: E402


# ------------------------------------------------------------------ detection
def test_detect_arkose_marker_is_solvable_funcaptcha():
    html = '<iframe src="https://cdn.arkoselabs.com/fc/api/?public_key=x"></iframe>'
    assert detect_sync_html(html) == "funcaptcha"


def test_detect_datadome_marker():
    assert detect_sync_html("<script src='https://js.datadome.co/tags.js'></script>") == "datadome"


def test_detect_clean_page():
    assert detect_sync_html("<html><body><div class='g-recaptcha' data-sitekey='k'></div></body></html>") is None


# ------------------------------------------------------------------ 2captcha
@pytest.fixture(scope="module")
def mock_solver():
    spec = importlib.util.spec_from_file_location("mock_2captcha", ROOT / "testsite" / "mock_2captcha.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.READY_AFTER = 1
    server = HTTPServer(("127.0.0.1", 0), mod.Handler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


def test_balance_and_solve_roundtrip(mock_solver, monkeypatch):
    monkeypatch.setattr("agent.captcha.POLL_INTERVAL", 0)
    client = TwoCaptcha("test-key", mock_solver)
    assert client.balance() == 10.0

    spec = Captcha(kind="recaptcha_v2", sitekey="6LeIxAcTAAAAAJcZVRqyHh71UMIEGNQ_MXjiZKhI",
                   pageurl="https://example.com/login")
    token = client.solve(spec)
    assert token.startswith("mock-token-")


def test_missing_key_rejected():
    with pytest.raises(TwoCaptchaError):
        TwoCaptcha("", "http://127.0.0.1:1")


def test_unsolvable_kind_raises(mock_solver):
    from agent.captcha import UnsolvableCaptcha
    with pytest.raises(UnsolvableCaptcha):
        TwoCaptcha("k", mock_solver).solve(Captcha(kind="unsolvable", reason="datadome"))


def test_funcaptcha_uses_publickey_and_surl(mock_solver):
    client = TwoCaptcha("k", mock_solver)
    sent: dict = {}
    client._poll = lambda rid, timeout=180: f"token-for-{rid}"  # skip polling
    original = client._submit

    def capture(payload):
        sent.update(payload)
        return original(payload)

    client._submit = capture
    token = client.solve(Captcha(kind="funcaptcha", sitekey="PUBKEY-1",
                                 surl="https://client-api.arkoselabs.com",
                                 pageurl="https://target.test/verify",
                                 user_agent="UA/1.0"))
    assert sent["method"] == "funcaptcha"
    assert sent["publickey"] == "PUBKEY-1"
    assert sent["surl"] == "https://client-api.arkoselabs.com"
    assert sent["useragent"] == "UA/1.0"
    assert token.startswith("token-for-")


# ------------------------------------------------------------------ config
def test_target_env_expansion(tmp_path, monkeypatch):
    monkeypatch.setenv("SITE_PASSWORD", "s3cret")
    monkeypatch.setenv("TARGETS_DIR", str(tmp_path))
    (tmp_path / "site.yaml").write_text(
        "name: site\nurl: https://s.test/\ncredentials:\n  password: ${SITE_PASSWORD}\n"
    )
    t = load_target("site")
    assert t.credentials["password"] == "s3cret"
    assert t.url == "https://s.test/"


def test_parse_args_tolerates_garbage():
    assert parse_args('{"a": 1}') == {"a": 1}
    assert parse_args("") == {}
    assert parse_args("not-json") == {"_raw": "not-json"}

# ------------------------------------------------------- facebook arkose capture
# Real request URLs observed against live Facebook on 2026-09-11. Facebook hides
# the Arkose widget behind its own fbsbx.com wrapper, so the public key never
# appears in the DOM - only in the traffic below.
FB_ARKOSE_URLS = [
    "https://www.fbsbx.com/captcha/arkose/iframe/?referer=https%3A%2F%2Fwww.facebook.com"
    "&locale=en_US&theme=base&captcha_client_config_name=pre_authentication_arkose_captcha_config"
    "&__cci=FQARESIVAhn1lQEC5ALSAvoCJC6KAz5ARkhKTE5QVFhcXmBiZGpscnR4eoABggGE",
    "https://meta-api.arkoselabs.com/v2/4.5.0/enforcement.ca3b41bdbcb85e7c7fb7892a970c52e8.js",
    "https://meta-api.arkoselabs.com/fc/gt2/public_key/2BF0FE95-9FB3-45E0-9CB9-3F5B5A8465B1",
]


def test_arkose_params_extracted_from_facebook_traffic():
    guid, host, blob = arkose_params_from_urls(FB_ARKOSE_URLS)
    assert guid == "2BF0FE95-9FB3-45E0-9CB9-3F5B5A8465B1"
    assert host == "https://meta-api.arkoselabs.com"
    assert blob.startswith("FQARESIVAhn1lQ")


def test_arkose_params_guid_from_enforcement_path():
    urls = ["https://some-tenant-api.arkoselabs.com/v2/4.5.0/enforcement.abc.js"]
    guid, host, blob = arkose_params_from_urls(urls)
    assert guid == ""  # enforcement URLs carry no GUID; host still resolves
    assert host == "https://some-tenant-api.arkoselabs.com"


def test_arkose_params_empty_when_no_arkose_traffic():
    assert arkose_params_from_urls([]) == ("", "", "")
    assert arkose_params_from_urls(["https://www.facebook.com/"]) == ("", "", "")


def test_funcaptcha_payload_includes_blob_and_api_server():
    """Facebook needs data.blob + api_server, or 2captcha rejects the task."""
    cap = Captcha(
        kind="funcaptcha",
        sitekey="2BF0FE95-9FB3-45E0-9CB9-3F5B5A8465B1",
        pageurl="https://www.facebook.com/two_step_verification/authentication/",
        surl="https://meta-api.arkoselabs.com",
        blob="FQARESIVAhn1lQ",
    )
    payload = TwoCaptcha._funcaptcha_payload(cap)
    assert payload["publickey"] == "2BF0FE95-9FB3-45E0-9CB9-3F5B5A8465B1"
    assert payload["api_server"] == "meta-api.arkoselabs.com"
    assert json.loads(payload["data"]) == {"blob": "FQARESIVAhn1lQ"}

