#!/usr/bin/env python3
"""
gfi-monitor. A read-only GitHub "good first issue" discovery bot.

Per run:
  1. Load config.yaml (repos and filter rules) and .env (secrets).
  2. Query the GitHub REST API for open issues in each repo.
  3. Filter by label, assignee, recency, and linked-PR rules.
  4. Dedupe against seen.json so you never get the same issue twice.
  5. Email one digest of new matches via Gmail App Password.

It only reads public data and never writes to GitHub. It runs once and exits.
The OS scheduler (Task Scheduler or cron) makes it recurring, not a loop.
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

# Issue titles can contain any Unicode. The Windows console defaults to cp1252,
# which crashes print() on emoji or CJK. Force UTF-8 and replace what it cannot
# encode.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass                                       # older Python or already wrapped

# Constants

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "config.yaml")
SEEN_PATH = os.path.join(HERE, "seen.json")
ENV_PATH = os.path.join(HERE, ".env")

GITHUB_API = "https://api.github.com"
# GitHub asks that every request identify itself with a User-Agent.
USER_AGENT = "gfi-monitor (read-only issue discovery bot)"
# Pause between API calls to stay polite.
REQUEST_DELAY_SECONDS = 0.5
# Retries for one request after a rate-limit or transient error.
MAX_RETRIES = 3
# Drop seen.json entries older than this. They can never re-trigger an email
# because the recency filter (max_age_days) already excludes stale issues, so
# pruning keeps the file bounded without causing duplicates.
SEEN_TTL_DAYS = 180

# Keys a per-repo entry can override from `defaults`.
FILTER_KEYS = (
    "labels_any",
    "require_unassigned",
    "require_no_linked_pr",
    "max_age_days",
    "min_age_days",
)


# Load config and secrets

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

    # Merge each repo's keys over the defaults so downstream code reads one flat
    # dict per repo.
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
    In --dry-run we pass False so filters can be tuned before Gmail is set up.
    """
    load_dotenv(ENV_PATH)

    secrets = {
        "GITHUB_TOKEN": os.getenv("GITHUB_TOKEN", "").strip(),
        "GMAIL_ADDRESS": os.getenv("GMAIL_ADDRESS", "").strip(),
        "GMAIL_APP_PASSWORD": os.getenv("GMAIL_APP_PASSWORD", "").strip(),
        "NOTIFY_TO": os.getenv("NOTIFY_TO", "").strip(),
    }

    # Treat the .env.example placeholders as "not set" so a half-filled .env
    # fails loudly instead of trying to log in with "replace_me".
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
    # placeholder so it is treated as absent, not sent as a bogus token.
    if secrets["GITHUB_TOKEN"] in placeholders:
        secrets["GITHUB_TOKEN"] = ""

    return secrets


# GitHub HTTP helper (auth header, delay, rate-limit handling, retries)

def github_session(token):
    """Build a requests.Session with the standard GitHub headers."""
    session = requests.Session()
    session.headers.update({
        "Accept": "application/vnd.github+json",
        "User-Agent": USER_AGENT,
        "X-GitHub-Api-Version": "2022-11-28",
    })
    if token:
        # Bearer auth raises the rate limit to 5,000 req/hr. A read-only public
        # token grants no extra access. It is only for the limit.
        session.headers["Authorization"] = f"Bearer {token}"
    return session


def github_get(session, url, params=None):
    """
    GET a GitHub URL with delay, rate-limit backoff, and retries.

    Returns the Response on success, or None if it ultimately failed so callers
    can log and continue instead of crashing the run.
    """
    for attempt in range(1, MAX_RETRIES + 1):
        time.sleep(REQUEST_DELAY_SECONDS)          # pause before every call
        try:
            resp = session.get(url, params=params, timeout=30)
        except requests.RequestException as exc:
            print(f"  ! network error ({exc}), attempt {attempt}/{MAX_RETRIES}")
            time.sleep(2 * attempt)
            continue

        # Primary rate limit is 403/429 with remaining == 0. Back off to reset.
        remaining = resp.headers.get("X-RateLimit-Remaining")
        if resp.status_code in (403, 429) and remaining == "0":
            reset = resp.headers.get("X-RateLimit-Reset")
            wait = _seconds_until_reset(reset)
            print(f"  ! rate limited, waiting {wait}s for reset "
                  f"(attempt {attempt}/{MAX_RETRIES})")
            time.sleep(wait)
            continue

        if resp.status_code == 200:
            return resp

        # 404 and similar are not worth retrying. Report and give up on this URL.
        print(f"  ! GitHub returned HTTP {resp.status_code} for {url}")
        return None

    print(f"  ! giving up on {url} after {MAX_RETRIES} attempts")
    return None


def _seconds_until_reset(reset_header):
    """Convert an X-RateLimit-Reset epoch header into a bounded sleep length."""
    try:
        reset_epoch = int(reset_header)
    except (TypeError, ValueError):
        return 60                                  # header missing or garbled
    now = int(datetime.now(timezone.utc).timestamp())
    # Add a 5s cushion. Clamp to [1, 3600] to avoid an absurdly long sleep.
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
        # The `next` URL already carries the query string, so drop our params
        # to avoid duplicating them.
        url = resp.links.get("next", {}).get("url")
        params = None


def fetch_all(session, url, params):
    """
    Like paginate() but returns (items, ok). `ok` is False if any page failed
    to fetch, so callers can tell an empty result from a request error.
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


# Query issues per repo

def fetch_repo_labels(session, repo):
    """
    Return the set of lowercased label names that exist in the repo.

    Returns None if the label list could not be fetched, so the caller can fall
    back to querying every configured label rather than skipping any.
    """
    owner, name = repo["owner"], repo["name"]
    url = f"{GITHUB_API}/repos/{owner}/{name}/labels"
    labels, ok = fetch_all(session, url, {"per_page": 100})
    if not ok:
        return None
    return {lbl["name"].lower() for lbl in labels}


def fetch_open_issues(session, repo):
    """
    Return open, non-PR issues for one repo, matching ANY of `labels_any`.

    GitHub's `labels=` param is AND-style (issue must carry ALL listed labels),
    but we want OR semantics. So we query once per label and union the results,
    deduping by issue number. This is far cheaper than fetching every open issue
    and filtering client-side. A big repo has thousands of issues but only a few
    tagged "good first issue".

    To avoid wasted requests, we first read the repo's label list and only query
    the configured labels that actually exist there. Most repos have just a few
    of our labels, so this cuts requests sharply with identical results. If the
    label list cannot be fetched we fall back to querying every label.

    If no labels are configured we fetch all open issues.
    """
    owner, name = repo["owner"], repo["name"]
    url = f"{GITHUB_API}/repos/{owner}/{name}/issues"
    # Coerce to strings. A YAML item like `- difficulty: easy` parses as a dict,
    # so guard against non-string entries rather than crashing the repo.
    labels_any = [str(lb) for lb in (repo.get("labels_any") or [])]

    if labels_any:
        existing = fetch_repo_labels(session, repo)
        if existing is not None:
            label_queries = [lb for lb in labels_any if lb.lower() in existing]
            if not label_queries:
                return []                          # none of our labels exist here
        else:
            label_queries = labels_any             # fetch failed, query them all
    else:
        label_queries = [None]                     # no filter, fetch everything

    by_number = {}                                 # dedupe issues seen via >1 label
    for label in label_queries:
        params = {"state": "open", "per_page": 100}
        if label:
            params["labels"] = label               # server-side single-label filter
        for item in paginate(session, url, params):
            # The /issues endpoint returns PRs too. A `pull_request` key marks them.
            if "pull_request" in item:
                continue
            by_number[item["number"]] = item
    return list(by_number.values())


# Filters (all driven by config)

def matches_labels(issue, labels_any):
    """True if the issue carries at least one of the wanted labels."""
    if not labels_any:
        return True                                # no label filter configured
    wanted = {str(label).lower() for label in labels_any}
    have = {lbl["name"].lower() for lbl in issue.get("labels", [])}
    return bool(wanted & have)  # non-empty intersection means at least one match


def is_unassigned(issue):
    """True if nobody is assigned to the issue."""
    return issue.get("assignee") is None and not issue.get("assignees")


def within_recency(issue, max_age_days, min_age_days):
    """
    True if the issue's `updated_at` is within [min_age_days, max_age_days].

    max_age_days skips stale issues not touched recently.
    min_age_days skips issues that are too new, to avoid racing on fresh ones.
    """
    updated = _parse_iso(issue.get("updated_at"))
    if updated is None:
        return True                                # cannot tell, so do not exclude
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
        # fromisoformat handles the '+00:00' form, so swap the trailing Z.
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def has_linked_pr(session, repo, issue):
    """
    Best-effort check of whether a PR is linked to this issue.

    The plain issue object does not expose linked PRs, so we read the issue
    timeline and look for events that connect a PR:
      - `connected`: an explicit linked pull request.
      - `cross-referenced` whose source is a PR.

    This costs one extra request per issue, so callers only run it on issues
    that already passed the cheaper filters.

    Returns (linked, reliable). If the timeline cannot be fetched it returns
    (False, False) so the caller keeps the issue but flags it as unverified
    instead of dropping a possibly good match.
    """
    owner, name = repo["owner"], repo["name"]
    number = issue["number"]
    url = f"{GITHUB_API}/repos/{owner}/{name}/issues/{number}/timeline"

    events, ok = fetch_all(session, url, {"per_page": 100})
    if not ok:
        # Fetch failed, so we do not know. Report unreliable so the caller keeps
        # the issue but flags it for a manual check.
        return False, False

    for event in events:
        etype = event.get("event")
        if etype in ("connected", "cross-referenced", "referenced"):
            # `connected` is always a PR link. For cross-referenced or
            # referenced, confirm the source is a PR and not another issue.
            source = event.get("source", {}) or {}
            src_issue = source.get("issue", {}) or {}
            if etype == "connected" or "pull_request" in src_issue:
                return True, True

    return False, True                             # fetched fine, no PR link


# Dedupe store (seen.json)

def load_seen():
    """Load the dedupe map {'owner/name#number': first_seen_iso}."""
    if not os.path.exists(SEEN_PATH):
        return {}
    try:
        with open(SEEN_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        print("  ! seen.json unreadable, starting fresh (may re-report issues)")
        return {}


def save_seen(seen):
    """Persist the dedupe map."""
    with open(SEEN_PATH, "w", encoding="utf-8") as f:
        json.dump(seen, f, indent=2, sort_keys=True)


def prune_seen(seen):
    """Drop entries older than SEEN_TTL_DAYS. Returns the number removed."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=SEEN_TTL_DAYS)
    stale = []
    for key, first_seen in seen.items():
        ts = _parse_iso(first_seen)
        # Keep entries with an unparseable timestamp to stay safe.
        if ts is not None and ts < cutoff:
            stale.append(key)
    for key in stale:
        del seen[key]
    return len(stale)


def issue_key(repo, issue):
    """Stable dedupe key, e.g. 'pytorch/pytorch#12345'."""
    return f"{repo['owner']}/{repo['name']}#{issue['number']}"


# Email digest

def rank_matches(matches):
    """
    Sort matches so the best opportunities come first.

    Order: least activity first (fewest comments), then newest (most recently
    created), then by labels alphabetically. Few comments means untouched, and
    among those the freshest issues are easiest to grab.
    """
    def sort_key(m):
        issue = m["issue"]
        comments = issue.get("comments", 0)
        created = _parse_iso(issue.get("created_at"))
        # Negate so a more recent (larger) timestamp sorts first.
        newest_first = -(created.timestamp() if created else 0)
        labels = ",".join(sorted(lbl["name"].lower()
                                  for lbl in issue.get("labels", [])))
        return (comments, newest_first, labels)

    return sorted(matches, key=sort_key)


def _age_days(iso_value):
    """Whole days since the given ISO timestamp, or None if unparseable."""
    ts = _parse_iso(iso_value)
    if ts is None:
        return None
    return (datetime.now(timezone.utc) - ts).days


def build_digest(matches):
    """Return (subject, text_body, html_body), ranked least-worked-on first."""
    matches = rank_matches(matches)
    count = len(matches)
    subject = f"[gfi-monitor] {count} new issue" + ("s" if count != 1 else "") + " to work on"

    # Plain-text version (fallback for clients that do not render HTML).
    lines = [f"{count} new issue(s), ranked least-worked-on first:", ""]
    for i, m in enumerate(matches, 1):
        issue = m["issue"]
        labels = ", ".join(lbl["name"] for lbl in issue.get("labels", [])) or "(none)"
        comments = issue.get("comments", 0)
        age = _age_days(issue.get("updated_at"))
        age_str = f"updated {age}d ago" if age is not None else "updated recently"
        lines.append(f"{i}. {m['repo_full']}#{issue['number']}: {issue['title']}")
        lines.append(f"    {comments} comment(s), {age_str}")
        lines.append(f"    labels: {labels}")
        lines.append(f"    {issue.get('html_url', '?')}")
        if m["pr_flag"]:
            lines.append("    verify no linked PR before starting "
                         "(automatic check was inconclusive)")
        lines.append("")
    lines.append("gfi-monitor (read-only). This tool never writes to GitHub.")
    text_body = "\n".join(lines)

    html_body = _build_html(matches, count)
    return subject, text_body, html_body


def _esc(text):
    """Minimal HTML escaping for issue titles and labels."""
    return (str(text).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def _build_html(matches, count):
    """Build a clean, restrained HTML digest. Ranked least-worked-on first."""
    rows = []
    for i, m in enumerate(matches, 1):
        issue = m["issue"]
        comments = issue.get("comments", 0)
        age = _age_days(issue.get("updated_at"))
        age_str = f"updated {age}d ago" if age is not None else "updated recently"
        url = _esc(issue.get("html_url", "#"))
        title = _esc(issue.get("title", "(no title)"))
        repo_ref = _esc(f"{m['repo_full']}#{issue['number']}")
        label_chips = "".join(
            f'<span style="display:inline-block;background:#eef2f7;color:#334;'
            f'border-radius:10px;padding:1px 8px;margin:2px 4px 2px 0;'
            f'font-size:12px;">{_esc(lbl["name"])}</span>'
            for lbl in issue.get("labels", [])
        )
        flag = ""
        if m["pr_flag"]:
            flag = ('<div style="color:#a15c00;font-size:12px;margin-top:4px;">'
                    "verify no linked PR before starting "
                    "(automatic check was inconclusive)</div>")
        rows.append(f"""
        <tr>
          <td style="vertical-align:top;padding:12px 8px;color:#888;
              font-size:14px;width:28px;">{i}</td>
          <td style="padding:12px 8px;border-bottom:1px solid #eee;">
            <a href="{url}" style="color:#1a56db;text-decoration:none;
               font-size:15px;font-weight:600;">{title}</a>
            <div style="color:#666;font-size:12px;margin:3px 0;">{repo_ref}
              &nbsp;&middot;&nbsp; {comments} comment(s)
              &nbsp;&middot;&nbsp; {age_str}</div>
            <div style="margin-top:4px;">{label_chips}</div>{flag}
          </td>
        </tr>""")

    return f"""<!DOCTYPE html>
<html><body style="margin:0;padding:0;background:#f6f7f9;">
  <div style="max-width:640px;margin:0 auto;padding:24px 16px;
       font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;">
    <h2 style="margin:0 0 4px;color:#111;font-size:18px;">
      {count} new issue{"s" if count != 1 else ""} to work on</h2>
    <p style="margin:0 0 16px;color:#666;font-size:13px;">
      Ranked least-worked-on first (fewest comments, then newest).</p>
    <table style="width:100%;border-collapse:collapse;background:#fff;
        border:1px solid #eee;border-radius:8px;">{"".join(rows)}
    </table>
    <p style="margin:16px 0 0;color:#999;font-size:11px;">
      gfi-monitor (read-only). This tool never writes to GitHub.</p>
  </div>
</body></html>"""


def send_email(secrets, subject, text_body, html_body=None, dry_run=False):
    """Send one digest email over Gmail SMTP-SSL. If dry_run, print instead."""
    msg = EmailMessage()
    msg["From"] = secrets["GMAIL_ADDRESS"]
    msg["To"] = secrets["NOTIFY_TO"]
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)
    msg.set_content(text_body)
    if html_body:
        # HTML alternative. Clients that cannot render it show the plain text.
        msg.add_alternative(html_body, subtype="html")

    if dry_run:
        print("\n===== DRY RUN, email not sent, showing what would be sent =====")
        print(f"From: {msg['From']}\nTo: {msg['To']}\nSubject: {msg['Subject']}\n")
        print(text_body)
        print("===== END DRY RUN =====\n")
        return

    context = ssl.create_default_context()
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=context) as server:
        server.login(secrets["GMAIL_ADDRESS"], secrets["GMAIL_APP_PASSWORD"])
        server.send_message(msg)
    print(f"  sent digest to {secrets['NOTIFY_TO']}")


# Main

def process_repo(session, repo):
    """
    Run all filters for one repo and return a list of match dicts.
    Never raises. On failure it logs and returns what it has so one bad repo
    cannot sink the whole run.
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
                continue                            # has a PR, skip
            if not reliable:
                pr_flag = True                      # inconclusive, keep but flag

        matches.append({
            "repo_full": full,
            "issue": issue,
            "pr_flag": pr_flag,
        })

    print(f"  {len(matches)} issue(s) passed all filters")
    return matches


def main():
    dry_run = "--dry-run" in sys.argv
    # --seed records current matches into seen.json without emailing. Use it
    # once on first setup so the first real run only emails genuinely new issues.
    seed = "--seed" in sys.argv

    # --test-email sends one fixed message and exits, to confirm SMTP works.
    if "--test-email" in sys.argv:
        secrets = load_secrets(require_email=True)
        send_email(
            secrets,
            "[gfi-monitor] test email",
            "This is a test from gfi-monitor. If you got this, sending works.",
            html_body=None,
            dry_run=False,
        )
        return

    _defaults, repos = load_config()
    # Seed and dry-run do not send email, so their secrets are optional.
    secrets = load_secrets(require_email=not dry_run and not seed)
    session = github_session(secrets["GITHUB_TOKEN"])
    seen = load_seen()

    # Drop stale entries first so the file stays bounded.
    removed = prune_seen(seen)
    if removed:
        print(f"pruned {removed} seen.json entr(ies) older than {SEEN_TTL_DAYS} days")

    print(f"gfi-monitor starting, watching {len(repos)} repo(s)"
          + (" [DRY RUN]" if dry_run else "")
          + (" [SEED]" if seed else ""))

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

    # Seed mode records everything as seen and exits without emailing.
    if seed:
        save_seen(seen)
        print(f"  seeded seen.json with {len(seen)} issue(s), no email sent")
        return

    if not new_matches:
        # Still save so pruning and any nothing-new bookkeeping persists.
        if not dry_run and removed:
            save_seen(seen)
        print("Nothing new to report. Silent success, no email sent.")
        return

    subject, text_body, html_body = build_digest(new_matches)
    send_email(secrets, subject, text_body, html_body=html_body, dry_run=dry_run)

    # Persist seen.json only after the email went out, so a send failure does
    # not make us permanently forget to report these issues. Dry-run skips
    # persistence so repeated runs show the same results.
    if not dry_run:
        save_seen(seen)
        print(f"  updated seen.json ({len(seen)} total tracked)")


if __name__ == "__main__":
    main()
