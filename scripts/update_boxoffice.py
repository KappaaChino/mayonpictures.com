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
def fetch_ph_weekly():
    """
    Returns: (movies, label, date_range, url)
      movies = [{'rank': int, 'title': str, 'weeks': int}, ...]
    Tries the most recent three ISO weeks so we always get the latest chart.
    """
    today = date.today()
    for offset in range(3):
        target = today - timedelta(weeks=offset)
        iso_year, iso_week, _ = target.isocalendar()
        url = (
            f"https://www.boxofficemojo.com/weekend/"
            f"{iso_year}W{iso_week:02d}/?area=PH"
        )
        try:
            r = requests.get(url, headers=HEADERS, timeout=20)
            r.raise_for_status()
            soup = BeautifulSoup(r.text, "html.parser")

            table = soup.find("table")
            if not table:
                continue

            movies = []
            for row in table.find_all("tr")[1:]:
                cols = row.find_all("td")
                if len(cols) < 3:
                    continue

                # Rank
                try:
                    rank = int(cols[0].get_text(strip=True))
                except ValueError:
                    continue

                # Title — in the 3rd <td>, inside an <a>
                title_el = cols[2].find("a")
                title = (title_el or cols[2]).get_text(strip=True)
                if not title:
                    continue

                # Weeks in release — second-to-last column
                try:
                    weeks = int(cols[-2].get_text(strip=True))
                except (ValueError, IndexError):
                    weeks = 1

                movies.append({"rank": rank, "title": title, "weeks": weeks})

            if movies:
                label = f"Weekend {iso_week}, {iso_year}"
                dates = weekend_dates(iso_year, iso_week)
                print(f"  ✓ PH Weekly: {len(movies)} films — {label} ({dates})")
                return movies, label, dates, url

        except Exception as exc:
            print(f"  ! PH Weekly offset={offset} failed: {exc}")

    print("  ✗ PH Weekly: all attempts failed — returning empty list.")
    return [], "Unknown", "", "https://www.boxofficemojo.com"


# ---------------------------------------------------------------------------
# Source 2 — US Weekly (The Numbers)
# ---------------------------------------------------------------------------
def fetch_us_weekly():
    """
    Returns: (movies, label, url)
      movies = [{'rank': int, 'title': str, 'weeks': int}, ...]
    Only ranked entries (1, 2, 3 … 12) are included; unranked rows are skipped.
    """
    url = "https://www.the-numbers.com/weekend-box-office-chart"
    try:
        r = requests.get(url, headers=HEADERS, timeout=20)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")

        # Weekend date from the <h1>
        h1 = soup.find("h1")
        label = h1.get_text(strip=True) if h1 else "Unknown"
        # Strip the long prefix
        label = re.sub(r"^Weekend Domestic Box Office\s*", "", label).strip()

        # First <table> is the main chart
        table = soup.find("table")
        if not table:
            print("  ✗ US Weekly: no table found.")
            return [], label, url

        movies = []
        for row in table.find_all("tr")[1:]:
            cols = row.find_all("td")
            if len(cols) < 3:
                continue

            # Only keep rows where rank column is a plain integer
            rank_text = cols[0].get_text(strip=True)
            try:
                rank = int(rank_text)
            except ValueError:
                continue  # dash / unranked rows

            # Title — 3rd column, inside <strong><a>
            title_el = cols[2].find("a") or cols[2].find("strong") or cols[2]
            title = title_el.get_text(strip=True)
            if not title:
                continue

            # Days in release → weeks
            try:
                days = int(cols[-1].get_text(strip=True))
                weeks = max(1, (days + 6) // 7)
            except (ValueError, IndexError):
                weeks = 1

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
        r"(href=\"https://www\.boxofficemojo\.com/weekend/[^\"]*\">)[^<]*(</a>)",
        rf"\1Box Office Mojo — Philippine {ph_label}, {ph_dates}\2",
        new_content,
    )

    # Update US source link text
    new_content = re.sub(
        r"(href=\"https://www\.the-numbers\.com/weekend-box-office-chart\">)[^<]*(</a>)",
        rf"\1The Numbers — US Weekend Domestic Box Office, {us_label}\2",
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

    print("Patching index.html…")
    ok = update_index_html(js_block, ph_label, ph_dates, ph_url, us_label)

    if ok:
        print("\n✓ All done. Box office data updated successfully.")
    else:
        print("\n✗ Update failed.")
        sys.exit(1)
