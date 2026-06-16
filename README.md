# World Cup Ticket Alert (GitHub Actions + Playwright)

Watches SeatGeek for **USA vs Australia, June 19 2026, Lumen Field** and emails
you when 100-200 level seats with 4+ together drop under $2,500. No API key —
it drives a real browser, so it reads the same data a person sees.

## Files
- `scrape.py` — the scraper and email logic
- `.github/workflows/ticket-alert.yml` — runs it every 15 minutes
- `requirements.txt` — one dependency (Playwright)
- `state.json` — created automatically; remembers what it already alerted on

## Setup (about 10 minutes)

1. **Make a repo** and add these files (keep the folder structure exactly).

2. **Make a Gmail app password** (the script sends mail through Gmail):
   - Turn on 2-Step Verification on your Google account.
   - Go to Google Account → Security → App passwords, create one, copy it.

3. **Add repo secrets** (Settings → Secrets and variables → Actions → *Secrets*):
   - `MAIL_USER` — your Gmail address
   - `MAIL_PASS` — the app password from step 2
   - `MAIL_TO` — where you want alerts sent

4. **(Optional) add repo variables** (same page → *Variables*) to change
   defaults without editing code: `PRICE_LIMIT`, `MIN_QTY`, `SECTION_MIN`,
   `SECTION_MAX`, `EVENT_URL`. Skip this and the built-in defaults apply.

5. **Turn on Actions** if prompted, then open the **Actions** tab, pick
   *WC Ticket Alert*, and click **Run workflow** to test it now.

## Checking the test run
- Open the run and read the logs. You want to see a line like
  `Captured N JSON blob(s)` with N greater than 0, and a floor price.
- If it captured 0 blobs, the site likely blocked the browser. Download the
  **debug-screenshot** artifact from the run to see what the page showed.

## Honest limits
- **Anti-bot:** SeatGeek uses DataDome. A real browser gets through far more
  often than a plain script, but a datacenter IP can still get challenged. If
  you see 0 captures repeatedly, that's the cause. Fixes: add the
  `playwright-stealth` package, or run on a different runner/IP.
- **Field names:** the scraper pulls listings by guessing common field names
  (section, price, quantity, splits). If a run captures data but finds 0
  matches when you can see qualifying seats on the site, the field names
  changed — print one captured blob from the logs and adjust `normalize()`.
- **Schedule timing:** GitHub may delay or skip scheduled runs under load, so
  treat 15 minutes as "about every 15-25 minutes," not exact.
- **Floor vs. your seats:** the email lists only true 100-200 level, 4-seat
  matches. The venue floor price is shown for context and may be a cheaper
  upper-deck seat.
- **FIFA official resale** can't be scraped reliably (it needs a login and is
  heavily protected). Use its own site alerts for that source.

## Cost
Free. Public repos get unlimited Actions minutes; a private repo gets 2,000
free minutes/month, and each run takes ~1-2 minutes.
