# PulseAPI — Task CRUD + Secure Auth (FastAPI + Supabase)

A FastAPI project that grows with the course. It starts as a simple REST API
for managing tasks (SQLite, then PostgreSQL), and is extended with a fully
secured authentication layer built on **Supabase Auth**:

- Users sign up, log in and log out.
- Protected endpoints verify a JSON Web Token (**JWT**) presented in the
  `Authorization: Bearer <token>` header.
- Swagger UI at `/docs` shows the padlock and supports the **Authorize**
  button so protected routes can be tested in the browser.

---

## Week 3 — Auth Login & Protect

### The big idea

Secure authentication relies on a trust triangle: the **Client**, your
**Backend Server**, and the **Identity Provider (Supabase)**.

1. **Sign Up / Log In:** the client sends email + password directly to Supabase.
2. **The Token:** Supabase validates the credentials and returns a JWT (Access Token).
3. **The Request:** the client attaches the JWT in an `Authorization` header.
4. **Verification:** the backend decodes and verifies the JWT with Supabase. If
   the token is valid, the server opens the protected door and responds.

We never write cryptography or password-hashing ourselves — Supabase is the
Identity Provider (IdP) and handles all of that securely.

### Tech stack (Python lane)

- **Python 3.10+** / **FastAPI**
- **supabase** (PyPi package) — the Supabase client + auth API
- **python-dotenv** — loads secrets from `.env`
- **Swagger UI** — built into FastAPI at `/docs`
- Git / GitHub

### Project structure

```
assignment2/
├── main.py                  # FastAPI app: task routes (Weeks 1-2) + auth routes (Week 3)
├── auth.py                  # Supabase client, connection check, bearer-token guard
├── postgres_repository.py   # PostgreSQL repository (Week 2)
├── sqlite_repository.py     # original SQLite repository (kept)
├── repository.py            # TaskRepository interface
├── .env                      # your secrets (NOT committed — see .gitignore)
├── .env.example             # template for required environment variables
├── requirements.txt         # Python dependencies
├── swagger-screenshot.png   # Swagger UI screenshot
└── README.md
```

### Set up environment variables

Copy `.env.example` to `.env` and fill in your Supabase project values from
**Project Settings → API** in the Supabase dashboard.

```bash
cp .env.example .env
```

Required variables:

```bash
# Supabase Auth (Project Settings -> API)
SUPABASE_URL=https://your-project-ref.supabase.co
SUPABASE_KEY=your-anon-public-key
PORT=8000

# Task database (Weeks 1-2) — PostgreSQL
DATABASE_URL=postgresql://taskuser:taskpassword@localhost:5432/tasksdb
```

> ⚠️ **Never commit `.env`.** It is already listed in `.gitignore`, so your
> Supabase keys stay private. A peer cloning the repo only sees `.env.example`
> with placeholder values.

> 💡 **One-time Supabase settings:** if your Supabase project has email
> confirmation enabled, new users must confirm their email before they can log
> in. For instant signup→login testing, disable "Confirm email" under
> **Authentication → Sign In / Providers → Email**.

### How to run it

```bash
python -m venv .venv
.venv\Scripts\activate            # Windows
pip install -r requirements.txt
uvicorn main:app --reload         # server starts on http://127.0.0.1:8000
```

On startup the server pings Supabase Auth and logs:

```
Server running and connected to Supabase
```

Swagger UI lives at **http://127.0.0.1:8000/docs**.

### API reference

| Method | Endpoint               | Auth required | Description                                        |
|--------|------------------------|---------------|----------------------------------------------------|
| POST   | /auth/signup           | No            | Create a new user account (email + password)      |
| POST   | /auth/login            | No            | Authenticate user, returns access + refresh JWTs  |
| POST   | /auth/logout           | **Yes**       | Terminate the user session (valid Bearer token)   |
| GET    | /protected/profile     | **Yes**       | Read the verified user's private profile metadata |
| GET    | /protected/dashboard   | **Yes**       | Example second protected route (shows the guard)  |
| GET    | /public/info           | No            | Public, unprotected data                          |
| GET    | /tasks                 | No            | List tasks (Weeks 1-2)                            |
| GET    | /tasks/{task_id}       | No            | Get a single task by ID                           |
| POST   | /tasks                 | No            | Create a new task                                 |
| PUT    | /tasks/{task_id}       | No            | Update a task                                     |
| DELETE | /tasks/{task_id}       | No            | Delete a task                                     |

### Status codes used

| Code | When                                                        |
|------|-------------------------------------------------------------|
| 201  | Successful signup                                           |
| 200  | Successful login / reading a protected resource            |
| 204  | Successful logout                                           |
| 400  | Missing or invalid inputs (empty email/password, bad JSON) |
| 401  | Missing, incorrect, malformed or expired token             |
| 404  | Task not found                                              |
| 503  | Supabase Auth is unreachable                                |

### How the guard works

All the token-checking logic lives in **one reusable FastAPI dependency**:
`get_current_user()` in `auth.py`. Every protected endpoint simply declares
`current: dict = Depends(get_current_user)` and FastAPI:

1. extracts the Bearer token from the `Authorization` header
   (`HTTPBearer` security scheme handles the `Bearer ` prefix parsing),
2. calls `supabase.auth.get_user(token)` to verify the JWT server-side,
3. returns `401 {"error": "Access token required"}` when the header is missing
   and `401 {"error": "Invalid or expired token"}` when Supabase rejects the
   token,
4. otherwise hands the route the verified user's metadata.

`/auth/logout` additionally calls `supabase.auth.admin.sign_out(token)` (the
Supabase SDK's sign-out endpoint) to revoke the session.

### Swagger UI

FastAPI generates Swagger documentation automatically at
**http://127.0.0.1:8000/docs**. The `HTTPBearer` security scheme is wired to
the protected routes, so:

- 🔒 a **lock icon** appears next to `POST /auth/logout`,
  `GET /protected/profile` and `GET /protected/dashboard`;
- clicking **Authorize**, pasting a valid access token, and using
  **Try it out** on `/protected/profile` works straight from the browser.

![Swagger UI screenshot](./swagger-screenshot.png)

### Git history (one commit per stage)

```
Stage 0: setup server and supabase client
Stage 1: signup and login routes working
Stage 2: public route and unverified protected route
Stage 3: profile route token verification
Stage 4: auth middleware and logout endpoint
Stage 5: Swagger UI documentation with bearer auth
Stage 6: publish to GitHub and write README
```

### Quick curl checkpoints

```bash
# register a new account -> 201
curl -i -X POST http://localhost:8000/auth/signup \
  -H "Content-Type: application/json" \
  -d '{"email":"test@example.com","password":"password123"}'

# log in -> 200 with "access_token"
curl -i -X POST http://localhost:8000/auth/login \
  -H "Content-Type: application/json" \
  -d '{"email":"test@example.com","password":"password123"}'

# public route -> 200
curl -i http://localhost:8000/public/info

# protected route without a token -> 401
curl -i http://localhost:8000/protected/profile

# protected route with a valid token -> 200 (paste your access_token)
curl -i http://localhost:8000/protected/profile \
  -H "Authorization: Bearer <PASTE_YOUR_ACCESS_TOKEN_HERE>"
```

---

## Week 4 — Polite Book Scraper

Every data pipeline starts with a question: where does the data come from?
This week's answer is a small, polite scraper that turns **three pages of
messy HTML into clean, checked JSON** — without ever being rude to the server.

It collects **60 books** from the free practice site `books.toscrape.com`,
turns text like `£51.77` into a real number, validates every record against a
schema, and survives a broken page without crashing.

### Politeness & hygiene rules

- **Robots.txt first.** The scraper fetches and parses `robots.txt` and checks
  every page URL against it before collecting. (`books.toscrape.com` ships no
  robots.txt, so it is treated as *allowed by default* — and our own rate limit
  still applies.)
- **Say who you are.** Every request sends a real `User-Agent` identifying the
  scraper.
- **Go slowly.** One request at a time, with a configurable delay (default 1s)
  between hits.
- **Retry, don't hammer.** Transient failures retry with gentle backoff (max 3).
- **Never trust data you didn't create.** Every record must pass the `Book`
  Pydantic schema before it can be written to JSON.
- **Broken pages don't crash the run.** A page that fails is recorded in the
  report and skipped; the remaining pages still complete.

### Files

```
scraper.py          # polite scraper: robots, retries, parsing, validation, JSON output
test_scraper.py     # 9 offline tests (no network needed)
books.json          # the 60 validated books (this run's clean output)
scrape_report.json  # run metadata: pages, rows, failures, timing
```

### How to run

```bash
python scraper.py                     # scrapes 3 pages, polite 1s delay
python scraper.py --pages 5 --delay 2 # your own settings
python test_scraper.py                # run the offline tests
```

Output goes to `books.json` (the clean, checked JSON array) and
`scrape_report.json` (run metadata). A previous run's output is already
committed, so the repo shows a real, validated `books.json`.

### What the output looks like

```json
{
  "title": "A Light in the Attic",
  "price": 51.77,
  "currency": "GBP",
  "availability": "In stock",
  "availability_count": null,
  "rating": 3,
  "url": "https://books.toscrape.com/catalogue/a-light-in-the-attic_1000/index.html"
}
```

Every `price` is a real `float` (no more `£51.77` strings), every `rating` an
integer 1–5, and every record was validated against the `Book` schema before it
was saved — 60/60 books, 0 schema violations in the committed run.

### Reference run

```
Scraped 3/3 pages, 60 books collected in 5.14s.
pages_ok: 3 | pages_failed: 0 | books_collected: 60 | price range: 12.84 - 57.31
```

---

## Week 5 — Trusted LLM Judgement

One workflow step, done by an AI model, with an answer the code can actually
trust. The endpoint `POST /ai/parse-receipt` takes messy receipt text and the
model extracts structured fields — **only** returned after they pass four gates:

1. **Schema** — the answer must validate against the Pydantic `Receipt` model
   (merchant, `YYYY-MM-DD` date, 3-letter currency, positive `total` with a
   rounding guard, and a `line_items` list with positive amounts).
2. **Timeout** — every model call has a hard `LLM_TIMEOUT` (default 20s); the
   request cannot hang forever.
3. **Retries that know when to stop** — network blips, `5xx`/`429`/`408`, a
   reply that is not JSON, or one that fails schema validation all trigger a
   re-ask with backoff — but only up to `LLM_MAX_RETRIES` (default 3). Auth
   errors (e.g. `401`) fail fast: retrying a bad key is pointless.
4. **Tests** — 9 offline tests prove every failure mode without any network or
   API credit (see `test_llm_receipt.py`).

### Files

| File | What it does |
| --- | --- |
| `llm.py` | `LLMClient` (OpenAI-compatible HTTP client), `judge_text()`, `extract_json()` (handles markdown fences / prose), Pydantic schemas, error hierarchy (503 vs 502). |
| `main.py` | adds `POST /ai/parse-receipt` |
| `test_llm_receipt.py` | 9 offline tests using `httpx.MockTransport` |

### Endpoint

```
POST /ai/parse-receipt
{"text": "Corner Cafe - 12 High St\nFlat white 3.50\nToast 4.24\nTotal 9.74"}
```

```json
{
  "merchant": "Corner Cafe",
  "date": "2026-09-20",
  "currency": "GBP",
  "total": 9.74,
  "line_items": [
    {"description": "Flat white", "amount": 3.5},
    {"description": "Toast", "amount": 4.24}
  ]
}
```

### Status codes used

| Code | Meaning |
| --- | --- |
| `200` | Valid judgement that passed the schema |
| `400` | Empty/unusable request text |
| `503` | No API key configured, provider unreachable, or quota/rate-limited after bounded retries |
| `502` | Model kept answering, but never produced schema-valid JSON |

### Providers

Any OpenAI-compatible endpoint works — set `LLM_API_KEY`, `LLM_BASE_URL` and
`LLM_MODEL` in `.env` (see `.env.example`). The default points at OpenAI; the
free options (e.g. Groq) or a local Ollama server are just a URL swap. If
`LLM_API_KEY` is empty the client falls back to the `OPENAI_API_KEY`
environment variable. The key is never committed.

### Run the tests

```
python test_llm_receipt.py
```

`9 LLM judgement tests passed` — including: valid parse, markdown-fenced reply,
gibberish-then-valid (retries), schema-violation recovery, garbage exhaustion
(stops after exactly 3 calls), timeout (bounded, no infinite retry), 401 fails
fast (exactly 1 call), and endpoint behaviour (400/503 mapping).

### Git history

Staged as `Week 5: ...` commits and pushed (see `git log --oneline`).

---

## Week 6 — Report Pipeline: Query → Render → Background Job → Link

The API now produces a **PDF task report** in the background. Querying the task
data, rendering a PDF, and doing the work off the request path — then returning
a **link** to the finished artifact instead of pushing megabytes of bytes
around.

### The pipeline

```
POST /reports            → returns immediately with a job_id (202)
POLL  /reports/{job_id}  → pending → running → done (+ artifact link)
GET   /reports/{job_id}/download → streams the PDF from disk
```

Three layers, each with one job:

1. **Query** — `aggregate_tasks()` (`repository.py`) runs real SQL over the
   tasks table: `COUNT(*)`, `SUM(done)`, `AVG(done)` → total / done / open /
   completion rate. Both SQLite and Postgres implement it.
2. **Render** — `reports.py` builds an A4 PDF with ReportLab: a headline, a
   status-kpi table, the top task rows (max 200), and a small "book collection"
   section (`books.json` from Week 4) with count, average price, most expensive
   and best rated.
3. **Background job** — `jobs.py` is a job store (queue tables `report_jobs` +
   `report_schedules`) mirrored after the repository pattern: PostgreSQL in
   production, `jobs.db` (SQLite) as fallback when Docker is off. `report_worker.py`
   runs two daemon threads in-process:
   - **`JobWorker`** claims a `pending` job → `running` → renders → `done`/`failed`.
   - **`ScheduleRunner`** wakes "due" schedules and enqueues a fresh job.

**Store-and-link:** a done job holds the artifact's *name* + *byte size* in the
`report_jobs` row; the bytes live in `artifacts/` on disk. The download endpoint
serves the file (`FileResponse`) — the API never passes PDF bytes through memory
or JSON, so a big report can't blow up the response.

### Startup resilience

At import time the app builds the job store (tries Postgres, falls back to
`jobs.db`). At startup it tries the task DB (falls back to `tasks.db`), and in
both cases prints a WARNING with the reason. The full stack still works with
Docker **or** plain SQLite — so `uvicorn main:app` runs everywhere.

### Endpoints

| Method | Endpoint | Returns | Meaning |
| --- | --- | --- | --- |
| POST | `/reports` | `202` | enqueue a report job → `job_id` + `status_url` |
| GET | `/reports` | `200` | recent job summaries (newest first) |
| GET | `/reports/{job_id}` | `200` | poll; `done` jobs carry an `artifact` link |
| GET | `/reports/{job_id}/download` | `200` PDF | `409` while running, `404` if no artifact, `410` if file gone |
| POST | `/reports/schedules` | `201` | `name` + `interval_minutes` (1–1440) recurring report |
| GET | `/reports/schedules` | `200` | list schedules |
| DELETE | `/reports/schedules/{schedule_id}` | `204` | stop a schedule |

### Files

```
jobs.py                    # JobRepository (ABC) + SQLite + Postgres impls, tables,
                           # iso_now()/add_minutes() timestamp helpers
reports.py                 # aggregate_books(), build_task_report_pdf(), run_task_report_job()
report_worker.py           # JobWorker + ScheduleRunner + make_report_handlers()
main.py                    # startup fallback, worker start/stop, report routes
test_report_pipeline.py    # 11 offline tests (no network, no Docker)
artifacts/                 # generated PDFs (gitignored)
jobs.db                    # SQLite fallback job store (gitignored)
```

### Try it

```bash
pip install -r requirements.txt        # adds reportlab
uvicorn main:app

# 1. enqueue a report (jobs instantly when Postgres is off -> SQLite fallback)
curl -X POST localhost:8000/reports
# => {"job_id":"...","status":"pending","status_url":"/reports/..."}

# 2. poll until done
curl localhost:8000/reports/<job_id>
# => {"report":{...,"status":"done","artifact":{"url":"/reports/<id>/download",...}}}

# 3. download the PDF
curl -OJ localhost:8000/reports/<job_id>/download

# 4. recurring report every hour
curl -X POST localhost:8000/reports/schedules \
  -H "Content-Type: application/json" \
  -d '{"name":"Hourly snapshot","interval_minutes":60}'
```

### Tests

```
python test_report_pipeline.py
```

`11 report pipeline tests passed` — job lifecycle (pending → done with a real
`%PDF-` artifact, failing jobs record the error), SQLite aggregation, the
schedule runner turning a due schedule into a job, and direct PDF rendering.
All offline, no network or Docker needed.

---

## Week 6 — Visual AI Decision Workflow (`workflow-ai/`)

A separate Next.js project (published at
[github.com/omar320250068-prog/workflow-ai](https://github.com/omar320250068-prog/workflow-ai)):
every node is an AI decision step that returns **YES** or **NO**, edited on a
**React Flow** canvas and executed by **Inngest**. Each node maps to an Inngest
step, asks the LLM a yes/no question, and the answer picks the next edge — so
execution hops through the graph until a terminal node.

- Editor: add decision nodes, wire **YES**/**NO** edges, edit prompts inline.
- Execution: `Inngest` function with one step per node; a deterministic **mock
  judge** runs with no API key, an OpenAI judge runs when `WORKFLOW_PROVIDER=
  openai` + `OPENAI_API_KEY` are set.
- Polish: live node/edge status on the canvas, execution logs panel, saved
  workflows, JSON export/import, execution history, and retry of failed runs.

```bash
cd workflow-ai
npm install
npm run dev                  # http://localhost:3000
npx inngest-cli dev -u http://localhost:3000/api/inngest --port 8288
```

See the [workflow-ai repository's README](https://github.com/omar320250068-prog/workflow-ai)
for the full guide.

---

*Everything below documents the earlier weeks. It is kept intact.*

---

# Task API — CRUD with SQLite

A simple RESTful API for managing tasks, built with FastAPI and backed by a SQLite database for persistent storage.

## Why SQLite?

SQLite was chosen because:
- It requires no separate database server — the entire database lives in a single file.
- It's built into Python's standard library (sqlite3), so no extra installation is needed.
- It's perfect for small projects and learning purposes, while still using real SQL.
- Data survives server restarts, unlike the in-memory list used in the previous version of this project.

## Where the database is stored

The database file is created automatically at "tasks.db" in the project root directory, the first time the application runs. If the file doesn't exist, it is created automatically along with the tasks table. If the table is empty, three example tasks are inserted automatically.

## Database Schema

Table: tasks

| Column | Type    | Description                 |
|--------|---------|-----------------------------|
| id     | INTEGER | Primary key (auto-increment) |
| title  | TEXT    | The task's title            |
| done   | BOOLEAN | Whether the task is completed |

## Exploring the database manually

The database can be inspected using DB Browser for SQLite (https://sqlitebrowser.org/dl/).

### Example SQL query used

UPDATE tasks SET done = 1 WHERE id = 1;
SELECT * FROM tasks;

This updates the first task to "done" directly in the database, and the change is immediately reflected through the API — proving that the API and the database are truly connected.

Screenshot of the database viewer:

![Database screenshot](./db-screenshot.png)

## What changed from the in-memory version

- Tasks are now stored in a real SQLite database (tasks.db) instead of an in-memory Python list.
- Data now persists across server restarts.
- All CRUD operations (GET, POST, PUT, DELETE) now execute real SQL queries (SELECT, INSERT, UPDATE, DELETE) instead of manipulating a list in memory.
- The API's routes, request bodies, and response shapes were not changed — only the storage layer underneath.

## PostgreSQL + Docker (Week 3 · Part 3)

The project has been extended to run PostgreSQL in Docker, with the app and database started together via Docker Compose.

### Architecture

Following the Repository Pattern, the storage layer is fully abstracted behind a `TaskRepository` interface (repository.py). Two implementations exist:

- `sqlite_repository.py` — the original SQLite implementation (kept in the codebase, no longer used by main.py)
- `postgres_repository.py` — the current PostgreSQL implementation, used by main.py

Switching from SQLite to PostgreSQL required changing only two lines in main.py (the import and the repository instantiation). No routes, request/response shapes, or status codes were changed — proving that storage is truly an implementation detail behind the repository interface.

### Running the full stack

1. Copy `.env.example` to `.env` and adjust values if needed.
2. Run:

   ```bash
   docker compose up --build
   ```

3. Verify the API is responding:

   ```bash
   curl http://localhost:8000/tasks
   ```

4. Confirm data persists across container restarts:

   ```bash
   curl -X POST http://localhost:8000/tasks \
     -H "Content-Type: application/json" \
     -d '{"title": "Test full stack persistence"}'

   docker compose down
   docker compose up -d

   curl http://localhost:8000/tasks
   ```

   Example output after restart:

   ```json
   [
     {"id":1,"title":"Buy groceries","done":false},
     {"id":2,"title":"Walk the dog","done":false},
     {"id":3,"title":"Read a book","done":false},
     {"id":4,"title":"Test full stack persistence","done":false}
   ]
   ```