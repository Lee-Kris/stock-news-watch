#!/usr/bin/env python3
"""Stock news watcher.

Reads tickers from ``tickers.txt``, fetches recent news for each ticker from
CNBC and Yahoo Finance (via Google News RSS scoped to each site), drops
low-content and paywalled headlines, detects articles that have not been seen
before, and emails the new links so they can be opened on a phone.

Already-seen article ids are stored in ``seen.json`` so only genuinely new
articles are ever emailed. The script is designed to run unattended on a
schedule (e.g. GitHub Actions), but also runs locally.

Environment variables:
    GMAIL_USER          Gmail address used to send the mail (SMTP login).
    GMAIL_APP_PASSWORD  Gmail *app password* (not the normal password).
    MAIL_TO             Recipient address. Defaults to GMAIL_USER.
    DRY_RUN             If "1", never sends mail; prints what it would send.
    EMAIL_ON_FIRST_RUN  If "1", email even on the very first run (default: no,
                        the first run only establishes a baseline).
"""

import calendar
import html
import json
import os
import re
import smtplib
import sys
import time
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from urllib.parse import quote_plus

import feedparser

# --- Configuration -----------------------------------------------------------

# (display name, domain) for each news source. Google News is queried scoped
# to each domain so every source returns real, clickable article links.
SOURCES = [
    ("CNBC", "cnbc.com"),
    ("Yahoo Finance", "finance.yahoo.com"),
    # Seeking Alpha는 대부분 유료(Premium/Pro) 기사라 기본 소스에서 제외했습니다.
    # 유료 구독 중이라 다시 보고 싶으면 아래 줄의 맨 앞 '# '를 지우세요.
    # ("Seeking Alpha", "seekingalpha.com"),
]

MAX_TICKERS = 10
TICKERS_FILE = "tickers.txt"
SEEN_FILE = "seen.json"
NOISE_FILTERS_FILE = "noise_filters.txt"
PAYWALL_FILTERS_FILE = "paywall_filters.txt"
TICKER_NAMES_FILE = "ticker_names.txt"
SEEN_RETENTION_DAYS = 30          # forget seen ids older than this
MAX_AGE_HOURS = 24                # only email articles published within this window
MAX_EMAIL_LINKS = 10              # at most this many links per email (spread across tickers)
REQUEST_DELAY_SEC = 1.0           # polite pause between feed fetches
USER_AGENT = "Mozilla/5.0 (compatible; StockNewsWatch/1.0)"

# --- Noise filtering ---------------------------------------------------------
# Low-content "price action / options / clickbait" headlines are skipped so the
# email keeps only substantive news. Phrases below are matched as whole words in
# the title (case-insensitive). Editable via noise_filters.txt (one per line).
DEFAULT_NOISE_FILTERS = [
    "option", "options",              # call/put options, unusual options, options activity
    "call option", "put option",
    "options activity", "options volume", "unusual options",
    "premarket", "pre-market",
    "after hours", "after-hours",
    "mover", "movers",
    "gainers", "losers",
    "here's why", "heres why",
    "what to know",
    "moving average",
    "rsi",
    "technical analysis",
    "price target",
    "stock to watch", "stocks to watch",
    "trending",
    # real-time price / quote / profile pages (not real articles)
    "in real time", "real-time",
    "stock price", "quote & analysis",
    "historical price", "historical prices", "historical data",
    "price history", "closing price",
    # volume / trade-history filler
    "trading volume", "shares traded", "day trading",
    # leveraged / single-stock ETF products (e.g. "Corgi AAPL 2X Daily ETF")
    "leveraged etf", "single-stock etf", "single stock etf", "daily target etf",
]

# Clickbait "Why/What is <stock> up/down ..." price-move framing, always dropped.
PRICE_MOVE_RE = re.compile(
    r"\b(why|what|how)\b.{0,50}\b(stocks?|shares?)\b.{0,50}"
    r"\b(up|down|higher|lower|surg\w*|soar\w*|plung\w*|jump\w*|tumbl\w*|"
    r"rise|rising|rose|ris\w*|fall\w*|fell|climb\w*|slid\w*|slip\w*|"
    r"gain\w*|drop\w*|slump\w*|rally|rallies|sink\w*|spike\w*|"
    r"pop\w*|crash\w*|dip\w*)\b",
    re.IGNORECASE,
)

# Leveraged / hype multipliers like "2X", "1.5x", "3X Daily" — usually leveraged
# single-stock ETF products or "could 2x" hype, not substantive news.
LEVERAGE_RE = re.compile(r"\b\d+(?:\.\d+)?x\b", re.IGNORECASE)


def load_noise_filters(path=NOISE_FILTERS_FILE):
    """Load noise phrases (one per line, # comments) as compiled word-regexes.

    Falls back to DEFAULT_NOISE_FILTERS when the file is missing or empty.
    """
    phrases = []
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as fh:
            for raw in fh:
                line = raw.strip()
                if line and not line.startswith("#"):
                    phrases.append(line)
    if not phrases:
        phrases = list(DEFAULT_NOISE_FILTERS)
    return [re.compile(r"\b" + re.escape(p) + r"\b", re.IGNORECASE) for p in phrases]


def is_noise(title, patterns):
    """True if the title looks like low-content price-action/options filler."""
    if PRICE_MOVE_RE.search(title) or LEVERAGE_RE.search(title):
        return True
    return any(p.search(title) for p in patterns)


# --- Paywall filtering -------------------------------------------------------
# Google News titles end with " - <Publisher>". Articles from hard-paywalled
# publishers (need login / paid subscription to read) are dropped so every
# emailed link is free to read. Matched as whole words in the title
# (case-insensitive). Editable via paywall_filters.txt (one publisher per line).
DEFAULT_PAYWALL_PUBLISHERS = [
    "Seeking Alpha",
    "The Wall Street Journal", "Wall Street Journal", "WSJ",
    "Bloomberg",
    "Barron's", "Barrons",
    "Financial Times",
    "The Economist",
    "The Information",
    "Business Insider",
    "Investor's Business Daily",
    "The New York Times", "New York Times",
    "CNBC Pro",
]


def load_paywall_filters(path=PAYWALL_FILTERS_FILE):
    """Load paywalled-publisher names (one per line, # comments) as regexes.

    Falls back to DEFAULT_PAYWALL_PUBLISHERS when the file is missing or empty.
    """
    phrases = []
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as fh:
            for raw in fh:
                line = raw.strip()
                if line and not line.startswith("#"):
                    phrases.append(line)
    if not phrases:
        phrases = list(DEFAULT_PAYWALL_PUBLISHERS)
    return [re.compile(r"\b" + re.escape(p) + r"\b", re.IGNORECASE) for p in phrases]


def is_paywalled(title, patterns):
    """True if the title's publisher is a known hard-paywalled outlet."""
    return any(p.search(title) for p in patterns)


# --- Relevance filtering -----------------------------------------------------
# Google News matches the ticker anywhere in the article, so a story about a
# different company that merely mentions "AAPL" once gets returned. We keep an
# item only if the ticker symbol OR a known company name appears in the TITLE.
# Company names are read from ticker_names.txt ("TICKER = Name1, Name2").

def load_ticker_names(path=TICKER_NAMES_FILE):
    """Map TICKER -> [company aliases] from 'TICKER = name1, name2' lines."""
    names = {}
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as fh:
            for raw in fh:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                sym, rest = line.split("=", 1)
                aliases = [a.strip() for a in rest.split(",") if a.strip()]
                names[sym.strip().upper()] = aliases
    return names


def is_relevant(title, ticker, names):
    """True if the ticker symbol or a known company alias appears in the title."""
    terms = [ticker] + names.get(ticker, [])
    for term in terms:
        if re.search(r"\b" + re.escape(term) + r"\b", title, re.IGNORECASE):
            return True
    return False


# --- Deduplication & selection -----------------------------------------------

def normalize_title(title):
    """Lowercase, drop trailing ' - Publisher', reduce to alphanumeric words."""
    base = re.sub(r"\s+[-|]\s+[^-|]+$", "", title)      # strip " - Publisher"
    base = re.sub(r"[^a-z0-9]+", " ", base.lower())
    return base.strip()


def select_for_email(new_items, limit):
    """Dedup near-identical stories, then pick up to `limit`, spread across
    tickers and newest-first.

    We cannot see real search volume, so "importance" is approximated by how
    many sources covered the same headline (coverage) — those rank first.
    """
    # Cluster by (ticker, normalized title); keep newest rep, count coverage.
    clusters = {}
    for it in new_items:
        ckey = (it["ticker"], normalize_title(it["title"]))
        cluster = clusters.get(ckey)
        if cluster is None:
            clusters[ckey] = {"item": it, "sources": {it["source"]}}
        else:
            cluster["sources"].add(it["source"])
            if (it.get("published_dt") or "") > (cluster["item"].get("published_dt") or ""):
                cluster["item"] = it

    by_ticker = {}
    for cluster in clusters.values():
        rep = dict(cluster["item"])
        rep["coverage"] = len(cluster["sources"])
        by_ticker.setdefault(rep["ticker"], []).append(rep)
    for reps in by_ticker.values():
        reps.sort(key=lambda r: (r["coverage"], r.get("published_dt") or ""), reverse=True)

    # Round-robin across tickers so no single ticker fills the whole email.
    order = sorted(by_ticker)
    selected, idx, guard = [], 0, 0
    while order and len(selected) < limit:
        reps = by_ticker[order[idx % len(order)]]
        if reps:
            selected.append(reps.pop(0))
        idx += 1
        guard += 1
        if guard > len(order) * (limit + 5):
            break
    return selected


# --- Ticker loading ----------------------------------------------------------

def load_tickers(path=TICKERS_FILE):
    """Read tickers, one per line. Ignores blanks and lines starting with #.

    Uppercases, de-duplicates (keeping order) and caps at MAX_TICKERS.
    """
    if not os.path.exists(path):
        print(f"[error] {path} not found. Create it with one ticker per line.")
        return []

    tickers = []
    with open(path, "r", encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            symbol = line.upper()
            if symbol not in tickers:
                tickers.append(symbol)

    if len(tickers) > MAX_TICKERS:
        print(f"[warn] {len(tickers)} tickers found; using first {MAX_TICKERS}.")
        tickers = tickers[:MAX_TICKERS]
    return tickers


# --- News fetching -----------------------------------------------------------

def google_news_url(ticker, domain):
    """Build a Google News RSS search URL for one ticker scoped to one domain."""
    query = quote_plus(f'"{ticker}" site:{domain}')
    return (
        f"https://news.google.com/rss/search?q={query}"
        "&hl=en-US&gl=US&ceid=US:en"
    )


def entry_published_dt(entry):
    """Return a tz-aware UTC datetime for the entry, or None if unavailable.

    feedparser normalises RSS pubDate into a struct_time in UTC (GMT), so
    calendar.timegm() gives the correct epoch seconds.
    """
    for key in ("published_parsed", "updated_parsed"):
        tm = entry.get(key)
        if tm:
            try:
                return datetime.fromtimestamp(calendar.timegm(tm), tz=timezone.utc)
            except (TypeError, ValueError, OverflowError):
                continue
    return None


def fetch_source(ticker, source_name, domain):
    """Fetch news items for one (ticker, source). Never raises.

    Returns a list of dicts: {key, ticker, source, title, link, published}.
    """
    url = google_news_url(ticker, domain)
    items = []
    try:
        feed = feedparser.parse(url, agent=USER_AGENT)
    except Exception as exc:  # noqa: BLE001 - one bad feed must not kill the run
        print(f"[warn] fetch failed for {ticker}/{source_name}: {exc}")
        return items

    for entry in feed.entries:
        link = entry.get("link", "").strip()
        if not link:
            continue
        key = entry.get("id") or link
        dt = entry_published_dt(entry)
        items.append(
            {
                "key": f"{ticker}::{key}",
                "ticker": ticker,
                "source": source_name,
                "title": entry.get("title", "(no title)").strip(),
                "link": link,
                "published": entry.get("published", ""),
                "published_dt": dt.isoformat() if dt else "",
            }
        )
    return items


def fetch_all(tickers):
    """Fetch every (ticker, source) combination. Returns a flat list of items."""
    all_items = []
    for ticker in tickers:
        for source_name, domain in SOURCES:
            all_items.extend(fetch_source(ticker, source_name, domain))
            time.sleep(REQUEST_DELAY_SEC)
    return all_items


# --- Seen-state persistence --------------------------------------------------

def load_seen(path=SEEN_FILE):
    """Load the {key: iso_timestamp} map of already-seen article ids."""
    if not os.path.exists(path):
        return None  # None signals "never run before"
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            return data
    except (json.JSONDecodeError, OSError) as exc:
        print(f"[warn] could not read {path} ({exc}); treating as first run.")
    return None


def save_seen(seen, path=SEEN_FILE):
    """Persist the seen map, pruning entries older than the retention window."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=SEEN_RETENTION_DAYS)
    pruned = {}
    for key, ts in seen.items():
        try:
            when = datetime.fromisoformat(ts)
        except (ValueError, TypeError):
            when = datetime.now(timezone.utc)  # keep malformed entries for now
        if when >= cutoff:
            pruned[key] = ts
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(pruned, fh, ensure_ascii=False, indent=2, sort_keys=True)


# --- Email -------------------------------------------------------------------

def build_email_html(new_items):
    """Render new items grouped by ticker then source into an HTML body."""
    by_ticker = {}
    for item in new_items:
        by_ticker.setdefault(item["ticker"], {}).setdefault(
            item["source"], []
        ).append(item)

    parts = [
        "<div style='font-family:-apple-system,Segoe UI,Roboto,sans-serif;"
        "font-size:15px;color:#111;line-height:1.5'>",
        f"<h2 style='margin:0 0 12px'>📈 New stock news "
        f"({len(new_items)} link{'s' if len(new_items) != 1 else ''})</h2>",
    ]
    for ticker in sorted(by_ticker):
        parts.append(
            f"<h3 style='margin:20px 0 6px;color:#0b57d0'>{html.escape(ticker)}</h3>"
        )
        for source in sorted(by_ticker[ticker]):
            parts.append(
                f"<div style='font-weight:600;margin:8px 0 2px;color:#555'>"
                f"{html.escape(source)}</div><ul style='margin:0 0 4px;padding-left:20px'>"
            )
            for item in by_ticker[ticker][source]:
                title = html.escape(item["title"])
                link = html.escape(item["link"], quote=True)
                when = html.escape(item["published"]) if item["published"] else ""
                meta = f" <span style='color:#999;font-size:12px'>{when}</span>" if when else ""
                parts.append(
                    f"<li style='margin:4px 0'>"
                    f"<a href='{link}' style='color:#0b57d0;text-decoration:none'>{title}</a>"
                    f"{meta}</li>"
                )
            parts.append("</ul>")
    parts.append(
        "<p style='color:#999;font-size:12px;margin-top:24px'>"
        "Sent by stock-news-watch. Reply-free automated digest.</p></div>"
    )
    return "\n".join(parts)


def send_email(subject, html_body):
    """Send the digest via Gmail SMTP over SSL. Returns True on success."""
    user = os.environ.get("GMAIL_USER", "").strip()
    password = os.environ.get("GMAIL_APP_PASSWORD", "").strip()
    recipient = os.environ.get("MAIL_TO", "").strip() or user

    if os.environ.get("DRY_RUN") == "1":
        print(f"[dry-run] would send to {recipient or '(unset)'}: {subject}")
        return True

    if not user or not password:
        print("[error] GMAIL_USER / GMAIL_APP_PASSWORD not set; cannot send mail.")
        return False

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = user
    msg["To"] = recipient
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as server:
            server.login(user, password)
            server.sendmail(user, [recipient], msg.as_string())
        print(f"[ok] emailed {recipient}: {subject}")
        return True
    except smtplib.SMTPException as exc:
        print(f"[error] failed to send mail: {exc}")
        return False


# --- Main --------------------------------------------------------------------

def main():
    tickers = load_tickers()
    if not tickers:
        print("[error] no tickers to watch. Exiting.")
        return 1
    print(f"[info] watching {len(tickers)} ticker(s): {', '.join(tickers)}")

    items = fetch_all(tickers)
    print(f"[info] fetched {len(items)} article link(s) across all sources.")

    patterns = load_noise_filters()
    kept = [it for it in items if not is_noise(it["title"], patterns)]
    dropped = len(items) - len(kept)
    if dropped:
        print(
            f"[info] filtered out {dropped} low-content item(s) "
            f"(options/price-action/clickbait); {len(kept)} remain."
        )
    items = kept

    paywall_patterns = load_paywall_filters()
    free_items = [it for it in items if not is_paywalled(it["title"], paywall_patterns)]
    blocked = len(items) - len(free_items)
    if blocked:
        print(
            f"[info] filtered out {blocked} paywalled item(s) "
            f"(Seeking Alpha/WSJ/Bloomberg/etc.); {len(free_items)} remain."
        )
    items = free_items

    # Only keep articles published within the recent window so old articles
    # (e.g. history dumped when a new ticker is added) are never emailed.
    try:
        max_age_hours = int(os.environ.get("MAX_AGE_HOURS", str(MAX_AGE_HOURS)))
    except ValueError:
        max_age_hours = MAX_AGE_HOURS
    cutoff = datetime.now(timezone.utc) - timedelta(hours=max_age_hours)
    recent, undated = [], 0
    for it in items:
        stamp = it.get("published_dt")
        if not stamp:
            undated += 1
            recent.append(it)  # undated is rare for Google News; keep to be safe
            continue
        try:
            when = datetime.fromisoformat(stamp)
        except ValueError:
            recent.append(it)
            continue
        if when >= cutoff:
            recent.append(it)
    old = len(items) - len(recent)
    if old:
        print(
            f"[info] filtered out {old} article(s) older than {max_age_hours}h; "
            f"{len(recent)} remain."
        )
    if undated:
        print(f"[info] {undated} article(s) had no publish date; kept them.")
    items = recent

    # Keep only articles that actually mention the ticker/company in the title,
    # so e.g. an AAPL search does not surface a Micron or SpaceX story.
    names = load_ticker_names()
    relevant = [it for it in items if is_relevant(it["title"], it["ticker"], names)]
    off_topic = len(items) - len(relevant)
    if off_topic:
        print(
            f"[info] filtered out {off_topic} off-topic item(s) "
            f"(ticker/company not in title); {len(relevant)} remain."
        )
    items = relevant

    seen = load_seen()
    first_run = seen is None
    if first_run:
        seen = {}

    now_iso = datetime.now(timezone.utc).isoformat()
    new_items = []
    for item in items:
        if item["key"] not in seen:
            new_items.append(item)
        seen[item["key"]] = seen.get(item["key"], now_iso)

    # Always persist state so the next run knows what we have seen.
    save_seen(seen)

    if first_run and os.environ.get("EMAIL_ON_FIRST_RUN") != "1":
        print(
            f"[info] first run: recorded {len(items)} article(s) as baseline. "
            "No email sent (future runs email only NEW articles)."
        )
        return 0

    if not new_items:
        print("[info] no new articles since last run. Nothing to email.")
        return 0

    try:
        max_links = int(os.environ.get("MAX_EMAIL_LINKS", str(MAX_EMAIL_LINKS)))
    except ValueError:
        max_links = MAX_EMAIL_LINKS
    total_new = len(new_items)
    new_items = select_for_email(new_items, max_links)
    if total_new > len(new_items):
        print(
            f"[info] {total_new} new item(s) after filters; trimmed to "
            f"{len(new_items)} (<= {max_links}), spread across tickers, newest first."
        )

    print(f"[info] {len(new_items)} new article(s) -> sending email.")
    subject = f"📈 {len(new_items)} new stock news link(s): {', '.join(sorted({i['ticker'] for i in new_items}))}"
    body = build_email_html(new_items)
    ok = send_email(subject, body)
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main())
