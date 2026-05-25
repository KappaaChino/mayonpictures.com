#!/usr/bin/env python3
"""
update_boxoffice.py
===================
Fetches box office data from three sources and updates index.html.
Called automatically by GitHub Actions every Monday.

Sources:
  - PH Weekly  : Box Office Mojo  (boxofficemojo.com)
  - US Weekly  : The Numbers      (the-numbers.com)
  - PH All Time: Wikipedia        (en.wikipedia.org)
"""

import re
import sys
import requests
from bs4 import BeautifulSoup
from datetime import date, timedelta


# ---------------------------------------------------------------------------
# Shared request headers — polite browser UA
# ---------------------------------------------------------------------------
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}


# ---------------------------------------------------------------------------
# Helper utilities
# ---------------------------------------------------------------------------
def parse_gross(gross_str: str) -> float:
    """Convert '₱1.6 billion' / '₱924 million' / '₱385M' to a float."""
    s = gross_str.lower().replace("₱", "").replace(",", "").strip()
    try:
        match = re.search(r"[\d.]+", s)
        if not match:
            return 0.0
        num = float(match.group())
        if "billion" in s:
            return num * 1_000_000_000
        if "million" in s or s.endswith("m"):
            return num * 1_000_000
        return num
    except Exception:
        return 0.0


def weekend_dates(iso_year: int, iso_week: int) -> str:
    """Return a human-readable Fri–Sun string for a given ISO week."""
    try:
        monday = date.fromisocalendar(iso_year, iso_week, 1)
        fri = monday + timedelta(days=4)
        sun = monday + timedelta(days=6)
        return f"{fri.strftime('%b %d')}–{sun.strftime('%b %d, %Y')}"
    except Exception:
        return ""


def esc(s: str) -> str:
    """Escape a string for embedding in a JS single-quoted literal."""
    return s.replace("\\", "\\\\").replace("'", "\\'")


# ---------------------------------------------------------------------------
# Source 1 — PH Weekly (Box Office Mojo)
# ---------------------------------------------------------------------------
def get_current_ph_week():
    """
    Read the BOM week number stored in the phMovies JS comment in index.html.
    We search specifically inside the PH WEEKLY comment block to avoid
    accidentally reading the home page stat box or other Weekend N references.
    Returns (year, week) so we never overwrite with older data.
    """
    try:
        with open("index.html", "r", encoding="utf-8") as f:
            content = f.read()
        # Match the comment line right before const phMovies:
        # // PH WEEKLY — Source: Box Office Mojo · [anything] Weekend N [anything] YYYY
        m = re.search(
            r"// PH WEEKLY[^\n]*Weekend (\d+)[^\d]+(\d{4})",
            content
        )
        if m:
            return int(m.group(2)), int(m.group(1))
    except Exception:
        pass
    return 0, 0


def scrape_bom_ph_page(url, bom_week, cf_requests):
    """
    Fetch a single BOM PH weekend page and return parsed movies list.
    Returns (movies, date_range) or ([], '') on failure.
    """
    r = cf_requests.get(
        url,
        impersonate="chrome124",
        headers={"Accept-Language": "en-US,en;q=0.9"},
        timeout=20,
    )
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")

    # Confirm it's a Philippine page
    h1 = soup.find("h1")
    page_label = h1.get_text(strip=True) if h1 else ""
    if "Philippine" not in page_label:
        print(f"  ! W{bom_week:02d}: not a PH page ({page_label[:40]})")
        return [], ""

    # Date range from <h4> e.g. "May 6-10, 2026"
    h4 = soup.find("h4")
    date_range = h4.get_text(strip=True) if h4 else ""

    table = soup.find("table")
    if not table:
        print(f"  ! W{bom_week:02d}: no table found")
        return [], ""

    # Detect "Weeks" column from header row
    header_row = table.find("tr")
    header_cells = [th.get_text(strip=True)
                    for th in header_row.find_all(["th", "td"])] if header_row else []
    weeks_col = next(
        (i for i, h in enumerate(header_cells)
         if h.lower() in ("weeks", "wks", "week")), None
    )

    movies = []
    for row in table.find_all("tr")[1:]:
        cols = row.find_all("td")
        if len(cols) < 3:
            continue
        try:
            rank = int(cols[0].get_text(strip=True))
        except ValueError:
            continue

        title_el = cols[2].find("a")
        title = (title_el or cols[2]).get_text(strip=True)
        if not title:
            continue

        # BOM PH columns: Rank(0) LW(1) Title(2) Gross(3) %(4)
        #                 Theaters(5) Change(6) Avg(7) Total(8) Weeks(9) Dist(10)
        weeks = 1
        for idx in ([weeks_col] if weeks_col is not None else []) + [9, -2, -3]:
            try:
                val = cols[idx].get_text(strip=True)
                weeks = int(val)
                break
            except (ValueError, TypeError, IndexError):
                continue

        movies.append({"rank": rank, "title": title, "weeks": weeks})

    return movies, date_range


def fetch_ph_weekly():
    """
    Returns: (movies, label, date_range, url)
      movies = [{'rank': int, 'title': str, 'weeks': int}, ...]

    Strategy 1 — scrape BOM's PH listing page to get the latest weekend URL
                 directly, no week-number guessing needed.
    Strategy 2 — fallback: try week numbers iso_week-1 through iso_week-6,
                 but NEVER accept a week older than what's already in index.html.
    """
    from curl_cffi import requests as cf_requests

    # Read current week stored in HTML — never go backwards
    current_year, current_week = get_current_ph_week()
    print(f"  Current PH week in HTML: Weekend {current_week}, {current_year}")

    today = date.today()
    iso_year, iso_week, _ = today.isocalendar()

    # ── Strategy 1: get the latest URL from BOM's PH listing page ──
    listing_url = "https://www.boxofficemojo.com/weekend/?area=PH"
    try:
        r = cf_requests.get(
            listing_url,
            impersonate="chrome124",
            headers={"Accept-Language": "en-US,en;q=0.9"},
            timeout=20,
        )
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")

        # Find all links to PH weekend pages — first one is the latest
        links = [
            a["href"] for a in soup.find_all("a", href=True)
            if re.search(r"/weekend/\d{4}W\d+/", a["href"])
            and "area=PH" in a["href"]
        ]

        if links:
            latest_href = links[0]
            m = re.search(r"/(\d{4})W(\d+)/", latest_href)
            if m:
                bom_year, bom_week = int(m.group(1)), int(m.group(2))
                url = f"https://www.boxofficemojo.com{latest_href}"
                if not url.startswith("https://www.boxofficemojo.com/weekend/"):
                    url = f"https://www.boxofficemojo.com/weekend/{bom_year}W{bom_week:02d}/?area=PH"

                print(f"  Listing page found latest: Weekend {bom_week}, {bom_year}")

                # Never go backwards
                if (bom_year, bom_week) < (current_year, current_week):
                    print(f"  ! Listing week W{bom_week:02d} is older than current W{current_week:02d} — skipping")
                else:
                    movies, date_range = scrape_bom_ph_page(url, bom_week, cf_requests)
                    if movies:
                        label = f"Weekend {bom_week}, {bom_year}"
                        print(f"  ✓ PH Weekly (via listing): {len(movies)} films — {label} ({date_range})")
                        return movies, label, date_range, url

    except Exception as exc:
        print(f"  ! Listing page strategy failed: {exc}")

    # ── Strategy 2: try week numbers, never older than current ──
    print("  Falling back to week-number strategy...")
    # On Monday the last completed weekend = Sunday of ISO week (current-1).
    # BOM week = ISO week - 1, so most recent BOM week = iso_week - 2.
    # Start at delta=2, go back, and stop as soon as we hit a week older
    # than what's already stored — never go backwards.
    bom_candidates = []
    for delta in range(2, 9):
        w = iso_week - delta
        y = iso_year
        if w < 1:
            w += 52
            y -= 1
        if (y, w) < (current_year, current_week):
            print(f"  ! Stopping at W{w:02d} — older than current W{current_week:02d}")
            break
        bom_candidates.append((y, w))

    for bom_year, bom_week in bom_candidates:
        url = f"https://www.boxofficemojo.com/weekend/{bom_year}W{bom_week:02d}/?area=PH"
        try:
            movies, date_range = scrape_bom_ph_page(url, bom_week, cf_requests)
            if movies:
                label = f"Weekend {bom_week}, {bom_year}"
                print(f"  ✓ PH Weekly (via fallback): {len(movies)} films — {label} ({date_range})")
                return movies, label, date_range, url
        except Exception as exc:
            print(f"  ! BOM W{bom_week:02d} failed: {exc}")

    print("  ✗ PH Weekly: all strategies failed — keeping existing data.")
    return [], "Unknown", "", "https://www.boxofficemojo.com"


# ---------------------------------------------------------------------------
# Source 2 — US Weekly (The Numbers)
# ---------------------------------------------------------------------------
def fetch_us_weekly():
    """
    Returns: (movies, label, url)
      movies = [{'rank': int, 'title': str, 'weeks': int}, ...]

    The Numbers changed their table format — the Rank column now shows '-'
    dashes instead of integers. We assign rank by row order instead.
    We also skip rows with no valid title or no days-in-release value.
    """
    url = "https://www.the-numbers.com/weekend-box-office-chart"
    try:
        r = requests.get(url, headers=HEADERS, timeout=20)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")

        # Weekend date from the <h1> e.g. "Weekend Domestic Box Office May 22, 2026"
        h1 = soup.find("h1")
        label = h1.get_text(strip=True) if h1 else "Unknown"
        label = re.sub(r"^Weekend Domestic Box Office\s*", "", label).strip()

        # First <table> is the main chart
        table = soup.find("table")
        if not table:
            print("  ✗ US Weekly: no table found.")
            return [], label, url

        movies = []
        rank = 0
        for row in table.find_all("tr")[1:]:
            cols = row.find_all("td")
            if len(cols) < 3:
                continue

            # Title — 3rd column (index 2), inside <a> or <strong>
            title_el = cols[2].find("a") or cols[2].find("strong") or cols[2]
            title = title_el.get_text(strip=True)
            if not title or title.lower().startswith("reporting"):
                continue

            # Days in release → weeks (last column)
            try:
                days_text = cols[-1].get_text(strip=True).replace(",", "")
                days = int(days_text)
                weeks = max(1, (days + 6) // 7)
            except (ValueError, IndexError):
                continue  # skip rows without a valid days value

            rank += 1
            movies.append({"rank": rank, "title": title, "weeks": weeks})

        print(f"  ✓ US Weekly: {len(movies)} films — {label}")
        return movies, label, url

    except Exception as exc:
        print(f"  ✗ US Weekly failed: {exc}")
        return [], "Unknown", url


# ---------------------------------------------------------------------------
# Source 3 — PH All Time (Wikipedia)
# ---------------------------------------------------------------------------
def fetch_ph_alltime():
    """
    Returns: (movies, url)
      movies = top-20 [{'rank', 'title', 'year', 'gross', 'prod'}, ...]
    """
    url = "https://en.wikipedia.org/wiki/List_of_highest-grossing_Philippine_films"
    try:
        r = requests.get(url, headers=HEADERS, timeout=20)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")

        entries = []
        for table in soup.find_all("table", class_="wikitable"):
            for row in table.find_all("tr")[1:]:
                cols = row.find_all(["td", "th"])
                if len(cols) < 4:
                    continue

                year_txt  = cols[0].get_text(strip=True)
                title_txt = cols[1].get_text(strip=True)
                prod_txt  = cols[2].get_text(strip=True)
                gross_txt = cols[3].get_text(strip=True).replace("\xa0", "")

                gross_num = parse_gross(gross_txt)
                if gross_num > 0 and title_txt:
                    entries.append({
                        "year":      year_txt,
                        "title":     title_txt,
                        "prod":      prod_txt,
                        "gross":     gross_txt,
                        "gross_num": gross_num,
                    })

        # Sort by gross descending, deduplicate titles, take top 20
        entries.sort(key=lambda x: x["gross_num"], reverse=True)
        seen, top20 = set(), []
        for e in entries:
            if e["title"] not in seen:
                seen.add(e["title"])
                top20.append(e)
            if len(top20) == 20:
                break

        movies = [
            {
                "rank":  i + 1,
                "title": e["title"],
                "year":  e["year"],
                "gross": e["gross"],
                "prod":  e["prod"],
            }
            for i, e in enumerate(top20)
        ]

        print(f"  ✓ PH All Time: {len(movies)} films fetched from Wikipedia")
        return movies, url

    except Exception as exc:
        print(f"  ✗ PH All Time failed: {exc}")
        return [], url


# ---------------------------------------------------------------------------
# Build the JS data block
# ---------------------------------------------------------------------------
def build_js_block(
    ph_movies, ph_label, ph_dates, ph_url,
    us_movies, us_label,  us_url,
    at_movies, at_url,
) -> str:
    updated = date.today().strftime("%B %d, %Y")

    ph_arr = ",\n  ".join(
        f"{{ rank:{m['rank']}, title:'{esc(m['title'])}', weeks:{m['weeks']} }}"
        for m in ph_movies
    )
    us_arr = ",\n  ".join(
        f"{{ rank:{m['rank']}, title:'{esc(m['title'])}', weeks:{m['weeks']} }}"
        for m in us_movies
    )
    at_arr = ",\n  ".join(
        f"{{ rank:{m['rank']}, title:'{esc(m['title'])}', year:{m['year']}, "
        f"gross:'{esc(m['gross'])}', prod:'{esc(m['prod'])}' }}"
        for m in at_movies
    )

    return (
        f"// ===== BOX OFFICE DATA =====\n"
        f"// Auto-updated by GitHub Actions every Monday — Last updated: {updated}\n"
        f"\n"
        f"// PH WEEKLY — Source: Box Office Mojo · {ph_label} ({ph_dates})\n"
        f"// {ph_url}\n"
        f"const phMovies = [\n"
        f"  {ph_arr}\n"
        f"];\n"
        f"\n"
        f"// US WEEKLY — Source: The Numbers · {us_label}\n"
        f"// {us_url}\n"
        f"const intlMovies = [\n"
        f"  {us_arr}\n"
        f"];\n"
        f"\n"
        f"// PH ALL TIME TOP 20 — Source: Wikipedia · List of Highest-Grossing Philippine Films\n"
        f"// {at_url}\n"
        f"const alltimeMovies = [\n"
        f"  {at_arr}\n"
        f"];"
    )


# ---------------------------------------------------------------------------
# Inject updated JS block + source citation links into index.html
# ---------------------------------------------------------------------------
def update_index_html(js_block: str, ph_label: str, ph_dates: str,
                      ph_url: str, us_label: str) -> bool:
    try:
        with open("index.html", "r", encoding="utf-8") as f:
            content = f.read()
    except FileNotFoundError:
        print("  ✗ index.html not found. Make sure the script runs from the repo root.")
        return False

    # Replace the JS data block
    pattern = r"// ===== BOX OFFICE DATA =====.*?const alltimeMovies = \[.*?\];"
    new_content, n = re.subn(pattern, js_block, content, flags=re.DOTALL)
    if n == 0:
        print("  ✗ Could not locate data block in index.html.")
        return False

    # Update PH source href
    new_content = re.sub(
        r'href="https://www\.boxofficemojo\.com/weekend/[^"]*"',
        f'href="{ph_url}"',
        new_content,
    )

    # Update PH source link text
    new_content = re.sub(
        r"(href=\"https://www\.boxofficemojo\.com/weekend/[^\"]*\"[^>]*>)[^<]*(</a>)",
        rf"\1Box Office Mojo: Philippine {ph_label}, {ph_dates}\2",
        new_content,
    )

    # Update US source link text
    new_content = re.sub(
        r"(href=\"https://www\.the-numbers\.com/weekend-box-office-chart\"[^>]*>)[^<]*(</a>)",
        rf"\1The Numbers: US Weekend Domestic Box Office, {us_label}\2",
        new_content,
    )

    with open("index.html", "w", encoding="utf-8") as f:
        f.write(new_content)

    print(f"  ✓ index.html updated ({n} replacement).")
    return True


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    print("=" * 50)
    print("  Mayon Pictures — Box Office Auto-Updater")
    print(f"  Running on: {date.today().strftime('%A, %B %d, %Y')}")
    print("=" * 50)

    print("\n[1/3] PH Weekly Box Office (Box Office Mojo)…")
    ph_movies, ph_label, ph_dates, ph_url = fetch_ph_weekly()

    print("\n[2/3] US Weekly Box Office (The Numbers)…")
    us_movies, us_label, us_url = fetch_us_weekly()

    print("\n[3/3] PH All Time Box Office (Wikipedia)…")
    at_movies, at_url = fetch_ph_alltime()

    # If PH fetch returned nothing, preserve whatever is already in index.html
    # by reading the existing phMovies block and keeping it untouched.
    if not ph_movies:
        print("\n  ⚠ PH Weekly fetch returned no data — existing PH data will be preserved.")

    # Abort only if ALL three sources failed
    if not ph_movies and not us_movies and not at_movies:
        print("\n✗ All three sources returned no data. Aborting — index.html unchanged.")
        sys.exit(1)

    print("\nBuilding JS data block…")
    js_block = build_js_block(
        ph_movies, ph_label, ph_dates, ph_url,
        us_movies, us_label, us_url,
        at_movies, at_url,
    )

    # If PH is empty, strip the phMovies block from js_block so it's not overwritten
    if not ph_movies:
        js_block = re.sub(
            r"// PH WEEKLY.*?const phMovies = \[.*?\];",
            "",
            js_block,
            flags=re.DOTALL,
        )

    print("Patching index.html…")
    ok = update_index_html(js_block, ph_label, ph_dates, ph_url, us_label)

    if ok:
        print("\n✓ All done. Box office data updated successfully.")
    else:
        print("\n✗ Update failed.")
        sys.exit(1)
