"""
Personal market monitor.
Checks prices + news for your holdings and the broader market, uses Claude
to filter out noise, and emails you when something is actually relevant.

Runs standalone (e.g. `python monitor.py`) and is designed to be triggered
on a schedule by GitHub Actions (see .github/workflows/monitor.yml).

State (which news you've already seen, pending digest items, etc.) is
persisted to state.json, which the GitHub Actions workflow commits back to
the repo after each run so it carries over between runs.
"""

import os
import json
import smtplib
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.mime.text import MIMEText

# ---------------------------------------------------------------------------
# CONFIG — edit this section for your own holdings and preferences
# ---------------------------------------------------------------------------

# Your holdings. "stooq" is the price-lookup symbol on stooq.com/q/l (free,
# no API key). "query" is what gets searched on Google News for this stock.
#
# IMPORTANT: Nordic-exchange stooq symbols below are best-effort guesses.
# Before relying on this, verify each one by visiting:
#   https://stooq.com/q/?s=<symbol>
# and confirming it shows the right company and a sane price. Fix any that
# are wrong — if a symbol is bad the script will just skip price data for
# that ticker (news search will still work regardless).
TICKERS = {
    "Alphabet (GOOGL)":       {"stooq": "googl.us",  "query": "Alphabet Google"},
    "Amazon":                 {"stooq": "amzn.us",   "query": "Amazon.com"},
    "Apple":                  {"stooq": "aapl.us",   "query": "Apple Inc"},
    "Bittium":                {"stooq": "bitti.he",  "query": "Bittium Oyj"},
    "CoreWeave":              {"stooq": "crwv.us",   "query": "CoreWeave"},
    "DNB Bank":                {"stooq": "dnb.ol",    "query": "DNB Bank ASA"},
    "Kongsberg Gruppen":      {"stooq": "kog.ol",    "query": "Kongsberg Gruppen"},
    # Kongsberg Maritime is a division of Kongsberg Gruppen, not its own
    # listing, so it shares the same price symbol but gets its own news search.
    "Kongsberg Maritime":     {"stooq": "kog.ol",    "query": "Kongsberg Maritime"},
    "Nordea Bank":             {"stooq": "nda-se.st", "query": "Nordea Bank"},
    "Norwegian Air Shuttle":  {"stooq": "nas.ol",    "query": "Norwegian Air Shuttle"},
    "Nvidia":                  {"stooq": "nvda.us",   "query": "Nvidia Jensen Huang"},
    "SailPoint":               {"stooq": "sail.us",   "query": "SailPoint"},
}

# Broad market / macro news searches, run alongside your holdings.
MACRO_QUERIES = [
    "stock market today",
    "Federal Reserve interest rate",
    "geopolitical tensions markets",
    "market sell off",
]

# If a fresh macro or company headline contains any of these (case-insensitive),
# it's treated as urgent and alerted immediately instead of waiting for the digest.
URGENT_KEYWORDS = [
    "attack", "invasion", "invade", "military strike", "airstrike", "missile",
    "declares war", "war on", "troops", "nuclear", "sanctions", "emergency rate",
    "rate hike", "rate cut", "recession", "tariff", "martial law", "coup",
]

PRICE_ALERT_THRESHOLD_PCT = 3.0   # immediate alert if a holding moves this much
# 17:00 UTC = 19:00 in Norway during CEST (summer, UTC+2). Norway switches to
# CET (UTC+1) in winter, which would shift this to 18:00 local time — if you
# want it to stay at 19:00 local year-round, change this to 18 around late
# October and back to 17 around late March (when clocks change).
DIGEST_HOURS_UTC = [17]

STATE_FILE = os.path.join(os.path.dirname(__file__), "state.json")

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
GMAIL_ADDRESS = os.environ.get("GMAIL_ADDRESS")
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD")
ALERT_EMAIL_TO = os.environ.get("ALERT_EMAIL_TO", GMAIL_ADDRESS)

# ---------------------------------------------------------------------------
# STATE
# ---------------------------------------------------------------------------

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    return {"seen_links": [], "pending_digest": [], "last_digest_date": None, "last_prices": {}}


def save_state(state):
    # keep seen_links from growing forever
    state["seen_links"] = state["seen_links"][-500:]
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


# ---------------------------------------------------------------------------
# DATA FETCHING (all free, no API key needed)
# ---------------------------------------------------------------------------

def fetch_price(symbol):
    url = f"https://stooq.com/q/l/?s={symbol}&f=sd2t2ohlcv&h&e=csv"
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            text = r.read().decode("utf-8")
        lines = text.strip().split("\n")
        if len(lines) < 2:
            return None
        headers = lines[0].split(",")
        vals = lines[1].split(",")
        close = float(vals[headers.index("Close")])
        open_ = float(vals[headers.index("Open")])
        if close <= 0 or open_ <= 0:
            return None
        change_pct = (close - open_) / open_ * 100
        return {"price": close, "change_pct": change_pct}
    except Exception:
        return None


def fetch_news(query, max_items=5):
    import urllib.parse
    q = urllib.parse.quote(query)
    url = f"https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en"
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            data = r.read()
        root = ET.fromstring(data)
        items = []
        for item in root.findall(".//item")[:max_items]:
            title = item.findtext("title") or ""
            link = item.findtext("link") or ""
            pub = item.findtext("pubDate") or ""
            items.append({"title": title, "link": link, "pubDate": pub})
        return items
    except Exception:
        return []


# ---------------------------------------------------------------------------
# AI FILTERING (Claude Haiku — cheap, only called a few times per run)
# ---------------------------------------------------------------------------

def call_claude(prompt, max_tokens=600):
    if not ANTHROPIC_API_KEY:
        return None
    body = json.dumps({
        "model": "claude-haiku-4-5-20251001",
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}],
    }).encode("utf-8")
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=body,
        headers={
            "Content-Type": "application/json",
            "x-api-key": ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.loads(r.read().decode("utf-8"))
        parts = [b["text"] for b in data.get("content", []) if b.get("type") == "text"]
        return "\n".join(parts).strip()
    except Exception as e:
        print(f"Claude API call failed: {e}")
        return None


def analyze_urgent(headline_or_move, context):
    prompt = (
        "You are a terse market-alert assistant for a retail investor. "
        "In 2-3 sentences, explain why this might matter for markets or their "
        "holdings, and which direction it could plausibly push things. Be "
        "measured — don't overstate certainty. No preamble.\n\n"
        f"Event: {headline_or_move}\nContext: {context}"
    )
    return call_claude(prompt, max_tokens=200)


def analyze_digest(items):
    listing = "\n".join(f"- [{it['ticker']}] {it['title']}" for it in items)
    prompt = (
        "You are a terse market-news assistant for a retail investor who owns "
        "the stocks mentioned. Below are headlines gathered over the last "
        "several hours. Pick out ONLY the ones genuinely likely to matter for "
        "their holdings or the broader market — ignore routine noise, "
        "opinion pieces, and repetitive coverage. For each one you keep, give "
        "one short line: the ticker, what happened, and likely relevance. If "
        "nothing is genuinely relevant, just say so in one line.\n\n"
        f"{listing}"
    )
    return call_claude(prompt, max_tokens=500)


# ---------------------------------------------------------------------------
# EMAIL
# ---------------------------------------------------------------------------

def send_email(subject, body):
    if not (GMAIL_ADDRESS and GMAIL_APP_PASSWORD and ALERT_EMAIL_TO):
        print("Email not configured — skipping send. Would have sent:")
        print(subject)
        print(body)
        return
    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = GMAIL_ADDRESS
    msg["To"] = ALERT_EMAIL_TO
    try:
        with smtplib.SMTP("smtp.gmail.com", 587) as server:
            server.starttls()
            server.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
            server.send_message(msg)
        print(f"Email sent: {subject}")
    except Exception as e:
        print(f"Email send failed: {e}")


def contains_urgent_keyword(text):
    t = text.lower()
    return any(kw in t for kw in URGENT_KEYWORDS)


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------

def main():
    state = load_state()
    now = datetime.now(timezone.utc)

    # 1. Price checks -> immediate alert on big moves
    for name, info in TICKERS.items():
        result = fetch_price(info["stooq"])
        if not result:
            continue
        prev = state["last_prices"].get(name)
        state["last_prices"][name] = result["price"]
        if abs(result["change_pct"]) >= PRICE_ALERT_THRESHOLD_PCT:
            key = f"{name}-{round(result['change_pct'])}"
            if state.get("last_price_alert_key_" + name) != key:
                move_desc = f"{name} is {'up' if result['change_pct']>=0 else 'down'} {abs(result['change_pct']):.1f}% today (${result['price']:.2f})"
                analysis = analyze_urgent(move_desc, "Price movement in a stock the user owns.")
                send_email(f"Price alert: {name}", move_desc + ("\n\n" + analysis if analysis else ""))
                state["last_price_alert_key_" + name] = key

    # 2. News checks — company-specific + macro
    all_queries = [(name, info["query"]) for name, info in TICKERS.items()]
    all_queries += [("MARKET", q) for q in MACRO_QUERIES]

    for ticker, query in all_queries:
        for item in fetch_news(query, max_items=5):
            if item["link"] in state["seen_links"]:
                continue
            state["seen_links"].append(item["link"])

            if contains_urgent_keyword(item["title"]):
                analysis = analyze_urgent(item["title"], f"Related to: {ticker}")
                send_email(f"Urgent: {ticker}", item["title"] + "\n" + item["link"] + ("\n\n" + analysis if analysis else ""))
            else:
                state["pending_digest"].append({"ticker": ticker, "title": item["title"], "link": item["link"]})

    # 3. Digest — once a day, at the configured hour, if there's anything
    # pending and we haven't already sent one today
    today_key = now.strftime("%Y-%m-%d")
    if now.hour in DIGEST_HOURS_UTC and state.get("last_digest_date") != f"{today_key}-{now.hour}":
        if state["pending_digest"]:
            summary = analyze_digest(state["pending_digest"])
            if not summary:
                # No API key configured yet, or the call failed — fall back
                # to a plain list so nothing gets silently dropped.
                summary = "\n".join(
                    f"- [{it['ticker']}] {it['title']}\n  {it['link']}"
                    for it in state["pending_digest"]
                )
            send_email("Market digest", summary)
            state["pending_digest"] = []
        state["last_digest_date"] = f"{today_key}-{now.hour}"

    save_state(state)


if __name__ == "__main__":
    main()
