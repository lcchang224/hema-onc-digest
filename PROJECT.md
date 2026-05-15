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
- **RSS** for Nature / Springer / PubMed (no Cloudflare issues)
- DOI-based deduplication prevents duplicates across sources

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

## Open questions / next steps
- *(none currently — running steady)*
