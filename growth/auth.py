"""Separate opt-in OAuth credentials for growth read/comment operations."""
import os
from pathlib import Path
from retry import build_google_service

READ_SCOPES = ["https://www.googleapis.com/auth/youtube.readonly"]
COMMENT_SCOPES = READ_SCOPES + ["https://www.googleapis.com/auth/youtube.force-ssl"]
SHEETS_SCOPE = ["https://www.googleapis.com/auth/spreadsheets"]
TOKEN_FILE = Path("token_growth.json")


def get_service(purpose="read"):
    scopes = READ_SCOPES if purpose == "read" else COMMENT_SCOPES if purpose == "comments" else SHEETS_SCOPE
    credentials = _get_credentials(scopes)
    if purpose == "sheets":
        return build_google_service("sheets", "v4", credentials)
    return build_google_service("youtube", "v3", credentials)


def _get_credentials(scopes):
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow

    credentials = None
    if TOKEN_FILE.exists():
        try:
            credentials = Credentials.from_authorized_user_file(str(TOKEN_FILE), scopes)
        except Exception:
            credentials = None
    if credentials and credentials.expired and credentials.refresh_token:
        credentials.refresh(Request())
    if not credentials or not credentials.valid or not set(scopes).issubset(set(credentials.scopes or [])):
        client_secret = Path("client_secret.json")
        if not client_secret.exists():
            raise FileNotFoundError("Growth OAuth needs client_secret.json in the project folder.")
        print("[Growth OAuth] Browser login/consent will open for the separate token_growth.json account scopes.")
        flow = InstalledAppFlow.from_client_secrets_file(str(client_secret), scopes)
        credentials = flow.run_local_server(port=0, open_browser=True)
        with TOKEN_FILE.open("w", encoding="utf-8") as handle:
            handle.write(credentials.to_json())
    return credentials


def get_analytics_service():
    """Build YouTube Analytics API v2 using the isolated read-only growth grant."""
    credentials = _get_credentials(READ_SCOPES)
    return build_google_service("youtubeAnalytics", "v2", credentials)

