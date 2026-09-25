"""Read-only Gmail access (OAuth 2.0, installed-app flow) for Kleinanzeigen
alert emails. Which messages were processed is tracked in the DB, not in
Gmail, so the read-only scope is enough.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]


@dataclass
class EmailMessage:
    message_id: str
    subject: str
    html: str


class GmailClient(Protocol):
    def list_message_ids(self, query: str) -> list[str]: ...
    def get_message(self, message_id: str) -> EmailMessage: ...


def extract_subject(payload: dict[str, Any]) -> str:
    for header in payload.get("headers", []):
        if header.get("name", "").lower() == "subject":
            return str(header.get("value", ""))
    return ""


def _find_part(payload: dict[str, Any], mime_type: str) -> str | None:
    data = payload.get("body", {}).get("data")
    if payload.get("mimeType") == mime_type and data:
        return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode("utf-8", errors="replace")
    for part in payload.get("parts", []):
        found = _find_part(part, mime_type)
        if found is not None:
            return found
    return None


def extract_html(payload: dict[str, Any]) -> str:
    return _find_part(payload, "text/html") or _find_part(payload, "text/plain") or ""


def authorize(credentials_path: str, token_path: str) -> None:
    """One-time interactive consent in the browser; writes a refreshable token."""
    from google_auth_oauthlib.flow import InstalledAppFlow

    flow = InstalledAppFlow.from_client_secrets_file(credentials_path, SCOPES)
    credentials = flow.run_local_server(port=0)
    Path(token_path).parent.mkdir(parents=True, exist_ok=True)
    Path(token_path).write_text(credentials.to_json(), encoding="utf-8")


class GoogleGmailClient:
    def __init__(self, token_path: str):
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build

        if not Path(token_path).exists():
            raise FileNotFoundError(f"{token_path} not found. Run `uv run python cli.py gmail-auth` first.")
        credentials = Credentials.from_authorized_user_file(token_path, SCOPES)  # type: ignore[no-untyped-call]
        if not credentials.valid and credentials.refresh_token:
            credentials.refresh(Request())
            Path(token_path).write_text(credentials.to_json(), encoding="utf-8")  # type: ignore[no-untyped-call]
        self._service = build("gmail", "v1", credentials=credentials, cache_discovery=False)

    def list_message_ids(self, query: str) -> list[str]:
        response = self._service.users().messages().list(userId="me", q=query, maxResults=50).execute()
        return [message["id"] for message in response.get("messages", [])]

    def get_message(self, message_id: str) -> EmailMessage:
        message = self._service.users().messages().get(userId="me", id=message_id, format="full").execute()
        payload = message["payload"]
        return EmailMessage(message_id=message_id, subject=extract_subject(payload), html=extract_html(payload))
