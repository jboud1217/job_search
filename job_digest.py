#!/usr/bin/env python3
"""
Daily job digest.

Pulls open roles from public ATS endpoints (Greenhouse, Lever, Ashby),
filters by title + location keywords, dedupes against jobs already seen,
and emails you only the *new* matches. Companies on systems we can't query
directly (e.g. Google) are emitted as pre-filtered "manual check" links.

State lives in seen.json, which the GitHub Action keeps in the Actions cache.
No API keys needed for any of the job sources — only SMTP creds to send mail.
"""

import html
import json
import os
import re
import smtplib
import sys
from datetime import datetime, timezone
from email.mime.text import MIMEText
from pathlib import Path
from urllib.parse import quote

import requests
import yaml

import scorer

CONFIG_PATH = Path(__file__).with_name("config.yaml")
STATE_PATH = Path(__file__).with_name("seen.json")
PROFILE_PATH = Path(__file__).with_name("profile.md")
TIMEOUT = 20
HEADERS = {"User-Agent": "personal-job-digest/1.0 (+personal use)"}


# --------------------------------------------------------------------------- #
# Config + state
# --------------------------------------------------------------------------- #
def load_config():
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f)


def load_seen():
    if STATE_PATH.exists():
        try:
            return set(json.loads(STATE_PATH.read_text()))
        except json.JSONDecodeError:
            return set()
    return set()


def save_seen(seen):
    STATE_PATH.write_text(json.dumps(sorted(seen), indent=0))


def load_profile():
    if PROFILE_PATH.exists():
        return PROFILE_PATH.read_text().strip()
    return ""


# --------------------------------------------------------------------------- #
# Source fetchers — each returns a list of normalized job dicts:
#   {id, title, location, url, company}
# --------------------------------------------------------------------------- #
def fetch_greenhouse(name, src):
    token = src["token"]
    url = f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs"
    r = requests.get(url, params={"content": "true"}, headers=HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    jobs = []
    for j in r.json().get("jobs", []):
        jobs.append(
            {
                "id": f"greenhouse:{token}:{j['id']}",
                "title": j.get("title", ""),
                "location": (j.get("location") or {}).get("name", ""),
                "url": j.get("absolute_url", ""),
                "company": name,
                "description": j.get("content", ""),
            }
        )
    return jobs


def fetch_lever(name, src):
    token = src["token"]
    url = f"https://api.lever.co/v0/postings/{token}"
    r = requests.get(url, params={"mode": "json"}, headers=HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    jobs = []
    for j in r.json():
        cats = j.get("categories") or {}
        jobs.append(
            {
                "id": f"lever:{token}:{j['id']}",
                "title": j.get("text", ""),
                "location": cats.get("location", ""),
                "url": j.get("hostedUrl", ""),
                "company": name,
                "description": j.get("descriptionPlain") or j.get("description", ""),
            }
        )
    return jobs


def fetch_ashby(name, src):
    token = src["token"]
    url = f"https://api.ashbyhq.com/posting-api/job-board/{quote(token, safe='')}"
    r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    jobs = []
    for j in r.json().get("jobs", []):
        jobs.append(
            {
                "id": f"ashby:{token}:{j.get('id')}",
                "title": j.get("title", ""),
                "location": j.get("location", ""),
                "url": j.get("jobUrl") or j.get("applyUrl", ""),
                "company": name,
                "description": j.get("descriptionPlain") or j.get("descriptionHtml", ""),
            }
        )
    return jobs


# Pattern: a job card's "Learn more" anchor carries both the canonical job URL
# (with numeric id + slug) and the clean title. We then look backwards a short
# distance in the HTML to grab the location text rendered higher up the card.
_GOOGLE_LINK_RE = re.compile(
    r'href="jobs/results/(?P<id>\d+)-(?P<slug>[^?"]+)[^"]*"[^>]*'
    r'aria-label="Learn more about (?P<title>[^"]+)"'
)
_GOOGLE_LOC_RE = re.compile(r'>([A-Za-z][A-Za-z .\'-]+, (?:[A-Z]{2}, USA|[A-Z][A-Za-z]+))<')
_GOOGLE_BASE = "https://www.google.com/about/careers/applications"


def fetch_google(name, src):
    queries = src.get("queries") or [""]
    locations = src.get("locations") or [""]
    max_pages = int(src.get("max_pages", 3))
    out, seen_ids = [], set()
    for q in queries:
        for loc in locations:
            first_page_had_hits = False
            for page_num in range(1, max_pages + 1):
                params = {"page": page_num}
                if q:
                    params["q"] = q
                if loc:
                    params["location"] = loc
                url = f"{_GOOGLE_BASE}/jobs/results/"
                r = requests.get(url, params=params, headers=HEADERS, timeout=TIMEOUT)
                r.raise_for_status()
                page = r.text
                matches = list(_GOOGLE_LINK_RE.finditer(page))
                if page_num == 1:
                    # Sanity check: a generic query against Google careers
                    # should return at least one hit. Zero on page 1 usually
                    # means the page layout changed and the regex stopped
                    # matching. Empty later pages just mean we ran out.
                    if not matches:
                        raise RuntimeError(
                            f"no results parsed for query={q!r} location={loc!r} "
                            f"(page {len(page)} bytes) - parser may be stale"
                        )
                    first_page_had_hits = True
                added_this_page = 0
                for m in matches:
                    jid = m.group("id")
                    if jid in seen_ids:
                        continue
                    seen_ids.add(jid)
                    added_this_page += 1
                    title = html.unescape(m.group("title"))
                    slug = m.group("slug")
                    job_url = f"{_GOOGLE_BASE}/jobs/results/{jid}-{slug}"
                    # Card's primary displayed location: last loc-like text
                    # before the link in this chunk. Append the searched
                    # location so the post-fetch location_keywords filter
                    # still passes for roles whose card shows a different
                    # primary office.
                    preceding = page[max(0, m.start() - 8000):m.start()]
                    loc_matches = _GOOGLE_LOC_RE.findall(preceding)
                    displayed = loc_matches[-1] if loc_matches else ""
                    location_str = displayed
                    if loc:
                        location_str = f"{displayed} (also: {loc})" if displayed else loc
                    out.append(
                        {
                            "id": f"google:{jid}",
                            "title": title,
                            "location": location_str,
                            "url": job_url,
                            "company": name,
                            "description": "",
                        }
                    )
                # Stop paginating when a page yields no new IDs (Google's SPA
                # silently serves page 1 once you walk past the last real page).
                if added_this_page == 0 and first_page_had_hits:
                    break
    return out


FETCHERS = {
    "greenhouse": fetch_greenhouse,
    "lever": fetch_lever,
    "ashby": fetch_ashby,
    "google": fetch_google,
}


# --------------------------------------------------------------------------- #
# Matching
# --------------------------------------------------------------------------- #
def matches(job, title_kw, loc_kw):
    title = job["title"].lower()
    if title_kw and not any(k in title for k in title_kw):
        return False
    if loc_kw:
        loc = job["location"].lower()
        if not any(k in loc for k in loc_kw):
            return False
    return True


# --------------------------------------------------------------------------- #
# Email
# --------------------------------------------------------------------------- #
def _job_line(j, e, show_score):
    loc = f" <span style='color:#666'>&mdash; {e(j['location'])}</span>" if j.get("location") else ""
    badge = ""
    if show_score and j.get("score") is not None:
        badge = f"<b>{j['score']}</b> &middot; "
    reason = (
        f"<br><span style='color:#555;font-size:13px'>{e(j['reason'])}</span>"
        if j.get("reason")
        else ""
    )
    return (
        f"<li style='margin-bottom:8px'>{badge}"
        f"<a href='{e(j['url'])}'>{e(j['title'])}</a> "
        f"<span style='color:#888'>({e(j['company'])})</span>{loc}{reason}</li>"
    )


def _render_ranked(jobs, e):
    tiers = [
        ("apply first", "Apply first", "#0a7d28"),
        ("worth a look", "Worth a look", "#b07d00"),
        ("long shot", "Long shot", "#777"),
    ]
    buckets = {"apply first": [], "worth a look": [], "long shot": [], "unscored": []}
    for j in jobs:
        if j.get("score") is None:
            buckets["unscored"].append(j)
        else:
            buckets[scorer.tier_for(j["score"])].append(j)

    out = []
    for key, label, color in tiers:
        items = sorted(buckets[key], key=lambda x: x["score"], reverse=True)
        if items:
            out.append(
                f"<h3 style='margin-bottom:4px;color:{color}'>{label} ({len(items)})</h3>"
                "<ul style='margin-top:4px'>"
            )
            out.extend(_job_line(j, e, True) for j in items)
            out.append("</ul>")
    if buckets["unscored"]:
        out.append("<h3 style='margin-bottom:4px'>Unscored</h3><ul style='margin-top:4px'>")
        out.extend(_job_line(j, e, False) for j in buckets["unscored"])
        out.append("</ul>")
    return "".join(out)


def _render_by_company(jobs, e):
    out = []
    by_company = {}
    for j in jobs:
        by_company.setdefault(j["company"], []).append(j)
    for company in sorted(by_company):
        out.append(f"<h3 style='margin-bottom:4px'>{e(company)}</h3><ul style='margin-top:4px'>")
        out.extend(_job_line(j, e, False) for j in by_company[company])
        out.append("</ul>")
    return "".join(out)


def build_html(new_jobs, link_sources, errors, ranked):
    e = html.escape
    parts = ['<div style="font-family:system-ui,Arial,sans-serif;font-size:15px;line-height:1.45">']

    if new_jobs:
        parts.append(f"<h2>{len(new_jobs)} new matching role(s)</h2>")
        parts.append(_render_ranked(new_jobs, e) if ranked else _render_by_company(new_jobs, e))
    else:
        parts.append("<p>No new matching roles today.</p>")

    if link_sources:
        parts.append("<h3>Manual-check boards</h3><ul>")
        for s in link_sources:
            parts.append(f"<li><a href='{e(s['url'])}'>{e(s['name'])}</a></li>")
        parts.append("</ul>")

    if errors:
        parts.append("<h3 style='color:#b00'>Source errors</h3><ul>")
        for err in errors:
            parts.append(f"<li>{e(err)}</li>")
        parts.append("</ul>")

    parts.append("</div>")
    return "".join(parts)


def send_email(subject, html_body):
    host = os.environ["SMTP_HOST"]
    port = int(os.environ.get("SMTP_PORT", "465"))
    user = os.environ["SMTP_USER"]
    pwd = os.environ["SMTP_PASS"]
    to = os.environ.get("EMAIL_TO", user)

    msg = MIMEText(html_body, "html")
    msg["Subject"] = subject
    msg["From"] = user
    msg["To"] = to

    with smtplib.SMTP_SSL(host, port) as server:
        server.login(user, pwd)
        server.sendmail(user, [addr.strip() for addr in to.split(",")], msg.as_string())


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main():
    cfg = load_config()
    title_kw = [k.lower() for k in cfg.get("title_keywords", [])]
    loc_kw = [k.lower() for k in cfg.get("location_keywords", [])]
    always_send = bool(cfg.get("always_send", False))
    seen = load_seen()

    new_jobs, link_sources, errors = [], [], []

    for src in cfg.get("sources", []):
        stype = src.get("type")
        name = src.get("name", "?")

        if stype == "link":
            link_sources.append(src)
            continue

        fetcher = FETCHERS.get(stype)
        if not fetcher:
            errors.append(f"{name}: unknown source type '{stype}'")
            continue

        try:
            jobs = fetcher(name, src)
        except Exception as exc:  # noqa: BLE001 - report, don't crash the run
            errors.append(f"{name}: {exc}")
            continue

        # Google's own search query (q=) already topic-matches, so trust it
        # and skip the post-fetch title_keywords filter for those roles.
        # Without this, good fits like "Pursuit Lead" or "Practice Architect"
        # get dropped because their titles don't contain a keyword literal.
        effective_title_kw = [] if stype == "google" else title_kw
        for job in jobs:
            if job["id"] in seen:
                continue
            if matches(job, effective_title_kw, loc_kw):
                new_jobs.append(job)

    # Log summary (shows up in the Actions run log)
    print(f"Found {len(new_jobs)} new matching role(s); {len(errors)} source error(s).")

    # --- Optional AI fit ranking ------------------------------------------- #
    ranked = False
    if cfg.get("rank_with_ai", True) and new_jobs:
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        profile = load_profile()
        if not api_key:
            errors.append("AI ranking skipped: ANTHROPIC_API_KEY not set.")
        elif not profile:
            errors.append("AI ranking skipped: profile.md is empty.")
        else:
            ranked = True
            model = cfg.get("model", "claude-haiku-4-5")
            cap = int(cfg.get("max_scored_per_run", 40))
            for job in new_jobs:
                job["score"] = None
            for job in new_jobs[:cap]:
                try:
                    result = scorer.score_job(job, profile, api_key, model=model)
                    job["score"] = result["score"]
                    job["reason"] = result["reason"]
                except Exception as exc:  # noqa: BLE001 - score what we can
                    errors.append(f"score '{job['title']}': {exc}")
            # Highest-fit first (None sorts last).
            new_jobs.sort(key=lambda j: (j.get("score") is not None, j.get("score") or 0), reverse=True)

    for j in new_jobs:
        s = j.get("score")
        tag = f"[{s:>3}] " if isinstance(s, int) else "[ - ] "
        print(f"  {tag}[{j['company']}] {j['title']} — {j['location']}")

    should_send = bool(new_jobs) or bool(errors) or always_send
    if not should_send:
        print("Nothing to send.")
        return

    today = datetime.now(timezone.utc).astimezone().strftime("%a %b %d")
    if ranked:
        top = sum(1 for j in new_jobs if isinstance(j.get("score"), int) and j["score"] >= 70)
        subject = f"Job digest — {top} to apply first / {len(new_jobs)} new — {today}"
    else:
        subject = f"Job digest — {len(new_jobs)} new — {today}"
    body = build_html(new_jobs, link_sources, errors, ranked)

    try:
        send_email(subject, body)
        print("Email sent.")
    except Exception as exc:  # noqa: BLE001
        # Do NOT record these as seen, so they retry on the next run.
        print(f"ERROR sending email: {exc}", file=sys.stderr)
        sys.exit(1)

    # Only mark as seen after a successful send.
    for job in new_jobs:
        seen.add(job["id"])
    save_seen(seen)


if __name__ == "__main__":
    main()
