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
    "Alphabet (GOOGL)":       {"stooq": "googl.us",  "query": "Alphabet Google",        "sector": "Tech",     "finnhub": "GOOGL"},
    "Amazon":                 {"stooq": "amzn.us",   "query": "Amazon.com",             "sector": "Tech",     "finnhub": "AMZN"},
    "Apple":                  {"stooq": "aapl.us",   "query": "Apple Inc",              "sector": "Tech",     "finnhub": "AAPL"},
    "Bittium":                {"stooq": "jot.def",   "query": "Bittium Oyj",            "sector": "Forsvar",  "finnhub": "BITTI.HE"},
    "CoreWeave":              {"stooq": "crwv.us",   "query": "CoreWeave",              "sector": "Tech",     "finnhub": "CRWV"},
    "DNB Bank":                {"stooq": "0o84.uk",   "query": "DNB Bank ASA",           "sector": "Bank",     "finnhub": "DNB.OL"},
    "Kongsberg Gruppen":      {"stooq": "0f08.uk",   "query": "Kongsberg Gruppen",      "sector": "Forsvar",  "finnhub": "KOG.OL"},
    # Note: stooq's listing (z4q.def) quotes this in EUR, not NOK like Nordnet.
    # The "currency" field below tells the script to auto-convert to NOK when
    # displaying the price in alerts (percent-change math is unaffected either way).
    "Kongsberg Maritime":     {"stooq": "z4q.def",   "query": "Kongsberg Maritime",     "sector": "Shipping", "finnhub": "KMAR.OL", "currency": "EUR"},
    "Nordea Bank":             {"stooq": "04q.def",   "query": "Nordea Bank",            "sector": "Bank",     "finnhub": "NDA-FI.HE"},
    "Norwegian Air Shuttle":  {"stooq": "0fgh.uk",   "query": "Norwegian Air Shuttle",  "sector": "Shipping", "finnhub": "NAS.OL"},
    "Nvidia":                  {"stooq": "nvda.us",   "query": "Nvidia Jensen Huang",    "sector": "Tech",     "finnhub": "NVDA"},
    "SailPoint":               {"stooq": "sail.us",   "query": "SailPoint",              "sector": "Tech",     "finnhub": "SAIL"},
}

# Sector peers — NOT owned, just candidates worth keeping an eye on within
# the same sectors as your holdings. These are a starting point, not a
# recommendation to buy anything; edit freely. Same caveat as above: verify
# stooq/finnhub symbols before trusting the data, especially non-US ones.
CANDIDATE_TICKERS = {
    "AMD":                 {"stooq": "amd.us",    "query": "AMD chips",              "sector": "Tech",     "finnhub": "AMD"},
    "Microsoft":           {"stooq": "msft.us",   "query": "Microsoft",              "sector": "Tech",     "finnhub": "MSFT"},
    "RTX Corporation":     {"stooq": "rtx.us",    "query": "RTX Corporation defense", "sector": "Forsvar",  "finnhub": "RTX"},
    "Saab AB":             {"stooq": "saab-b.st", "query": "Saab AB defense",         "sector": "Forsvar",  "finnhub": "SAAB-B.ST"},
    "Frontline":           {"stooq": "fro.us",    "query": "Frontline shipping",      "sector": "Shipping", "finnhub": "FRO"},
    "Golden Ocean Group":  {"stooq": "gogl.us",   "query": "Golden Ocean Group",      "sector": "Shipping", "finnhub": "GOGL"},
    "Handelsbanken":       {"stooq": "shb-a.st",  "query": "Svenska Handelsbanken",   "sector": "Bank",     "finnhub": "SHB-A.ST"},
    "Danske Bank":         {"stooq": "danske.cp", "query": "Danske Bank",             "sector": "Bank",     "finnhub": "DANSKE.CO"},
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
NTFY_TOPIC = os.environ.get("NTFY_TOPIC")  # push notifications via ntfy.sh
FINNHUB_API_KEY = os.environ.get("FINNHUB_API_KEY")  # optional; free key from finnhub.io — enables key figures (P/E, growth, ROE)

# ---------------------------------------------------------------------------
# STATE
# ---------------------------------------------------------------------------

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r") as f:
            state = json.load(f)
    else:
        state = {}
    state.setdefault("seen_links", [])
    state.setdefault("pending_digest", [])
    state.setdefault("pending_candidate_digest", [])
    state.setdefault("last_digest_date", None)
    state.setdefault("last_prices", {})
    return state


def save_state(state):
    # keep seen_links from growing forever
    state["seen_links"] = state["seen_links"][-500:]
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


# ---------------------------------------------------------------------------
# DATA FETCHING (all free, no API key needed)
# ---------------------------------------------------------------------------

def fetch_fx_rate(pair):
    """E.g. pair='eurnok'. Free, no key needed, same stooq CSV endpoint."""
    try:
        url = f"https://stooq.com/q/l/?s={pair}&f=sd2t2ohlcv&h&e=csv"
        with urllib.request.urlopen(url, timeout=10) as r:
            text = r.read().decode("utf-8")
        lines = text.strip().split("\n")
        if len(lines) < 2:
            return None
        headers = lines[0].split(",")
        vals = lines[1].split(",")
        close = float(vals[headers.index("Close")])
        return close if close > 0 else None
    except Exception:
        return None


def format_price_with_conversion(price, currency):
    """Returns a display string, converting to NOK if the ticker's native
    stooq listing is in a foreign currency (currently only relevant for
    Kongsberg Maritime's EUR-quoted German listing)."""
    if not currency or currency == "NOK":
        return f"{price:.2f} NOK"
    rate = fetch_fx_rate(f"{currency.lower()}nok")
    if rate:
        return f"{price:.2f} {currency} (≈{price * rate:.2f} NOK)"
    return f"{price:.2f} {currency}"


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


def fetch_fundamentals(finnhub_symbol):
    """Free-tier key figures from Finnhub. Returns None if no key configured
    or the symbol isn't covered. Not real-time, updated periodically by Finnhub."""
    if not FINNHUB_API_KEY:
        return None
    import urllib.parse
    sym = urllib.parse.quote(finnhub_symbol)
    url = f"https://finnhub.io/api/v1/stock/metric?symbol={sym}&metric=all&token={FINNHUB_API_KEY}"
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            data = json.loads(r.read().decode("utf-8"))
        m = data.get("metric", {})
        if not m:
            return None
        return {
            "pe": m.get("peBasicExclExtraTTM"),
            "revenue_growth_pct": m.get("revenueGrowthTTMYoy"),
            "eps_growth_pct": m.get("epsGrowthTTMYoy"),
            "roe_pct": m.get("roeTTM"),
            "52w_high": m.get("52WeekHigh"),
            "52w_low": m.get("52WeekLow"),
        }
    except Exception:
        return None


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


def analyze_digest(items, fundamentals, candidate_items, candidate_fundamentals):
    def fmt_fundamentals(f):
        if not f:
            return "no key figures available"
        parts = []
        if f.get("pe") is not None: parts.append(f"P/E {f['pe']:.1f}")
        if f.get("revenue_growth_pct") is not None: parts.append(f"revenue growth {f['revenue_growth_pct']:.1f}%")
        if f.get("eps_growth_pct") is not None: parts.append(f"EPS growth {f['eps_growth_pct']:.1f}%")
        if f.get("roe_pct") is not None: parts.append(f"ROE {f['roe_pct']:.1f}%")
        return ", ".join(parts) if parts else "no key figures available"

    holdings_block = "\n".join(f"- [{it['ticker']}] {it['title']}" for it in items) or "(no fresh headlines)"
    fundamentals_block = "\n".join(f"- {name}: {fmt_fundamentals(f)}" for name, f in fundamentals.items())
    candidates_news_block = "\n".join(f"- [{it['ticker']}] {it['title']}" for it in candidate_items) or "(no fresh headlines)"
    candidates_fundamentals_block = "\n".join(f"- {name}: {fmt_fundamentals(f)}" for name, f in candidate_fundamentals.items())

    prompt = (
        "You are a measured market-briefing assistant for a retail investor. "
        "You are NOT a financial advisor and must not recommend buying or "
        "selling anything — just summarize facts and describe sentiment from "
        "the news you're given. If key figures are missing, say so plainly "
        "rather than guessing. Structure your reply in two clearly labeled "
        "sections:\n\n"
        "1. 'Your holdings' — for each stock with fresh news or notable key "
        "figures, one short paragraph: what happened, the key figures if "
        "available, and the general tone of recent coverage (positive/mixed/"
        "negative), if it can be reasonably assessed from the news given.\n\n"
        "2. 'Sector candidates to watch' — same format, for the peer stocks "
        "listed, framed as 'worth knowing about', not as suggestions to buy.\n\n"
        "Be concise — a few sentences per stock, not paragraphs. If nothing "
        "notable happened for a given stock, skip it rather than padding.\n\n"
        f"--- Your holdings: recent headlines ---\n{holdings_block}\n\n"
        f"--- Your holdings: key figures ---\n{fundamentals_block}\n\n"
        f"--- Sector candidates: recent headlines ---\n{candidates_news_block}\n\n"
        f"--- Sector candidates: key figures ---\n{candidates_fundamentals_block}\n"
    )
    return call_claude(prompt, max_tokens=1200)


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


def send_push(title, body):
    if not NTFY_TOPIC:
        return
    try:
        req = urllib.request.Request(
            f"https://ntfy.sh/{NTFY_TOPIC}",
            data=body.encode("utf-8"),
            headers={
                "Title": title.encode("ascii", "ignore").decode(),  # headers must be ASCII
                "Priority": "default",
            },
            method="POST",
        )
        urllib.request.urlopen(req, timeout=10)
        print(f"Push sent: {title}")
    except Exception as e:
        print(f"Push send failed: {e}")


def notify(subject, body):
    send_email(subject, body)
    send_push(subject, body)


def contains_urgent_keyword(text):
    t = text.lower()
    return any(kw in t for kw in URGENT_KEYWORDS)


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------

def main():
    state = load_state()
    now = datetime.now(timezone.utc)

    # 1. Price checks -> immediate alert on big moves (owned holdings only)
    for name, info in TICKERS.items():
        if not info.get("stooq"):
            continue  # no price source configured for this one (e.g. Bittium)
        result = fetch_price(info["stooq"])
        if not result:
            continue
        state["last_prices"][name] = result["price"]
        if abs(result["change_pct"]) >= PRICE_ALERT_THRESHOLD_PCT:
            key = f"{name}-{round(result['change_pct'])}"
            if state.get("last_price_alert_key_" + name) != key:
                price_str = format_price_with_conversion(result["price"], info.get("currency"))
                move_desc = f"{name} is {'up' if result['change_pct']>=0 else 'down'} {abs(result['change_pct']):.1f}% today ({price_str})"
                analysis = analyze_urgent(move_desc, "Price movement in a stock the user owns.")
                notify(f"Price alert: {name}", move_desc + ("\n\n" + analysis if analysis else ""))
                state["last_price_alert_key_" + name] = key

    # 2. News checks — your holdings (can trigger urgent alerts) + macro
    all_queries = [(name, info["query"]) for name, info in TICKERS.items()]
    all_queries += [("MARKET", q) for q in MACRO_QUERIES]

    for ticker, query in all_queries:
        for item in fetch_news(query, max_items=5):
            if item["link"] in state["seen_links"]:
                continue
            state["seen_links"].append(item["link"])

            if contains_urgent_keyword(item["title"]):
                analysis = analyze_urgent(item["title"], f"Related to: {ticker}")
                notify(f"Urgent: {ticker}", item["title"] + "\n" + item["link"] + ("\n\n" + analysis if analysis else ""))
            else:
                state["pending_digest"].append({"ticker": ticker, "title": item["title"], "link": item["link"]})

    # 2b. News checks — sector candidates (never urgent, always goes to digest)
    for name, info in CANDIDATE_TICKERS.items():
        for item in fetch_news(info["query"], max_items=3):
            if item["link"] in state["seen_links"]:
                continue
            state["seen_links"].append(item["link"])
            state["pending_candidate_digest"].append({"ticker": name, "title": item["title"], "link": item["link"]})

    # 3. Digest — once a day, at the configured hour
    today_key = now.strftime("%Y-%m-%d")
    if now.hour in DIGEST_HOURS_UTC and state.get("last_digest_date") != f"{today_key}-{now.hour}":
        if state["pending_digest"] or state["pending_candidate_digest"]:
            # Fundamentals are only fetched once a day, at digest time, to
            # keep API usage light — they don't change minute to minute anyway.
            fundamentals = {name: fetch_fundamentals(info["finnhub"]) for name, info in TICKERS.items()}
            candidate_fundamentals = {name: fetch_fundamentals(info["finnhub"]) for name, info in CANDIDATE_TICKERS.items()}

            summary = analyze_digest(
                state["pending_digest"], fundamentals,
                state["pending_candidate_digest"], candidate_fundamentals,
            )
            if not summary:
                # No API key configured yet, or the call failed — fall back
                # to a plain list so nothing gets silently dropped.
                lines = ["Your holdings:"]
                lines += [f"- [{it['ticker']}] {it['title']}\n  {it['link']}" for it in state["pending_digest"]]
                lines.append("\nSector candidates:")
                lines += [f"- [{it['ticker']}] {it['title']}\n  {it['link']}" for it in state["pending_candidate_digest"]]
                summary = "\n".join(lines)
            notify("Market digest", summary)
            state["pending_digest"] = []
            state["pending_candidate_digest"] = []
        state["last_digest_date"] = f"{today_key}-{now.hour}"

    save_state(state)


if __name__ == "__main__":
    main()
