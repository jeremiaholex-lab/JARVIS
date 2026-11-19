"""Read-only Gmail, Calendar and Tasks daily connector using local OAuth tokens.

Place a Google OAuth desktop client JSON at google-oauth-client.json. The user
must enable Gmail, Calendar and Tasks APIs in that Google Cloud project.
"""
from __future__ import annotations
import json
from datetime import datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/calendar.readonly",
    "https://www.googleapis.com/auth/tasks.readonly",
]


class GoogleDailyConnector:
    def __init__(self, client_file: str | Path = ROOT / "google-oauth-client.json", token_file: str | Path = ROOT / ".google-token.json"):
        self.client_file, self.token_file = Path(client_file), Path(token_file)

    def _credentials(self):
        from google.auth.exceptions import RefreshError
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow

        credentials = Credentials.from_authorized_user_file(str(self.token_file), SCOPES) if self.token_file.exists() else None
        if credentials and credentials.expired and credentials.refresh_token:
            try:
                credentials.refresh(Request())
            except RefreshError as exc:
                # A revoked/expired refresh grant requires the owner to sign in
                # again. Do not discard the old token until new consent succeeds.
                if "invalid_grant" not in str(exc):
                    raise
                credentials = None
        if not credentials or not credentials.valid:
            if not self.client_file.exists():
                raise FileNotFoundError(f"Google OAuth client config missing: {self.client_file}")
            credentials = InstalledAppFlow.from_client_secrets_file(str(self.client_file), SCOPES).run_local_server(port=0)
            self.token_file.write_text(credentials.to_json(), encoding="utf-8")
        return credentials

    def fetch_view(self, view: str, *, limit: int = 8) -> list[str]:
        """Fetch one read-only Google view, avoiding unrelated account data."""
        from googleapiclient.discovery import build

        versions = {"email": ("gmail", "v1"), "calendar": ("calendar", "v3"), "tasks": ("tasks", "v1")}
        if view not in versions:
            raise ValueError("view must be email, calendar, or tasks")
        name, version = versions[view]
        service = build(name, version, credentials=self._credentials(), cache_discovery=False)
        if view == "email":
            messages = service.users().messages().list(
                userId="me", q="newer_than:1d", maxResults=max(1, min(int(limit), 20))
            ).execute().get("messages", [])
            emails = []
            for message in messages:
                item = service.users().messages().get(
                    userId="me", id=message["id"], format="metadata",
                    metadataHeaders=["Subject", "From"],
                ).execute()
                headers = {h["name"].lower(): h["value"] for h in item.get("payload", {}).get("headers", [])}
                emails.append(f"{headers.get('from', 'Unknown sender')}: {headers.get('subject', '(no subject)')}")
            return emails
        if view == "calendar":
            now = datetime.now().astimezone()
            start = now.replace(hour=0, minute=0, second=0, microsecond=0)
            end = start + timedelta(days=1)
            events = service.events().list(
                calendarId="primary", timeMin=start.isoformat(), timeMax=end.isoformat(),
                singleEvents=True, orderBy="startTime", maxResults=max(1, min(int(limit), 50)),
            ).execute().get("items", [])
            return [
                f"{e.get('start', {}).get('dateTime', e.get('start', {}).get('date', ''))}: "
                f"{e.get('summary', '(untitled event)')}" for e in events
            ]
        task_lists = service.tasklists().list(maxResults=20).execute().get("items", [])
        tasks = []
        for task_list in task_lists:
            pending = service.tasks().list(
                tasklist=task_list["id"], showCompleted=False, showHidden=False,
                maxResults=max(1, min(int(limit), 50)),
            ).execute().get("items", [])
            tasks.extend(
                f"{task.get('title', 'Untitled')}" + (f" due {task['due'][:10]}" if task.get("due") else "")
                for task in pending
            )
            if len(tasks) >= limit:
                break
        return tasks[:limit]

    def fetch_daily(self) -> dict[str, list[str]]:
        """Fetch bounded metadata for the morning digest."""
        return {view: self.fetch_view(view) for view in ("email", "calendar", "tasks")}
