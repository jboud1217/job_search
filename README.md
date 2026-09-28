# Daily Job Digest

A tiny, dependency-light automation that emails you new openings from your
target companies every morning — **ranked by fit**. It reads public ATS
endpoints (no scraping, no API keys for the job sources), filters by title +
location, scores each new role against your profile using Claude, and sends
them grouped into *Apply first / Worth a look / Long shot* with a one-line
reason each.

Currently configured for: **Anthropic, Glean, Palantir** (queried directly) and
**Google Cloud** (search link). Edit `config.yaml` to add more.

```
job-digest/
├── job_digest.py                 # the script
├── scorer.py                     # Claude fit-scoring (Anthropic API)
├── profile.example.md            # template for profile.md (your real one is gitignored)
├── config.yaml                   # companies + title/location filters + ranking opts
├── requirements.txt
└── .github/workflows/job-digest.yml
```

## How it works

1. For each Greenhouse/Lever/Ashby source, it calls the platform's public job
   board API and normalizes the results (including the job description).
2. It keeps roles whose title matches `title_keywords` and whose location
   matches `location_keywords`.
3. If `rank_with_ai` is on, each new role is scored 0-100 against `profile.md`
   via Claude Haiku, sorted into tiers, and annotated with a one-line reason.
4. New matches (not in `seen.json`) get emailed to you, highest fit first.
   After a successful send, their IDs are written to `seen.json`, which the
   Action keeps in the Actions cache so you never get the same role twice.
5. Google (and any company on a non-queryable system) is emitted as a
   pre-filtered search link you click yourself.

If the Anthropic key or `profile.md` is missing, ranking is skipped gracefully
and you still get a plain digest grouped by company.

## Setup (~10 minutes)

### 1. Create the repo
Fork or copy this repo (keeping the `.github/workflows/` path intact).

### 2. Create a Gmail App Password
The script sends mail via Gmail SMTP. You need an App Password (not your normal
password), which requires 2-Step Verification to be on:

- Google Account → Security → 2-Step Verification (enable if needed)
- Then Security → App passwords → generate one for "Mail"
- Copy the 16-character password

### 3. Add repository secrets
Repo → Settings → Secrets and variables → Actions → **New repository secret**:

| Secret             | Value                                   |
|--------------------|-----------------------------------------|
| `SMTP_USER`        | your full Gmail address                 |
| `SMTP_PASS`        | the 16-char App Password from step 2    |
| `EMAIL_TO`         | where to send the digest (your address) |
| `ANTHROPIC_API_KEY`| your Anthropic API key (for fit ranking) |
| `PROFILE_MD`       | full contents of your `profile.md`      |

`EMAIL_TO` can be a comma-separated list if you ever want it elsewhere too.
`ANTHROPIC_API_KEY` and `PROFILE_MD` are optional — leave either out and
ranking is simply skipped.

### 3b. Edit your profile
Copy `profile.example.md` to `profile.md` and tune it to your real background,
target level, and dealbreakers. `profile.md` is gitignored; paste its contents
into the `PROFILE_MD` secret so the Action can use it. This is what every role is scored against, so specificity here
directly improves the ranking quality.

### 4. Test it
Repo → Actions tab → **Daily Job Digest** → **Run workflow**. Check the run log
for the summary line, and your inbox for the email. (Set `always_send: true`
in `config.yaml` temporarily if you want a guaranteed test email even when
there are no new roles.)

That's it — it'll then run every morning on the cron schedule.

## Tuning

- **Too few results?** Broaden `title_keywords`, or empty `location_keywords`
  to see roles everywhere and decide on location yourself.
- **Too noisy?** Tighten `title_keywords` (e.g. drop the broad `ai engineer`).
- **Add a company:** find its careers URL — `boards.greenhouse.io/SLUG`,
  `jobs.lever.co/SLUG`, or `jobs.ashbyhq.com/SLUG` — and add a source with the
  matching `type` and `token: SLUG`.
- **Change send time:** edit the `cron` line in the workflow (it's in UTC).
- **Sharper ranking:** set `model: claude-sonnet-4-6` in `config.yaml` for more
  nuanced scoring at higher cost, or refine `profile.md`.
- **Control cost:** `max_scored_per_run` caps API calls per run; Haiku scoring
  a few roles a day is fractions of a cent.

## Notes

- GitHub disables scheduled workflows after ~60 days of no repo activity;
  re-enable it from the Actions tab if that happens.
- Local test: `pip install -r requirements.txt`, export the four SMTP env vars,
  then `python job_digest.py`.
- Filtering is title-based by design (the public endpoints don't support
  full-text search), so keyword tuning is where you'll get the signal right.
