#!/usr/bin/env python3
"""
gfi-monitor — a read-only GitHub "good first issue" discovery bot.

What it does, once per run:
  1. Loads config.yaml (repos + filter rules) and .env (secrets).
  2. Queries the GitHub REST API for open issues in each configured repo.
  3. Filters them by label / assignee / recency / linked-PR rules.
  4. Dedupes against seen.json so you never get the same issue twice.
  5. Emails you ONE digest of the new matches (via Gmail App Password).

It never writes to GitHub. It only reads public issue data. The recurring
behavior is provided by your OS scheduler (Task Scheduler / cron), NOT by a
loop in this script — the script runs once and exits. See the README.
"""

import json
import os
import smtplib
import ssl
import sys
import time
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import formatdate

import requests
import yaml
from dotenv import load_dotenv

# Issue titles from GitHub can contain any Unicode (emoji, CJK, ...). The
# Windows console defaults to a legacy code page (cp1252) that can't encode
# those, which would crash on print(). Reconfigure our streams to UTF-8 and
# replace anything unencodable rather than raising.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass                                       # older Python / already-wrapped

# --- Constants -------------------------------------------------------------

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "config.yaml")
SEEN_PATH = os.path.join(HERE, "seen.json")
ENV_PATH = os.path.join(HERE, ".env")

GITHUB_API = "https://api.github.com"
# GitHub asks that every request identify itself with a User-Agent.
USER_AGENT = "gfi-monitor (read-only issue discovery bot)"
# Polite pause between API calls so we never hammer GitHub.
REQUEST_DELAY_SECONDS = 0.5
# How many times to retry a single request after a rate-limit / transient error.
MAX_RETRIES = 3

# Keys that a per-repo entry may override from `defaults`.
FILTER_KEYS = (
    "labels_any",
    "require_unassigned",
    "require_no_linked_pr",
    "max_age_days",
    "min_age_days",
)


# ===========================================================================
# 1. Load config + secrets
# ===========================================================================

def load_config():
    """Read config.yaml and return (defaults, repos). Exit clearly on error."""
    if not os.path.exists(CONFIG_PATH):
        sys.exit(f"ERROR: config.yaml not found at {CONFIG_PATH}")
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    defaults = raw.get("defaults", {}) or {}
    repos = raw.get("repos", []) or []
    if not repos:
        sys.exit("ERROR: config.yaml has no `repos:` to watch.")

    # Merge each repo's own keys over the global defaults so downstream code
    # can just read one flat dict per repo.
    merged = []
    for entry in repos:
        if "owner" not in entry or "name" not in entry:
            sys.exit(f"ERROR: repo entry missing owner/name: {entry!r}")
        repo = dict(defaults)                       # start from defaults
        for key in FILTER_KEYS:
            if key in entry:
                repo[key] = entry[key]              # per-repo override wins
        repo["owner"] = entry["owner"]
        repo["name"] = entry["name"]
        merged.append(repo)
    return defaults, merged


def load_secrets(require_email=True):
    """
    Load .env and return the secrets dict.

    If require_email is True, fail clearly when the Gmail secrets are missing.
    In --dry-run we pass False so you can tune filters before setting up Gmail.
    """
    load_dotenv(ENV_PATH)

    secrets = {
        "GITHUB_TOKEN": os.getenv("GITHUB_TOKEN", "").strip(),
        "GMAIL_ADDRESS": os.getenv("GMAIL_ADDRESS", "").strip(),
        "GMAIL_APP_PASSWORD": os.getenv("GMAIL_APP_PASSWORD", "").strip(),
        "NOTIFY_TO": os.getenv("NOTIFY_TO", "").strip(),
    }

    # Treat the .env.example placeholders as "not set" so a half-filled .env
    # fails loudly instead of silently trying to log in with "replace_me".
    placeholders = {"", "replace_me_optional_read_only_public_token",
                    "replace_me_16_char_app_password", "you@gmail.com"}

    required = ["GMAIL_ADDRESS", "GMAIL_APP_PASSWORD", "NOTIFY_TO"]
    missing = [k for k in required if secrets[k] in placeholders]
    if missing and require_email:
        sys.exit(
            "ERROR: missing required secrets in .env: "
            + ", ".join(missing)
            + f"\nCopy {os.path.join(HERE, '.env.example')} to .env and fill it in."
        )

    # GITHUB_TOKEN is optional (only raises the rate limit). Clear the
    # placeholder so we treat it as absent rather than sending a bogus token.
    if secrets["GITHUB_TOKEN"] in placeholders:
        secrets["GITHUB_TOKEN"] = ""

    return secrets


# ===========================================================================
# 2. GitHub HTTP helper (auth header, delay, rate-limit handling, retries)
# ===========================================================================

def github_session(token):
    """Build a requests.Session with the standard GitHub headers."""
    session = requests.Session()
    session.headers.update({
        "Accept": "application/vnd.github+json",
        "User-Agent": USER_AGENT,
        "X-GitHub-Api-Version": "2022-11-28",
    })
    if token:
        # Bearer auth raises the rate limit to 5,000 req/hr. A read-only
        # public token grants no extra access — it's purely for the limit.
        session.headers["Authorization"] = f"Bearer {token}"
    return session


def github_get(session, url, params=None):
    """
    GET a GitHub URL with polite delay, rate-limit backoff, and retries.

    Returns the requests.Response on success, or None if it ultimately failed
    (so callers can log-and-continue rather than crash the whole run).
    """
    for attempt in range(1, MAX_RETRIES + 1):
        time.sleep(REQUEST_DELAY_SECONDS)          # be polite before every call
        try:
            resp = session.get(url, params=params, timeout=30)
        except requests.RequestException as exc:
            print(f"  ! network error ({exc}); attempt {attempt}/{MAX_RETRIES}")
            time.sleep(2 * attempt)
            continue

        # Primary rate limit: 403/429 with remaining == 0. Back off until reset.
        remaining = resp.headers.get("X-RateLimit-Remaining")
        if resp.status_code in (403, 429) and remaining == "0":
            reset = resp.headers.get("X-RateLimit-Reset")
            wait = _seconds_until_reset(reset)
            print(f"  ! rate limited; waiting {wait}s for reset "
                  f"(attempt {attempt}/{MAX_RETRIES})")
            time.sleep(wait)
            continue

        if resp.status_code == 200:
            return resp

        # 404 etc. — not worth retrying; report and give up on this URL.
        print(f"  ! GitHub returned HTTP {resp.status_code} for {url}")
        return None

    print(f"  ! giving up on {url} after {MAX_RETRIES} attempts")
    return None


def _seconds_until_reset(reset_header):
    """Convert an X-RateLimit-Reset epoch header into a bounded sleep length."""
    try:
        reset_epoch = int(reset_header)
    except (TypeError, ValueError):
        return 60                                  # header missing/garbled: safe default
    now = int(datetime.now(timezone.utc).timestamp())
    # +5s cushion; clamp to [1, 3600] so we never sleep absurdly long.
    return max(1, min(reset_epoch - now + 5, 3600))


def paginate(session, url, params):
    """Yield every item across all pages, following the RFC 5988 `Link` header."""
    params = dict(params)
    params.setdefault("per_page", 100)
    while url:
        resp = github_get(session, url, params=params)
        if resp is None:
            return                                 # error already logged
        for item in resp.json():
            yield item
        # After the first page, the `next` URL already carries the query
        # string, so drop our params to avoid duplicating them.
        url = resp.links.get("next", {}).get("url")
        params = None


def fetch_all(session, url, params):
    """
    Like paginate() but returns (items, ok). `ok` is False if any page failed
    to fetch, so callers can tell "empty result" apart from "request errored".
    """
    items = []
    params = dict(params)
    params.setdefault("per_page", 100)
    while url:
        resp = github_get(session, url, params=params)
        if resp is None:
            return items, False                    # a page failed
        items.extend(resp.json())
        url = resp.links.get("next", {}).get("url")
        params = None
    return items, True


# ===========================================================================
# 2b. Query issues per repo
# ===========================================================================

def fetch_open_issues(session, repo):
    """
    Return open, non-PR issues for one repo, matching ANY of `labels_any`.

    GitHub's `labels=` query param is AND-style (an issue must carry ALL listed
    labels), but the user wants OR semantics. So we query ONCE PER LABEL and
    union the results, deduping by issue number. This is dramatically cheaper
    than fetching every open issue and filtering client-side — a big repo like
    pytorch has thousands of open issues but only a handful tagged
    "good first issue".

    If no labels are configured we fall back to fetching all open issues.
    """
    owner, name = repo["owner"], repo["name"]
    url = f"{GITHUB_API}/repos/{owner}/{name}/issues"
    labels_any = repo.get("labels_any") or []

    by_number = {}                                 # dedupe issues seen via >1 label
    label_queries = labels_any if labels_any else [None]
    for label in label_queries:
        params = {"state": "open", "per_page": 100}
        if label:
            params["labels"] = label               # server-side single-label filter
        for item in paginate(session, url, params):
            # The /issues endpoint returns PRs too; a `pull_request` key marks them.
            if "pull_request" in item:
                continue
            by_number[item["number"]] = item
    return list(by_number.values())


# ===========================================================================
# 3. Filters (all driven by config)
# ===========================================================================

def matches_labels(issue, labels_any):
    """True if the issue carries at least one of the wanted labels."""
    if not labels_any:
        return True                                # no label filter configured
    wanted = {label.lower() for label in labels_any}
    have = {lbl["name"].lower() for lbl in issue.get("labels", [])}
    return bool(wanted & have)


def is_unassigned(issue):
    """True if nobody is assigned to the issue."""
    return issue.get("assignee") is None and not issue.get("assignees")


def within_recency(issue, max_age_days, min_age_days):
    """
    True if the issue's `updated_at` is within [min_age_days, max_age_days].

    max_age_days: skip issues not touched recently (stale).
    min_age_days: skip issues that are TOO new (avoid racing on brand-new ones).
    """
    updated = _parse_iso(issue.get("updated_at"))
    if updated is None:
        return True                                # can't tell; don't exclude
    now = datetime.now(timezone.utc)
    age = now - updated
    if max_age_days and age > timedelta(days=max_age_days):
        return False
    if min_age_days and age < timedelta(days=min_age_days):
        return False
    return True


def _parse_iso(value):
    """Parse a GitHub ISO-8601 timestamp like '2024-01-02T03:04:05Z'."""
    if not value:
        return None
    try:
        # Python's fromisoformat handles the '+00:00' form; swap trailing Z.
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def has_linked_pr(session, repo, issue):
    """
    Best-effort detection of whether a PR is linked to this issue.

    The plain issue object does NOT expose linked PRs, so we read the issue
    timeline and look for events that connect a PR:
      - `connected` / `disconnected` — an explicit "linked pull request"
      - `cross-referenced` whose source is a PR (someone referenced this issue
        from a PR)

    This costs one extra request per issue, so callers only invoke it for
    issues that already passed the cheaper filters.

    Returns (linked: bool, reliable: bool). If the timeline can't be fetched,
    we return (False, False) so the caller can flag the issue as "unverified"
    rather than silently dropping a potentially-good match.
    """
    owner, name = repo["owner"], repo["name"]
    number = issue["number"]
    url = f"{GITHUB_API}/repos/{owner}/{name}/issues/{number}/timeline"

    events, ok = fetch_all(session, url, {"per_page": 100})
    if not ok:
        # The timeline request failed — we genuinely don't know. Report
        # unreliable so the caller keeps the issue but flags it for manual check.
        return False, False

    for event in events:
        etype = event.get("event")
        if etype in ("connected", "cross-referenced", "referenced"):
            # `connected` is always an explicit PR link. For cross-referenced /
            # referenced, confirm the source is actually a PR, not another issue.
            source = event.get("source", {}) or {}
            src_issue = source.get("issue", {}) or {}
            if etype == "connected" or "pull_request" in src_issue:
                return True, True

    return False, True                             # fetched fine, saw no PR link


# ===========================================================================
# 4. Dedupe store (seen.json)
# ===========================================================================

def load_seen():
    """Load the dedupe map {'owner/name#number': first_seen_iso}."""
    if not os.path.exists(SEEN_PATH):
        return {}
    try:
        with open(SEEN_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        print("  ! seen.json unreadable; starting fresh (may re-report issues)")
        return {}


def save_seen(seen):
    """Persist the dedupe map."""
    with open(SEEN_PATH, "w", encoding="utf-8") as f:
        json.dump(seen, f, indent=2, sort_keys=True)


def issue_key(repo, issue):
    """Stable dedupe key, e.g. 'pytorch/pytorch#12345'."""
    return f"{repo['owner']}/{repo['name']}#{issue['number']}"


# ===========================================================================
# 5. Email digest
# ===========================================================================

def build_digest(matches):
    """Return (subject, plaintext_body) for the list of new matches."""
    count = len(matches)
    subject = f"[gfi-monitor] {count} new good-first-issue match" + ("es" if count != 1 else "")

    lines = [
        f"Found {count} new issue(s) matching your filters:",
        "",
    ]
    for m in matches:
        issue = m["issue"]
        labels = ", ".join(lbl["name"] for lbl in issue.get("labels", [])) or "(none)"
        lines.append(f"• {m['repo_full']}#{issue['number']}: {issue['title']}")
        lines.append(f"    labels : {labels}")
        lines.append(f"    updated: {issue.get('updated_at', '?')}")
        lines.append(f"    url    : {issue.get('html_url', '?')}")
        if m["pr_flag"]:
            lines.append("    ⚠ verify no linked PR before starting "
                         "(automatic check was inconclusive)")
        lines.append("")

    lines.append("— gfi-monitor (read-only). This tool never writes to GitHub.")
    return subject, "\n".join(lines)


def send_email(secrets, subject, body, dry_run=False):
    """Send one digest email over Gmail SMTP-SSL. If dry_run, print instead."""
    msg = EmailMessage()
    msg["From"] = secrets["GMAIL_ADDRESS"]
    msg["To"] = secrets["NOTIFY_TO"]
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)
    msg.set_content(body)

    if dry_run:
        print("\n===== DRY RUN — email NOT sent; showing what would be sent =====")
        print(f"From: {msg['From']}\nTo: {msg['To']}\nSubject: {msg['Subject']}\n")
        print(body)
        print("===== END DRY RUN =====\n")
        return

    context = ssl.create_default_context()
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=context) as server:
        server.login(secrets["GMAIL_ADDRESS"], secrets["GMAIL_APP_PASSWORD"])
        server.send_message(msg)
    print(f"  ✓ sent digest to {secrets['NOTIFY_TO']}")


# ===========================================================================
# Main
# ===========================================================================

def process_repo(session, repo):
    """
    Run all filters for one repo and return a list of match dicts.
    Never raises: on failure it logs and returns whatever it has so a single
    bad repo can't sink the whole run.
    """
    full = f"{repo['owner']}/{repo['name']}"
    print(f"» {full}")
    matches = []

    try:
        issues = fetch_open_issues(session, repo)
    except Exception as exc:                        # noqa: BLE001 - stay alive
        print(f"  ! failed to fetch issues: {exc}")
        return matches

    print(f"  fetched {len(issues)} open non-PR issue(s)")

    for issue in issues:
        # Cheap filters first (no extra API calls).
        if not matches_labels(issue, repo.get("labels_any")):
            continue
        if repo.get("require_unassigned") and not is_unassigned(issue):
            continue
        if not within_recency(issue, repo.get("max_age_days", 0),
                               repo.get("min_age_days", 0)):
            continue

        # Expensive filter last: linked-PR check (one request per issue).
        pr_flag = False
        if repo.get("require_no_linked_pr"):
            try:
                linked, reliable = has_linked_pr(session, repo, issue)
            except Exception as exc:                # noqa: BLE001
                print(f"  ! linked-PR check failed for #{issue['number']}: {exc}")
                linked, reliable = False, False
            if linked:
                continue                            # confidently has a PR: skip
            if not reliable:
                pr_flag = True                      # inconclusive: keep but flag

        matches.append({
            "repo_full": full,
            "issue": issue,
            "pr_flag": pr_flag,
        })

    print(f"  {len(matches)} issue(s) passed all filters")
    return matches


def main():
    dry_run = "--dry-run" in sys.argv

    _defaults, repos = load_config()
    secrets = load_secrets(require_email=not dry_run)
    session = github_session(secrets["GITHUB_TOKEN"])
    seen = load_seen()

    print(f"gfi-monitor starting — watching {len(repos)} repo(s)"
          + (" [DRY RUN]" if dry_run else ""))

    all_matches = []
    for repo in repos:
        all_matches.extend(process_repo(session, repo))

    # Dedupe against seen.json.
    now_iso = datetime.now(timezone.utc).isoformat()
    new_matches = []
    for m in all_matches:
        key = issue_key({"owner": m["repo_full"].split("/")[0],
                         "name": m["repo_full"].split("/")[1]}, m["issue"])
        if key not in seen:
            new_matches.append(m)
            seen[key] = now_iso

    print(f"\n{len(all_matches)} total match(es), {len(new_matches)} new.")

    if not new_matches:
        print("Nothing new to report. (silent success — no email sent)")
        return

    subject, body = build_digest(new_matches)
    send_email(secrets, subject, body, dry_run=dry_run)

    # Only persist seen.json once the email actually went out, so a send
    # failure doesn't cause us to permanently forget to report these issues.
    # In dry-run we skip persistence so you can re-run and see the same results.
    if not dry_run:
        save_seen(seen)
        print(f"  ✓ updated seen.json ({len(seen)} total tracked)")


if __name__ == "__main__":
    main()
