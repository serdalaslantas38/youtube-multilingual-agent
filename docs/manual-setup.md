# Manual setup steps (owner actions)

These steps require your Google account and cannot be automated by the agent.
**Do not paste secrets into chat.** Put them in `.env` (local) or Secret Manager (cloud).

## A. Local hygiene (do now)

1. **Move the OAuth client JSON out of the repository.** A file named
   `client_secret_…apps.googleusercontent.com.json` is in the project root. It is
   now git-ignored and docker-ignored, but keep it outside the repo, e.g.:
   ```bash
   mkdir -p ~/.config/youtube-multilingual-agent
   mv client_secret_*.json ~/.config/youtube-multilingual-agent/client_secret.json
   chmod 600 ~/.config/youtube-multilingual-agent/client_secret.json
   ```
   Then set `GOOGLE_OAUTH_CLIENT_SECRETS_FILE` in `.env` to that path.
2. Ensure nothing secret was ever committed: the repository has no commits yet, so
   nothing leaked to git history. If this folder is synced (iCloud/Dropbox), consider
   rotating the client secret in Cloud Console.

## B. Google Cloud project

1. Use the existing project the OAuth client belongs to (or create
   `youtube-multilingual-agent`). Enable billing only when you reach Stage 9.
2. **APIs & Services → Library**: enable **YouTube Data API v3**. Later (Stage 5)
   enable **Vertex AI API** if using Gemini via Vertex, or create a Gemini API key
   in Google AI Studio for local development.
3. **Quotas**: note the default 10,000 units/day for YouTube Data API v3.

## C. OAuth consent screen (Google Auth Platform)

1. User type: **External** (unless you use Google Workspace, then Internal is simpler).
2. App name, support email, developer contact.
3. Scopes: add only `https://www.googleapis.com/auth/youtube.force-ssl`
   (needed for captions list/download/insert and localizations) plus `openid`,
   `email` for dashboard sign-in. No other YouTube scopes.
4. Test users: add your wife's Google account (the one that owns/manages the channel)
   and your own.
5. **Publishing status (keep in *Testing* mode initially):**
   - Keep the consent screen in **Testing** mode during development and initial testing.
   - **Refresh-token implication:** Google expires OAuth refresh tokens after **7 days** when the consent screen is in *Testing* mode. Our channel health checks will surface `token_expired` clearly so you can re-authorize with one click.
   - **Security, consent-screen, and verification implications of switching to *In production* later:**
     - `youtube.force-ssl` is classified by Google as a **sensitive scope**.
     - If you switch the app to *In production* without submitting for Google's OAuth verification process, Google displays an **"Google hasn't verified this app"** warning screen during consent, and caps unverified sensitive-scope users.
     - Completing formal verification requires an official privacy policy URL, a homepage on a verified domain, and a YouTube demonstration video showing how the scope is used.
     - Alternatively, if you use a Google Workspace organization and set User Type to **Internal**, tokens do not expire after 7 days and no verification or warning screen applies (Workspace accounts only).
     - **Rule:** We will keep OAuth in *Testing* mode until Stage 9, and never ask you to accept an unverified-app warning or switch to *In production* without explicit sign-off.
6. **Brand Account vs. Personal Channel considerations:**
   - Do not assume the channel type in advance. During OAuth authorization, Google shows an account/channel selector if the signed-in Google user owns or manages one or more **Brand Accounts**.
   - Whoever authorizes the app must select the **Turkish YouTube channel** (not the personal profile entry) on that selector screen, and must hold **Owner** or **Manager** permissions on the Brand Account (or **Manager / Editor** permissions in YouTube Studio).
   - Immediately after the OAuth callback, our service calls `channels.list(part="snippet,contentDetails", mine=true)` and displays the authenticated **Channel Title**, **Channel ID (`UC...`)**, custom handle, and uploads playlist ID in the dashboard so you can visually confirm the exact channel identity before locking `YOUTUBE_CHANNEL_ID`.

## D. OAuth client (already created — type "Web application")

Authorized redirect URIs:
- Local YouTube authorization: `http://localhost:8000/auth/callback`
- Local dashboard OIDC sign-in: `http://localhost:8000/auth/oidc/callback`
- Production YouTube authorization: `https://<cloud-run-url>/auth/callback`
- Production dashboard OIDC sign-in: `https://<cloud-run-url>/auth/oidc/callback`

Authorized JavaScript origins (dashboard, Stage 6): `http://localhost:5173`.

## E. Development video for the PoC (Stage 3) 🔐

Create an **unlisted** test video on the channel (or a separate test channel) with:
- A short Turkish title and description containing a URL, a hashtag, a number and a name.
- An uploaded **Turkish subtitle file** (standard track), ~10–20 cues.
- Optionally leave YouTube's automatic Turkish captions enabled to test ASR behavior.
Send me only the **video ID** — never credentials.

## F. Values I will eventually need (no secrets in chat)

| Value | Where it goes | Stage |
|---|---|---|
| Legacy workspace channel pin (`UC…`) — optional | `.env` `YOUTUBE_CHANNEL_ID` | 4 |
| OAuth client JSON path | `.env` `GOOGLE_OAUTH_CLIENT_SECRETS_FILE` | 4 |
| `ENCRYPTION_KEY` (generate locally) | `.env` | 4 |
| Gemini API key (local) | `.env` `GEMINI_API_KEY` | 5 |
| Initial legacy workspace bootstrap emails | `.env` `DASHBOARD_ALLOWED_EMAILS` | 6 |
| GCP project id, region | `.env` / Terraform vars | 9 |
| `WEBSUB_SECRET` (generate) | Secret Manager | 8/9 |

## G. Google Cloud production (Stage 9)

Cloud Run (api, worker), Cloud SQL PostgreSQL, Secret Manager, Cloud KMS key ring,
Cloud Scheduler, Artifact Registry, service account with least privilege, budget
alert (recommended: low monthly budget with email alerts), optional IAP.
Exact commands will be provided in Stage 9's runbook.

