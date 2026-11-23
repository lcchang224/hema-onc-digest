# Daily Heme/Onc Journal Digest

Automated daily hematology/oncology journal digest. Fetches new articles from
the RSS feeds and CrossRef journals listed in
`daily_hema_onc_rss_digest.toml` (that file is the source of truth for which
journals are covered), generates AI summaries via Claude API (嘻嘻/不嘻嘻
commentary), and publishes an HTML report each morning at 8 AM Taipei time.

Nothing is emailed. The report is committed to `reports/`, a Cloudflare Worker
serves that directory at digest.lcchema.cc, and hub.lcchema.cc links to each
day's report via `manifests/latest.json`.

**Why:** Stay current on heme/onc literature without manually checking
multiple journals every day.

## Architecture
- **Repo:** git working tree only. The git directory lives outside OneDrive at
  `C:/Users/lccha/repos/hema-onc-digest-gitdir`, reached through the `.git`
  pointer file, so OneDrive never syncs git internals. Do not "fix" that file.
- **Schedule:** GitHub Actions cron `0 0 * * *` = 8 AM Taipei
- **Pipeline:** fetch → dedup (DOI-based) → AI summarize → render HTML → commit
  report back to repo. lcchema-hub picks up `manifests/latest.json` on its own;
  the repository_dispatch step that used to follow was dead (401 every run) and
  was removed on 2026-11-18.
- **Model:** `CLAUDE_MODEL` in `generate_digest.py` (Claude Sonnet 5.5 at effort
  `low` since 2026-11-18; Haiku 4.5 before). Calls go through the beta endpoint
  for the server-side refusal fallback (`fallbacks="default"`).
- **Test run:** Actions → Run workflow → tick `test_only` (or
  `gh workflow run digest.yml --ref <branch> -f test_only=true`). It ignores the
  ledger, uploads the HTML as an artifact, and commits nothing. Use it to try a
  model or prompt change on a branch before merging. On a Monday the normal
  window holds only Sunday's few articles; add `-f hours=96`.

## Fetch strategy
- **CrossRef API** for ASCO / ASH / Wiley / OUP / Elsevier (bypasses Cloudflare)
- **RSS** for Nature and NEJM (no Cloudflare issues). The Springer and BMC
  feeds were retired by their publishers and now answer HTTP 200 with an empty
  body; those three journals moved to CrossRef on 2026-08-20.
- DOI-based deduplication within a run, plus a cross-run ledger at
  `manifests/seen_dois.json` (21-day TTL) so the overlapping lookback window
  does not report the same article two days running.
- CrossRef sorts by *index* date, but publishers re-index back-catalogue in
  bulk, so a recent index date does not mean a recent article. Where the
  publisher answers `from-pub-date`, that filter is applied server-side;
  Elsevier and Lippincott titles do not, so those are paged and filtered
  locally. See the `fetch_crossref` docstring.
- The lookback window must stay above 24h. Several publishers stamp articles
  with a date only (parsed as 00:00 UTC) and GitHub Actions starts the job
  late, so a flat 24h window drops those articles permanently.

## Relevance demotion
There is still no keyword topic filter. The Claude call that writes the
summaries also returns a `heme` boolean per article, and articles it marks
false (solid-tumor oncology, general medicine, policy, news) are moved into a
collapsed `<details>` block at the bottom of the report instead of being
dropped.

Every uncertain case resolves to *keep in the main section*, on purpose:
- articles with no abstract are never sent to the model, so they have no verdict
- a failed or unparseable AI chunk leaves its 30 articles with no verdict
- `--no-ai` produces no verdicts at all
- a stringified or unexpected value is treated as keep unless it unambiguously
  reads false

So the worst failure mode is a filter that quietly does nothing, never one that
hides papers. If the collapsed block is always empty, suspect the AI reply
shape before assuming there was nothing to demote.

## Project radar
The same Claude call also returns a `radar` list per article: keys of the
topics in the TOML's `[radar] topics` that the article bears on directly. Tagged
articles are listed in a "Project radar" box at the top of the report and carry
a 🎯 badge on their card. An empty topic list turns it off.

- **The repo is public**, and so is every report committed to it. Topic labels
  and descriptions must stay at field level: no hypotheses, study designs, grant
  numbers, or anything about a patient.
- Fail-quiet like the demotion: an unknown key or a malformed field leaves the
  article untagged. Two signals keep that from going unseen: the box says "no
  hits" on an empty day instead of disappearing, and the log warns when no
  reply carried a `radar` field at all (the reply shape broke).
- Every reply must echo its article's `title_start`, and a mismatch drops the
  reply, so a tag can never land on the wrong paper (see the 2026-11-18 note in
  `_title_matches`).

## Key files
- `generate_digest.py` — main script (fetch → dedup → AI summarize → render HTML)
- `daily_hema_onc_rss_digest.toml` — journal config (which feeds, which CrossRef endpoints)
- `build_index.py` — builds `reports/index.html` and `manifests/latest.json`
  after each run
- `.github/workflows/digest.yml` — GitHub Actions workflow (the only one)
- `reports/` — HTML reports committed back to repo after each run
- `manifests/latest.json` — the 5 most recent digests, consumed by lcchema-hub
- `manifests/seen_dois.json` — cross-run dedup ledger, committed by the workflow
- `wrangler.jsonc` — Cloudflare Worker config; serves `./reports` as static
  assets at digest.lcchema.cc. No workflow deploys it, so the deploy is wired
  up on the Cloudflare side against this repo. Verify there before assuming a
  push is enough to publish.

## Secrets
GitHub secrets used by `.github/workflows/digest.yml`:
- `ANTHROPIC_API_KEY` — AI summaries
- `ELSEVIER_API_KEY` — abstract backfill fallback for Elsevier DOIs

There is no mail path. `gh secret list` on 2026-11-18 still showed `GMAIL_USER`
and `GMAIL_APP_PASSWORD` set on this repo (2026-04-29), unused by any workflow;
earlier versions of this file said no such secret existed. `HUB_DISPATCH_TOKEN`
was never set.

## Status
**DEPLOYED and working** as of 2026-05-06. Daily run at 8 AM Taipei.

Coverage was audited on 2026-08-20 after solid-tumor articles were noticed in
the digest. The solid-tumor articles turned out to be benign (there is no topic
filter by design; they come from the general-oncology journals in the config),
but the audit found three unrelated defects that were silently dropping
hematology articles: index-date churn starving the CrossRef row quota, the
sub-24h lookback window, and three retired RSS endpoints failing without a
warning. All three fixed in PR #1. Blood, Haematologica, JCO and the Nature
titles had been contributing at or near zero.

The audit measurements are not reproduced here on purpose; they age badly and
depend on which day you run them. To re-measure coverage, run
`generate_digest.py --no-ai --no-ledger --output <tmp>` and compare the
per-journal counts against the report `reports/` holds for the same date.

## Open questions / next steps
- **Project radar deployed 2026-11-23**, with the nine field-level topics in the
  TOML. Richard chose to keep the labels public (2026-11-23) over the two
  alternatives: making the repo private (breaks the hub's client-side manifest
  fetch from raw.githubusercontent.com, the path that works on hospital WiFi)
  or keeping radar hits out of the committed report. Watch the first week for
  over-tagging; the fix is a tighter `description`, not a looser rule.
- CrossRef answered `429 Too Many Requests` for 7-13 journals in every
  scheduled run from 2026-11-18 to 2026-11-23 (Blood and JCO among them), and
  each 429 dropped that journal for the day. Fixed 2026-11-23 in
  `_crossref_get`: requests are spaced and a 429/503 is retried with backoff.
  If `[WARN] CrossRef ... 429` still appears after four retries, CrossRef's
  limit has tightened further; check its current published rate limits before
  raising `CROSSREF_MIN_GAP`.
- Expect higher and lumpier daily volume now that batch deposits are no longer
  truncated (some journals deposit a whole issue at once). Watch the AI-summary
  token cost for a week or two. If it needs trimming, the lever is the journal
  list in the TOML, not a keyword filter: JTH and Transplantation and Cellular
  Therapy are the two heaviest contributors by a wide margin.
- `__pycache__/*.pyc` is tracked in git and there is no `.gitignore`. Harmless
  but noisy, and one of the tracked files is stale Python 3.10 bytecode.
- `design/` (three palette previews from 2026-05-15) is untracked, so it exists
  only in OneDrive and not in git history.
- The `heme` demotion has not been exercised against the live API. There is no
  ANTHROPIC_API_KEY on the Windows laptop, so only the render logic is tested.
  Check the collapsed block looks sane on the first real run.
- The 14-day publication-date floor is a reject filter, not a lookback window.
  It never widens what a run collects; the 28h index-date window does that.
  Articles missed while the bugs were live are gone unless a publisher
  re-indexes them.
- *(resolved 2026-08-20)* The full NEJM ISSN was dropped from the CrossRef list;
  the `NEJM Hematology-Oncology` RSS feed covers the relevant subset.
