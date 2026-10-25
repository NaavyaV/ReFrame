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
| `spacetime/` | SpacetimeDB module: user accounts and the live agent tracker tables |
| `demo-site/` | Static utility-company site for testing ([reframe-demo.pages.dev](https://reframe-demo.pages.dev)) |
| `tracker/` | Live tracker page ([reframe-tracker.pages.dev](https://reframe-tracker.pages.dev)) |
| `docs/BUILD_PLAN.md` | Phased build plan |
| `agents/` | Orchestrator, technical helper, scraper, and caretaker |

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
| `CARTESIA_API_KEY` | Your Cartesia key (for voice) |
| `CARTESIA_VOICE_ID` | Optional Cartesia voice UUID; a default voice is used otherwise |

The Agentverse and Apify variables aren't used yet. Never commit `.env`.

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

### 5. Agents

```bash
python run_all.py
```

Orchestrator on port 8040, technical helper on 8010, scraper on 8030, caretaker on 8020. They share `.env`.

## Install the extension

1. Open `chrome://extensions`.
2. Turn on **Developer mode** (top right).
3. Click **Load unpacked** and select the `extension/` folder.
4. Pin ReFrame from the puzzle-piece menu.

The extension talks to `http://localhost:8000` by default, so keep the backend running.

## Use it

1. Open any website (or the demo site).
2. Click the ReFrame toolbar icon, the **⋯** button in the top-right corner, or press **Alt+Shift+R**.
3. Create an account or sign in.
4. Click the mic and say a request, or type it in the chat, for example:
   - "I want to download my electricity bill"
   - "Make the text bigger"
   - "Dark mode"
   - "Hide the ads"
5. Press **Restore** to undo everything on the page.

### Voice

Click the mic and speak. It stops when you go quiet, or click it again. The speech goes to Cartesia (`ink-whisper`) and the text goes to the agent like a typed request. While it works the button shows bouncing dots; then the reply is spoken (Cartesia `sonic`), the button shows a live waveform, and the words appear as they're said. Click the button while it speaks to stop it.

Once you've spoken, replies on the following pages are spoken too, until you type something. If the mic is blocked or voice isn't set up, use the **⋯** chat.

| Endpoint | Does |
| --- | --- |
| `POST /voice/transcribe` | audio → text |
| `POST /voice/speak` | text → WAV, word timings and a loudness envelope |

## Live tracker

Every request writes its progress to SpacetimeDB under the ReFrame user's id and name: what was asked, what the agent read, removed, changed and kept, the reply, and any alerts. The tracker page subscribes to those tables and updates as it happens. Filter by user with the menu, or link to `?user=<user id>`.

| Table | One row per |
| --- | --- |
| `agent_run` | request, including automatic rebuilds on the next page: status, step, progress, counts, reply |
| `agent_event` | thing the agent did: `request`, `scan`, `remove`, `change`, `keep`, `reply` |
| `agent_alert` | flag raised during a run: `info`, `warning` or `critical` |

The tables are public so the page can read them without signing in. They hold labels and counts only, never field values or email addresses. Only the backend (the database admin) can write to them.

```bash
cd tracker
npm install
npm run dev        # http://localhost:5173 (stop the demo site first, or use --port)
npm run generate   # after changing the SpacetimeDB module
npm run deploy     # build and publish to Cloudflare Pages
```

On the demo site the steps, alerts and timings are scripted.

## Update the extension after code changes

- Extension code: click the reload icon on ReFrame in `chrome://extensions`, then refresh open tabs. A red ↻ on the toolbar icon means the files changed since the last reload.
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

The Groq values match the free tier for `openai/gpt-oss-120b` and `openai/gpt-oss-20b`. Each model has its own budget; when `GROQ_MODEL` runs out, requests go to `GROQ_FAST_MODEL`. Local quick commands, cached pages and the demo site don't count. Groq usage is saved to `backend/.groq-usage.json`, so restarts keep the day's count. Automatic rebuilds skip the model when the page hasn't really changed.

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
