# gfi-monitor

A tiny, **zero-cost, read-only** bot that scans a list of GitHub repositories
for beginner-friendly issues that are actually available to work on, and emails
you a digest when it finds new ones.

- **It only reads public GitHub data.** It never forks, branches, comments, or
  opens PRs. No write access is used or needed.
- It runs **once and exits**. The "always watching" behavior comes from your
  operating system's scheduler (Windows Task Scheduler or Linux cron) firing it
  every few hours — which survives reboots and needs no always-on process.
- **No paid infrastructure.** It runs on a machine you already own, uses the free
  GitHub REST API, and emails through Gmail with a free App Password.

---

## How it works

On each run, `monitor.py`:

1. Loads `config.yaml` (which repos to watch + filter rules) and `.env` (secrets).
2. Queries open issues for each repo via the GitHub REST API (PRs excluded).
3. Filters them by your rules: label match, unassigned, recency window, and a
   best-effort "no linked PR" check.
4. Dedupes against `seen.json` so you're never emailed the same issue twice.
5. Emails you **one** digest of the new matches. If there's nothing new, it
   sends no email (but still logs that it ran).

---

## 1. Install

Requires Python 3.8+.

```bash
cd gfi-monitor
python -m pip install -r requirements.txt
```

## 2. Create your `.env`

Copy the template and fill in your own values. **Never commit `.env`** — it's
already git-ignored for you.

```bash
cp .env.example .env      # on Windows: copy .env.example .env
```

Then edit `.env`:

| Variable             | What it is                                                        |
|----------------------|-------------------------------------------------------------------|
| `GITHUB_TOKEN`       | *Optional.* A read-only public token, purely to raise the rate limit. |
| `GMAIL_ADDRESS`      | The Gmail account that sends the digest.                          |
| `GMAIL_APP_PASSWORD` | A 16-char Gmail **App Password** (not your normal password).      |
| `NOTIFY_TO`          | Where the digest is delivered (can be the same as `GMAIL_ADDRESS`). |

### GitHub token (optional but recommended)

Without a token you get **60 requests/hour**; with one you get **5,000/hour**.
The token is used *only* for the higher rate limit — grant it **no scopes**.

1. GitHub → Settings → Developer settings → Personal access tokens.
2. Create a token. For a **fine-grained** token, give it **no repository access**
   beyond public read; for a **classic** token, tick **no scopes at all**.
3. Paste it into `.env` as `GITHUB_TOKEN`.

### Gmail App Password

1. The Gmail account must have **2-Step Verification** enabled.
2. Go to **Google Account → Security → App passwords**.
3. Generate a new app password (any name, e.g. "gfi-monitor"). Google shows a
   **16-character** password once.
4. Paste it into `.env` as `GMAIL_APP_PASSWORD` (spaces optional; the code
   strips whitespace).

## 3. Configure which repos to watch

Edit `config.yaml`. `defaults` apply to every repo; any repo can override any
default. See the comments in that file. The four filters, all toggleable per repo:

1. **`labels_any`** — issue matches if it has ANY of these labels (OR semantics).
2. **`require_unassigned`** — skip issues that already have an assignee.
3. **`require_no_linked_pr`** — skip issues that appear to have a linked PR.
4. **recency** — `max_age_days` (skip stale issues) and optional `min_age_days`
   (skip issues that are too new to avoid racing on them).

## 4. Dry run first

Run with `--dry-run` to see exactly which issues would be reported and what the
email would say — **no email is sent** and `seen.json` is **not** written, so you
can run it repeatedly while tuning `config.yaml`.

```bash
python monitor.py --dry-run
```

When the filtering looks right, run it for real once to send the first digest
and populate `seen.json`:

```bash
python monitor.py
```

---

## 5. Schedule it (make it "constant")

The script runs once and exits — your OS scheduler makes it recurring. This is
more robust than a `while True` loop and survives reboots.

### Windows Task Scheduler (default, zero-cost on your own PC)

**GUI:**

1. Open **Task Scheduler → Create Basic Task**.
2. Name it "gfi-monitor". Trigger: **Daily**.
3. After creating, open the task's **Triggers** tab → edit the trigger →
   check **Repeat task every: 3 hours**, for a duration of **1 day**.
4. **Action:** *Start a program.*
   - **Program/script:** the full path to `python.exe`
     (find it with `where python` or `python -c "import sys; print(sys.executable)"`).
   - **Add arguments:** the full path to `monitor.py`, e.g.
     `"C:\Users\you\Downloads\Open Source\gfi-monitor\monitor.py"`
   - **Start in:** the project folder, e.g.
     `C:\Users\you\Downloads\Open Source\gfi-monitor`
     (this matters so `config.yaml`, `.env`, and `seen.json` are found).
5. Finish. It only runs while the PC is on — that's fine and free.

**Command-line equivalent** (adjust the paths):

```cmd
schtasks /Create /TN "gfi-monitor" /SC HOURLY /MO 3 ^
  /TR "\"C:\Path\To\python.exe\" \"C:\Users\you\Downloads\Open Source\gfi-monitor\monitor.py\"" ^
  /ST 08:00
```

> Note: `schtasks` sets the working directory to `system32`, so the script uses
> its own file location to find `config.yaml`/`.env`/`seen.json` regardless.
> Setting **Start in** via the GUI is still good practice.

### Linux cron (if you later move to a server you already own)

```cron
0 */3 * * * cd /path/to/gfi-monitor && /usr/bin/python3 monitor.py >> monitor.log 2>&1
```

Gotchas:
- `cron` runs with a minimal environment and `/` as the working directory, so
  the `cd` is important (and the script also anchors paths to its own location).
- Use the **absolute** path to `python3` (`which python3` to find it).
- `>> monitor.log 2>&1` captures output so you can confirm it ran.

---

## No paid cloud, ever

This tool is designed to cost nothing. It runs on hardware you already own via a
free OS scheduler, uses the free GitHub REST API, and free Gmail App Passwords.

Cloud "free tier" VMs (AWS/GCP/Oracle) are **out of scope**: they have expiry
dates, credit limits, and silent billing risk, so they are **not** guaranteed
zero-cost. The owned-machine + scheduler path above is the intended answer.

---

## Files

| File              | Purpose                                                    |
|-------------------|------------------------------------------------------------|
| `monitor.py`      | The whole tool: load → query → filter → dedupe → email.    |
| `config.yaml`     | Repos to watch + filter rules. Non-secret; edit freely.    |
| `.env.example`    | Template of required secrets (placeholders only).          |
| `.env`            | **You** create this locally. Git-ignored. Real secrets.    |
| `seen.json`       | Auto-written dedupe store. Git-ignored.                    |
| `requirements.txt`| Python dependencies (`requests`, `python-dotenv`, `PyYAML`).|

## Notes on the "no linked PR" filter

The plain GitHub issue object doesn't say whether a PR is linked, so the tool
reads each candidate issue's **timeline** and looks for `connected` /
`cross-referenced` events whose source is a PR. This costs one extra API request
per candidate, so it only runs on issues that already passed the cheaper filters.

If that check can't be completed (e.g. a request error), the tool is
**conservative**: it still emails the issue but flags it with
"⚠ verify no linked PR before starting" — a false positive is cheaper than
missing a good issue.
