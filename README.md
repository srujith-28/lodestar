# Lodestar

Lodestar is a personal AI workspace for the NVIDIA × Nebius Global AI Hackathon Personal AI Track. It brings user-controlled persistent memory, server-routed Nebius chat, an inspectable allowlisted agent runner with optional Nebius planning, and persisted task history into one React + TypeScript experience backed by FastAPI and SQLAlchemy. Local development uses SQLite; hosted deployment uses PostgreSQL so data is not lost with an ephemeral application filesystem.

The backend forwards validated chat messages and, when explicitly requested, agent planning prompts to Nebius Token Factory through its OpenAI-compatible API. The `.env.example` uses `nvidia/Nemotron-3_5-Lightning`, an ID currently shown in Nebius's [Nemotron Token Factory catalog](https://nebius.com/services/token-factory/models/nvidia-nemotron-models-inference). The project owner reports one successful real Chat response through Lodestar with the configured account. Lodestar does not persist proof of that request, so each demo environment should make its own live check. The public template leaves `NEBIUS_API_KEY` blank; configuration alone does not prove a successful inference request. Chat and planning never fabricate a response or fall back to another provider. Model planning can only propose bounded tool calls that the backend validates locally. Research and scheduled watches are not implemented.

## Authentication and public deployment security

Lodestar remains a single-user demo password gate, not a multi-user identity system. Login attempts are rate-limited in the application using an atomic, database-backed bucket: by default, five attempts per observed client address in a 15-minute window. The bucket stores a keyed hash of the address rather than the raw address, survives application restarts, and returns HTTP 429 with `Retry-After` while blocked. Configure `LOGIN_RATE_LIMIT_MAX_ATTEMPTS` and `LOGIN_RATE_LIMIT_WINDOW_SECONDS` to adjust the fallback. It uses SQLite or PostgreSQL atomic upserts and fails closed with a service error on unsupported database dialects or database failures. If the socket peer is missing or invalid, requests share a conservative `unknown` bucket.

Before public access, configure a trusted hosting-edge or reverse-proxy limit on `POST /api/auth/login` as the primary control; match or strengthen the application's default of five requests per client per 15 minutes and return HTTP 429. If the backend is behind a proxy, set `TRUSTED_PROXY_CIDRS` only to exact proxy CIDRs confirmed for that deployment, and ensure that proxy removes caller-supplied forwarding headers and writes a verified `X-Forwarded-For` chain. Lodestar trusts that header only when the direct connection comes from one of those configured CIDRs. Without this setting, it ignores forwarding headers and keys by the direct socket peer; behind a shared proxy, visitors therefore share a fallback bucket. Do not use broad or guessed CIDRs. Keep the backend inaccessible from the public network except through the trusted proxy.

Each signed session includes a random session identifier. The database stores only a keyed hash of that identifier and its expiry, and protected requests check that the record is still active. A successful logout deletes the record, so replaying a copied cookie is rejected by the server; clearing the browser cookie alone is not the revocation mechanism. If the database is unavailable, protected requests fail closed and logout cannot confirm revocation; retry logout after storage recovers. Requests that passed authorization before logout may finish. Sessions expire after eight hours. Deploying this change invalidates cookies created by the earlier stateless-session version; users must sign in again. Keep the signing secret private: rotating it also invalidates every session.

Use HTTPS, Secure cookies, exact HTTPS `CORS_ORIGINS`, and a same-origin frontend/API deployment. Production configuration rejects SQLite and requires PostgreSQL. Public deployment still needs trusted edge-level rate limiting for login and a separate deployment security review. The Render free blueprint below is a temporary demo configuration, not a production durability promise.

## Requirements

- Windows 10 or later with PowerShell
- Python 3.11 or later (the commands below use Python 3.12)
- Node.js 20.19+ or 22.12+ and npm

## Local setup

From the repository root, create a local environment file. The template contains no credentials; the Nebius API key and local login settings are blank. The copied `.env` is ignored by Git:

```powershell
Copy-Item .env.example .env
```

Before starting the app, edit the root `.env` and set both `LODESTAR_AUTH_PASSWORD` and `LODESTAR_SESSION_SECRET`. Use a unique password with at least 12 characters and generate a random session secret with at least 32 characters (for example, `py -3.12 -c "import secrets; print(secrets.token_urlsafe(48))"`). Keep these values private and out of source control. Private API routes fail closed until both are configured. In `APP_ENV=production`, the backend refuses to start if either is missing, requires HTTPS entries in `CORS_ORIGINS`, and requires a PostgreSQL `DATABASE_URL`; SQLite is retained for local development only. Keep frontend and API on the same origin behind the host's HTTPS edge.

### Terminal 1: backend

```powershell
Set-Location .\backend
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m uvicorn app.main:app --reload
```

The API is available at <http://127.0.0.1:8000>. Open <http://127.0.0.1:8000/health> for a minimal API and SQLite connectivity check. The local database and registered tables are initialized at application startup; the default database file is `backend/lodestar.db`. In development, FastAPI's `/docs`, `/redoc`, and `/openapi.json` are available locally. They are disabled when `APP_ENV=production`. `POST /api/chat` accepts a list of user and assistant messages and forwards them to the configured Nebius model only when server-side provider settings are present. When a frontend production build exists, FastAPI serves it at `/` and serves its assets from the same origin.

### Terminal 2: frontend

```powershell
Set-Location .\frontend
npm ci
npm run dev -- --host 127.0.0.1
```

Open the local address printed by Vite (normally <http://127.0.0.1:5173>). Sign in with the configured demo password to open the workspace. Sessions use an eight-hour signed, HttpOnly cookie backed by a revocable database session record; unsafe API requests also require a matching CSRF token. The cookie is `Secure` in production and `SameSite=Lax`; local development over HTTP uses a non-Secure cookie. The login limiter allows five attempts per 15 minutes by default and is configurable through the variables documented below. The screen calls `GET /health` for local API/SQLite health and, after login, `GET /api/provider/status` for server-side configuration only. Chat requests go to the backend; provider and authentication credentials are never sent to the browser. If provider settings are missing or a request fails, the interface shows an error instead of a fabricated answer. Chat history is held in the page only and is not persisted.

## Render free demo deployment

The root `render.yaml` and `Dockerfile` describe one Render Docker web service. The image builds the existing Vite frontend, installs the FastAPI backend, serves the built frontend and `/api/*` from the same origin, and starts Uvicorn without reload on `0.0.0.0:$PORT`. The final image runs as the dedicated non-root `lodestar` user; its local SQLite data directory and temporary home are writable by that user, while Render's `DATABASE_URL` overrides the image's local-development SQLite default. Render's `/health` check exercises the configured database. The Blueprint links `DATABASE_URL` to a new Render PostgreSQL database and sets exact HTTPS CORS to the service's Render URL. No Nebius key is embedded in the deployment files.

### Dashboard steps

1. Push the reviewed deployment files to the existing GitHub repository. This guide does not create another repository. Do not start deployment from a commit that omits the Dockerfile, `render.yaml`, PostgreSQL driver, or frontend-serving changes.
2. In the Render Dashboard choose **New → Blueprint**, connect the existing Lodestar repository, select the branch containing those files, and confirm the root Blueprint file is `render.yaml`. The repository root is the service's build context; do not set a subdirectory root.
3. Review the Blueprint resources: one **Free** Docker web service and one **Free** PostgreSQL database. Keep them in the same Render region. Render builds from `Dockerfile`; its build stage runs `npm ci` and `npm run build`, installs `backend/requirements.txt`, and its container command is `uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-10000}` (without `--reload`). The configured health-check path is `/health`.
   Render's linked connection string is the standard SQLAlchemy form `postgresql://<user>:<password>@<host>:5432/<database>` (the actual value stays in Render's environment and is not copied into source). If configuring a database manually, use the database provider's exact connection string and percent-encode reserved characters; do not commit it.
4. During initial Blueprint setup, enter a unique `LODESTAR_AUTH_PASSWORD` privately in Render. The Blueprint asks for it as an unsynchronized value and generates `LODESTAR_SESSION_SECRET`. Do not import the local `.env`. `APP_ENV=production`, the database connection, exact `CORS_ORIGINS`, and default login limits are provided by the Blueprint. Production startup rejects SQLite and requires both authentication values and HTTPS CORS origins. If you later attach a custom domain, add that domain's exact HTTPS origin to `CORS_ORIGINS` as well.
5. Wait for the database and web service to become healthy. Open the Render service's HTTPS URL and check `/health`, then sign in and verify memory/task persistence. The app creates missing tables on startup in a fresh database; this is not a schema migration and does not copy data from the local SQLite file. Existing local data is left untouched.
6. If you later choose to enable Nebius, add `NEBIUS_API_KEY` through the Render service's Environment page only. Keep it out of `render.yaml`, Git, and Vite variables. Add the existing selected model ID only after confirming that it is available for that account. Provider status reports configuration; it does not itself verify inference.
7. Before sharing the public URL, configure and test trusted per-client edge rate limiting for `POST /api/auth/login` (at least the app default of five attempts per 15 minutes and HTTP 429). Render's built-in DDoS protection is not per-login credential-stuffing protection. Render's [current DDoS guidance](https://render.com/articles/how-render-handles-ddos-attacks) says requests pass through Cloudflare and Render load balancers, the service sees a proxy IP by default, and applications should read `X-Forwarded-For` for the visitor IP. The reviewed Render documentation does not specify a trusted inbound proxy CIDR or guarantee that caller-supplied `X-Forwarded-For` values are sanitized. Accordingly, `render.yaml` intentionally leaves `TRUSTED_PROXY_CIDRS` unset: Lodestar ignores the header and keys the fallback limiter by the socket peer. Visitors reaching the same observed proxy peer may share one five-attempt bucket; per-client application throttling is not verified on Render. Do not guess proxy CIDRs. Until a deployment-level login limit is configured and tested, keep the demo link private. The current Blueprint contains no login-specific edge rate-limit rule.

### Free-tier limits and data lifetime

This is a temporary demonstration setup. Render Free web services have an ephemeral filesystem, cannot attach persistent disks, and spin down after 15 minutes without inbound traffic; the next request can take about a minute to wake. The free workspace allowance is 750 web-service hours per calendar month. The free PostgreSQL instance is limited to 1 GB, expires 30 days after creation, has no backups or managed connection pooling, and becomes inaccessible on expiry; Render documents a 14-day upgrade grace period before deletion. Do not store important or long-term user data there. Free plan availability and monthly build/bandwidth quotas are controlled by Render; $0 assumes the workload stays within included usage and no paid resources are selected. See Render's [free-tier details](https://render.com/docs/free), [web-service HTTPS and port behavior](https://render.com/docs/web-services), and [Blueprint reference](https://render.com/docs/blueprint-spec).

The Memory section lets users add, edit, search, and permanently delete individual notes. Saving is always explicit; chat messages are never copied into memory automatically. Memory content, category, and source stay in the configured database and are not sent to Nebius or Tavily in this milestone. Storage is not encrypted by this application.

Home is a command center. It reads exact memory/task totals, pending approval count, recent tasks, and recent persisted trace events from `GET /api/overview`. Loading and unavailable states are explicit; no sample records are shown. API/database health comes from `GET /health`. `GET /api/provider/status` reports whether a server-side key and model ID are configured without returning the key or making a model call. It does not retain live-call history; a successful Chat reply confirms that request worked, while hackathon model eligibility still needs separate confirmation.

## Agent runtime and reusable skills

The Agent view offers two explicit modes. **Local deterministic** mode runs the registered tool or approved skill selected by the user and makes no model call. **Plan with Nebius** sends the task description to the configured Nebius Token Factory model to propose a JSON plan of at most five steps. The server parses it strictly, rejects unknown tools and invalid or extra arguments, derives permissions from the local registry, and only then passes validated steps to the existing executor. The model cannot execute a tool, set permissions, or approve an action. Planning failures are persisted as failed tasks with no tool execution; there is no automatic fallback, and the local deterministic mode remains available as a separate user choice.

Registered tools are `memory_search` (local keyword search), `current_datetime` (read-only UTC time), and `save_memory` (local write). The planner receives only the task description and server-built tool catalog; it does not receive saved memories or previous tool results. Memory is accessed only when a user selects memory search directly, the model proposes it and the server validates it, or an approved skill includes it. No task automatically saves conversation messages.

Write tools pause in `awaiting_approval` before execution. Approval is per tool call, including each model-proposed write and each write step inside a skill. A model proposal never approves itself. Denial is recorded and the pending tool is not run. Traces record whether planning was performed by the local deterministic path or Nebius, plan validation outcomes, the validated proposal, actual tool inputs/results, approval decisions, and failures. Tool inputs, safe results, lifecycle events, and status transitions are stored in the configured SQLAlchemy database (SQLite locally, PostgreSQL on the hosted demo). Known credential-shaped values are rejected from task/skill text and redacted from trace data; this filtering is not a substitute for storing secrets outside Lodestar.

Skills are saved sequences of registered tool names and validated JSON inputs. They move through `draft`, `pending_approval`, `approved`, and `disabled`; only approved skills can run. Disabling a skill prevents new runs and stops a pending task when it is checked again. Skill approval does not waive the separate per-call approval required by write tools. The runtime never evaluates skill text as Python or shell code.

Agent endpoints:

All `/api` endpoints that expose workspace data or perform actions require the signed demo session. Unsafe requests also require the CSRF header set by the frontend. `/health` and the login/session setup endpoints are public; `/api/auth/logout` requires a session and CSRF token. Health reports service/database status only and never returns user records. Development documentation routes are public locally and disabled in production.

- `GET /api/agent/tools` lists the registered tool schemas and permission levels
- `POST /api/agent/tasks` creates and runs a task until completion, failure, or approval is needed
- `POST /api/agent/tasks/plan` asks the configured Nebius model for a bounded proposal, validates it locally, then uses the same executor and per-write approval flow; a missing provider or invalid plan creates a failed task trace without executing tools
- `GET /api/agent/tasks` lists persisted tasks with pagination and optional status filtering
- `POST /api/agent/tasks/search` searches task input/result summaries and filters by status with a JSON body containing `q`, `status`, `limit`, and `offset`; task search text is not placed in the URL
- `GET /api/agent/tasks/{id}` retrieves task status and plan
- `GET /api/agent/tasks/{id}/trace` retrieves persisted execution events
- `POST /api/agent/tasks/{id}/rerun` creates a new task from a completed or failed task; write-capable tasks require `{ "confirm_write": true }`
- `POST /api/agent/tasks/{id}/approval` records `{ "approved": true|false }` for the waiting tool call
- `/api/agent/skills` lists or creates skills; `GET /api/agent/skills/{id}` inspects one
- `POST /api/agent/skills/{id}/submit`, `/approve`, and `/disable` advance its approval state
- `GET /api/overview` provides persisted counts and a compact local activity summary for Home
- `GET /api/provider/status` reports server-side configuration only; it does not probe the model

The executor runs synchronously in the request that creates or approves a task. Nebius planning is a separate synchronous request and does not itself execute tools. A server interruption can leave a task marked `running`; only its persisted trace can be inspected, and the old in-memory execution cannot be resumed. Cancellation is not implemented. Completed writes are not automatically rolled back if a later step fails. Skill steps do not interpolate tool outputs. The runtime never executes arbitrary code or invokes shell commands. Lodestar has a single-user password gate for this demo, not a multi-user identity system. Use explicit `CORS_ORIGINS`; traces, memories, and skills are not encrypted at rest. Before any public deployment, use HTTPS, a same-origin frontend/API deployment, edge-level login rate limiting, and a separate deployment security review. History lists persisted agent tasks and traces; chat conversations remain page-only and are not included.

History can search task descriptions and result summaries and filter by persisted status. A saved task stores its input, plan snapshot, status, result summary, and trace, but not a live Python execution context. A task left `running` after a server interruption cannot be resumed. **Run again as a new task** reconstructs a supported one-tool deterministic task or starts a new run of its still-approved skill against current local data. For a Nebius-planned task, it makes a new model planning request from the saved task description; it does not replay the old in-memory execution. The new trace links to the source task and leaves the old trace unchanged. A source plan that may write requires confirmation before the new task is created, and every newly proposed write still pauses for its own approval. Repeating or replanning may repeat earlier effects.

The memory API is under `/api/memories`:

- `GET /api/memories` lists memories (supports `limit` and `offset`)
- `POST /api/memories` creates a memory
- `GET /api/memories/{id}` retrieves one memory
- `PUT /api/memories/{id}` replaces its content, category, and source
- `DELETE /api/memories/{id}` permanently deletes it from the application database
- `POST /api/memories/search` performs local keyword search over content, category, and source using a JSON body such as `{ "q": "coastal walks" }`; the search text is not placed in the URL

The former URL-query search forms are no longer supported. Update API clients to use these POST bodies; URL query strings can be recorded in HTTP or proxy access logs. The application does not log request bodies, and deployments should not enable body logging for these endpoints.

Inputs have length limits and obvious credential formats or credential assignments are rejected before persistence. Validation errors omit submitted values so rejected secret-like input is not echoed in the response. Do not use the memory feature to store credentials.

## Nebius configuration

Nebius's [Token Factory quickstart](https://docs.tokenfactory.nebius.com/quickstart) documents the OpenAI-compatible API base URL as `https://api.tokenfactory.nebius.com/v1/`; this is the default in `.env.example`. The example keeps the existing model ID, `nvidia/Nemotron-3_5-Lightning`, which appears on Nebius's [Nemotron Token Factory page](https://nebius.com/services/token-factory/models/nvidia-nemotron-models-inference). A public catalog listing does not prove that the model is enabled for your account. The key field is blank.

When Token Factory access is available, create an API key in the [Token Factory console](https://tokenfactory.nebius.com), then set `NEBIUS_API_KEY` in the ignored root `.env` file. Leave the documented `/v1/` base URL in place, and verify the exact model ID is listed for the account before sending a request. Restart the backend, sign in to Lodestar, and send this harmless prompt in Chat: `Reply with exactly: LODESTAR_NEBIUS_OK`. `GET /api/provider/status` only confirms that settings are present; it does not make an inference request. Treat live inference as verified only after the UI receives and displays a successful reply from that real request. Do not put keys in frontend variables or commit `.env`.

The backend reads:

- `NEBIUS_API_KEY` — server-side Token Factory key
- `NEBIUS_BASE_URL` — OpenAI-compatible endpoint base URL; HTTPS is required
- `NEBIUS_MODEL_ID` — model ID selected from the live catalog; the example is catalog-listed but still needs a successful account-level inference check
- `NEBIUS_TIMEOUT_SECONDS` — whole-request deadline and read/write timeout from 1 to 120 seconds (default 30)
- `NEBIUS_CONNECT_TIMEOUT_SECONDS` — connection and connection-pool timeout from 1 to 30 seconds (default 10)
- `LODESTAR_AUTH_PASSWORD` — single-user demo password, 12–256 characters
- `LODESTAR_SESSION_SECRET` — random server-side session signing secret, at least 32 characters
- `LOGIN_RATE_LIMIT_MAX_ATTEMPTS` — failed or successful sign-in attempts allowed per client per window (default `5`)
- `LOGIN_RATE_LIMIT_WINDOW_SECONDS` — fixed login window in seconds (default `900`)
- `TRUSTED_PROXY_CIDRS` — optional comma-separated exact proxy CIDRs; only set these when the proxy sanitizes and supplies `X-Forwarded-For`
- `APP_ENV` — `development` for local HTTP; set `production` to require authentication settings and issue Secure cookies
- `DATABASE_URL` — local SQLite URL for development; production accepts PostgreSQL URLs supported by the pinned `psycopg2-binary` driver and rejects SQLite
- `APP_NAME` and `CORS_ORIGINS` — service label and explicit browser origins; production origins must be HTTPS
- `TAVILY_API_KEY` and `TAVILY_BASE_URL` — reserved placeholders; no Tavily calls are made in this milestone
- `VITE_API_BASE_URL` — optional frontend API origin when the API is hosted separately. Local Vite development proxies `/api` and `/health` to `http://127.0.0.1:8000`. For browser cookies, serve frontend and API from the same site (for example, sibling subdomains) over HTTPS in production.

Restart the backend after changing `.env` so the new server-side settings are loaded. Nebius calls have bounded connection, read, and whole-request deadlines and automatic SDK retries are disabled to avoid silently repeating a potentially billable request. A timeout does not prove whether the provider received or billed a request. Application throttling is a fallback; configure an edge rate limit before public exposure. Do not trust forwarded client IP headers unless the direct proxy CIDRs are explicitly configured and the proxy overwrites untrusted input. Memory and history search bodies are not logged by the application; avoid adding request-body logging in the app, reverse proxy, or hosting platform.

Without both a configured Nebius key and model ID, `POST /api/chat` and `POST /api/agent/tasks/plan` report a clear not-configured failure. Provider errors are mapped to safe messages; upstream response bodies and credentials are not returned to the browser. Chat and planner tests use mocked responses and are not live inference verification. Explicit tool/skill tasks continue to use local deterministic plans without calling the model.

## Three-minute demo script

1. Start both terminals and open <http://127.0.0.1:5173>. On Home, show the actual locally persisted counts and API/database health. Explain that provider status is configuration-only and does not call a model.
2. Select **Memory → Add a memory** and save a harmless, distinctive note such as `For trip planning, I prefer quiet coastal walks and early coffee.` The note is stored only after you click **Save memory**.
3. Select **Agent**, keep `memory_search` selected, and enter `coastal walks` as the search term. Run the task. Show its completed status, deterministic local plan, actual tool input, matched memory, and persisted execution trace. If Token Factory access is configured, optionally select **Plan with the configured Nebius model** to show its validated proposal and separate actual tool execution. Otherwise use the local path; do not present mocked tests as inference evidence.
4. Open **History** to inspect the saved task and trace. To demonstrate approval, submit a `save_memory` task, inspect the pending input, then approve or deny it in the task view. The write runs only after approval; each write step in an approved skill still asks separately.
5. Open **Skills** to show the draft/submission/approval states. Finish in **Chat**. If valid Token Factory access is configured, send a short prompt and show the reply only if returned by Nebius. Otherwise show the explicit configuration message. Do not present mocked tests as provider evidence.

## Tests and production build

Run the backend suite from `backend`:

```powershell
Set-Location .\backend
.\.venv\Scripts\python.exe -m pytest
```

Memory and agent tests use isolated SQLite files and verify CRUD, search, validation, task lifecycle, per-call approvals, skill state transitions, safe failures, and persisted traces. Agent planner and chat provider tests use mocked responses/client calls; they do not make live inference calls and do not verify Nebius account access. Build the frontend from `frontend`:

```powershell
Set-Location .\frontend
npm run build
```

## Project layout

```text
backend/
  app/api/             Health, authentication, overview, chat, memory, agent, skills, and validated schemas
  app/core/            Environment-backed settings and signed session/CSRF checks
  app/models.py        SQLAlchemy memory, task, trace, and skill models
  app/services/        Nebius adapter, bounded agent planner, local agent runtime, memory search, and tool registry
  tests/               Health, auth, mocked chat, SQLite memory, and agent tests
  requirements.txt     Pinned Python dependencies, including the PostgreSQL driver
frontend/
  src/                 React, TypeScript, Home, History, and feature views
  package.json         Pinned frontend dependencies and scripts
.env.example           Safe local and provider setting placeholders
Dockerfile             Multi-stage frontend build and production FastAPI image
render.yaml            Free Render web-service and PostgreSQL Blueprint
.dockerignore          Excludes secrets, local databases, and generated artifacts from image builds
```

## License

Lodestar is licensed under the MIT License. See [LICENSE](LICENSE).
