"""Gmail OAuth. Handles first-time consent and silent token refresh."""

import os
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

# Read-only for now. Adding gmail.compose later means deleting token.json
# and re-consenting, since scopes are baked into the token.
SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

BASE_DIR = Path(__file__).parent
CREDENTIALS_FILE = BASE_DIR / "credentials.json"
TOKEN_FILE = BASE_DIR / "token.json"


def get_credentials() -> Credentials:
    """Return valid creds, prompting for browser consent only if needed."""
    creds = None

    if TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)

    if creds and creds.valid:
        return creds

    # Token expired but we have a refresh token - renew silently.
    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            TOKEN_FILE.write_text(creds.to_json())
            return creds
        except Exception as e:
            print(f"Refresh failed ({e}), re-authenticating...")
            creds = None

    if not CREDENTIALS_FILE.exists():
        raise FileNotFoundError(
            f"Missing {CREDENTIALS_FILE}.\n"
            "Download it from Google Cloud Console > APIs & Services > "
            "Credentials > your OAuth client > Download JSON."
        )

    flow = InstalledAppFlow.from_client_secrets_file(str(CREDENTIALS_FILE), SCOPES)
    # port=0 picks a free port; opens your browser for the consent screen.
    creds = flow.run_local_server(port=0, prompt="consent")

    TOKEN_FILE.write_text(creds.to_json())
    os.chmod(TOKEN_FILE, 0o600)  # token is a live credential - don't leave it world-readable
    print(f"Saved token to {TOKEN_FILE}")

    return creds


def get_service():
    """Authenticated Gmail API client."""
    return build("gmail", "v1", credentials=get_credentials(), cache_discovery=False)


if __name__ == "__main__":
    service = get_service()
    profile = service.users().getProfile(userId="me").execute()
    print(f"Authenticated as : {profile['emailAddress']}")
    print(f"Total messages   : {profile['messagesTotal']:,}")
    print(f"Total threads    : {profile['threadsTotal']:,}")
    print(f"Current historyId: {profile['historyId']}")
