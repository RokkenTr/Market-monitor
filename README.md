# Market monitor

Watches your stock holdings plus broad market/macro news, filters it through
Claude to cut the noise, and emails you when something's actually relevant.
Runs for free on GitHub Actions — no server, no PC that has to stay on.

**Before you rely on this for real trading decisions**, read the "Limitations"
section at the bottom. This is a fast-notification and summarization tool,
not investment advice, and it can miss things or misfire.

## What it does

- Every ~5 minutes: checks prices and news for your holdings, plus broad
  market/macro searches (Fed, geopolitical tension, etc).
- **Immediate email** if: a holding moves more than 3% in a day, a headline
  contains urgent keywords (war, attack, sanctions, rate emergency, etc), or
  price data updates in a way worth flagging right away.
- **Once-daily digest email** (set for 19:00 Norway time / 17:00 UTC during
  summer — see the comment above `DIGEST_HOURS_UTC` in `monitor.py` for the
  one-line tweak needed when clocks change in winter) for everything else
  that's relevant but not urgent, so routine news doesn't spam you all day.

## One-time setup (~30-45 minutes)

### 1. Create a GitHub repository
- Go to github.com, click "New repository", make it **private**, give it any
  name (e.g. `market-monitor`).
- Upload all the files in this folder to that repo (drag-and-drop on the
  GitHub website works fine, or use `git push` if you're comfortable with git).

### 2. Get an Anthropic API key
- Go to platform.claude.com, sign up if needed, create an API key.
- This is pay-as-you-go, separate from any Claude.ai subscription. Expect
  roughly $1-3/month for this use case (see cost notes below).

### 3. Set up a Gmail app password (for sending you email)
- You need a Gmail account (a new free one dedicated to this is fine).
- Go to your Google Account → Security → 2-Step Verification (must be
  turned on) → App passwords → create one for "Mail".
- Copy the 16-character password it gives you.

### 4. Add secrets to your GitHub repo
In your repo: Settings → Secrets and variables → Actions → New repository secret.
Add these four:

| Secret name | Value |
|---|---|
| `ANTHROPIC_API_KEY` | your Anthropic API key |
| `GMAIL_ADDRESS` | the Gmail address you created |
| `GMAIL_APP_PASSWORD` | the 16-character app password |
| `ALERT_EMAIL_TO` | the email address you want alerts sent to (can be the same Gmail, or your normal inbox) |

### 5. Turn on Actions
- Go to the "Actions" tab in your repo, click "I understand my workflows, go
  ahead and enable them" if prompted.
- The workflow will now run automatically every 15 minutes. You can also
  trigger it manually from the Actions tab (Run workflow button) to test it
  immediately rather than waiting.

### 6. Verify your ticker symbols
Open `monitor.py` and check the `TICKERS` section. For each Nordic-exchange
stock (DNB, Nordea, Kongsberg, Norwegian Air, Bittium), visit
`https://stooq.com/q/?s=<symbol>` (the symbol is in the `"stooq"` field) and
confirm it shows the right company and a sane price. Fix any that don't
resolve — Nordic exchange symbol formats are the part most likely to need a
manual correction, and I can't verify them without live internet access
myself.

## Adjusting it later

- **Price alert threshold**: `PRICE_ALERT_THRESHOLD_PCT` in `monitor.py`.
- **Urgent keywords**: `URGENT_KEYWORDS` list — add/remove terms.
- **Digest timing**: `DIGEST_HOURS_UTC` — these are UTC hours, convert from
  your local timezone.
- **Check frequency**: the cron line in `.github/workflows/monitor.yml`.
  `*/15` means every 15 minutes; GitHub won't reliably go much faster than
  every 5 minutes even if you ask.
- **Add/remove holdings**: edit the `TICKERS` dict.

## Cost

- Price and news checks are free (public data, no API key).
- Claude Haiku calls only happen when something urgent is detected, or twice
  a day for the digest — expect roughly **$1-3/month** for a watchlist this
  size, likely less. You can watch actual spend in your Anthropic console.
- **GitHub Actions minutes — read this one.** At every 5 minutes, that's
  ~8,600 runs/month. Each run (checkout + Python setup + script + commit)
  takes roughly 30-60 seconds, which adds up to somewhere around 4,000-8,000
  minutes/month. That's well **over** the free 2,000 minutes/month private-repo
  tier, and GitHub bills roughly $0.008/minute past that (so a few tens of
  dollars/month if you stay private). Two ways around it:
  - **Make the repo public.** Public repos get unlimited free Actions
    minutes. Nothing in this repo is sensitive — your API keys and email
    password live in encrypted Secrets, never in the code — but your ticker
    list and state.json (headlines/prices, no personal info) would be
    visible to anyone. Most people are fine with this trade-off.
  - **Keep it private and accept the cost**, or dial the interval back up
    (e.g. every 10-15 minutes) if you'd rather stay inside the free tier.

## Limitations, read this

- **Nothing can predict news before it happens.** This catches things fast
  after they're published — typically within the 15-minute check window —
  it can't warn you before a statement is made.
- **Coverage isn't complete.** It relies on Google News RSS and free price
  data; it can miss things, especially fast-moving or non-English coverage,
  and Nordic-market coverage may be thinner than US coverage.
- **The AI analysis is a fast first read, not a forecast.** Treat it as a
  prompt to go look closer yourself, not a trading signal.
- **GitHub Actions scheduling isn't millisecond-precise.** Expect a few
  minutes of jitter, more during high load on GitHub's infrastructure.
- Consider this a monitoring aid, not a system to trade on unattended.
