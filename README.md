# Lodestar

Lodestar is a personal AI workspace for the NVIDIA × Nebius Global AI Hackathon Personal AI Track. It brings user-controlled SQLite memory, server-routed Nebius chat, an inspectable local tool runner, and persisted task history into one React + TypeScript experience backed by FastAPI.

The backend is prepared to forward validated chat messages to Nebius Token Factory through its OpenAI-compatible API. The `.env.example` uses `nvidia/Nemotron-3_5-Lightning`, an ID currently shown in Nebius's [Nemotron Token Factory catalog](https://nebius.com/services/token-factory/models/nvidia-nemotron-models-inference), but there is no live inference verification in this workspace. `NEBIUS_API_KEY` remains blank. Chat never fabricates a response or falls back to another provider. The Agent view runs local deterministic plans using an explicit tool allowlist; it does not call an LLM. Research and scheduled watches are not implemented.

## Requirements

- Windows 10 or later with PowerShell
- Python 3.11 or later (the commands below use Python 3.12)
- Node.js 20.19+ or 22.12+ and npm

## Local setup

From the repository root, create a local environment file. The template contains no credentials; it has a catalog-listed model ID candidate and a blank API key. The copied `.env` is ignored by Git:

```powershell
Copy-Item .env.example .env
```

### Terminal 1: backend

```powershell
Set-Location .\backend
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m uvicorn app.main:app --reload
```

The API is available at <http://127.0.0.1:8000>. Open <http://127.0.0.1:8000/health> for an API and SQLite connectivity check. The local database and registered tables are initialized at application startup; the default database file is `backend/lodestar.db`. `POST /api/chat` accepts a list of user and assistant messages and forwards it to the configured Nebius model.

### Terminal 2: frontend

```powershell
Set-Location .\frontend
npm ci
npm run dev -- --host 127.0.0.1
```

Open the local address printed by Vite (normally <http://127.0.0.1:5173>). The screen calls `GET /health` for local API/SQLite health and `GET /api/provider/status` for server-side configuration only. Chat requests go to the backend; credentials are never sent to the browser. If provider settings are missing or the request fails, the interface shows an error instead of a fabricated answer. Chat history is held in the page only and is not persisted.

The Memory section lets users add, edit, search, and permanently delete individual notes. Saving is always explicit; chat messages are never copied into memory automatically. Memory content, category, and source stay in the configured local database and are not sent to Nebius or Tavily in this milestone. Storage is not encrypted by this application.

Home is a local command center. It reads exact memory/task totals, pending approval count, recent tasks, and recent persisted trace events from `GET /api/overview`. Loading and unavailable states are explicit; no sample records are shown. Local API/database health comes from `GET /health`. `GET /api/provider/status` reports whether a server-side key and model ID are configured without returning the key or making a model call. Configuration remains unverified until a real chat request succeeds; a successful reply still needs the hackathon team to confirm model eligibility.

## Agent runtime and reusable skills

The Agent view accepts a task and an explicitly selected registered tool or approved skill. Planning is local and deterministic; there are no model calls or generated AI answers in this runtime. Registered tools are `memory_search` (local keyword search), `current_datetime` (read-only UTC time), and `save_memory` (local write). Memory is accessed only when the user selects memory search directly or runs a skill that includes it. No task automatically saves conversation messages.

Write tools pause in `awaiting_approval` before execution. Approval is per tool call, including each write step inside a skill. Denial is recorded and the pending tool is not run. Tool inputs, safe results, lifecycle events, and status transitions are stored in SQLite and available through the execution trace. Known credential-shaped values are rejected from task/skill text and redacted from trace data; this filtering is not a substitute for storing secrets outside Lodestar.

Skills are saved sequences of registered tool names and validated JSON inputs. They move through `draft`, `pending_approval`, `approved`, and `disabled`; only approved skills can run. Disabling a skill prevents new runs and stops a pending task when it is checked again. Skill approval does not waive the separate per-call approval required by write tools. The runtime never evaluates skill text as Python or shell code.

Agent endpoints:

- `GET /api/agent/tools` lists the registered tool schemas and permission levels
- `POST /api/agent/tasks` creates and runs a task until completion, failure, or approval is needed
- `GET /api/agent/tasks` lists persisted tasks for the History view (supports `limit`, `offset`, `q` for task input/result summary, and `status`)
- `GET /api/agent/tasks/{id}` retrieves task status and plan
- `GET /api/agent/tasks/{id}/trace` retrieves persisted execution events
- `POST /api/agent/tasks/{id}/rerun` creates a new task from a completed or failed task; write-capable tasks require `{ "confirm_write": true }`
- `POST /api/agent/tasks/{id}/approval` records `{ "approved": true|false }` for the waiting tool call
- `/api/agent/skills` lists or creates skills; `GET /api/agent/skills/{id}` inspects one
- `POST /api/agent/skills/{id}/submit`, `/approve`, and `/disable` advance its approval state
- `GET /api/overview` provides persisted counts and a compact local activity summary for Home
- `GET /api/provider/status` reports server-side configuration only; it does not probe the model

The runtime executes synchronously in the request that creates or approves a task. A server interruption can leave a task marked `running`; only its persisted trace can be inspected, and the old in-memory execution cannot be resumed. Cancellation is not implemented. Completed writes are not automatically rolled back if a later step fails. The runtime does not plan from natural language, interpolate outputs between skill steps, authenticate multiple users, call Nebius, or invoke external services. The API currently has no user authentication, so run it only in a trusted local environment; do not expose it to the public internet. Set `CORS_ORIGINS` to explicit frontend origins for the environment. Task traces and skills are not encrypted at rest. History lists persisted agent tasks and traces; chat conversations remain page-only and are not included.

History can search task descriptions and result summaries and filter by persisted status. A saved task stores its input, plan snapshot, status, result summary, and trace, but not a live Python execution context. A task left `running` after a server interruption cannot be resumed. **Run again as a new task** reconstructs a supported one-tool task from its saved plan or starts a new run of its still-approved skill against current local data; the new trace links to the source task and leaves the old trace unchanged. Write-capable runs require confirmation before the new task is created, then each write still pauses for its usual per-tool approval. Repeating a plan may repeat earlier effects.

The memory API is under `/api/memories`:

- `GET /api/memories` lists memories (supports `limit` and `offset`)
- `POST /api/memories` creates a memory
- `GET /api/memories/{id}` retrieves one memory
- `PUT /api/memories/{id}` replaces its content, category, and source
- `DELETE /api/memories/{id}` permanently deletes it from the application database
- `GET /api/memories/search?q=...` performs local keyword search over content, category, and source

Inputs have length limits and obvious credential formats or credential assignments are rejected before persistence. Validation errors omit submitted values so rejected secret-like input is not echoed in the response. Do not use the memory feature to store credentials.

## Nebius configuration

The Nebius Token Factory OpenAI-compatible base URL is prefilled in `.env.example` as `https://api.tokenfactory.nebius.com/`, matching Nebius's current Nemotron API example. The example selects `nvidia/Nemotron-3_5-Lightning`, which Nebius currently lists as a public Nemotron endpoint. The key is blank. Put a real key in the ignored root `.env` only after Token Factory access is granted. Verify current catalog and account access before treating live inference as ready. Do not put keys in frontend variables or commit `.env`.

The backend reads:

- `NEBIUS_API_KEY` — server-side Token Factory key
- `NEBIUS_BASE_URL` — OpenAI-compatible endpoint base URL; HTTPS is required
- `NEBIUS_MODEL_ID` — model ID selected from the live catalog; the example is catalog-listed but still needs a successful account-level inference check
- `NEBIUS_TIMEOUT_SECONDS` — request timeout from 1 to 120 seconds (default 30)
- `DATABASE_URL`, `APP_NAME`, and `CORS_ORIGINS` — local service configuration
- `TAVILY_API_KEY` and `TAVILY_BASE_URL` — reserved placeholders; no Tavily calls are made in this milestone
- `VITE_API_BASE_URL` — optional frontend API origin for deployments where the API is hosted separately. Local Vite development proxies `/api` and `/health` to `http://127.0.0.1:8000`.

Restart the backend after changing `.env` so the new server-side settings are loaded.

Without both a configured Nebius key and model ID, `POST /api/chat` returns a clear not-configured response. Provider errors are mapped to safe messages; upstream response bodies and credentials are not returned to the browser. Chat tests use a mocked SDK client and are not live inference verification.

## Three-minute demo script

1. Start both terminals and open <http://127.0.0.1:5173>. On Home, show the actual SQLite-backed counts and local API/database health. Explain that provider status is configuration-only and does not call a model.
2. Select **Memory → Add a memory** and save a harmless, distinctive note such as `For trip planning, I prefer quiet coastal walks and early coffee.` The note is stored only after you click **Save memory**.
3. Select **Agent**, keep `memory_search` selected, and enter `coastal walks` as the search term. Run the task. Show its completed status, deterministic local plan, actual tool input, matched SQLite memory, and persisted execution trace.
4. Open **History** to inspect the saved task and trace. To demonstrate approval, submit a `save_memory` task, inspect the pending input, then approve or deny it in the task view. The write runs only after approval; each write step in an approved skill still asks separately.
5. Open **Skills** to show the draft/submission/approval states. Finish in **Chat**. If valid Token Factory access is configured, send a short prompt and show the reply only if returned by Nebius. Otherwise show the explicit configuration message. Do not present mocked tests as provider evidence.

## Tests and production build

Run the backend suite from `backend`:

```powershell
Set-Location .\backend
.\.venv\Scripts\python.exe -m pytest
```

Memory and agent tests use isolated SQLite files and verify CRUD, search, validation, task lifecycle, per-call approvals, skill state transitions, safe failures, and persisted traces. Chat provider tests use a mocked SDK client and test-only sentinel values; they do not make live inference calls and do not verify Nebius account access. Build the frontend from `frontend`:

```powershell
Set-Location .\frontend
npm run build
```

## Project layout

```text
backend/
  app/api/             Health, overview, chat, memory, agent, skills, and validated schemas
  app/core/            Environment-backed settings
  app/models.py        SQLAlchemy memory, task, trace, and skill models
  app/services/        Nebius adapter, local agent runtime, and tool registry
  tests/               Health, mocked chat, SQLite memory, and agent tests
  requirements.txt     Pinned Python dependencies
frontend/
  src/                 React, TypeScript, Home, History, and feature views
  package.json         Pinned frontend dependencies and scripts
.env.example           Safe local and provider setting placeholders
```

## License

Lodestar is licensed under the MIT License. See [LICENSE](LICENSE).
