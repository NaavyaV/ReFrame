# ReFrame

A Chrome extension that rebuilds the page you're on into a simpler, larger, calmer version — in the site's own fonts, colours and layout — from a plain-language request like "I want to pay my bill" or "make the text bigger".

Only the front end changes. ReFrame never runs model-generated scripts, and passwords, card numbers and other sensitive field values are never sent to the model.

## How it works

1. The content script takes a snapshot of the page: visible controls, text blocks and theme (fonts, colours, button/card/header classes).
2. The service worker sends the snapshot and your request to the FastAPI backend.
3. The backend asks Groq for a list of structured operations (`render`, `hide`, `relabel`, `style`, `page_style`, `scroll_to`, `restore`), validates them and returns them.
4. The extension applies them. `render` inserts sanitized HTML into the page itself, so the site's CSS applies; its buttons click the real page elements and its inputs mirror the real fields.
5. When you navigate while a goal is active, the new page is rebuilt for the same goal (same site only, cached per page).

Simple visual requests (bigger/smaller text, dark mode, high contrast, hide images, reduce motion, underline links, spacing, undo) are handled locally without calling Groq.

## Repository layout

| Path | Contents |
| --- | --- |
| `extension/` | Chrome MV3 extension (content scripts, service worker, widget) |
| `backend/` | FastAPI server: auth, `/reframe`, Groq calls |
| `spacetime/` | SpacetimeDB module storing user accounts |
| `demo-site/` | Static utility-company site for testing |
| `docs/BUILD_PLAN.md` | Phased build plan |

## Requirements

- Google Chrome
- Python 3.12 and [uv](https://docs.astral.sh/uv/) (`brew install uv`)
- Node.js and the [SpacetimeDB CLI](https://spacetimedb.com/install) (only to publish the database module)
- A [Groq](https://console.groq.com) API key

## Setup

### 1. Environment

```bash
cp .env.example .env
```

Fill in at least:

| Variable | Value |
| --- | --- |
| `GROQ_API_KEY` | Your Groq key |
| `GROQ_MODEL` | `openai/gpt-oss-120b` |
| `GROQ_FAST_MODEL` | `openai/gpt-oss-20b` (automatic rebuilds and overflow) |
| `SPACETIME_HOST` | `https://maincloud.spacetimedb.com` |
| `SPACETIME_DB_NAME` | Your database name |
| `SPACETIME_TOKEN` | Output of `spacetime login show --token` |
| `JWT_SECRET` | Any long random string, e.g. `openssl rand -hex 32` |

The voice, Agentverse and Apify variables aren't used yet. Never commit `.env`.

### 2. Database

```bash
cd spacetime/spacetimedb
npm install
cd ..
spacetime login
spacetime publish <your-db-name> --server maincloud
```

The identity that publishes the module becomes its admin; the backend must use that identity's token as `SPACETIME_TOKEN`.

### 3. Backend

```bash
cd backend
uv sync
uv run uvicorn app.main:app --port 8000 --reload
```

Check it at <http://localhost:8000/health>.

### 4. Demo site (optional)

```bash
cd demo-site
python3 -m http.server 5173
```

Open <http://localhost:5173>.

## Install the extension

1. Open `chrome://extensions`.
2. Turn on **Developer mode** (top right).
3. Click **Load unpacked** and select the `extension/` folder.
4. Pin ReFrame from the puzzle-piece menu.

The extension talks to `http://localhost:8000` by default, so keep the backend running.

## Use it

1. Open any website (or the demo site).
2. Click the ReFrame toolbar icon, the orb in the top-right corner, or press **Alt+Shift+R**.
3. Create an account or sign in.
4. Type a request, for example:
   - "I want to download my electricity bill"
   - "Make the text bigger"
   - "Dark mode"
   - "Hide the ads"
5. Press **Restore** to undo everything on the page.

## Update the extension after code changes

- Extension code: click the reload icon on ReFrame in `chrome://extensions`, then refresh open tabs.
- Backend code: reloads automatically with `--reload`.

## Rate limits

| Limit | Default | Setting |
| --- | --- | --- |
| Groq requests per minute, per model | 30 | `GROQ_RPM` |
| Groq tokens per minute, per model | 8,000 | `GROQ_TPM` |
| Groq requests per day, per model | 1,000 | `GROQ_RPD` |
| Groq tokens per day, per model | 200,000 | `GROQ_TPD` |
| Max wait for Groq budget before a 429 | 8 s | `GROQ_MAX_WAIT_SECONDS` |
| AI requests per user | 6/min, 60/hour, one at a time | `USER_REFRAME_PER_MINUTE`, `USER_REFRAME_PER_HOUR` |
| Login/register per IP | 10/min | `AUTH_PER_MINUTE` |

The Groq values match the free tier for `openai/gpt-oss-120b` and `openai/gpt-oss-20b`. Each model has its own budget; when `GROQ_MODEL` runs out, requests go to `GROQ_FAST_MODEL`. Local quick commands and cached pages don't count.

## Tests

With the backend running:

```bash
cd backend
uv run pytest
```

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| "Can't reach the ReFrame server." | Start the backend on port 8000. |
| Slow replies or 429 errors | Groq free-tier rate limit; wait a few seconds. |
| Groq 404 model error | Set `GROQ_MODEL=openai/gpt-oss-120b` in `.env`. |
| Sign-up fails | Check `SPACETIME_*` values and that the module is published with the same identity. |
| Nothing changes after editing extension code | Reload the extension and refresh the tab. |
