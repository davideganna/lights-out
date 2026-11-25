"""Alerts: POST {"text": ...} to a webhook (Slack, Discord's /slack endpoint, Mattermost).

The webhook URL is a secret: it is never logged, and httpx exception messages (which embed it)
are never used in log output.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable

import httpx

from ..core.config import ALERT_EVENT_NAMES

log = logging.getLogger(__name__)


class Alerter:
    def __init__(self, url: str, events: Iterable[str] = ALERT_EVENT_NAMES, *, http: httpx.Client | None = None):
        self._url = url
        self.events = set(events)
        self._http = http or httpx.Client(timeout=10)

    @property
    def enabled(self) -> bool:
        return bool(self._url)

    def post(self, text: str) -> bool:
        """Send `text` regardless of the event filter. Never raises."""
        if not self.enabled:
            return False
        try:
            resp = self._http.post(self._url, json={"text": text})
        except (httpx.HTTPError, httpx.InvalidURL) as e:  # str(e) may contain the URL: log only the class
            log.warning("Alert not sent: %s", type(e).__name__)
            return False
        if resp.status_code >= 400:
            log.warning("Alert not sent: HTTP %s", resp.status_code)
            return False
        return True

    def send(self, event: str, text: str) -> bool:
        if event not in self.events:
            return False
        return self.post(text)


class NullAlerter:
    enabled = False

    def send(self, event: str, text: str) -> bool:
        return False

    def post(self, text: str) -> bool:
        return False


def card_ref(ticket_or_short_id, title: str, url: str, pr_url: str | None = None) -> str:
    """`#12 "Title" <card url>`, plus ` PR: <pr url>` when there is a PR."""
    ref = f'#{ticket_or_short_id} "{title}" {url}'
    return ref + (f" PR: {pr_url}" if pr_url else "")
