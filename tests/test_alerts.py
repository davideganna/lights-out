"""Alerter and ALERT_* configuration; no network."""

from __future__ import annotations

import logging
import os

import httpx
import pytest

from lights_out.core.config import ALERT_EVENT_NAMES, Config, ConfigError, parse_alert_events
from lights_out.integrations.alerts import Alerter, NullAlerter, card_ref

URL = "https://hooks.example/SECRET"


def client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_payload_is_text_only_post_to_the_url():
    seen = []

    def handler(req):
        seen.append(req)
        return httpx.Response(200)

    assert Alerter(URL, http=client(handler)).send("failed", "lights-out: hi") is True
    (req,) = seen
    assert req.method == "POST" and str(req.url) == URL
    assert req.read() == b'{"text":"lights-out: hi"}' or req.read() == b'{"text": "lights-out: hi"}'


def test_empty_url_sends_nothing():
    def handler(req):
        raise AssertionError("no request expected")

    a = Alerter("", http=client(handler))
    assert not a.enabled and a.send("failed", "x") is False and a.post("x") is False


def test_event_filter_applies_to_send_but_not_post():
    seen = []
    a = Alerter(URL, ["failed"], http=client(lambda r: seen.append(r) or httpx.Response(200)))
    assert a.send("budget", "x") is False and not seen
    assert a.send("failed", "x") is True and len(seen) == 1
    assert a.post("y") is True and len(seen) == 2


@pytest.mark.parametrize("mode", ["timeout", "http500"])
def test_failures_are_swallowed_and_logged_without_the_url(caplog, mode):
    def handler(req):
        if mode == "timeout":
            raise httpx.ReadTimeout(f"timed out talking to {req.url}")
        return httpx.Response(500)

    # httpx's own INFO request log names the URL; cli.main() silences it (WARNING), so mirror that here.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    with caplog.at_level(logging.DEBUG):
        assert Alerter(URL, http=client(handler)).send("failed", "x") is False
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "SECRET" not in caplog.text and "hooks.example" not in caplog.text


def test_default_client_timeout_is_ten_seconds():
    assert Alerter(URL)._http.timeout.read == 10


def test_null_alerter_and_card_ref():
    n = NullAlerter()
    assert n.send("failed", "x") is False and n.post("x") is False and not n.enabled
    assert card_ref("12", "T", "u") == '#12 "T" u'
    assert card_ref("12", "T", "u", "p") == '#12 "T" u PR: p'


def test_parse_alert_events():
    assert parse_alert_events("") == list(ALERT_EVENT_NAMES)
    assert parse_alert_events("  ") == list(ALERT_EVENT_NAMES)
    assert parse_alert_events(" Failed , budget,failed,, ") == ["failed", "budget"]
    with pytest.raises(ConfigError) as ei:
        parse_alert_events("failed,bogus")
    assert "bogus" in str(ei.value) and "needs_info" in str(ei.value)


def test_from_env_reads_alert_settings_and_scrubs_the_url(monkeypatch, tmp_path):
    for k, v in {
        "TRELLO_API_KEY": "k",
        "TRELLO_TOKEN": "t",
        "TRELLO_BOARD_ID": "b",
        "GITHUB_TOKEN": "g",
        "REPO_LABELS": "app=o/app",
        "DATA_DIR": str(tmp_path),
        "ALERT_WEBHOOK_URL": URL,
        "ALERT_EVENTS": "failed,error",
    }.items():
        monkeypatch.setenv(k, v)
    cfg = Config.from_env()
    assert cfg.alert_webhook_url == URL and cfg.alert_events == ["failed", "error"]
    assert "ALERT_WEBHOOK_URL" not in os.environ
    assert "SECRET" not in repr(cfg)


def test_from_env_defaults_to_alerts_off_and_all_events(monkeypatch, tmp_path):
    for k, v in {
        "TRELLO_API_KEY": "k",
        "TRELLO_TOKEN": "t",
        "TRELLO_BOARD_ID": "b",
        "GITHUB_TOKEN": "g",
        "REPO_LABELS": "app=o/app",
        "DATA_DIR": str(tmp_path),
    }.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("ALERT_WEBHOOK_URL", raising=False)
    monkeypatch.delenv("ALERT_EVENTS", raising=False)
    cfg = Config.from_env()
    assert cfg.alert_webhook_url == "" and cfg.alert_events == list(ALERT_EVENT_NAMES)
