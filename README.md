# gfi-monitor

A small, read-only bot that scans GitHub repos for beginner-friendly
issues and emails you a digest of new ones. It only reads public data and never
writes to GitHub. It runs once and exits. Your OS scheduler makes it recurring.

## How it works

1. Loads `config.yaml` (repos plus filter rules) and `.env` (secrets).
2. Queries open issues per repo via the GitHub REST API. PRs excluded.
3. Filters by label, unassigned, recency, and a best-effort no-linked-PR check.
4. Dedupes against `seen.json` so you never get the same issue twice.
5. Emails one digest of new matches. Nothing new means no email.

## Install

```bash
cd gfi-monitor
python -m pip install -r requirements.txt
```

## Create your `.env`

Copy the template and fill in your values. Never commit `.env`. It is git-ignored.

```bash
cp .env.example .env      # Windows: copy .env.example .env
```

- `GITHUB_TOKEN`: optional read-only public token, only to raise the rate limit
  (60/hr without, 5,000/hr with). Grant it no scopes.
- `GMAIL_ADDRESS`: the Gmail account that sends the digest.
- `GMAIL_APP_PASSWORD`: a 16-char Gmail App Password. Requires 2-Step
  Verification. Generate under Google Account, Security, App passwords.
- `NOTIFY_TO`: where the digest goes. Can equal `GMAIL_ADDRESS`.

## Configure repos

Edit `config.yaml`. `defaults` apply to every repo and any repo can override
them. It ships with about 100 ML, CUDA, and scientific repos. Four toggleable
filters:

- `labels_any`: match if the issue has ANY of these labels. Defaults to
  "good first issue", "help wanted", "good second issue", and "easy", so you
  catch beginner and next-level issues.
- `require_unassigned`: skip issues with an assignee.
- `require_no_linked_pr`: skip issues that appear to have a linked PR.
- `max_age_days` / `min_age_days`: recency window. Default is the last 90 days.

## Dry run first

No email is sent and `seen.json` is not written, so you can run it repeatedly
while tuning `config.yaml`.

```bash
python monitor.py --dry-run
```

Test that email sending works, without waiting for real matches:

```bash
python monitor.py --test-email
```

Then run for real to send the first digest and populate `seen.json`:

```bash
python monitor.py
```

## Schedule it

### GitHub Actions (runs 24/7, does not need your computer)

The workflow in `.github/workflows/monitor.yml` runs on GitHub's servers every
3 hours, so it works whether your computer is on, asleep, or off. Free for a
public repo.

Setup:

1. In the repo, go to Settings, Secrets and variables, Actions, New repository
   secret. Add three secrets:
   - `GMAIL_ADDRESS`: your sending Gmail address.
   - `GMAIL_APP_PASSWORD`: the 16-char Gmail App Password.
   - `NOTIFY_TO`: where the digest goes.
2. The workflow uses the built-in token for API reads, so no extra token is
   required. With about 100 repos, adding a personal token gives more rate-limit
   headroom (5,000 req/hr instead of 1,000). To use one, create a read-only
   public token with no scopes and add it as a secret named `GH_API_TOKEN`. The
   workflow uses it automatically if present.
3. Trigger a first run from the Actions tab (Run workflow) to confirm it works.

The run commits `seen.json` back to the repo so dedupe state carries across
runs. That file holds only issue keys and timestamps, no secrets.

### Windows Task Scheduler (local, only while the PC is on)

This runs only while the computer is powered on and not rebooted. Use GitHub
Actions above if you want true 24/7.

1. Create Basic Task named "gfi-monitor". Trigger: Daily.
2. Edit the trigger, check "Repeat task every: 3 hours" for 1 day.
3. Action: Start a program. Program: full path to `python.exe`. Arguments: full
   path to `monitor.py`. Start in: the project folder.

Command-line equivalent:

```cmd
schtasks /Create /TN "gfi-monitor" /SC HOURLY /MO 3 ^
  /TR "\"C:\Path\To\python.exe\" \"C:\Path\To\gfi-monitor\monitor.py\"" /ST 08:00
```

### Linux cron

```cron
0 */3 * * * cd /path/to/gfi-monitor && /usr/bin/python3 monitor.py >> monitor.log 2>&1
```

Use absolute paths for `python3` and the `cd`. The script also anchors its own
file paths, so `config.yaml`, `.env`, and `seen.json` are found either way.
