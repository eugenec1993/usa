"""
World Cup ticket alert — USA vs Australia, June 19 2026, Lumen Field.

Uses a real Chromium browser (Playwright) to load the SeatGeek event page and
capture the listing data the page fetches in the background. No API key needed.
Filters for 100-200 level seats with 4+ together under your price limit, then
emails you. Keeps a small state file so it won't email the same listing twice.

Config comes from environment variables (set as GitHub Actions "vars"/"secrets"):
  EVENT_URL    - SeatGeek event page (default is this match)
  PRICE_LIMIT  - alert when a seat is under this many dollars (default 2500)
  MIN_QTY      - seats needed together (default 4)
  SECTION_MIN  - lowest section number to accept (default 100)
  SECTION_MAX  - highest section number to accept (default 299)
  MAIL_USER    - Gmail address that sends the alert
  MAIL_PASS    - Gmail APP PASSWORD (not your login password)
  MAIL_TO      - where the alert is sent
"""

import os
import re
import json
import smtplib
import ssl
from email.message import EmailMessage
from playwright.sync_api import sync_playwright

EVENT_URL   = os.environ.get("EVENT_URL",
    "https://seatgeek.com/fifa-world-cup-tickets/international-soccer/2026-06-19-12-pm/17248696")
PRICE_LIMIT = float(os.environ.get("PRICE_LIMIT", "2500"))
MIN_QTY     = int(os.environ.get("MIN_QTY", "4"))
SECTION_MIN = int(os.environ.get("SECTION_MIN", "100"))
SECTION_MAX = int(os.environ.get("SECTION_MAX", "299"))
STATE_FILE  = "state.json"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

BUY_LINKS = [
    ("SeatGeek", EVENT_URL),
    ("FIFA Resale (official)", "https://fwc26-resale-usd.tickets.fifa.com"),
    ("TickPick", "https://www.tickpick.com/buy-fifa-world-cup-26-group-d-united-states-vs-australia-match-32-tickets-lumen-field-6-19-26-12pm/6259615/"),
    ("StubHub", "https://www.stubhub.com/world-cup-seattle-tickets-6-19-2026/event/153020544"),
    ("Gametime", "https://gametime.co/us_australia/fifa-world-cup-usa-vs-australia-match-32-group-d-tickets/6-19-2026-seattle-wa-lumen-field/events/66aa92642e8443f895e2dbc8"),
]


# ---------- capture listing data from the page's own network calls ----------

captured = []

def on_response(resp):
    """Grab any JSON response that looks like a listings feed."""
    try:
        url = resp.url
        if "listing" in url.lower():
            ctype = (resp.headers or {}).get("content-type", "")
            if "json" in ctype:
                captured.append(resp.json())
    except Exception:
        pass


def load_page():
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
        )
        ctx = browser.new_context(
            user_agent=UA,
            locale="en-US",
            timezone_id="America/Los_Angeles",
            viewport={"width": 1366, "height": 900},
        )
        page = ctx.new_page()
        page.on("response", on_response)
        try:
            page.goto(EVENT_URL, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(6000)          # let background calls fire
            for _ in range(4):                   # scroll to load more listings
                page.mouse.wheel(0, 4000)
                page.wait_for_timeout(1500)
        except Exception as e:
            print("Page load issue:", e)
        try:
            page.screenshot(path="debug.png")   # helps you debug if blocked
        except Exception:
            pass
        browser.close()


# ---------- pull listing dicts out of whatever JSON we captured ----------

def extract_listings(blob):
    """Walk arbitrary JSON and collect dicts that look like a ticket listing."""
    found = []

    def walk(node):
        if isinstance(node, dict):
            keys = {k.lower() for k in node.keys()}
            has_section = any(k in keys for k in ("section", "sg_section", "sec"))
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
    for k in ("section", "sg_section", "sec"):
        if k in low:
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


def find_matches():
    seen, matches, all_prices = set(), [], []
    for blob in captured:
        for raw in extract_listings(blob):
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
    user, pwd, to = os.environ.get("MAIL_USER"), os.environ.get("MAIL_PASS"), os.environ.get("MAIL_TO")
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


def links_block():
    return "\n".join(f"{name}:\n  {url}" for name, url in BUY_LINKS)


def main():
    load_page()
    print(f"Captured {len(captured)} JSON blob(s) from the page.")
    matches, floor = find_matches()
    print(f"Floor price seen: {floor}.  Qualifying matches: {len(matches)}.")

    if not captured:
        print("No data captured — likely blocked or the page changed. "
              "Check the debug.png artifact.")
        return

    state = load_state()
    alerted = set(state.get("alerted", []))
    fresh = [m for m in matches
             if f"{m['section']}|{m['qty']}|{m['price']}" not in alerted]

    if fresh:
        lines = "\n".join(
            f"  Section {m['section']} — {m['qty']} seats — ${m['price']:.0f} each"
            for m in fresh)
        body = (f"100-200 level seats with {MIN_QTY}+ together under "
                f"${PRICE_LIMIT:.0f} for USA vs Australia:\n\n{lines}\n\n"
                f"Floor price across the venue: "
                f"{('$%.0f' % floor) if floor else 'n/a'}\n\n"
                f"Buy fast:\n\n{links_block()}\n")
        send_email(f"MATCH: {len(fresh)} lower-level set(s) under ${PRICE_LIMIT:.0f}", body)
        for m in fresh:
            alerted.add(f"{m['section']}|{m['qty']}|{m['price']}")
        state["alerted"] = list(alerted)[-100:]
        save_state(state)
    else:
        print("Nothing new qualifies this run.")


if __name__ == "__main__":
    main()
