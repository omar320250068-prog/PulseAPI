# How to Add the Next Case Study

A short, concrete playbook so the next portfolio case is a 20-minute log,
not a rebuild. Reuses the Week 2 three-beat shape.

## Where the next case study goes

1. **GitHub repo README** — as a new section, exactly like the Week 4–7
   sections in `PulseAPI/README.md` (best example: the "Week 7 — Background AI
   Jobs" section).
2. **This folder** — the short note + any renders (PDF) live in
   `C:\assignment2\` so they're ready to upload.
3. **Portfolio / case-study page** — the repo link is posted where my other
   case studies are listed (the Week 10 submission link).

## Steps to add one

1. Build the piece of work (see "Next piece" below) and commit + push it.
2. Open the last README section and copy its structure.
3. Edit the three beats — **Problem** · **What I did** · **What came of it** —
   in 3–5 lines each.
4. Run the tests, note the counts in the README, commit + push.
5. Save the short "My 10x / case study" doc here and submit the repo link.

## The three-beat template

- **Problem** — what was broken or manual, and who felt it.
- **What I did** — the build: concept, stack, files, how each program concept
  maps to a piece of code.
- **What came of it** — outcomes: tests passing, numbers, screenshots, how it
  changed the workflow.

## The named next piece of work

**PulseAPI — Week 8: scheduled email report.**

Deliver the task-report PDF (already generated in Week 6) by **email** on a
schedule instead of only via `/reports/{id}/download`, with a lightweight
cache so repeated downloads don't re-render.

- Problem: reports live behind the API; stakeholders must remember to open them.
- What I did: SMTP/Mailtrap sender + a `report_receipts` delivery job on the
  existing scheduler; cache the last rendered PDF.
- What came of it: weekly report lands in the inbox; download is instant.

## Reminder (evidence set)

- **Calendar nudge:** `next-case-study-reminder.ics` in this folder — weekly,
  Sunday 10:00, 30 min. Import: double-click for Outlook, or
  Google Calendar → Settings → Import calendar.
- **Recurring note:** this file is the note; re-open it each Sunday to re-check
  the next beat.

## Build context (kept — why future updates are cheap)

- Stack: FastAPI · PostgreSQL/SQLite · Supabase Auth · ReportLab · OpenAI-compat
  LLM client · BeautifulSoup · httpx · Docker.
- Repo: `github.com/omar320250068-prog/PulseAPI` (public, one commit per week,
  README documents each week).
- Voice: plain English, "we" avoided, concrete file references, test counts.
- Identity kit: name **Omar Hindawy**, repo owner `omar320250068-prog`.
- This conversation (the opencode/Claude project) holds all of the above, so
  the next case is a short conversation, not a rebuild.