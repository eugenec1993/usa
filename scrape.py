"""World Cup ticket alert using Playwright and resilient JSON discovery."""
import json
import os
import re
import smtplib
import ssl
from email.message import EmailMessage
from playwright.sync_api import sync_playwright

SOURCES = [
    {"name": "SeatGeek", "url": "https://seatgeek.com/fifa-world-cup-tickets/international-soccer/2026-06-19-12-pm/17248696"},
    {"name": "TickPick", "url": "https://www.tickpick.com/buy-fifa-world-cup-26-group-d-united-states-vs-australia-match-32-tickets-lumen-field-6-19-26-12pm/6259615/"},
    {"name": "StubHub", "url": "https://www.stubhub.com/world-cup-seattle-tickets-6-19-2026/event/153020544"},
    {"name": "Vivid Seats", "url": "https://www.vividseats.com/world-cup-soccer-tickets-lumen-field-6-19-2026--sports-soccer/production/5080483"},
    {"name": "Gametime", "url": "https://gametime.co/us_australia/fifa-world-cup-usa-vs-australia-match-32-group-d-tickets/6-19-2026-seattle-wa-lumen-field/events/66aa92642e8443f895e2dbc8"},
]


def _env(name, default):
    value = os.environ.get(name)
    return value if value not in (None, "") else default


PRICE_LIMIT = float(_env("PRICE_LIMIT", "2500"))
MIN_QTY = int(_env("MIN_QTY", "4"))
SECTION_MIN = int(_env("SECTION_MIN", "100"))
SECTION_MAX = int(_env("SECTION_MAX", "299"))
DEBUG = _env("DEBUG", "") not in ("", "0", "false", "False")
STATE_FILE = "state.json"

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36"
)
FEED_HINTS = (
    "listing", "inventory", "offer", "quote", "ticket", "seat", "event",
    "graphql", "search", "production", "map", "availability", "manifest",
)
DATA_MARKERS = (
    "section", "sectionname", "section_id", "sectionid", "row", "price",
    "displayprice", "quantity", "qty", "splits", "availablequantities",
    "ticketlisting", "listings", "inventory", "offers",
)


def payload_looks_useful(data):
    """Use payload contents, not only endpoint names, to identify inventory JSON."""
    try:
        sample = json.dumps(data, separators=(",", ":"), ensure_ascii=True)[:2_000_000].lower()
    except Exception:
        return False
    hits = sum(marker in sample for marker in DATA_MARKERS)
    return hits >= 2 and any(x in sample for x in ("price", "displayprice", "amount"))


def scrape_source(url, name):
    captured, json_urls, diagnostics = [], [], []
    seen_payloads = set()

    def add_payload(source_url, data, origin):
        try:
            fingerprint = json.dumps(data, sort_keys=True, separators=(",", ":"), default=str)[:10000]
        except Exception:
            fingerprint = repr(data)[:10000]
        key = hash(fingerprint)
        if key in seen_payloads:
            return
        if any(h in source_url.lower() for h in FEED_HINTS) or payload_looks_useful(data):
            seen_payloads.add(key)
            captured.append({"url": source_url, "data": data, "origin": origin})

    def on_response(resp):
        try:
            ctype = (resp.headers or {}).get("content-type", "").lower()
            if "json" not in ctype and not any(h in resp.url.lower() for h in FEED_HINTS):
                return
            data = resp.json()
            json_urls.append(resp.url)
            add_payload(resp.url, data, "network")
        except Exception:
            pass

    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
                "--no-sandbox",
                "--disable-infobars",
            ],
        )
        ctx = browser.new_context(
            user_agent=UA,
            locale="en-US",
            timezone_id="America/Los_Angeles",
            viewport={"width": 1440, "height": 1000},
            extra_http_headers={
                "Accept-Language": "en-US,en;q=0.9",
                "DNT": "1",
                "Upgrade-Insecure-Requests": "1",
            },
        )
        ctx.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
            Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});
            Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
            window.chrome = window.chrome || {runtime: {}};
        """)
        page = ctx.new_page()
        page.on("response", on_response)

        for attempt in range(2):
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=75000)
                page.wait_for_timeout(8000 + attempt * 3000)
                for selector in (
                    "button:has-text('Accept')", "button:has-text('Allow all')",
                    "button:has-text('Got it')", "button:has-text('Continue')",
                ):
                    try:
                        page.locator(selector).first.click(timeout=800)
                    except Exception:
                        pass
                for _ in range(6):
                    page.mouse.wheel(0, 3000)
                    page.wait_for_timeout(1200)
                break
            except Exception as exc:
                diagnostics.append(f"attempt {attempt + 1}: {exc}")
                if attempt == 0:
                    try:
                        page.reload(wait_until="domcontentloaded", timeout=75000)
                    except Exception:
                        pass

        try:
            scripts = page.locator("script[type='application/json'], script[type='application/ld+json']")
            for i in range(min(scripts.count(), 100)):
                text = scripts.nth(i).text_content() or ""
                if not text.strip():
                    continue
                try:
                    add_payload(f"{page.url}#embedded-{i}", json.loads(text), "embedded")
                except Exception:
                    pass
        except Exception as exc:
            diagnostics.append(f"embedded JSON: {exc}")

        try:
            title = page.title()
            body = (page.locator("body").inner_text(timeout=3000) or "")[:2000]
            challenge_words = ("access denied", "captcha", "verify you are human", "datadome", "blocked")
            if any(word in f"{title}\n{body}".lower() for word in challenge_words):
                diagnostics.append(f"anti-bot challenge detected; title={title!r}")
            diagnostics.append(f"final_url={page.url}")
        except Exception:
            pass

        try:
            page.screenshot(path=f"debug-{slug}.png", full_page=True)
        except Exception:
            pass
        browser.close()

    return captured, json_urls, diagnostics


def extract_listings(blob):
    found = []
    section_keys = {"section", "section_id", "sectionid", "section_name", "sectionname", "sid", "lid", "sg_section", "sec"}
    price_keys = {"price", "display_price", "displayprice", "p", "dp", "pf", "amount", "ticket_price", "ticketprice"}

    def walk(node):
        if isinstance(node, dict):
            keys = {str(k).lower() for k in node}
            if keys & section_keys and keys & price_keys:
                found.append(node)
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(blob)
    return found


def to_number(value):
    if isinstance(value, dict):
        for key in ("amount", "value", "price", "display", "formatted"):
            if key in value:
                return to_number(value[key])
        return None
    try:
        cleaned = re.sub(r"[^\d.]", "", str(value))
        return float(cleaned) if cleaned else None
    except Exception:
        return None


def normalize(listing):
    low = {str(k).lower(): v for k, v in listing.items()}
    section = None
    for key in ("sid", "section_id", "sectionid", "section", "sg_section", "sec", "section_name", "sectionname", "lid"):
        if low.get(key) not in (None, ""):
            match = re.search(r"\d+", str(low[key]))
            if match:
                section = int(match.group())
                break

    price = None
    for key in ("price", "display_price", "displayprice", "ticket_price", "ticketprice", "amount", "dp", "pf", "p"):
        if low.get(key) not in (None, ""):
            price = to_number(low[key])
            if price is not None:
                break

    qty = None
    for key in ("quantity", "qty", "q", "available_quantity", "availablequantity", "max_quantity", "maxquantity"):
        if key in low:
            try:
                qty = int(low[key])
            except Exception:
                pass
            if qty is not None:
                break
    if qty is None:
        for key in ("splits", "split_options", "splitoptions", "available_quantities", "availablequantities"):
            values = low.get(key)
            if isinstance(values, list) and values:
                try:
                    qty = max(int(x.get("quantity", x)) if isinstance(x, dict) else int(x) for x in values)
                except Exception:
                    pass
                break
    return {"section": section, "price": price, "qty": qty}


def find_matches(captured):
    seen, matches, all_prices = set(), [], []
    for entry in captured:
        for raw in extract_listings(entry["data"]):
            item = normalize(raw)
            if item["price"] is not None and item["price"] > 0:
                all_prices.append(item["price"])
            if not (item["section"] and item["price"] and item["qty"]):
                continue
            if SECTION_MIN <= item["section"] <= SECTION_MAX and item["qty"] >= MIN_QTY and item["price"] < PRICE_LIMIT:
                key = f"{item['section']}|{item['qty']}|{item['price']}"
                if key not in seen:
                    seen.add(key)
                    matches.append(item)
    matches.sort(key=lambda item: item["price"])
    return matches, min(all_prices) if all_prices else None


def load_state():
    try:
        with open(STATE_FILE) as file:
            return json.load(file)
    except Exception:
        return {"alerted": []}


def save_state(state):
    with open(STATE_FILE, "w") as file:
        json.dump(state, file)


def send_email(subject, body):
    user, password, recipient = os.environ.get("MAIL_USER"), os.environ.get("MAIL_PASS"), os.environ.get("MAIL_TO")
    if not (user and password and recipient):
        print("Mail secrets missing — printing instead:\n", subject, "\n", body)
        return
    message = EmailMessage()
    message["From"], message["To"], message["Subject"] = user, recipient, subject
    message.set_content(body)
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ssl.create_default_context()) as smtp:
        smtp.login(user, password)
        smtp.send_message(message)
    print("Alert email sent.")


def main():
    all_matches, floors = [], {}
    for source in SOURCES:
        try:
            captured, json_urls, diagnostics = scrape_source(source["url"], source["name"])
            if DEBUG or not captured:
                print(f"[{source['name']}] JSON endpoints seen: {len(json_urls)}")
                for line in diagnostics:
                    print(f"[{source['name']}] diagnostic: {line}")
                if DEBUG:
                    for endpoint in json_urls[:40]:
                        print(f"[{source['name']}] endpoint: {endpoint}")
                    for entry in captured[:5]:
                        print(f"[{source['name']}] captured via {entry['origin']}: {entry['url']}")
                        print(f"[{source['name']}] preview: {json.dumps(entry['data'])[:800]}")
            matches, floor = find_matches(captured)
            floors[source["name"]] = floor
            for match in matches:
                match["source"], match["url"] = source["name"], source["url"]
            all_matches.extend(matches)
            print(f"[{source['name']}] captured {len(captured)} feed(s), floor {('$%.0f' % floor) if floor else 'no data'}, qualifying {len(matches)}")
        except Exception as exc:
            print(f"[{source['name']}] error: {exc}")

    floor_line = ", ".join(f"{name} {('$%.0f' % value) if value else 'no data'}" for name, value in floors.items())
    print("Floors:", floor_line)
    state = load_state()
    alerted = set(state.get("alerted", []))
    fresh = [m for m in all_matches if f"{m['source']}|{m['section']}|{m['qty']}|{m['price']}" not in alerted]
    if not fresh:
        print("Nothing new qualifies this run.")
        return

    by_source = {}
    for match in fresh:
        by_source.setdefault(match["source"], []).append(match)
    blocks = []
    for name, items in by_source.items():
        rows = "\n".join(f"  Section {m['section']} — {m['qty']} seats — ${m['price']:.0f} each" for m in items)
        blocks.append(f"{name}  ({items[0]['url']})\n{rows}")
    body = (f"100-200 level seats with {MIN_QTY}+ together under ${PRICE_LIMIT:.0f}:\n\n" + "\n\n".join(blocks) + f"\n\nVenue floor by site: {floor_line}\n\nBuy fast.\n")
    send_email(f"MATCH: {len(fresh)} lower-level set(s) under ${PRICE_LIMIT:.0f}", body)
    for match in fresh:
        alerted.add(f"{match['source']}|{match['section']}|{match['qty']}|{match['price']}")
    state["alerted"] = list(alerted)[-200:]
    save_state(state)


if __name__ == "__main__":
    main()
