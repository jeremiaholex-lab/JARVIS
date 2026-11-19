"""Google connector checks.

Offline by default: verifies the connector is configured (client file present,
token file present) without touching the network. Set JARVIS_LIVE_GOOGLE=1 to
run the real Gmail/Calendar/Tasks fetch the morning briefing depends on.
"""
import os
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent


def test_oauth_client_and_token_files_exist():
    assert (ROOT / "google-oauth-client.json").is_file(), (
        "missing OAuth client file — see docs/google-oauth-setup.md"
    )
    assert (ROOT / ".google-token.json").is_file(), (
        "no stored Google token — run: python phase3.py connect-google"
    )


def test_token_shape_is_readable():
    import json

    data = json.loads((ROOT / ".google-token.json").read_text(encoding="utf-8"))
    assert data.get("token") or data.get("access_token"), "token file has no access token"
    assert "expiry" in data or "expires_in" in data, "token file has no expiry information"


@pytest.mark.skipif(os.environ.get("JARVIS_LIVE_GOOGLE") != "1",
                    reason="set JARVIS_LIVE_GOOGLE=1 to fetch real Google data")
def test_live_daily_fetch():
    from jarvis.skills.google_daily import GoogleDailyConnector

    data = GoogleDailyConnector().fetch_daily()
    for key in ("email", "calendar", "tasks"):
        assert key in data, f"fetch_daily() is missing the {key!r} section"
