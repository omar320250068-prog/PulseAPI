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