"""Build reports/index.html with collapsible month groups (terracotta palette)."""
import re
from datetime import date
from pathlib import Path

REPORTS = Path("reports")
MONTHS = {"01":"Jan","02":"Feb","03":"Mar","04":"Apr","05":"May","06":"Jun",
          "07":"Jul","08":"Aug","09":"Sep","10":"Oct","11":"Nov","12":"Dec"}

files = sorted(REPORTS.glob("hema_onc_digest_*.html"), reverse=True)

groups = {}
for f in files:
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", f.name)
    if not m: continue
    key = (m.group(1), m.group(2))
    groups.setdefault(key, []).append((m.group(3), f.name, f"{m.group(1)}-{m.group(2)}-{m.group(3)}"))

keys = sorted(groups.keys(), reverse=True)
current = keys[0] if keys else None

CSS = """*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
:root{--bg:#fbf6f1;--surface:#fff;--border:#e7d3c0;--accent:#a04a1c;--accent2:#bf6433;--text:#3b2618;--muted:#a8856a;--shadow-sm:0 1px 3px rgba(0,0,0,.06);--radius:10px}
@media (prefers-color-scheme:dark){:root{--bg:#1f140c;--surface:#2c1d12;--border:#5e3a22;--accent:#d49374;--accent2:#bf8362;--text:#e8d4be;--muted:#a8856a;--shadow-sm:0 1px 3px rgba(0,0,0,.3)}}
html{scroll-behavior:smooth}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;background:var(--bg);color:var(--text);line-height:1.6;font-size:16px}
a{color:var(--accent);text-decoration:none}a:hover{text-decoration:underline}
.site-header{background:linear-gradient(135deg,#a04a1c 0%,#bf6433 100%);color:#fff;padding:1.5rem;box-shadow:0 2px 8px rgba(160,74,28,.3)}
@media (prefers-color-scheme:dark){.site-header{background:linear-gradient(135deg,#5e3a22 0%,#7a4a2c 100%)}}
.site-header h1{font-size:1.3rem;font-weight:700;letter-spacing:-.01em}
.site-header p{font-size:.85rem;opacity:.88;margin-top:.3rem}
.container{max-width:720px;margin:0 auto;padding:1.5rem 1rem 3rem}
details{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);margin-bottom:.75rem;box-shadow:var(--shadow-sm);overflow:hidden}
summary{cursor:pointer;padding:.85rem 1.1rem;font-weight:600;font-size:1rem;color:var(--accent);user-select:none;list-style:none}
summary::-webkit-details-marker{display:none}
summary::before{content:'\\25B6';display:inline-block;margin-right:.55rem;transition:transform .15s;font-size:.7em;color:var(--muted)}
details[open] summary::before{transform:rotate(90deg)}
summary:hover{background:var(--bg)}
.month-count{color:var(--muted);font-weight:500;font-size:.85em;margin-left:.4rem}
.days{list-style:none;padding:.3rem 1.1rem 1rem 2rem;margin:0}
.days li{padding:.25rem 0;font-size:.92rem}
.days a{color:var(--text)}
.days a:hover{color:var(--accent)}"""

html_groups = []
for key in keys:
    year, month = key
    days = groups[key]
    is_open = " open" if key == current else ""
    items = "\n".join(f'<li><a href="{name}">{full}</a></li>' for _, name, full in days)
    html_groups.append(
        f'<details{is_open}><summary>{MONTHS[month]} {year}<span class="month-count">({len(days)})</span></summary>'
        f'<ul class="days">{items}</ul></details>'
    )

today = date.today().isoformat()
html = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="light dark">
<title>Daily Heme/Onc Digest</title>
<style>{CSS}</style></head><body>
<header class="site-header">
<h1>Daily Heme/Onc Digest</h1>
<p>NCKUH Hematology &middot; Updated {today}</p>
</header>
<div class="container">
{"".join(html_groups)}
</div>
</body></html>"""

(REPORTS / "index.html").write_text(html, encoding="utf-8")
print(f"Built index: {len(files)} reports across {len(keys)} months")
