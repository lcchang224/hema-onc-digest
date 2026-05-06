#!/usr/bin/env python3
"""
generate_digest.py
==================
Fetches hematology/oncology journal articles from RSS (Nature/Springer)
and CrossRef API, filters to the past 28 hours, optionally summarises
each article via Claude API, renders a mobile-friendly responsive HTML
report, and optionally sends it by email.

Requirements:
    pip install httpx feedparser anthropic tomli   # tomli only if Python < 3.11

Usage:
    python generate_digest.py                      # normal daily run
    python generate_digest.py --demo               # 72-hour window for demo
    python generate_digest.py --no-ai              # skip Claude summaries
    python generate_digest.py --hours 48           # custom lookback window
    python generate_digest.py --output ./reports   # custom output folder
    python generate_digest.py --config path/to.toml
    python generate_digest.py --email-to you@example.com   # send HTML email

Email env vars (required when --email-to is set):
    GMAIL_USER          sender Gmail address
    GMAIL_APP_PASSWORD  16-char Google App Password (not your login password)
"""

import argparse
import json
import os
import re
import smtplib
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

# ── TOML loading (stdlib 3.11+, else tomli) ─────────────────────────────────
try:
    import tomllib
except ImportError:
    try:
        import tomli as tomllib  # type: ignore
    except ImportError:
        sys.exit("Python < 3.11 detected and tomli is not installed.\n"
                 "Run: pip install tomli")

# ── HTTP ─────────────────────────────────────────────────────────────────────
try:
    import httpx
except ImportError:
    sys.exit("httpx is not installed.\nRun: pip install httpx")

# ── RSS parsing ──────────────────────────────────────────────────────────────
try:
    import feedparser  # type: ignore
except ImportError:
    sys.exit("feedparser is not installed.\nRun: pip install feedparser")



# ═════════════════════════════════════════════════════════════════════════════
# Constants
# ═════════════════════════════════════════════════════════════════════════════

_SCRIPT_DIR     = Path(__file__).parent
DEFAULT_CONFIG  = _SCRIPT_DIR / "daily_hema_onc_rss_digest.toml"
DEFAULT_OUTPUT  = _SCRIPT_DIR / "reports"
DEFAULT_HOURS   = 28
DEMO_HOURS      = 72          # wider window so demo has plenty of articles
CROSSREF_BASE   = "https://api.crossref.org/works"
RSS_TIMEOUT     = 20
CROSSREF_TIMEOUT= 30
CLAUDE_MODEL    = "claude-haiku-4-5-20251001"
CHUNK_SIZE      = 30                        # articles per Claude call


# ═════════════════════════════════════════════════════════════════════════════
# Fetch helpers
# ═════════════════════════════════════════════════════════════════════════════

_RSS_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "application/rss+xml, application/xml;q=0.9, */*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


def fetch_rss_feed(feed: dict, cutoff: datetime) -> list[dict]:
    """Fetch one RSS feed; return articles indexed after *cutoff*."""
    url, name = feed["url"], feed["name"]
    try:
        with httpx.Client(headers=_RSS_HEADERS, follow_redirects=True,
                          timeout=RSS_TIMEOUT) as client:
            resp = client.get(url)
            resp.raise_for_status()
        parsed = feedparser.parse(resp.text)
    except Exception as exc:
        print(f"  [WARN] RSS '{name}': {exc}", file=sys.stderr)
        return []

    articles = []
    for entry in parsed.entries:
        pub = _entry_datetime(entry)
        if pub < cutoff:
            continue
        articles.append({
            "journal":   name,
            "title":     getattr(entry, "title", "(no title)"),
            "authors":   _rss_authors(entry),
            "abstract":  _clean_html(getattr(entry, "summary", ""))[:600],
            "url":       getattr(entry, "link", ""),
            "doi":       _extract_doi(entry),
            "published": pub.strftime("%Y-%m-%d"),
            "source":    "rss",
        })
    return articles


def fetch_crossref(journal: dict, cutoff: datetime,
                   rows: int, email: str) -> list[dict]:
    """Fetch recent works from CrossRef by ISSN; filter to after *cutoff*."""
    from_date = (cutoff - timedelta(hours=1)).strftime("%Y-%m-%d")
    params = {
        "filter":  f"issn:{journal['issn']},from-index-date:{from_date}",
        "rows":    rows,
        "sort":    "indexed",
        "order":   "desc",
        "mailto":  email,
        "select":  "DOI,title,author,published,abstract,URL,indexed,type",
    }
    try:
        with httpx.Client(timeout=CROSSREF_TIMEOUT) as client:
            resp = client.get(CROSSREF_BASE, params=params)
            resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        print(f"  [WARN] CrossRef '{journal['name']}': {exc}", file=sys.stderr)
        return []

    # CrossRef re-indexes old articles when publishers update metadata.
    # 30-day floor blocks decade-old re-indexed articles while still allowing
    # genuinely recent papers. For partial dates (year+month only) we use the
    # last day of that month so April articles aren't wrongly excluded.
    pub_date_floor = cutoff - timedelta(days=14)

    articles = []
    for item in data.get("message", {}).get("items", []):
        # ── Indexed-date filter ───────────────────────────────────────────────
        idx_str = item.get("indexed", {}).get("date-time", "")
        try:
            indexed_dt = datetime.fromisoformat(idx_str.replace("Z", "+00:00"))
        except Exception:
            indexed_dt = cutoff  # unknown → treat as borderline, let pub-date decide
        if indexed_dt < cutoff:
            continue

        # ── Type filter (skip journal-issue, book-chapter, etc.) ──────────────
        if item.get("type", "journal-article") != "journal-article":
            continue

        # ── Publication-date filter (guards against re-indexed legacy articles) ─
        dp = item.get("published", {}).get("date-parts", [[]])
        pub_str = ""
        if dp and dp[0]:
            parts_list = dp[0]
            year  = parts_list[0] if len(parts_list) > 0 else None
            month = parts_list[1] if len(parts_list) > 1 else 1
            day   = parts_list[2] if len(parts_list) > 2 else 1
            if year:
                try:
                    if len(parts_list) >= 3:
                        pub_dt = datetime(year, month, day, tzinfo=timezone.utc)
                    else:
                        # Year+month only: assume end of that month so current-month
                        # articles aren't wrongly excluded by the floor check.
                        next_m = month % 12 + 1
                        next_y = year + (1 if month == 12 else 0)
                        pub_dt = datetime(next_y, next_m, 1, tzinfo=timezone.utc) - timedelta(days=1)
                    if pub_dt < pub_date_floor:
                        continue
                except Exception:
                    pass
            pub_str = "-".join(str(p).zfill(2) for p in parts_list)
        if not pub_str:
            pub_str = indexed_dt.strftime("%Y-%m-%d")

        title_list = item.get("title", [])
        title = _clean_html(title_list[0]) if title_list else ""
        if not title:
            continue   # issue/volume entries have no title

        authors_raw = item.get("author", [])
        author_parts = [
            f"{a.get('family', '')} {(a.get('given') or '')[:1]}".strip()
            for a in authors_raw[:4]
        ]
        authors = ", ".join(p for p in author_parts if p)
        if len(authors_raw) > 4:
            authors += " et al."

        abstract = _clean_html(item.get("abstract", ""))[:600]
        doi = item.get("DOI", "")
        url = item.get("URL", f"https://doi.org/{doi}" if doi else "")

        articles.append({
            "journal":   journal["name"],
            "title":     title,
            "authors":   authors,
            "abstract":  abstract,
            "url":       url,
            "doi":       doi,
            "published": pub_str,
            "source":    "crossref",
        })
    return articles


# ═════════════════════════════════════════════════════════════════════════════
# AI summarisation
# ═════════════════════════════════════════════════════════════════════════════

_SUMMARY_SYSTEM = """\
You are a witty hematology/oncology fellow summarising papers for your colleagues.

For EVERY article in the list, produce:
  1. A 1-2 sentence English plain-text summary of the key finding.
  2. 「嘻嘻」— a warm, enthusiastic comment in Traditional Chinese (1 sentence, fun and encouraging).
  3. 「不嘻嘻」— a playfully sarcastic or teasing comment in Traditional Chinese (1 sentence, humorous, not mean).

Reply with a valid JSON array — one object per article in the same order:
  [{"idx": <int>, "summary": "...", "hehe": "...", "nohehe": "..."}, ...]

Rules:
- Cover EVERY article; do not skip any.
- No markdown code fences.
- Strictly valid JSON."""


def ai_summarize(articles: list[dict], api_key: str) -> dict[int, dict]:
    """Return {global_idx: {summary, hehe, nohehe}} using Anthropic Claude Haiku.
    Articles without abstracts are skipped entirely."""
    try:
        import anthropic  # type: ignore
    except ImportError:
        print("  [WARN] anthropic package not installed — skipping AI summaries.\n"
              "         Run: pip install anthropic", file=sys.stderr)
        return {}

    client = anthropic.Anthropic(api_key=api_key)
    results: dict[int, dict] = {}

    # Only articles with abstracts get summarised
    indexed = [(gidx, a) for gidx, a in enumerate(articles) if a.get("abstract")]
    chunks = [indexed[i:i + CHUNK_SIZE] for i in range(0, len(indexed), CHUNK_SIZE)]

    for chunk_no, chunk in enumerate(chunks):
        lines = []
        for gidx, a in chunk:
            lines.append(f"[{gidx}] {a['journal']} — {a['title']}")
            lines.append(f"    Abstract: {a['abstract'][:350]}")
        for attempt in range(3):
            try:
                resp = client.messages.create(
                    model=CLAUDE_MODEL,
                    max_tokens=8192,
                    system=_SUMMARY_SYSTEM,
                    messages=[{"role": "user", "content": "\n".join(lines)}],
                )
                raw = resp.content[0].text.strip()
                raw = re.sub(r"^```[a-z]*\n?", "", raw)
                raw = re.sub(r"\n?```$", "", raw)
                parsed = json.loads(raw)
                for entry in parsed:
                    results[entry["idx"]] = entry
                break
            except Exception as exc:
                if "429" in str(exc) or "overloaded" in str(exc).lower() or "rate" in str(exc).lower():
                    wait = 60 * (attempt + 1)
                    print(f"  [WARN] Rate limited (chunk {chunk_no}), retrying in {wait}s…",
                          file=sys.stderr)
                    time.sleep(wait)
                else:
                    print(f"  [WARN] AI summary chunk {chunk_no}: {exc}", file=sys.stderr)
                    break
        time.sleep(2)

    return results


# ═════════════════════════════════════════════════════════════════════════════
# HTML rendering
# ═════════════════════════════════════════════════════════════════════════════

_CSS = """
/* ── Reset ──────────────────────────────────── */
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

/* ── Design tokens ──────────────────────────── */
:root {
  --bg: #f4f6fb;
  --surface: #ffffff;
  --border: #dde2ec;
  --accent: #1a56db;
  --accent2: #2563eb;
  --accent-light: #eef2ff;
  --text: #111827;
  --muted: #6b7280;
  --hehe-bg: #ecfdf5;
  --hehe-bdr: #059669;
  --nohehe-bg: #fff7ed;
  --nohehe-bdr: #d97706;
  --tag: #e9edf7;
  --shadow-sm: 0 1px 3px rgba(0,0,0,.07);
  --radius: 10px;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #0d1017;
    --surface: #161c2c;
    --border: #252e44;
    --accent: #6ea8fe;
    --accent2: #5b8dee;
    --accent-light: #0f1a30;
    --text: #e2e8f0;
    --muted: #8b95a8;
    --hehe-bg: #03260f;
    --hehe-bdr: #16a34a;
    --nohehe-bg: #1a0e02;
    --nohehe-bdr: #ca8a04;
    --tag: #1e2637;
    --shadow-sm: 0 1px 3px rgba(0,0,0,.3);
  }
}

/* ── Base ───────────────────────────────────── */
html { scroll-behavior: smooth; }
body {
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto,
               Helvetica, Arial, sans-serif;
  background: var(--bg);
  color: var(--text);
  line-height: 1.65;
  font-size: 16px;
}
a { color: var(--accent); text-decoration: none; }
a:hover { text-decoration: underline; }

/* ── Header ─────────────────────────────────── */
.site-header {
  background: linear-gradient(135deg, #1a56db 0%, #2563eb 100%);
  color: #fff;
  padding: 1.1rem 1.5rem;
  position: sticky; top: 0; z-index: 100;
  display: flex; align-items: center; gap: 1rem;
  box-shadow: 0 2px 8px rgba(26,86,219,.35);
}
.site-header h1 {
  font-size: 1.05rem; font-weight: 700; flex: 1;
  letter-spacing: -.01em;
}
.header-meta { font-size: .78rem; opacity: .88; white-space: nowrap; }

/* ── Stats bar ──────────────────────────────── */
.stats-bar {
  background: var(--accent-light);
  border-bottom: 1px solid var(--border);
  padding: .55rem 1.5rem;
  display: flex; gap: 1.25rem; flex-wrap: wrap;
  font-size: .82rem; color: var(--accent2);
  font-weight: 500;
}
.stats-bar b { font-weight: 700; }

/* ── Layout ─────────────────────────────────── */
.container {
  max-width: 900px; margin: 0 auto;
  padding: 1.5rem 1rem 3rem;
}

/* ── TOC / journal nav ──────────────────────── */
.toc {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: 1rem 1.25rem;
  margin-bottom: 1.75rem;
  box-shadow: var(--shadow-sm);
}
.toc-title {
  font-weight: 700; font-size: .82rem; text-transform: uppercase;
  letter-spacing: .07em; color: var(--muted);
  margin-bottom: .6rem;
}
.toc-links {
  display: flex; flex-wrap: wrap; gap: .4rem;
}
.toc-links a {
  background: var(--tag);
  color: var(--text);
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: .22rem .65rem;
  font-size: .78rem;
  font-weight: 500;
  transition: background .15s;
}
.toc-links a:hover {
  background: var(--accent-light);
  color: var(--accent2);
  text-decoration: none;
  border-color: var(--accent2);
}

/* ── Journal section ────────────────────────── */
.journal-section { margin-bottom: 2rem; }
.journal-header {
  display: flex; align-items: center; gap: .65rem;
  padding: .65rem .9rem;
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--radius) var(--radius) 0 0;
  border-bottom: 2px solid var(--accent);
}
.journal-icon { font-size: 1.15rem; line-height: 1; }
.journal-title { font-weight: 700; font-size: .96rem; }
.journal-count {
  margin-left: auto;
  background: var(--accent);
  color: #fff;
  font-size: .7rem; font-weight: 700;
  padding: .15rem .5rem;
  border-radius: 999px;
  min-width: 1.4rem; text-align: center;
}

/* ── Article card ───────────────────────────── */
.article-card {
  background: var(--surface);
  border: 1px solid var(--border);
  border-top: none;
  padding: .95rem 1rem .9rem;
  transition: box-shadow .15s;
}
.article-card:last-child { border-radius: 0 0 var(--radius) var(--radius); }
.article-card:hover { box-shadow: var(--shadow-sm); }
.article-card + .article-card { border-top: 1px solid var(--border); }

.article-meta {
  display: flex; flex-wrap: wrap; gap: .35rem;
  font-size: .72rem; color: var(--muted);
  margin-bottom: .35rem;
}
.badge {
  background: var(--tag);
  border: 1px solid var(--border);
  border-radius: 4px;
  padding: .08rem .38rem;
  white-space: nowrap;
}

.article-title {
  font-weight: 600; font-size: .95rem;
  line-height: 1.45; margin-bottom: .25rem;
}
.article-title a { color: var(--text); }
.article-title a:hover { color: var(--accent); }

.article-authors {
  font-size: .8rem; color: var(--muted);
  margin-bottom: .55rem;
}

/* ── Summary block ──────────────────────────── */
.article-summary {
  font-size: .84rem; color: var(--muted);
  border-left: 2.5px solid var(--border);
  padding-left: .6rem;
  margin: .45rem 0 .65rem;
  line-height: 1.5;
}

/* ── 嘻嘻 / 不嘻嘻 ─────────────────────────── */
.ai-box {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: .5rem;
  margin-top: .6rem;
}
@media (max-width: 540px) { .ai-box { grid-template-columns: 1fr; } }

.comment-card {
  border-radius: 7px;
  padding: .55rem .7rem;
  font-size: .81rem; line-height: 1.5;
}
.comment-card.hehe {
  background: var(--hehe-bg);
  border-left: 3px solid var(--hehe-bdr);
}
.comment-card.nohehe {
  background: var(--nohehe-bg);
  border-left: 3px solid var(--nohehe-bdr);
}
.comment-label {
  display: block; font-weight: 700; font-size: .72rem;
  margin-bottom: .18rem; letter-spacing: .03em;
}
.comment-card.hehe .comment-label { color: var(--hehe-bdr); }
.comment-card.nohehe .comment-label { color: var(--nohehe-bdr); }

/* ── Empty state ────────────────────────────── */
.empty-state {
  text-align: center; padding: 4rem 1rem;
  color: var(--muted); font-size: 1rem;
}

/* ── Footer ─────────────────────────────────── */
.site-footer {
  text-align: center;
  padding: 1.5rem 1rem;
  font-size: .76rem; color: var(--muted);
  border-top: 1px solid var(--border);
  margin-top: 2rem;
}

/* ── Responsive tweaks ──────────────────────── */
@media (max-width: 600px) {
  .site-header { padding: .85rem 1rem; }
  .site-header h1 { font-size: .97rem; }
  .stats-bar { padding: .5rem 1rem; gap: .85rem; }
  .container { padding: 1rem .75rem 2.5rem; }
  .article-card { padding: .8rem .75rem; }
  .journal-header { padding: .55rem .75rem; }
}
"""


def _e(s: str) -> str:
    """Minimal HTML-escape."""
    return (
        (s or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def render_html(articles: list[dict], summaries: dict[int, dict],
                run_at: datetime, hours: int) -> str:
    by_journal: dict[str, list] = defaultdict(list)
    for i, a in enumerate(articles):
        by_journal[a["journal"]].append((i, a))

    total = len(articles)
    n_journals = len(by_journal)
    has_ai = bool(summaries)
    run_str = run_at.strftime("%Y-%m-%d %H:%M UTC")

    # ── TOC links ──────────────────────────────────────────────────────────
    toc_links = "".join(
        f'<a href="#j-{_slug(j)}">{_e(j)} <span style="opacity:.6">({len(items)})</span></a>'
        for j, items in sorted(by_journal.items())
    )
    toc_html = f"""
<div class="toc">
  <div class="toc-title">📚 Journals in this digest</div>
  <div class="toc-links">{toc_links}</div>
</div>""" if toc_links else ""

    # ── Journal sections ────────────────────────────────────────────────────
    sections = []
    for journal, items in sorted(by_journal.items()):
        cards = []
        for i, a in items:
            s = summaries.get(i, {})

            pub_badge = (
                f'<span class="badge">📅 {_e(a["published"][:10])}</span>'
                if a.get("published") else ""
            )
            doi_badge = (
                f'<span class="badge">DOI: {_e(a["doi"][:22])}</span>'
                if a.get("doi") else ""
            )
            src_badge = (
                '<span class="badge">RSS</span>'
                if a["source"] == "rss"
                else '<span class="badge">CrossRef</span>'
            )

            authors_html = (
                f'<p class="article-authors">{_e(a["authors"])}</p>'
                if a.get("authors") else ""
            )

            if s.get("summary"):
                summary_html = f'<p class="article-summary">{_e(s["summary"])}</p>'
            elif not a.get("abstract"):
                summary_html = ('<p class="article-summary" style="font-style:italic;opacity:.55">'
                                'No abstract available — AI summary skipped.</p>')
            else:
                summary_html = ""

            ai_html = ""
            if s.get("hehe") or s.get("nohehe"):
                hh = (f'<div class="comment-card hehe">'
                      f'<span class="comment-label">嘻嘻 😄</span>{_e(s["hehe"])}</div>'
                      if s.get("hehe") else "")
                nh = (f'<div class="comment-card nohehe">'
                      f'<span class="comment-label">不嘻嘻 🙄</span>{_e(s["nohehe"])}</div>'
                      if s.get("nohehe") else "")
                ai_html = f'<div class="ai-box">{hh}{nh}</div>'

            url = a.get("url") or (f"https://doi.org/{a['doi']}" if a.get("doi") else "#")

            cards.append(f"""
<div class="article-card">
  <div class="article-meta">{pub_badge}{doi_badge}{src_badge}</div>
  <div class="article-title">
    <a href="{_e(url)}" target="_blank" rel="noopener">{_e(a["title"])}</a>
  </div>
  {authors_html}
  {summary_html}
  {ai_html}
</div>""")

        sections.append(f"""
<div class="journal-section" id="j-{_slug(journal)}">
  <div class="journal-header">
    <span class="journal-icon">📋</span>
    <span class="journal-title">{_e(journal)}</span>
    <span class="journal-count">{len(items)}</span>
  </div>
  {"".join(cards)}
</div>""")

    content = (
        toc_html + "\n".join(sections)
        if sections
        else '<div class="empty-state">🔍 No new articles found in the past '
             + str(hours) + ' hours.</div>'
    )

    ai_note = " · AI summaries by Claude Haiku" if has_ai else " · (no AI summaries)"

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <meta name="color-scheme" content="light dark">
  <title>Heme/Onc Digest · {run_at.strftime('%Y-%m-%d')}</title>
  <style>{_CSS}</style>
</head>
<body>
<header class="site-header">
  <h1>🩸 Hematology &amp; Oncology Daily Digest</h1>
  <span class="header-meta">{run_str}</span>
</header>

<div class="stats-bar">
  <span>📰 <b>{total}</b> articles</span>
  <span>📚 <b>{n_journals}</b> journals</span>
  <span>⏱ Past <b>{hours}h</b></span>
  {"<span>🤖 AI-summarized</span>" if has_ai else ""}
</div>

<main class="container">
{content}
</main>

<footer class="site-footer">
  Generated {run_str}{ai_note}<br>
  Sources: Nature RSS · Springer RSS · CrossRef API
</footer>
</body>
</html>"""


# ═════════════════════════════════════════════════════════════════════════════
# Helpers
# ═════════════════════════════════════════════════════════════════════════════

def _entry_datetime(entry) -> datetime:
    for field in ("published_parsed", "updated_parsed", "created_parsed"):
        t = getattr(entry, field, None)
        if t:
            try:
                return datetime(*t[:6], tzinfo=timezone.utc)
            except Exception:
                pass
    return datetime.now(timezone.utc)  # unknown → include


def _rss_authors(entry) -> str:
    names = []
    for a in getattr(entry, "authors", [])[:4]:
        n = getattr(a, "name", "")
        if n:
            names.append(n)
    if not names and hasattr(entry, "author"):
        names = [entry.author]
    if len(getattr(entry, "authors", [])) > 4:
        names.append("et al.")
    return ", ".join(names)


def _clean_html(text: str) -> str:
    if not text:
        return ""
    text = re.sub(r"<[^>]+>", "", text)
    for old, new in [("&nbsp;", " "), ("&amp;", "&"), ("&lt;", "<"),
                     ("&gt;", ">"), ("&quot;", '"'), ("&#039;", "'"),
                     ("&#8203;", "")]:
        text = text.replace(old, new)
    return re.sub(r"\s+", " ", text).strip()


def _extract_doi(entry) -> str:
    for tag in getattr(entry, "tags", []):
        t = getattr(tag, "term", "")
        if t.startswith("10."):
            return t
    for src in (getattr(entry, "link", ""), getattr(entry, "id", "")):
        m = re.search(r"10\.\d{4,}/[^\s\"'<>]+", src)
        if m:
            return m.group(0).rstrip(".")
    return ""


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


# ═════════════════════════════════════════════════════════════════════════════
# Email delivery
# ═════════════════════════════════════════════════════════════════════════════

def send_html_email(html: str, subject: str, to_addr: str,
                    smtp_user: str, smtp_pass: str) -> None:
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = smtp_user
    msg["To"]      = to_addr
    msg.attach(MIMEText(html, "html", "utf-8"))
    with smtplib.SMTP("smtp.gmail.com", 587) as server:
        server.starttls()
        server.login(smtp_user, smtp_pass)
        server.sendmail(smtp_user, to_addr, msg.as_string())


# ═════════════════════════════════════════════════════════════════════════════
# Entry point
# ═════════════════════════════════════════════════════════════════════════════

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Daily Heme/Onc RSS + CrossRef digest generator"
    )
    parser.add_argument("--config",  default=str(DEFAULT_CONFIG),
                        help=f"TOML config path (default: {DEFAULT_CONFIG})")
    parser.add_argument("--output",  default=str(DEFAULT_OUTPUT),
                        help=f"Output directory (default: {DEFAULT_OUTPUT})")
    parser.add_argument("--hours",   type=int, default=DEFAULT_HOURS,
                        help=f"Lookback window in hours (default: {DEFAULT_HOURS})")
    parser.add_argument("--no-ai",   action="store_true",
                        help="Skip Claude AI summaries")
    parser.add_argument("--demo",    action="store_true",
                        help=f"Use {DEMO_HOURS}h window to ensure articles for demo")
    parser.add_argument("--email-to", default="",
                        help="Send the HTML digest to this email address via Gmail SMTP. "
                             "Requires GMAIL_USER and GMAIL_APP_PASSWORD env vars.")
    args = parser.parse_args()

    # ── Load config ──────────────────────────────────────────────────────────
    cfg_path = Path(args.config)
    if not cfg_path.exists():
        sys.exit(f"Config not found: {cfg_path}")
    with open(cfg_path, "rb") as fh:
        cfg = tomllib.load(fh)

    # Honour TOML's filter_window_hours if --hours wasn't explicitly passed
    toml_hours = (cfg.get("rss", {})
                     .get("content_rules", {})
                     .get("filter_window_hours", DEFAULT_HOURS))
    hours = DEMO_HOURS if args.demo else (args.hours if args.hours != DEFAULT_HOURS else toml_hours)
    run_at = datetime.now(timezone.utc)
    cutoff = run_at - timedelta(hours=hours)
    print(f"\n{'='*60}")
    print(f"  Heme/Onc Daily Digest")
    print(f"  Run at : {run_at.strftime('%Y-%m-%d %H:%M:%S')} UTC")
    print(f"  Window : past {hours} hours  (cutoff: {cutoff.strftime('%Y-%m-%dT%H:%M')} UTC)")
    print(f"{'='*60}\n")

    # ── RSS feeds ─────────────────────────────────────────────────────────────
    rss_feeds = cfg.get("rss", {}).get("feeds", [])
    all_articles: list[dict] = []

    print(f"── RSS feeds ({len(rss_feeds)}) ──────────────────────────────────")
    for feed in rss_feeds:
        arts = fetch_rss_feed(feed, cutoff)
        print(f"  {feed['name']}: {len(arts)}")
        all_articles.extend(arts)
        time.sleep(0.3)

    # ── CrossRef ──────────────────────────────────────────────────────────────
    crossref_cfg = cfg.get("crossref", {})
    journals     = crossref_cfg.get("journals", [])
    email        = crossref_cfg.get("polite_email", "anonymous@example.com")
    rows         = crossref_cfg.get("rows", 20)

    # Safety net: don't send literal placeholder to CrossRef
    if email in ("your@email.com", "", None):
        email = "anonymous@example.com"

    print(f"\n── CrossRef journals ({len(journals)}) ──────────────────────────")
    for journal in journals:
        arts = fetch_crossref(journal, cutoff, rows, email)
        print(f"  {journal['name']}: {len(arts)}")
        all_articles.extend(arts)
        time.sleep(0.5)   # polite rate-limit

    # ── Deduplicate by DOI (RSS + CrossRef can overlap) ──────────────────────
    seen: set[str] = set()
    deduped: list[dict] = []
    for a in all_articles:
        key = a.get("doi") or a["title"]
        if key not in seen:
            seen.add(key)
            deduped.append(a)
    if len(deduped) < len(all_articles):
        print(f"  Deduplicated: {len(all_articles)} → {len(deduped)} articles")
    all_articles = deduped

    print(f"\n  Total articles collected: {len(all_articles)}")

    # ── AI summaries ──────────────────────────────────────────────────────────
    summaries: dict[int, dict] = {}
    if not args.no_ai and all_articles:
        anthropic_key = os.environ.get("ANTHROPIC_API_KEY", "")
        if not anthropic_key:
            print("\n[INFO] ANTHROPIC_API_KEY not set — skipping AI summaries.")
        else:
            with_abstract = sum(1 for a in all_articles if a.get("abstract"))
            print(f"\n── AI summaries (Claude Haiku) ──────────────────────")
            print(f"  {with_abstract}/{len(all_articles)} articles have abstracts")
            summaries = ai_summarize(all_articles, anthropic_key)
            print(f"  Summarised {len(summaries)}/{with_abstract} articles")

    # ── Render & save ─────────────────────────────────────────────────────────
    html = render_html(all_articles, summaries, run_at, hours)

    out_dir = Path(args.output)
    out_dir.mkdir(parents=True, exist_ok=True)
    fname    = f"hema_onc_digest_{run_at.strftime('%Y-%m-%d')}.html"
    out_path = out_dir / fname
    out_path.write_text(html, encoding="utf-8")

    print(f"\n✓ Report saved → {out_path}")
    print(f"  Articles: {len(all_articles)}  |  Journals: {len(set(a['journal'] for a in all_articles))}")
    if summaries:
        print(f"  AI summaries: {len(summaries)}")

    # ── Email delivery ────────────────────────────────────────────────────────
    if args.email_to:
        smtp_user = os.environ.get("GMAIL_USER", "")
        smtp_pass = os.environ.get("GMAIL_APP_PASSWORD", "")
        if not smtp_user or not smtp_pass:
            print("\n[WARN] --email-to set but GMAIL_USER / GMAIL_APP_PASSWORD "
                  "not found in environment — email skipped.")
        else:
            n_journals = len(set(a["journal"] for a in all_articles))
            subject = (f"\U0001f9ea Heme/Onc Digest · "
                       f"{run_at.strftime('%Y-%m-%d')} · "
                       f"{len(all_articles)} articles · "
                       f"{n_journals} journals")
            print(f"\n── Sending email → {args.email_to} ─────────────────────")
            try:
                send_html_email(html, subject, args.email_to, smtp_user, smtp_pass)
                print(f"✓ Email sent → {args.email_to}")
            except Exception as exc:
                print(f"[ERROR] Email failed: {exc}", file=sys.stderr)


if __name__ == "__main__":
    main()
