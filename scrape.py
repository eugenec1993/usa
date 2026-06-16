"""
World Cup ticket alert — USA vs Australia, June 19 2026, Lumen Field.

Uses a real Chromium browser (Playwright) to load each ticketing site's event
page and capture the listing data the page loads in the background. No API key.
Checks every site in SOURCES, filters for 100-200 level seats with 4+ together
under your price limit, and emails you. A state file stops repeat alerts.

Add or remove platforms by editing the SOURCES list below. The parser is
generic, so most sites work with the same code; some need field-name tweaks.

Environment variables (GitHub Actions "vars"/"secrets"):
  PRICE_LIMIT, MIN_QTY, SECTION_MIN, SECTION_MAX  - filter settings
  MAIL_USER, MAIL_PASS, MAIL_TO                    - Gmail sending + recipient
"""

import os
import re
import json
import smtplib
import ssl
from email.message import EmailMessage
from playwright.sync_api import sync_playwright

# ---------------- platforms to check ----------------
# Shipped on: SeatGeek + TickPick (most readable).
# Uncomment the others to add them. They use stronger anti-bot protection,
# so expect them to get blocked more often and need occasional tuning.
SOURCES = [
    {"name": "SeatGeek", "url":
        "https://seatgeek.com/fifa-world-cup-tickets/international-soccer/2026-06-19-12-pm/17248696"},
    {"name": "TickPick", "url":
        "https://www.tickpick.com/buy-fifa-world-cup-26-group-d-united-states-vs-australia-match-32-tickets-lumen-field-6-19-26-12pm/6259615/"},
    {"name": "StubHub", "url":
        "https://www.stubhub.com/world-cup-seattle-tickets-6-19-2026/event/153020544"},
    {"name": "Vivid Seats", "url":
        "https://www.vividseats.com/world-cup-soccer-tickets-lumen-field-6-19-2026--sports-soccer/production/5080483"},
    {"name": "Gametime", "url":
        "https://gametime.co/us_australia/fifa-world-cup-usa-vs-australia-match-32-group-d-tickets/6-19-2026-seattle-wa-lumen-field/events/66aa92642e8443f895e2dbc8"},
]
# FIFA official resale is intentionally NOT here: it needs your FIFA login and
# is heavily protected. Use FIFA's own site alerts for that source.

def _env(name, default):
    """Return the env value, but fall back to default if it's missing OR blank."""
    v = os.environ.get(name)
    return v if v not in (None, "") else default

PRICE_LIMIT = float(_env("PRICE_LIMIT", "2500"))
MIN_QTY     = int(_env("MIN_QTY", "4"))
SECTION_MIN = int(_env("SECTION_MIN", "100"))
SECTION_MAX = int(_env("SECTION_MAX", "299"))
DEBUG       = _env("DEBUG", "") not in ("", "0", "false", "False")
STATE_FILE  = "state.json"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

# URL hints that usually mark a listings/inventory feed across sites.
FEED_HINTS = ("listing", "inventory", "offer", "quote", "ticketlist", "seats")


# ---------- load one site and capture its listing JSON ----------

def scrape_source(url, name):
    captured = []
    json_urls = []

    def on_response(resp):
        try:
            ctype = (resp.headers or {}).get("content-type", "")
            if "json" not in ctype:
                return
            json_urls.append(resp.url)
            u = resp.url.lower()
            if any(h in u for h in FEED_HINTS):
                captured.append({"url": resp.url, "data": resp.json()})
        except Exception:
            pass

    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
        )
        ctx = browser.new_context(
            user_agent=UA, locale="en-US",
            timezone_id="America/Los_Angeles",
            viewport={"width": 1366, "height": 900},
        )
        page = ctx.new_page()
        page.on("response", on_response)
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(6000)
            for _ in range(4):
                page.mouse.wheel(0, 4000)
                page.wait_for_timeout(1500)
        except Exception as e:
            print(f"[{name}] page load issue: {e}")
        try:
            page.screenshot(path=f"debug-{slug}.png")
        except Exception:
            pass
        browser.close()
    return captured, json_urls


# ---------- pull listing dicts out of captured JSON ----------

def extract_listings(blob):
    found = []

    def walk(node):
        if isinstance(node, dict):
            keys = {k.lower() for k in node.keys()}
            has_section = any(k in keys for k in
                              ("section", "section_id", "section_name",
                               "sid", "lid", "sg_section", "sec"))
            has_price = any(k in keys for k in ("price", "display_price", "p", "dp", "pf"))
            if has_section and has_price:
                found.append(node)
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(blob)
    return found


def to_number(value):
    try:
        return float(re.sub(r"[^\d.]", "", str(value)))
    except Exception:
        return None


def normalize(listing):
    low = {k.lower(): v for k, v in listing.items()}

    section = None
    for k in ("sid", "section_id", "section", "sg_section", "sec", "section_name", "lid"):
        if k in low and low[k] not in (None, ""):
            m = re.search(r"\d+", str(low[k]))
            if m:
                section = int(m.group())
                break

    price = None
    for k in ("price", "display_price", "dp", "pf", "p"):
        if k in low and low[k] not in (None, ""):
            price = to_number(low[k])
            if price:
                break

    qty = None
    for k in ("quantity", "qty", "q"):
        if k in low:
            try:
                qty = int(low[k])
            except Exception:
                pass
            break
    if qty is None:
        for k in ("splits", "split_options", "available_quantities"):
            if k in low and isinstance(low[k], list) and low[k]:
                try:
                    qty = max(int(x) for x in low[k])
                except Exception:
                    pass
                break

    return {"section": section, "price": price, "qty": qty}


def find_matches(captured):
    seen, matches, all_prices = set(), [], []
    for entry in captured:
        for raw in extract_listings(entry["data"]):
            n = normalize(raw)
            if n["price"]:
                all_prices.append(n["price"])
            if not (n["section"] and n["price"] and n["qty"]):
                continue
            if (SECTION_MIN <= n["section"] <= SECTION_MAX
                    and n["qty"] >= MIN_QTY and n["price"] < PRICE_LIMIT):
                key = f"{n['section']}|{n['qty']}|{n['price']}"
                if key not in seen:
                    seen.add(key)
                    matches.append(n)
    matches.sort(key=lambda m: m["price"])
    floor = min(all_prices) if all_prices else None
    return matches, floor


# ---------- state + email ----------

def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {"alerted": []}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)


def send_email(subject, body):
    user, pwd, to = (os.environ.get("MAIL_USER"),
                     os.environ.get("MAIL_PASS"),
                     os.environ.get("MAIL_TO"))
    if not (user and pwd and to):
        print("Mail secrets missing — printing instead:\n", subject, "\n", body)
        return
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = user, to, subject
    msg.set_content(body)
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ssl.create_default_context()) as s:
        s.login(user, pwd)
        s.send_message(msg)
    print("Alert email sent.")


def main():
    all_matches = []
    floors = {}

    for src in SOURCES:
        try:
            captured, json_urls = scrape_source(src["url"], src["name"])
            if DEBUG:
                print(f"[{src['name']}] JSON endpoints seen: {len(json_urls)}")
                for u in json_urls[:40]:
                    print(f"[{src['name']}]   endpoint: {u}")
                for i, entry in enumerate(captured):
                    data = entry["data"]
                    top = list(data.keys()) if isinstance(data, dict) else "(list)"
                    print(f"[{src['name']}]   top-level: {top}")
                    print(f"[{src['name']}]   preview: {json.dumps(data)[:600]}")
            matches, floor = find_matches(captured)
            floors[src["name"]] = floor
            for m in matches:
                m["source"] = src["name"]
                m["url"] = src["url"]
            all_matches.extend(matches)
            print(f"[{src['name']}] captured {len(captured)} feed(s), "
                  f"floor {('$%.0f' % floor) if floor else 'no data'}, "
                  f"qualifying {len(matches)}")
        except Exception as e:
            print(f"[{src['name']}] error: {e}")

    # Report the floor across every site that returned data.
    floor_line = ", ".join(
        f"{name} {('$%.0f' % f) if f else 'no data'}" for name, f in floors.items())
    print("Floors:", floor_line)

    state = load_state()
    alerted = set(state.get("alerted", []))
    fresh = [m for m in all_matches
             if f"{m['source']}|{m['section']}|{m['qty']}|{m['price']}" not in alerted]

    if not fresh:
        print("Nothing new qualifies this run.")
        return

    # Group the new matches by platform for the email.
    by_src = {}
    for m in fresh:
        by_src.setdefault(m["source"], []).append(m)
    blocks = []
    for name, items in by_src.items():
        rows = "\n".join(
            f"  Section {m['section']} — {m['qty']} seats — ${m['price']:.0f} each"
            for m in items)
        blocks.append(f"{name}  ({items[0]['url']})\n{rows}")

    body = (f"100-200 level seats with {MIN_QTY}+ together under "
            f"${PRICE_LIMIT:.0f} for USA vs Australia:\n\n"
            + "\n\n".join(blocks)
            + f"\n\nVenue floor by site: {floor_line}\n\nBuy fast.\n")

    send_email(f"MATCH: {len(fresh)} lower-level set(s) under ${PRICE_LIMIT:.0f}", body)

    for m in fresh:
        alerted.add(f"{m['source']}|{m['section']}|{m['qty']}|{m['price']}")
    state["alerted"] = list(alerted)[-200:]
    save_state(state)


if __name__ == "__main__":
    main()
