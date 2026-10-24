# ReFrame — Step-by-Step Build Plan

Source: ReFrame Revised PRD (MVP). This document turns the PRD into ordered build phases. Each phase ends with a **Done when** check that must pass before moving on.

---

## 0. Architecture at a Glance

```
┌──────────────────────── Chrome Extension (MV3) ────────────────────────┐
│ Side panel (voice/text, status, Restore, Pause tracking, Privacy)      │
│ Content script: DOM snapshot → overlay renderer → action executor      │
│ Service worker: session, auth token, backend calls, nav tracking       │
└───────────────┬────────────────────────────────────────────────────────┘
                │ HTTPS (JWT)
┌───────────────▼──────────────── FastAPI backend ───────────────────────┐
│ /auth  /goal  /page  /stt  /tts  /privacy  /assist                     │
│ Holds all secrets (Groq, Cartesia, SpacetimeDB token, Apify)           │
└───────┬───────────────────────────┬──────────────────────┬─────────────┘
        │ uAgents REST bridge       │ HTTP API              │
┌───────▼────────┐  messages  ┌─────▼──────────┐    ┌───────▼────────┐
│ Orchestrator   │◄──────────►│ Website Agent  │    │  SpacetimeDB   │
│ (uAgent, chat) │            │ (uAgent)       │    │  (Maincloud)   │
└───────▲────────┘            └────────────────┘    └───────▲────────┘
        │ AssistanceRequest                                 │
┌───────┴────────┐                                          │
│ Caregiver Agent│──────────── reads consented data ────────┘
│ (uAgent, chat) │
└───────▲────────┘
        │ ASI:One / Agentverse chat
   Caregiver / Judge
```

Key rules carried through every phase:
- The extension only executes a fixed set of **structured actions** against elements it already tagged. No LLM-generated JavaScript, ever.
- Sensitive values (passwords, card numbers, CVV, SSN, OTP, any `autocomplete="cc-*"` / `current-password` field) are stripped **in the content script** before anything leaves the browser.
- All API keys live in the backend `.env`. The extension never holds third-party keys.

---

## 1. Repository Layout (target)

```
ReFrame/
├── docs/BUILD_PLAN.md
├── .env.example
├── extension/                 # Chrome MV3, plain JS/HTML/CSS
│   ├── manifest.json
│   ├── service_worker.js
│   ├── sidepanel/ (index.html, sidepanel.js, sidepanel.css)
│   ├── content/  (snapshot.js, overlay.js, executor.js, sanitize.js, overlay.css)
│   └── shared/   (schema.js, api.js)
├── backend/                   # Python 3.12, FastAPI
│   ├── pyproject.toml
│   ├── app/
│   │   ├── main.py
│   │   ├── auth.py            # Argon2id + JWT
│   │   ├── routes/ (goal.py, page.py, voice.py, privacy.py, assist.py)
│   │   ├── stdb.py            # SpacetimeDB HTTP client
│   │   ├── groq_client.py
│   │   ├── cartesia_client.py
│   │   └── apify_client.py
│   └── tests/
├── agents/                    # Fetch.ai uAgents
│   ├── shared/models.py       # message schemas shared by all agents
│   ├── orchestrator/agent.py
│   ├── website_agent/agent.py
│   └── caregiver/agent.py
└── spacetime/                 # SpacetimeDB module
    └── reframe/ (module source + seed script)
```

---

## 2. Phase 0 — Accounts, Tools, Keys

### 2.1 Install locally
| Tool | Purpose | Install |
|---|---|---|
| `uv` | Python 3.12 env (uAgents is not reliable on 3.14) | `brew install uv` |
| SpacetimeDB CLI | Publish module, query DB | `curl -sSf https://install.spacetimedb.com \| sh` |
| Rust toolchain (only if module is Rust) | Build SpacetimeDB module | `brew install rustup && rustup-init` |
| `git` | Version control | already on macOS |
| `cloudflared` or `ngrok` (optional) | Public URL for backend during ASI demo | `brew install cloudflared` |
| Google Chrome | Load unpacked extension | — |

### 2.2 Accounts and keys
| Service | What to get | Env var |
|---|---|---|
| Groq | API key from console.groq.com | `GROQ_API_KEY` |
| Cartesia | API key from play.cartesia.ai; pick a voice ID | `CARTESIA_API_KEY`, `CARTESIA_VOICE_ID` |
| Agentverse / ASI | API key from agentverse.ai (Profile → API Keys) | `AGENTVERSE_API_KEY` |
| SpacetimeDB | Run `spacetime login` (Maincloud), then `spacetime login show --token` | `SPACETIME_TOKEN`, `SPACETIME_DB_NAME`, `SPACETIME_HOST` |
| Apify (optional) | API token from console.apify.com | `APIFY_API_TOKEN` |

Generated locally (no account needed): `JWT_SECRET`, `ORCHESTRATOR_SEED`, `WEBSITE_AGENT_SEED`, `CAREGIVER_SEED`, `BRIDGE_SHARED_SECRET`.

### Done when
- `uv --version` and `spacetime version` work.
- `.env` is filled in and `spacetime login show` shows a logged-in identity.

---

## 3. Phase 1 — Scaffold

1. `git init`, add `.gitignore` (`.env`, `.venv`, `__pycache__`, `node_modules`, `target/`).
2. Create the layout from section 1.
3. `backend/` and `agents/` share one `uv` project (Python 3.12) with: `fastapi`, `uvicorn`, `pydantic`, `httpx`, `argon2-cffi`, `pyjwt`, `uagents`, `uagents-core`, `groq`, `cartesia`, `apify-client`, `python-dotenv`, `pytest`.
4. Minimal `extension/manifest.json` (MV3) with permissions: `sidePanel`, `storage`, `activeTab`, `scripting`, `tabs`, `webNavigation`; host permissions `<all_urls>`.

### Done when
- `uv run uvicorn app.main:app` serves `GET /health → {"ok": true}`.
- Extension loads unpacked in Chrome with no errors and opens an empty side panel.

---

## 4. Phase 2 — SpacetimeDB Module

SpacetimeDB has no official Python SDK. The module runs in SpacetimeDB (Rust or TypeScript). The backend and the agents read it with the HTTP SQL endpoint and write through reducer calls on the HTTP API, using the owner token.

### 4.1 Tables
| Table | Columns |
|---|---|
| `user` | `user_id` (pk, uuid string), `display_name`, `email`, `password_hash` (Argon2id), `prefs_json` (font scale, contrast, speech rate, verbosity), `tracking_paused` (bool), `created_at` |
| `task` | `task_id` (pk), `user_id` (idx), `objective`, `domain`, `status` (`active` / `blocked` / `completed` / `abandoned` / `unknown`), `verified_outcome` (nullable), `created_at`, `updated_at` |
| `task_event` | `event_id` (pk, auto-inc), `task_id` (idx), `user_id` (idx), `kind` (`step` / `navigation` / `error` / `blocker` / `interruption` / `assist_offered` / `assist_accepted` / `completed`), `summary` (redacted text), `url_path` (path only, no query string), `created_at` |
| `caregiver_permission` | `permission_id` (pk), `user_id` (idx), `caregiver_identity` (verified ASI sender address or caregiver account ID), `caregiver_name`, `scope` (`task_summary` / `task_events` / `request_assist`, as a list), `revoked` (bool), `granted_at`, `revoked_at` |
| `pairing_code` | `code` (pk, short random), `user_id`, `scope`, `expires_at`, `used` (bool) |
| `assistance_request` | `request_id` (pk), `user_id`, `task_id`, `from_caregiver`, `suggestion`, `status` (`pending` / `offered` / `accepted` / `declined`), `created_at` |

All tables are private. Only the backend/agents (owner identity) read and write them.

### 4.2 Reducers
`create_user`, `update_prefs`, `set_tracking_paused`, `start_task`, `update_task_status`, `log_task_event`, `create_pairing_code`, `redeem_pairing_code`, `revoke_caregiver`, `create_assistance_request`, `update_assistance_request`, `delete_user_history`.

### 4.3 Seed data
A fictional demo profile, "Margaret (Grandma)", with:
- One completed task (paid her water bill).
- One blocked task: "Download electricity bill". Events: reached billing dashboard, could not find Statements.
- A pre-made pairing code reserved for judges.

### Done when
- `spacetime publish` succeeds.
- `spacetime sql <db> "SELECT * FROM task"` returns seeded rows.
- `backend/app/stdb.py` can call `log_task_event` and read it back (pytest).

---

## 5. Phase 3 — Backend Core and Auth

1. `auth.py`: register/login with Argon2id hashing; issue short-lived JWT (`sub` = internal user ID). The user ID is never accepted as a credential on its own; every route takes it from the verified JWT.
2. Routes (all JWT-protected except auth/health):
   - `POST /auth/register`, `POST /auth/login`
   - `POST /goal` — `{ text, tab_url }` → forwards to Orchestrator
   - `POST /page` — `{ task_id, snapshot }` → forwards to Orchestrator
   - `POST /action-result` — reports success or failure of an executed action
   - `POST /stt` (audio → text), `POST /tts` (text → audio stream)
   - `GET /assist/pending` — extension polls for caregiver suggestions
   - `POST /assist/{id}/respond` — accept or decline
   - `POST /privacy/pause`, `POST /privacy/pairing-code`, `DELETE /privacy/caregiver/{id}`, `DELETE /privacy/history`
3. Rate limiting and payload size caps on `/page`.

### Done when
- Register → login → call `/goal` with JWT works; calling it without a JWT returns 401.
- Pytest covers auth and the redaction of URLs (query strings removed).

---

## 6. Phase 4 — Shared Message Schemas

Define these once in `agents/shared/models.py` (uAgents `Model` / Pydantic). Mirror them in `extension/shared/schema.js`.

**DOM snapshot (extension → backend)**
```json
{
  "url": "https://power.example.com/account",
  "title": "My Account",
  "viewport": {"w": 1440, "h": 900},
  "elements": [
    {"ref": "rf-12", "tag": "a", "role": "link", "text": "Billing & Payments",
     "label": null, "href_path": "/billing", "visible": true, "in_nav": true},
    {"ref": "rf-40", "tag": "input", "role": "textbox", "label": "Account number",
     "input_type": "text", "sensitive": false, "has_value": true}
  ],
  "headings": ["My Account", "Recent activity"],
  "landmarks": ["nav", "main", "footer"]
}
```
Sensitive fields appear only as `{"ref": "...", "sensitive": true, "label": "Password"}` with no value.

**Agent messages**
- `GoalRequest { session_id, user_id, task_id?, text }`
- `PageAnalysisRequest { session_id, task_id, objective, snapshot, history: [step summaries], failure?: {...} }`
- `ReconstructedUI { task_id, page_summary, spoken_guidance, sections: [Section], next_expected: str, confidence }`
- `Section { heading, items: [UIItem] }`
- `UIItem { id, label, kind: "button"|"link"|"field"|"info", target_ref, action: Action, emphasis: "primary"|"normal", requires_confirmation: bool }`
- `Action { type: "click"|"focus"|"scroll_to"|"fill"|"select"|"navigate_link", target_ref, value? }`
- `ActionResult { task_id, item_id, ok, error?, new_url_path? }`
- `AssistanceRequest { request_id, user_id, task_id, suggestion, from_caregiver }`
- `AssistanceAck { request_id, status }`

**Validation rules** (applied by the Orchestrator *and* again by the extension):
- `target_ref` must exist in the snapshot that produced the UI.
- `fill` / `select` are never allowed on `sensitive: true` elements.
- `navigate_link` only targets an existing `<a>` element (no arbitrary URLs).
- Payment submission, purchase, delete, sign-out, and cross-origin links force `requires_confirmation: true`.

### Done when
- Schema unit tests pass, including rejection of invalid refs and sensitive fills.

---

## 7. Phase 5 — Website Agent

1. uAgent with a protocol handling `PageAnalysisRequest` → `ReconstructedUI`.
2. Pipeline:
   1. Pre-rank elements deterministically (keyword overlap with objective, nav and landmark position, visibility) and keep the top ~80 so the prompt stays small.
   2. Groq call with JSON schema output (model configurable via `GROQ_MODEL`). Prompt asks for plain-language labels, at most about 6 primary items, unrelated content dropped, and a single next step.
   3. Validate against section 6 rules; on failure, retry once with the validation errors appended; if still invalid, return a minimal fallback (top-ranked matching links).
3. Re-evaluation: when `failure` is present (action failed, unexpected page, modal), the prompt includes the failure and asks for a recovery route.

### Done when
- Given 3 saved snapshots (account home, billing dashboard, statements page) for "download my electricity bill", it returns valid UIs pointing to the correct refs.

---

## 8. Phase 6 — Orchestrator Agent

1. uAgent with:
   - REST bridge (`on_rest_post`) for the backend, authenticated with `BRIDGE_SHARED_SECRET`.
   - Protocol client to the Website Agent (real uAgents messages, not function calls).
   - Agent chat protocol for Agentverse/ASI (Phase 10).
2. Goal handling: Groq intent parse → `{objective, domain_hint, consequential: bool}` → `start_task` in SpacetimeDB.
3. Page handling: send `PageAnalysisRequest` to the Website Agent, await `ReconstructedUI`, validate, log a `step` event, return to the backend.
4. Progress tracking: on each `ActionResult` / navigation, log `navigation` or `error`. After 2 failed attempts at the same step, set task `blocked` and log a `blocker` event with a plain summary ("Couldn't find Statements on billing dashboard").
5. Completion: when the Website Agent reports the objective is reached (e.g. statement download link clicked), set `completed` with `verified_outcome`.
6. Respect `tracking_paused`: guidance still works, nothing is persisted.

### Done when
- Backend `/goal` + `/page` round-trips through Orchestrator → Website Agent → back, visible in agent logs as two separate agent addresses exchanging messages.
- Task and events appear in SpacetimeDB.

---

## 9. Phase 7 — Chrome Extension

### 7a. Snapshot (`content/snapshot.js`, `content/sanitize.js`)
- Walk visible interactive elements (links, buttons, inputs, selects, `[role=button]`, `[onclick]`), tag each with `data-reframe-ref`.
- Compute accessible name (aria-label, label[for], text, title, alt).
- Sanitize: no input values, mark sensitive fields, strip query strings and fragments from hrefs, truncate long text.

### 7b. Side panel
- Big mic button (push-to-talk) plus a text box.
- Status line, spoken guidance transcript, **Restore original page** button, **Pause tracking** toggle, **Caregivers** (create pairing code, revoke), **Delete my history**.
- Keyboard-operable, visible focus rings, minimum 48px targets, WCAG AA contrast.

### 7c. Overlay renderer (`content/overlay.js`)
- Renders `ReconstructedUI` in a Shadow DOM root covering the page (original DOM untouched underneath).
- Large cards, simplified labels, primary action highlighted, user prefs applied (font scale, contrast).
- One-click Restore removes the overlay and the `data-reframe-ref` attributes.

### 7d. Action executor (`content/executor.js`)
- Switch over `Action.type` only; each case calls a fixed DOM operation on `document.querySelector('[data-reframe-ref="…"]')`.
- Re-checks validation rules locally; refuses sensitive fills.
- `requires_confirmation` shows a large "Yes, continue / No" dialog first.
- For fields the user must type (passwords, card numbers), the overlay temporarily reveals and focuses the **original** field so the user types directly into the site.

### 7e. Multi-page continuity (`service_worker.js`)
- Keeps `{session_id, task_id, objective}` per tab in `chrome.storage.session`.
- On `webNavigation.onCompleted` or SPA URL change, re-snapshots and calls `/page` automatically.
- MutationObserver detects interruptions (modal, cookie banner, session timeout) → sends a snapshot with `failure.kind = "interruption"`.

### Done when
- On a real utility-style site (or a local 3-page demo site), a typed goal produces an overlay whose buttons work across 3 navigation steps, and Restore brings back the original page.

---

## 10. Phase 8 — Voice

1. STT: side panel records audio with `MediaRecorder` → `POST /stt` → Cartesia STT (Groq Whisper as fallback via `STT_PROVIDER`).
2. TTS: `spoken_guidance` → `POST /tts` → Cartesia streaming audio → played in side panel; speech rate from prefs.
3. Voice commands that never hit the LLM: "restore", "repeat", "stop", "yes", "no".

### Done when
- Speaking "I want to pay my electricity bill" produces the overlay and spoken guidance within a few seconds.

---

## 11. Phase 9 — Caregiver Agent and Assistance Flow

### 9a. Identity and consent
- User creates a pairing code in the side panel (scope picked by user, expires in 24h).
- Caregiver messages the Caregiver Agent on ASI:One: "link 4F7K-92". The agent redeems the code and stores the **verified sender address** as `caregiver_identity`.
- Every later request checks `caregiver_permission` for that sender address, `revoked = false`, and the needed scope. No match → refuse, nothing disclosed.

### 9b. Capabilities
- "What is she having trouble with?" → reads blocked/active tasks + events → Groq summary limited to objective, domain, step reached, blocker. No full URLs, no form data, no unrelated browsing.
- "What did she finish this week?" → completed tasks.
- Recurring difficulty: group blockers by type across tasks ("often can't find Statements/Documents links") → suggest pref changes (larger text, slower speech).
- "Can you help her?" → requires `request_assist` scope → writes `assistance_request` and sends `AssistanceRequest` to the Orchestrator → replies with what will happen.

### 9c. User side
- Extension polls `GET /assist/pending`; on the next visit to that domain, side panel says: "Your daughter suggested a route to Statements. Show it?" User accepts or declines. Nothing is clicked on the user's behalf without acceptance.
- Accept/decline logged as `assist_accepted` / `assist_offered`, visible to the caregiver.

### Done when
- Using the seeded Grandma profile, an ASI:One chat returns the real blocker from SpacetimeDB, and an assistance request shows up in the extension and requires user acceptance.

---

## 12. Phase 10 — Agentverse / ASI Demo Capabilities

1. Register Orchestrator and Caregiver agents on Agentverse with mailbox enabled, the chat protocol, README/description, and tags so they are discoverable in ASI:One.
2. **Website guidance (Orchestrator chat):** judge sends a public URL + goal → Orchestrator fetches public content (Apify crawler if configured, otherwise `httpx` with robots.txt respected) → builds a snapshot server-side → Website Agent → replies with step-by-step "where to click" text. Reply is worded as guidance ("Look for…", "Select…") and never claims it clicked anything.
3. **Caregiver support:** judge uses the reserved demo pairing code to link to Grandma, then asks questions.

### Done when
- Both agents respond in ASI:One from a fresh account, with the extension closed.

---

## 13. Phase 11 — Security and Privacy Pass

Checklist:
- [ ] No `eval`, `new Function`, `innerHTML` with model text, or `chrome.scripting.executeScript` with model strings anywhere in the extension.
- [ ] Sensitive fields never serialized (unit test with a page containing password + card fields).
- [ ] Query strings and fragments stripped from all stored URLs.
- [ ] Confirmation dialog for consequential actions.
- [ ] Restore works from any state.
- [ ] Pause tracking stops all writes; delete history removes tasks, events, and assistance requests; revoke caregiver blocks access right away.
- [ ] Caregiver agent refuses unlinked or revoked senders.
- [ ] Keys exist only in backend `.env`; extension bundle contains none.
- [ ] Keyboard-only run-through of the side panel and overlay.

---

## 14. Phase 12 — Demo Script and Acceptance

### Extension demo (judge watches)
1. Open demo utility site → speak "I want to download my electricity bill."
2. Overlay appears; ReFrame speaks the first step.
3. Navigate 3 pages via overlay buttons.
4. A surprise modal ("Rate our site!") appears → ReFrame adapts and offers "Close this popup."
5. Statements page reached → task `completed`.
6. Switch to ASI:One → Caregiver Agent explains what happened, from SpacetimeDB.

### ASI demo (judge drives)
1. Orchestrator chat: public URL + goal → click-by-click guidance.
2. Caregiver chat: link with demo code → "What is Grandma having trouble with?" → "Can you help her?"

### MVP acceptance mapping
| PRD criterion | Verified in |
|---|---|
| Speak a goal, receive reconstructed page | Phase 8 Done when |
| Controls work across ≥3 navigation steps | Phase 7 Done when |
| Two agents exchange real structured messages | Phase 6 Done when |
| Task events persisted in SpacetimeDB | Phase 6 / 9 |
| Caregiver Agent explains events via ASI | Phase 9 / 10 |
| Caregiver requests help without taking control | Phase 9c |
| Agentverse and extension testable independently | Phase 10 Done when |

---

## 15. Defaults Chosen (change before Phase 1 if needed)

| Decision | Default |
|---|---|
| SpacetimeDB hosting | Maincloud (hosted), local `spacetime start` for dev |
| SpacetimeDB module language | TypeScript (Rust if preferred) |
| Auth | Own JWT + Argon2id (no managed provider) |
| Groq model | `openai/gpt-oss-120b` for structured output, configurable |
| STT | Cartesia, Groq Whisper fallback |
| Agent hosting | Local agents with Agentverse mailbox |
| Demo website | A local 3-page fake utility site + one real public site |
