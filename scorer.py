#!/usr/bin/env python3
"""
Fit scoring via the Anthropic Messages API.

Given a candidate profile and a job (title + location + description), asks
Claude for an honest 0-100 fit score and a one-line reason. Uses Haiku by
default — fast and cheap ($1/$5 per million tokens in/out), so scoring a
day's worth of new roles costs a fraction of a cent.

Kept dependency-light on purpose: raw HTTP via `requests`, no SDK.
"""

import html as html_mod
import json
import re

import requests

API_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"

SYSTEM_TEMPLATE = """You screen job postings for one specific candidate. Score how well each role fits THIS candidate's background, level, and goals. Be honest and discriminating: most roles are a mediocre fit, and a 90+ should be rare. Penalize clear mismatches in seniority, required domain, or location.

Candidate profile:
{profile}

Return ONLY a JSON object — no prose, no markdown code fences. Schema:
{{"score": <integer 0-100>, "reason": "<under 18 words; name the strongest fit or the biggest gap>"}}"""


def clean_text(text, limit=2500):
    """Strip HTML/entities and collapse whitespace; truncate for the prompt."""
    text = text or ""
    text = html_mod.unescape(text)          # Greenhouse content is entity-encoded
    text = re.sub(r"<[^>]+>", " ", text)    # drop tags
    text = html_mod.unescape(text)          # catch any double-encoding
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


def tier_for(score):
    if score >= 70:
        return "apply first"
    if score >= 45:
        return "worth a look"
    return "long shot"


def score_job(job, profile, api_key, model="claude-haiku-4-5", timeout=40):
    """Return {'score': int, 'reason': str}. Raises on API/parse failure."""
    user_msg = (
        f"Role: {job.get('title', '')}\n"
        f"Company: {job.get('company', '')}\n"
        f"Location: {job.get('location', '') or 'unspecified'}\n\n"
        f"Description:\n{clean_text(job.get('description', ''))}"
    )

    resp = requests.post(
        API_URL,
        timeout=timeout,
        headers={
            "x-api-key": api_key,
            "anthropic-version": ANTHROPIC_VERSION,
            "content-type": "application/json",
        },
        json={
            "model": model,
            "max_tokens": 200,
            "system": SYSTEM_TEMPLATE.format(profile=profile),
            "messages": [{"role": "user", "content": user_msg}],
        },
    )
    resp.raise_for_status()
    data = resp.json()

    text = "".join(
        b.get("text", "") for b in data.get("content", []) if b.get("type") == "text"
    ).strip()
    # tolerate accidental code fences
    text = text.removeprefix("```json").removeprefix("```").removesuffix("```").strip()

    obj = json.loads(text)
    score = max(0, min(100, int(obj["score"])))
    return {"score": score, "reason": str(obj.get("reason", "")).strip()}
