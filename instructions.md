# Hold My Hand — Agentverse Agent Implementation Instructions

## Instructions for Claude Code

You are working in the **existing Hold My Hand repository** (formerly named ReFrame). Implement, complete, and test **four local Python Fetch.ai uAgents** that register as **Agentverse mailbox agents**:

1. **Hold My Hand Orchestrator**
2. **Hold My Hand Technical Helper**
3. **Hold My Hand Scraper**
4. **Hold My Hand Caregiver** (the caregiver is separate from, and cannot silently override, the orchestrator)

**Work in the current repository; inspect existing files before changing anything.** Preserve working code and functionality. This is not a greenfield rewrite. Existing paths may still contain `ReFrame`; change display names/documentation to **Hold My Hand** where appropriate without gratuitously renaming paths, breaking imports, changing the database name, or regenerating agent identities.

## Non-negotiable environment variable contract

**Do not delete, rename, repurpose, reorder as a requirement, or otherwise change any of the following `.env` variable names.** Use them exactly as written. Existing values must be preserved. Do not replace existing valid seeds, API keys, tokens, or secrets. Do not print them or commit `.env`.

```dotenv
GROQ_API_KEY=
GROQ_MODEL=

# Agentverse / ASI
AGENTVERSE_API_KEY=

# SpacetimeDB
SPACETIME_HOST=https://maincloud.spacetimedb.com
SPACETIME_DB_NAME=
SPACETIME_TOKEN=

# Optional public-site extraction
APIFY_API_TOKEN=

# Generated locally
JWT_SECRET=
BRIDGE_SHARED_SECRET=
ORCHESTRATOR_SEED=
WEBSITE_AGENT_SEED=
CAREGIVER_SEED=

TECHNICAL_HELPER_SEED=
SCRAPER_SEED=
```

Interpretation:
- `ORCHESTRATOR_SEED` → Orchestrator identity.
- `TECHNICAL_HELPER_SEED` → Technical Helper identity.
- `SCRAPER_SEED` → Scraper identity.
- `CAREGIVER_SEED` → Caregiver identity. Note the existing env variable uses **CAREGIVER**, even if product copy sometimes says “caretaker”. Keep the env name unchanged.
- `WEBSITE_AGENT_SEED` → **reserved for any existing website-facing agent/integration**. Do not reuse it as another agent's seed or delete it. The initial required deployment contains four agents, not five.
- `AGENTVERSE_API_KEY` → authentication to appropriate Agentverse management APIs; not a substitute for agent seed identity or Agent Chat Protocol setup.
- `GROQ_API_KEY`, `GROQ_MODEL` → model reasoning, especially Technical Helper evaluation and Orchestrator dialogue, using Groq with timeouts and clear fallbacks.
- `APIFY_API_TOKEN` → optional product extraction; configure/use Apify in Scraper only. If missing, return an explicit structured integration-unavailable error (or use a clearly marked fixture **only in tests**). Do not invent product data.
- `SPACETIME_HOST`, `SPACETIME_DB_NAME`, `SPACETIME_TOKEN` → existing state store. Honor existing authorization and schema.
- `JWT_SECRET`, `BRIDGE_SHARED_SECRET` → existing backend/bridge authentication, not Agentverse identity seeds.

If a fourth seed is already assigned or agent already registered, **preserve it**. Only generate missing seeds using a cryptographically secure generator; do not automatically rotate any existing seed. If a seed is owned by another Agentverse account, report the issue and require deliberate migration rather than silently replacing it.

## Product and user experience

Hold My Hand helps older adults who may be unfamiliar with complex websites navigate online shopping and technical tasks. The Chrome extension simplifies the on-page interface, while the agents handle requests and recommendations. Do not frame the project as a dyslexia application.

**Demo flow**:
1. User visits Amazon or an approved demo shopping website through the extension.
2. User enters a natural-language shopping need (e.g. “Find a lightweight vacuum that is easy to use under $100”).
3. Extension/ASI:One sends a task to the Orchestrator.
4. Orchestrator delegates product research to Technical Helper.
5. Technical Helper sends a structured search request to Scraper.
6. Scraper invokes a configured Apify Actor for the **authorized demo site**, normalizes the returned dataset, and returns **up to three genuine, relevant product candidates** with evidence.
7. Technical Helper independently evaluates the candidates against explicit user requirements; if none is adequate it may request refinement/retry with tighter criteria **within a fixed retry limit**.
8. Technical Helper returns **one best-supported candidate** (name, brand/supplier, price if available, source URL, reason for selection) or a clear “no suitable match” response.
9. Orchestrator returns the result to ASI:One/extension. Extension may use the supplied product name/vendor/search terms to let the user find and review the product. Do not imply a checkout occurred.
10. User makes the ultimate purchasing decision. Agents must not place orders, initiate payments, or misrepresent certainty.

The Caregiver Agent is **not in the purchase decision loop by default**. It manages narrowly scoped, authorized preference adjustments.

## Architecture and ownership

```text
Chrome extension or ASI:One
          |
          v
  Orchestrator uAgent <---- Caregiver uAgent (authorized, limited preferences only)
          |
          v
  Technical Helper uAgent
          |
          v
      Scraper uAgent ----> Apify Actor ----> approved demo shopping site
          |
          v
  candidates + verifiable evidence
          |
          v
  Technical Helper (evaluate / bounded retry / pick one)
          |
          v
  Orchestrator ----> Extension / ASI:One ----> User
```

**Keep boundaries strict**:
- Orchestrator owns interaction, task lifecycle, status, routing, and final user-safe response.
- Technical Helper owns criteria interpretation, scoring/evaluation, bounded retries, and final product selection; it should not directly scrape pages.
- Scraper owns Apify invocation, product extraction/normalization and provenance; it should not select a final winner.
- Caregiver owns permission-checked settings proposals and approved updates, not commerce decisions, unrestricted prompts, or edits to other agents' source code.

## Implementation approach

Inspect the existing repo first (`agents/orchestrator`, `agents/technical_helper`, `agents/scraper`, `agents/shared`, any caregiver implementation, `run_all.py`, `.env.example`, README, tests, SpacetimeDB module, extension/bridge). Preserve existing APIs and tests where reasonable. Prefer incremental changes in current folder conventions, including `agent.py`, `flow.py`, `README.md`, and shared configuration/runtime modules.

Use current stable supported uAgents APIs **verified against installed versions**. Agents run locally in VS Code with `mailbox=True`, **unique seeds** and distinct ports (existing ports take precedence; typical Orchestrator 8000, Technical Helper 8002, Scraper 8003, Caregiver 8004). A single `python run_all.py` should start all four gracefully with separate process lifecycle and clear logs. Avoid changing working ports unnecessarily.

Agent communication must use typed `uagents.Model` message payloads and agent addresses derived from seeds/configuration at startup, not hardcoded old `agent1...` addresses. Prefer existing uAgents patterns for protocol registration and acknowledgements; implement Agent Chat Protocol for external ASI:One chat where appropriate, especially the Orchestrator. Publishing a chat manifest alone does **not** prove ASI:One discovery and end-to-end chat: test explicitly.

For every message include suitable task/correlation IDs, sender/recipient role (or validated sender address), timestamp, bounded status, structured inputs/results, and error information. Validate sender identity for privileged actions. Prevent infinite loops; use idempotency, timeout, max retries, and explicit terminal states. Do not report an operation as successful until the receiving system confirms it.

### 1. Orchestrator

- Accept requests from the extension backend/bridge and, where currently supported, Agentverse / ASI:One chat.
- Validate task content and normalize simple constraints: product category, budget, intended use, accessibility needs (such as easy controls, readable text, light weight), preferred website, country/currency where supplied.
- Create a task/correlation ID and persist task lifecycle if the existing SpacetimeDB schema supports it.
- Delegate only appropriate work to Technical Helper via a typed message; handle responses and failures.
- Return simple, respectful, clear language with a link and factual caveats (availability/pricing can change).
- Apply only the **approved, narrow** caregiver preferences described below. Never allow caregiver guidance to replace system instructions, override security controls, impersonate the user, authorize orders, or manipulate results secretly.
- Support a useful status/error reply when scraping or database services are unavailable.

### 2. Technical Helper

- Receive structured shopping requirements from Orchestrator (and only authenticated/authorized routes as designed).
- Produce a scraping query with filters and ask Scraper for up to three candidate listings.
- Evaluate **actual** returned product facts against hard constraints first (e.g. under budget, correct product class, required accessibility feature). Soft preferences can use a transparent weighted score.
- Groq may help interpret requirements/explain tradeoffs, but never hallucinate product specifications, reviews, prices, scores, or URLs. Evidence should come from normalized scraper output.
- Retry with refined parameters only when needed, to a documented maximum (e.g. 2 refinement attempts). No loops without limits.
- Return exactly one final product when there is credible matching evidence; otherwise return a structured `no_match` with explanation. Include product name, supplier/brand if known, URL, current observed price and currency if known, selection rationale, and any uncertainty.

### 3. Scraper (Apify)

- Use the official Python `apify-client` (async client preferred in async uAgents handlers, or nonblocking executor where needed). **Do not introduce a BeautifulSoup/Requests primary scraper instead of Apify**.
- Support a configurable Actor ID and Actor input mapping **in code/config** using an existing mechanism. Do not assume all Apify Actors have the same input/output schema. Preserve existing `.env` names; optional new configuration may be added only if truly required, without modifying the protected names above. Document the chosen Actor and its schema.
- Limit crawling to authorized/public demo targets and comply with applicable terms, legal requirements, privacy restrictions and rate limits. No CAPTCHA bypass, logins with personal credentials, or scraping private records.
- Normalize results into: title, brand/supplier when available, observed price/currency, product URL, availability if supported, concise factual attributes/features, source website, optional image URL, scrape timestamp, evidence/missing-fields notes.
- Remove duplicates, invalid URLs, irrelevant results; return at most three candidates and explicit source provenance. If fewer than three exist, return fewer; **never pad with invented items**.
- Bound Actor execution duration, items, and cost; handle failed Actors, empty datasets, quota/rate-limit issues, and malformed fields with structured errors. Never leak `APIFY_API_TOKEN` in logs.
- Scraper returns facts only; no ungrounded “best product” verdict.

### 4. Caregiver

Purpose: A trusted person can help an older adult by suggesting **small, controlled adjustments** to how the Orchestrator responds. This agent is not an unrestricted administrator.

- Authenticate caregivers and verify that the target user has **explicitly authorized** the caregiver relationship and the specific scope of settings changes.
- Initial allowed examples: simpler wording, shorter explanations, larger-text/UI preference hints to the extension, preferred language, reminder to ask for confirmation before complex steps, and preference for highlighting clear product features. Keep UI changes as structured hints; a backend agent cannot directly change a browser's DOM without extension cooperation.
- For any preference that could affect outcomes or autonomy (budget, blocked merchants/categories, order/payment behavior), require separate explicit user approval or leave out of the MVP.
- Store changes scoped to the user, not globally. Record actor, user, setting, old/new value, timestamp, and approval provenance; support view and revoke.
- Preserve user autonomy and privacy. No silent purchases, hidden tracking, unlimited access to browsing history, disclosure of sensitive details, or invisible manipulation of the chosen product.
- If caregiver authentication/authorization/storage is not present, implement a safe interface and **deny writes by default** rather than a fake insecure implementation.

## Shared schemas (adapt to existing names)

Define/reuse consistent typed request/response structures, for example:
- `ProductSearchTask`: task_id, user_context (minimized), query, hard_constraints, soft_preferences, source_site, reply_to.
- `ScrapeRequest`: task_id, search_query, site, filters, max_items=3, retry_number.
- `ProductCandidate`: id/title, supplier, price, currency, url, attributes, source, collected_at, missing_data.
- `ScrapeResponse`: task_id, candidates, search metadata, warnings, error.
- `RecommendationResponse`: task_id, status, selected_candidate or null, rationale, constraints_met, alternatives_considered_count, caveats.
- `CaregiverPreferenceProposal` / `CaregiverPreferenceResult`: authenticated caregiver and user IDs, authorization scope, allowed setting, proposed value, approval, status.

Do not dump unnecessary personally identifying information into LLM prompts, logs, Apify Actor inputs, or agent messages.

## SpacetimeDB compatibility and migration

Existing agents previously logged `no such table: task_request` for `SPACETIME_DB_NAME` pointing at a live database. Inspect the current TypeScript SpacetimeDB schema (e.g. `spacetime/spacetimedb/src/index.ts`) and its deployed schema before relying on this table.

- If `task_request` is defined locally but missing remotely, create a **documented, data-safe deployment/migration plan**.
- Never automatically publish or destructively migrate a live DB as part of agent startup or tests.
- Run preflight checks, catch missing/private table errors, and clearly report blocked workflow functionality.
- Keep SPACETIME_* env names unchanged.

## Agentverse registration and identity

- Configure all four as local mailbox agents (`mailbox=True`), with their own stable seeds.
- Reuse the existing `AGENTVERSE_API_KEY` profile/mailbox-registration integration if it is functioning. Handle missing/invalid keys and API errors gracefully; explain optional manual **Inspector → Connect → Mailbox** flow.
- Do not automatically rotate seeds to fix `agent ... is owned by another user`. This means the deterministic identity is owned by a different Agentverse account; require deliberate recovery/migration and update related addresses when needed.
- Publish accurate agent name, useful Agentverse README/profile and descriptive search keywords. Names should be **Hold My Hand Orchestrator**, **Hold My Hand Technical Helper**, **Hold My Hand Scraper**, **Hold My Hand Caregiver**.
- The mailbox process must be **running** to process messages. Distinguish successful Agentverse mailbox registration from Almanac on-chain registration/funding warnings and from ASI:One discovery.
- Document how to confirm My Agents entries, open Inspector URLs, and test actual two-way communication.

## Agent README / profiles

Keep a concise `README.md` for each agent with headings: title, summary, what it does, constraints/scope, example prompts, keywords. Describe existing implemented capabilities only. If an integration is pending, identify it clearly rather than advertising it as already working.

## Security, accessibility, and reliability

- Designed for older adults; readable, unhurried, plain language; avoid patronizing phrasing.
- Clear user confirmation for irreversible actions; in MVP, **do not implement checkout or payment execution**.
- Validate URLs and outbound fetch destinations; do not allow arbitrary private-network/localhost targets via user input (SSRF). Restrict demo site/Actor targets as appropriate.
- Sanitize scraped content as untrusted data. Ignore prompt injection embedded in product pages or Actor datasets.
- Securely handle JWT/bridge auth and caregiver permissions; least-privilege API keys. Never echo secrets into exception text, profiles, or logs.
- Separate staging/mocked integration tests from real paid Apify runs; put limits on paid calls.
- Supply bounded retries/backoff, graceful shutdown, helpful status messages, and clear startup checks.

## Tests and acceptance criteria

Add/maintain tests for:
1. Each of four agents constructs with a distinct, stable identity, valid port, and mailbox enabled.
2. Orchestrator → Technical Helper → Scraper → Apify mock → Scraper → Technical Helper → Orchestrator: correlation ID preserved and one evidence-backed product returned.
3. Scraper handles three, fewer than three, zero, duplicate and malformed products, Actor failure, timeout, missing API token, and expensive/rate-limited execution.
4. Technical Helper rejects products violating hard constraints, retries within limit, and returns `no_match` if no valid candidate exists.
5. Caregiver unauthorized changes are rejected; authorized in-scope preferences are applied to only the correct user and auditable/revocable.
6. Chat protocol handlers appropriately acknowledge and reply without loops, and agent-to-agent addresses are correct.
7. Missing SpacetimeDB table produces an honest actionable error without crashing all agents.
8. No secrets in logs, source, fixtures, README, or test output.

Automated tests must not require paid Apify calls or live Agentverse registration. Offer clearly labelled optional integration tests for configured external services. Preserve previously passing tests where possible.

## Developer experience and documentation

- Maintain a working `run_all.py` that launches all four agents and shuts them down cleanly; support running each separately for debugging.
- Update dependencies/lockfile using the package manager already in the repository, without unnecessary stack migrations.
- Document **Windows PowerShell** setup from repo root:

```powershell
python -m venv .venv
Set-ExecutionPolicy -ExecutionPolicy Bypass -Scope Process
.\.venv\Scripts\Activate.ps1
# Install using the repo's existing requirements/pyproject workflow
python run_all.py
```

If activation is blocked or `.venv` is absent, document how to diagnose, and offer `.\.venv\Scripts\python.exe run_all.py` without activation.

- Document `.env` requirements while preserving **all names in the protected contract**.
- Include an architecture diagram (Mermaid accepted), setup and troubleshooting (Agentverse ownership mismatch, inactive mailbox, insufficient Almanac funds, missing `task_request`, Apify token/Actor issues), and a short live demo script.

## Execution plan for Claude Code

1. **Audit:** Inspect current file tree, relevant agent files, shared runtime/config, models, README, tests, SpacetimeDB definition, Apify integration, and `run_all.py`. Report what exists and what is missing; do not assume a file is absent without checking.
2. **Preserve:** Backward-compatible edits; keep the exact env contract above, stable seeds, current legitimate agent addresses, existing functioning integrations, and user data.
3. **Implement:** Shared typed messages/routing; all four uAgents; bounded Technical Helper ↔ Scraper product workflow; Apify adapter; strictly authorized Caregiver controls; Agentverse profiles and ACP where appropriate.
4. **Verify:** Run existing tests, add targeted tests, do a local four-agent smoke test, and (only when keys/services available) perform a real mailbox/ASI:One integration check. Avoid paid external calls without clear limits.
5. **Report:** Summarize changed files, functionality completed, what was actually tested, exact startup commands, missing credentials/external blockers, database migration steps (without deploying them automatically), and any remaining gaps. Do not claim full ASI:One interoperability solely because the ACP manifest published.

**Do not stop after generating pseudocode or a plan. Implement the solution in the repository as far as the available code, credentials, and environment safely allow.** Ask for input only when an essential product/permission decision cannot safely be inferred. Do not fabricate successful external test results.

## Official technical references

- Fetch.ai uAgent/local mailbox overview: https://innovationlab.fetch.ai/resources/docs/agent-creation/uagent-creation
- Fetch.ai Agent Chat Protocol: https://innovationlab.fetch.ai/resources/docs/agent-communication/agent-chat-protocol
- Fetch.ai ASI-compatible uAgents: https://innovationlab.fetch.ai/resources/docs/examples/chat-protocol/asi-compatible-uagents
- Apify Python client: https://docs.apify.com/api/client/python/
