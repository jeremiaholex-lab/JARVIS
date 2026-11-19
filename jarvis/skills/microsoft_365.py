"""Read-only Microsoft 365 mail, calendar, and To Do connector via Graph."""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[2]
GRAPH = "https://graph.microsoft.com/v1.0"
SCOPES = ["User.Read", "Mail.Read", "Calendars.Read", "Tasks.Read"]


class Microsoft365Connector:
    def __init__(self, client_id: str | None = None, cache_file: str | Path | None = None):
        self.client_id = (client_id or os.environ.get("JARVIS_MICROSOFT_CLIENT_ID", "")).strip()
        self.cache_file = Path(cache_file or ROOT / ".microsoft-token.bin")

    def _access_token(self) -> str:
        if not self.client_id:
            raise RuntimeError(
                "Microsoft 365 is not configured. Set JARVIS_MICROSOFT_CLIENT_ID "
                "to the Application (client) ID of a Microsoft Entra public-client app."
            )
        try:
            import msal
        except ImportError as exc:
            raise RuntimeError("Microsoft 365 support requires MSAL; install the project dependencies.") from exc

        cache = msal.SerializableTokenCache()
        if self.cache_file.is_file():
            cache.deserialize(self.cache_file.read_text(encoding="utf-8"))
        app = msal.PublicClientApplication(
            self.client_id, authority="https://login.microsoftonline.com/common", token_cache=cache
        )
        accounts = app.get_accounts()
        result = app.acquire_token_silent(SCOPES, account=accounts[0]) if accounts else None
        if not result or "access_token" not in result:
            raise RuntimeError("Microsoft 365 is not signed in. Run: python connect_microsoft_365.py")
        if cache.has_state_changed:
            self.cache_file.write_text(cache.serialize(), encoding="utf-8")
        return result["access_token"]

    def connect(self) -> None:
        """Run interactive device-code consent and save the local token cache."""
        if not self.client_id:
            raise RuntimeError(
                "Set JARVIS_MICROSOFT_CLIENT_ID first. Create a Microsoft Entra app "
                "registration with 'Allow public client flows' enabled."
            )
        try:
            import msal
        except ImportError as exc:
            raise RuntimeError("Install MSAL first: python -m pip install msal") from exc

        cache = msal.SerializableTokenCache()
        if self.cache_file.is_file():
            cache.deserialize(self.cache_file.read_text(encoding="utf-8"))
        app = msal.PublicClientApplication(
            self.client_id, authority="https://login.microsoftonline.com/common", token_cache=cache
        )
        flow = app.initiate_device_flow(scopes=SCOPES)
        if "user_code" not in flow:
            raise RuntimeError("Microsoft device sign-in could not start; check the client ID and app configuration.")
        print(flow.get("message", "Complete Microsoft sign-in in your browser."), flush=True)
        result = app.acquire_token_by_device_flow(flow)
        if "access_token" not in result:
            raise RuntimeError(f"Microsoft sign-in failed: {result.get('error_description', result.get('error', 'unknown error'))}")
        self.cache_file.write_text(cache.serialize(), encoding="utf-8")
        print("Microsoft 365 connected with read-only mail, calendar, and task permissions.", flush=True)

    def fetch_view(self, view: str, *, limit: int = 8) -> list[str]:
        """Fetch bounded summaries from one Graph endpoint; never fetch mail bodies."""
        import requests

        if view not in {"email", "calendar", "tasks"}:
            raise ValueError("view must be email, calendar, or tasks")
        limit = max(1, min(int(limit), 20))
        token = self._access_token()
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        if view == "email":
            url = f"{GRAPH}/me/messages?$top={limit}&$select=from,subject,receivedDateTime&$orderby=receivedDateTime%20desc"
            response = requests.get(url, headers=headers, timeout=20)
            response.raise_for_status()
            return [
                f"{item.get('from', {}).get('emailAddress', {}).get('name', 'Unknown sender')}: "
                f"{item.get('subject') or '(no subject)'} ({item.get('receivedDateTime', '')})"
                for item in response.json().get("value", [])
            ]
        if view == "calendar":
            now = datetime.now().astimezone()
            start = quote(now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat())
            end = quote((now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)).isoformat())
            url = (
                f"{GRAPH}/me/calendarView?startDateTime={start}&endDateTime={end}&$top={limit}"
                "&$select=subject,start,end&$orderby=start/dateTime"
            )
            response = requests.get(url, headers=headers, timeout=20)
            response.raise_for_status()
            return [
                f"{event.get('start', {}).get('dateTime', '')}: {event.get('subject') or '(untitled event)'}"
                for event in response.json().get("value", [])
            ]
        response = requests.get(f"{GRAPH}/me/todo/lists?$top=20", headers=headers, timeout=20)
        response.raise_for_status()
        tasks: list[str] = []
        for task_list in response.json().get("value", []):
            list_id = quote(task_list["id"], safe="")
            task_response = requests.get(
                f"{GRAPH}/me/todo/lists/{list_id}/tasks?$top={limit}&$filter=status%20ne%20'completed'"
                "&$select=title,dueDateTime,status",
                headers=headers,
                timeout=20,
            )
            task_response.raise_for_status()
            for task in task_response.json().get("value", []):
                due = (task.get("dueDateTime") or {}).get("dateTime")
                tasks.append(f"{task.get('title') or 'Untitled'}" + (f" due {due[:10]}" if due else ""))
                if len(tasks) >= limit:
                    return tasks
        return tasks
