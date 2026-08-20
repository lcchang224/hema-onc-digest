# Daily Heme/Onc Journal Digest

Automated daily hematology/oncology journal digest. Fetches new articles from
10 RSS feeds + 18 CrossRef journals, generates AI summaries via Claude API
(嘻嘻/不嘻嘻 commentary), and emails an HTML report to lcchang224@gmail.com
each morning at 8 AM Taipei time.

**Why:** Stay current on heme/onc literature without manually checking
multiple journals every day.

## Architecture
- **Repo:** this folder is a git repo (deployed via GitHub Actions)
- **Schedule:** GitHub Actions cron `0 0 * * *` = 8 AM Taipei
- **Pipeline:** fetch → dedup (DOI-based) → AI summarize → render HTML → email → commit report back to repo

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

## Key files
- `generate_digest.py` — main script (fetch → dedup → AI summarize → render HTML → email)
- `daily_hema_onc_rss_digest.toml` — journal config (which feeds, which CrossRef endpoints)
- `.github/workflows/digest.yml` — GitHub Actions workflow
- `reports/` — HTML reports committed back to repo after each run

## Email & secrets
`smtplib` via Gmail SMTP. Requires GitHub secrets:
- `GMAIL_USER`
- `GMAIL_APP_PASSWORD`
- `ANTHROPIC_API_KEY`

## Status
**DEPLOYED and working** as of 2026-05-06. Daily run at 8 AM Taipei.

Coverage was audited on 2026-08-20 after solid-tumor articles were noticed in
the digest. The solid-tumor articles turned out to be benign (there is no topic
filter by design; they come from the general-oncology journals in the config),
but the audit found three unrelated defects that were silently dropping
hematology articles. All three are fixed on branch `fix/coverage-gaps`:
index-date churn starving the CrossRef row quota, the sub-24h lookback window,
and three retired RSS endpoints failing without a warning. Before/after on the
same window: 17 articles from 7 journals -> 137 from 21. Blood, Haematologica,
JCO, NEJM and the Nature titles had been contributing at or near zero.

## Open questions / next steps
- Expect higher and lumpier daily volume now that batch deposits are no longer
  truncated (some journals deposit a whole issue at once). Watch the AI-summary
  token cost for a week or two.
- *(resolved 2026-08-20)* The full NEJM ISSN was dropped from the CrossRef list;
  the `NEJM Hematology-Oncology` RSS feed covers the relevant subset.
