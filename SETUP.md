# Gmail Ingest — Setup

Stage 1 of the actionable dashboard pipeline. Authenticates against Gmail,
pulls messages, stores them raw in SQLite so downstream extraction can be
re-run without re-fetching.

## 1. Google Cloud Console (one time, ~10 min)

1. Go to https://console.cloud.google.com — create a project, name it
   something like `gmail-action-dashboard`.
2. **APIs & Services → Library** → search "Gmail API" → **Enable**.
3. **APIs & Services → OAuth consent screen**
   - User type: **External** (Internal is only for Workspace orgs)
   - Fill app name, your email for support + developer contact
   - **Scopes**: add `.../auth/gmail.readonly`
   - **Test users**: add your own Gmail address (and Andy's if he'll demo it)
   - Leave it in **Testing** — do NOT submit for verification yet
4. **APIs & Services → Credentials → Create Credentials → OAuth client ID**
   - Application type: **Desktop app**
   - Create → **Download JSON**
5. Rename the downloaded file to `credentials.json` and drop it in this folder.

Note: while the app is unverified you'll see a "Google hasn't verified this
app" warning on the consent screen. Click *Advanced → Go to (unsafe)*. This is
expected and fine for test users. Verification only matters if this is ever
rolled out beyond ~100 people.

## 2. Install

```bash
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

## 3. Authenticate

```bash
python cli.py auth
```

Opens your browser once. On success it writes `token.json` (chmod 600) and
prints your address, message count, and current historyId. Every run after
this refreshes silently.

## 4. First sync

Start small to confirm it works:

```bash
python cli.py sync --full --limit 200
python cli.py stats
```

Then the real run:

```bash
python cli.py sync --full
```

Default query is `newer_than:90d -category:promotions -category:social`.
Override with `--query` or use `--all-mail` for everything.

## 5. Every run after

```bash
python cli.py sync
```

Uses the stored historyId to fetch only what changed. Gmail retains history
for roughly a week — if the stored id goes stale the code detects the 404 and
falls back to a full sync automatically.

## Inspecting what landed

```bash
python cli.py stats
python cli.py show <message_id>
python cli.py thread <thread_id>
```

`thread` is the one to look at most — it's the unit downstream extraction
will operate on, and it's where you'll see how much quoted-reply noise needs
stripping in stage 2.

## Files

| file | role |
|---|---|
| `auth.py` | OAuth flow, token refresh |
| `db.py` | SQLite schema + helpers |
| `ingest.py` | MIME parsing, full sync, incremental sync |
| `cli.py` | commands |

## Security

`credentials.json`, `token.json` and `*.db` are gitignored. The token is a
live credential to your mailbox — treat it like a password. The database will
contain the full text of your email, so keep it local.

## Scope note

Currently `gmail.readonly` — the app physically cannot send, delete, or modify
anything. When you add draft creation later, add `gmail.compose` to `SCOPES`
in `auth.py`, delete `token.json`, and re-run `auth` to re-consent. Scopes are
baked into the issued token.
