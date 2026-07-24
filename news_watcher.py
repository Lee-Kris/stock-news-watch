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
    GEMINI_API_KEY      Google Gemini API key (free tier; GOOGLE_API_KEY also works).
                        If set, each ticker's news is summarized in Korean and shown
                        above its links. If unset, links only.
    SUMMARIZE           If "0", skip AI summaries even when a key is present.
    BLOGGER_CLIENT_ID       Google OAuth client id (Blogger API v3).
    BLOGGER_CLIENT_SECRET   Google OAuth client secret.
    BLOGGER_REFRESH_TOKEN   OAuth refresh token with the blogger scope.
    BLOGGER_BLOG_ID         Numeric blog id (or set BLOGGER_BLOG_URL instead).
    BLOGGER_BLOG_URL        Blog URL (e.g. https://xxx.blogspot.com); the id is
                            resolved from it when BLOGGER_BLOG_ID is unset.
                            When all Blogger vars are set, the same briefing is
                            auto-published as a post titled "YYYY년 M월 D일 포트폴리오 뉴스".
    FORCE_SEND          If "1", email/post the recent articles even if already
                        seen (on-demand test send).
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
import urllib.request
from urllib.parse import quote_plus, urlencode

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
MAX_AGE_HOURS = 28                # only email articles published within this window (once-daily run + buffer)
MAX_PER_TICKER = 20               # at most this many links per ticker per email
SUMMARY_MODEL = "gemini-flash-lite-latest"  # Google Gemini model (free tier) for summaries
# Fallbacks tried in order. Use only "Flash Lite" models: their free-tier RPM
# limit is 10-15 vs plain "Flash" (only 5), so they are far less likely to 429.
SUMMARY_FALLBACK_MODELS = ["gemini-2.5-flash-lite", "gemini-2.0-flash-lite"]
SUMMARY_MAX_TOKENS = 700            # cap on the summary length per ticker
# Set when every Gemini attempt fails, so the email can say why (not "no key").
LAST_SUMMARY_ERROR = None
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

# Options / derivatives talk: bare "options", call/put near an options word,
# "put/call", "$250 calls", a title mentioning both calls and puts, etc.
OPTIONS_RE = re.compile(
    r"\boptions?\b"
    r"|\b(?:calls?|puts?)\b.{0,15}\b(?:option|options|contract|strike|expir\w*|premium)\b"
    r"|\b(?:option|options|strike|expir\w*)\b.{0,15}\b(?:calls?|puts?)\b"
    r"|\bput[-/ ]?call\b|\bcall[-/ ]?put\b"
    r"|\b(?:calls?)\b.{0,20}\b(?:puts?)\b|\b(?:puts?)\b.{0,20}\b(?:calls?)\b"
    r"|\$\d[\d.,]*\s?(?:calls?|puts?)\b"
    r"|\bstrike price\b",
    re.IGNORECASE,
)

# Price / quote / valuation talk (시세·가격 얘기), not substantive news.
PRICE_TALK_RE = re.compile(
    r"\b(?:share|stock|target)\s+price\b"
    r"|\bprice\s+(?:target|prediction|forecast|analysis)\b"
    r"|\bhistorical (?:price|prices|data)\b"
    r"|\bprice history\b|\bclosing price\b"
    r"|\bstock quote\b|\bquote & analysis\b"
    r"|\bin real[- ]time\b"
    r"|\b(?:overvalued|undervalued|valuation|fair value)\b"
    r"|\bbuy or sell\b|\bworth buying\b",
    re.IGNORECASE,
)


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
    if (PRICE_MOVE_RE.search(title) or LEVERAGE_RE.search(title)
            or OPTIONS_RE.search(title) or PRICE_TALK_RE.search(title)):
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


def select_for_email(new_items, per_ticker_limit):
    """Dedup near-identical stories, then cap each ticker to `per_ticker_limit`.

    Within a ticker, stories covered by more sources rank first (a rough proxy
    for importance, since the free RSS has no real search-volume data), then
    newest first.
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

    selected = []
    for ticker in sorted(by_ticker):
        reps = by_ticker[ticker]
        reps.sort(key=lambda r: (r["coverage"], r.get("published_dt") or ""), reverse=True)
        selected.extend(reps[:per_ticker_limit])
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


# --- AI summary --------------------------------------------------------------
# For each ticker with fresh news, ask Google Gemini to summarize the headlines
# in Korean. Gemini has a free tier (Google AI Studio key, no billing needed),
# called here via the plain REST API so there is no extra dependency.
# Best-effort: any failure (no key, network, API error) falls back to a
# links-only email, so the digest is never blocked on the LLM.

SUMMARY_SYSTEM = (
    "You are a news-desk anchor delivering a short market briefing in Korean. "
    "Given a stock ticker and today's news headlines for it, write a brief "
    "anchor-style news briefing as flowing Korean prose (2-4 sentences, NOT bullet "
    "points), as if reading the day's developments for that stock on air. Cover only "
    "substantive developments (earnings, products, deals, guidance, regulation, "
    "analyst actions); merge redundant headlines into one narrative; use a factual, "
    "neutral, professional broadcast tone; no investment advice and no price "
    "speculation. Open by naming the company/ticker naturally. Output only the "
    "Korean briefing text, nothing else."
)

# Batch variant: one call summarizes ALL tickers at once (keeps free-tier
# usage to a single request per run, avoiding the daily 429 quota).
SUMMARY_BATCH_SYSTEM = (
    "You are a news-desk anchor writing short Korean market briefings. For each "
    "stock ticker given (with its headlines), write a brief anchor-style briefing "
    "in flowing Korean prose (2-4 sentences, not bullet points), covering only "
    "substantive developments (earnings, products, deals, guidance, regulation, "
    "analyst actions); factual and neutral; no investment advice or price "
    "speculation. Respond with a single JSON object that maps each ticker "
    "(uppercase) to its Korean briefing string, and nothing else."
)


def _gemini_generate(model, api_key, user_text):
    """One Gemini generateContent call. Returns the text (may be ""), or raises."""
    body = json.dumps(
        {
            "systemInstruction": {"parts": [{"text": SUMMARY_SYSTEM}]},
            "contents": [{"role": "user", "parts": [{"text": user_text}]}],
            "generationConfig": {
                "maxOutputTokens": SUMMARY_MAX_TOKENS,
                "temperature": 0.3,
                # Gemini 2.5 flash "thinks" by default and would consume the
                # small token budget before writing; disable it for a direct answer.
                "thinkingConfig": {"thinkingBudget": 0},
            },
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"{model}:generateContent",
        data=body,
        headers={"Content-Type": "application/json", "x-goog-api-key": api_key},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=30) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    candidates = data.get("candidates") or []
    if not candidates:
        return ""
    parts = candidates[0].get("content", {}).get("parts") or []
    return "".join(p.get("text", "") for p in parts).strip()


def summarize_ticker(ticker, items, model=SUMMARY_MODEL):
    """Return a short Korean summary of the ticker's news, or None on failure.

    Tries the configured model first, then falls back to other free-tier models,
    retrying once on transient errors (429 rate limit / 5xx). The last error is
    recorded in LAST_SUMMARY_ERROR so the email can explain what went wrong.
    """
    global LAST_SUMMARY_ERROR

    api_key = (
        os.environ.get("GEMINI_API_KEY", "").strip()
        or os.environ.get("GOOGLE_API_KEY", "").strip()
    )
    if not api_key:
        return None

    headlines = "\n".join(f"- {it['title']} ({it['source']})" for it in items)
    user = (
        f"티커: {ticker}\n\n오늘 수집된 뉴스 제목:\n{headlines}\n\n"
        "이 종목의 오늘 뉴스를 뉴스 데스크 앵커가 브리핑하듯 한국어 2~4문장으로 정리해 주세요."
    )

    models = [model] + [m for m in SUMMARY_FALLBACK_MODELS if m != model]
    last_err = None
    for candidate in models:
        for attempt in (1, 2):
            try:
                text = _gemini_generate(candidate, api_key, user)
                if text:
                    return text
                last_err = f"{candidate}: empty response"
                break  # empty answer -> try the next model
            except Exception as exc:  # noqa: BLE001 - best-effort, never fatal
                detail = ""
                try:
                    detail = exc.read().decode("utf-8")[:200]  # HTTPError body
                except Exception:  # noqa: BLE001
                    pass
                last_err = f"{candidate}: {exc} {detail}".strip()
                transient = any(
                    code in str(exc) for code in ("429", "500", "502", "503", "504")
                )
                if transient and attempt == 1:
                    time.sleep(3)
                    continue  # one quick retry for rate limit / server hiccup
                break  # otherwise move on to the next model

    LAST_SUMMARY_ERROR = last_err
    print(f"[warn] summary failed for {ticker}: {last_err}")
    return None


def _gemini_batch_call(model, api_key, prompt, max_tokens):
    """One Gemini call returning JSON text. Returns "" on empty; raises on HTTP error."""
    body = json.dumps(
        {
            "systemInstruction": {"parts": [{"text": SUMMARY_BATCH_SYSTEM}]},
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {
                "maxOutputTokens": max_tokens,
                "temperature": 0.3,
                "responseMimeType": "application/json",
                "thinkingConfig": {"thinkingBudget": 0},
            },
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"{model}:generateContent",
        data=body,
        headers={"Content-Type": "application/json", "x-goog-api-key": api_key},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=45) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    candidates = data.get("candidates") or []
    if not candidates:
        return ""
    parts = candidates[0].get("content", {}).get("parts") or []
    return "".join(p.get("text", "") for p in parts).strip()


def summarize_all(new_items):
    """Summarize EVERY ticker's news in a single Gemini call. {ticker: text}.

    One batched request per run (instead of one per ticker) keeps free-tier
    usage low so the daily 429 quota is not exhausted after a few runs.
    """
    global LAST_SUMMARY_ERROR
    LAST_SUMMARY_ERROR = None
    if os.environ.get("SUMMARIZE", "1") == "0" or os.environ.get("DRY_RUN") == "1":
        return {}
    api_key = (
        os.environ.get("GEMINI_API_KEY", "").strip()
        or os.environ.get("GOOGLE_API_KEY", "").strip()
    )
    if not api_key:
        print("[warn] GEMINI_API_KEY not set -- sending headline digest only.")
        return {}

    by_ticker = {}
    for it in new_items:
        by_ticker.setdefault(it["ticker"], []).append(it)
    if not by_ticker:
        return {}

    blocks = []
    for ticker in sorted(by_ticker):
        heads = "\n".join(f"- {it['title']}" for it in by_ticker[ticker])
        blocks.append(f"[{ticker}]\n{heads}")
    prompt = (
        "다음은 종목별 오늘의 뉴스 제목입니다. 각 종목마다 뉴스 데스크 앵커가 "
        "브리핑하듯 한국어 2~4문장으로 정리해 주세요. 반드시 JSON 객체 하나로만 "
        "응답하고, 키는 종목 티커(대문자), 값은 그 종목의 한국어 브리핑 문자열입니다.\n\n"
        + "\n\n".join(blocks)
    )
    max_tokens = min(4000, 400 + 300 * len(by_ticker))

    models = [SUMMARY_MODEL] + [m for m in SUMMARY_FALLBACK_MODELS if m != SUMMARY_MODEL]
    raw, last_err = None, None
    for candidate in models:
        for attempt in (1, 2):
            try:
                raw = _gemini_batch_call(candidate, api_key, prompt, max_tokens)
                if raw:
                    break
                last_err = f"{candidate}: empty response"
            except Exception as exc:  # noqa: BLE001 - best-effort, never fatal
                detail = ""
                try:
                    detail = exc.read().decode("utf-8")[:200]  # HTTPError body
                except Exception:  # noqa: BLE001
                    pass
                last_err = f"{candidate}: {exc} {detail}".strip()
                if "429" in str(exc) and attempt == 1:
                    time.sleep(20)  # rate limit: wait once, then retry same model
                    continue
                break  # non-429, or already retried -> next model
        if raw:
            break

    if not raw:
        LAST_SUMMARY_ERROR = last_err
        print(f"[warn] batched summary failed: {last_err}")
        return {}

    summaries = {}
    try:
        cleaned = raw.strip().strip("`")
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start != -1 and end != -1:
            cleaned = cleaned[start:end + 1]
        parsed = json.loads(cleaned)
        for ticker in by_ticker:
            val = (
                parsed.get(ticker)
                or parsed.get(ticker.upper())
                or parsed.get(ticker.lower())
            )
            if isinstance(val, str) and val.strip():
                summaries[ticker] = val.strip()
    except Exception as exc:  # noqa: BLE001
        LAST_SUMMARY_ERROR = f"parse error: {exc}"
        print(f"[warn] could not parse batched summary JSON: {exc}")
        return {}

    if summaries:
        print(f"[info] generated AI briefings for {len(summaries)} ticker(s) in 1 call.")
    else:
        LAST_SUMMARY_ERROR = last_err or "empty parsed result"
    return summaries


def summary_to_html(summary):
    """Render the Korean news briefing as a styled prose block."""
    paras = "".join(
        f"<p style='margin:4px 0'>{html.escape(ln.lstrip('-*• ').strip())}</p>"
        for ln in summary.splitlines()
        if ln.strip()
    )
    return (
        "<div style='background:#f5f7fa;border-left:3px solid #0b57d0;"
        "padding:10px 14px;margin:6px 0 12px;border-radius:4px'>"
        "<div style='font-size:12px;color:#0b57d0;font-weight:700;margin-bottom:4px'>"
        "📰 뉴스 브리핑</div>"
        f"<div style='color:#222;line-height:1.6'>{paras}</div></div>"
    )


# --- Email -------------------------------------------------------------------

def _clean_title(title):
    """Strip the trailing ' - Publisher' suffix from a Google News title."""
    return re.sub(r"\s+[-|]\s+[^-|]+$", "", title).strip()


def build_email_html(new_items, summaries=None, summary_error=None):
    """Render a per-ticker news briefing.

    When an AI briefing exists for a ticker, only the briefing is shown (raw
    links are intentionally omitted). When it does not (e.g. GEMINI_API_KEY is
    unset), a clean de-duplicated headline digest is shown instead of raw links.
    """
    summaries = summaries or {}
    by_ticker = {}
    for item in new_items:
        by_ticker.setdefault(item["ticker"], []).append(item)

    parts = [
        "<div style='font-family:-apple-system,Segoe UI,Roboto,sans-serif;"
        "font-size:15px;color:#111;line-height:1.5'>",
        f"<h2 style='margin:0 0 12px'>📰 오늘의 종목 뉴스 브리핑 "
        f"<span style='font-weight:400;color:#888;font-size:13px'>"
        f"({len(new_items)}건)</span></h2>",
    ]
    for ticker in sorted(by_ticker):
        parts.append(
            f"<h3 style='margin:20px 0 6px;color:#0b57d0'>{html.escape(ticker)}</h3>"
        )
        if summaries.get(ticker):
            parts.append(summary_to_html(summaries[ticker]))
            continue
        # No AI briefing — show a clean, de-duplicated headline digest
        # (titles only, no raw links) rather than passing links through.
        seen, titles = set(), []
        for it in by_ticker[ticker]:
            clean = _clean_title(it["title"])
            if clean.lower() not in seen:
                seen.add(clean.lower())
                titles.append(clean)
        lis = "".join(
            f"<li style='margin:3px 0'>{html.escape(t)}</li>" for t in titles
        )
        parts.append(
            f"<ul style='margin:4px 0;padding-left:20px;color:#333'>{lis}</ul>"
        )

    if not any(summaries.get(t) for t in by_ticker):
        key_set = bool(
            os.environ.get("GEMINI_API_KEY", "").strip()
            or os.environ.get("GOOGLE_API_KEY", "").strip()
        )
        if not key_set:
            note = (
                "ℹ️ AI 브리핑이 아직 생성되지 않아 <b>제목만</b> 표시했습니다. "
                "GitHub 레포 <b>Settings → Secrets → Actions</b> 에 "
                "<b>GEMINI_API_KEY</b> 를 등록하면, 종목별로 제목을 분석해 "
                "하나의 뉴스 브리핑으로 만들어 드립니다."
            )
        else:
            reason = html.escape(str(summary_error or "알 수 없는 오류"))
            note = (
                "⚠️ AI 브리핑 생성에 <b>실패</b>해 제목만 표시했습니다. "
                f"(키는 정상 등록됨)<br>원인: <code>{reason}</code><br>"
                "무료 사용량 초과(429)라면 하루 지나면 자동으로 복구됩니다."
            )
        parts.append(
            "<p style='background:#fff6e5;border-left:3px solid #f5a623;"
            "padding:8px 12px;color:#7a5b00;font-size:13px;margin:16px 0;"
            f"border-radius:4px'>{note}</p>"
        )
    parts.append(
        "<p style='color:#999;font-size:12px;margin-top:24px'>"
        "Sent by stock-news-watch. Reply-free automated digest.</p></div>"
    )
    return "\n".join(parts)


def send_email(subject, html_body, recipient=None):
    """Send an HTML email via Gmail SMTP over SSL. Returns True on success.

    ``recipient`` defaults to MAIL_TO; pass an address (e.g. a Blogger
    Mail2Blogger address) to send the same body somewhere else.
    """
    user = os.environ.get("GMAIL_USER", "").strip()
    password = os.environ.get("GMAIL_APP_PASSWORD", "").strip()
    recipient = recipient or os.environ.get("MAIL_TO", "").strip() or user

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


# --- Blogger auto-post -------------------------------------------------------
# Publish the same briefing to a Blogger blog via the Blogger API v3, using a
# stored OAuth refresh token (no extra dependency; stdlib HTTP). Best-effort:
# any failure or missing config just skips the post and logs a warning.

def _google_access_token(client_id, client_secret, refresh_token):
    """Exchange a refresh token for a short-lived access token."""
    data = urlencode(
        {
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        "https://oauth2.googleapis.com/token",
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))["access_token"]


def _blogger_blog_id(access_token, blog_url):
    """Resolve a numeric blog id from a blog URL via the Blogger API."""
    url = (
        "https://www.googleapis.com/blogger/v3/blogs/byurl?url="
        + quote_plus(blog_url)
    )
    req = urllib.request.Request(
        url, headers={"Authorization": "Bearer " + access_token}
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))["id"]


def post_to_blogger(title, html_body):
    """Publish an HTML post to Blogger. Returns True/False, or None if unconfigured."""
    client_id = os.environ.get("BLOGGER_CLIENT_ID", "").strip()
    client_secret = os.environ.get("BLOGGER_CLIENT_SECRET", "").strip()
    refresh_token = os.environ.get("BLOGGER_REFRESH_TOKEN", "").strip()
    blog_id = os.environ.get("BLOGGER_BLOG_ID", "").strip()
    blog_url = os.environ.get("BLOGGER_BLOG_URL", "").strip()
    if not (client_id and client_secret and refresh_token and (blog_id or blog_url)):
        return None  # Blogger auto-post not configured

    try:
        token = _google_access_token(client_id, client_secret, refresh_token)
        if not blog_id:
            blog_id = _blogger_blog_id(token, blog_url)
        payload = json.dumps(
            {"kind": "blogger#post", "title": title, "content": html_body}
        ).encode("utf-8")
        req = urllib.request.Request(
            f"https://www.googleapis.com/blogger/v3/blogs/{blog_id}/posts/",
            data=payload,
            headers={
                "Authorization": "Bearer " + token,
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            published = json.loads(resp.read().decode("utf-8"))
        print(f"[info] blog post published: {published.get('url', '(no url)')}")
        return True
    except Exception as exc:  # noqa: BLE001 - blog post is best-effort
        detail = ""
        try:
            detail = exc.read().decode("utf-8")[:300]  # HTTPError body, if any
        except Exception:  # noqa: BLE001
            pass
        print(f"[warn] Blogger post failed: {exc} {detail}")
        return False


# --- Main --------------------------------------------------------------------

def kst_today_title():
    """Blog post title like '2026년 7월 16일 포트폴리오 뉴스' (Korean date, KST)."""
    kst = datetime.now(timezone(timedelta(hours=9)))
    return f"{kst.year}년 {kst.month}월 {kst.day}일 포트폴리오 뉴스"


def write_shorts_json(new_items, summaries, path="data/latest_shorts.json"):
    """Publish ALL tickers' briefings so the shorts-generator repo can weave them
    into a single '오늘의 증시' Short (JSON bridge). Writes
    {date, topic, items:[{ticker,name,summary}...], script}, ordered by how many
    fresh articles each ticker had (most newsworthy first). `script` is a simple
    single-ticker fallback for consumers that don't recompose. Never fails."""
    if not summaries:
        return
    counts = {}
    for it in new_items:
        counts[it["ticker"]] = counts.get(it["ticker"], 0) + 1
    names = load_ticker_names()
    ordered = sorted(summaries.keys(), key=lambda t: counts.get(t, 0), reverse=True)
    items = []
    for t in ordered:
        aliases = names.get(t.upper(), [])
        items.append({"ticker": t,
                      "name": aliases[0] if aliases else t,
                      "summary": summaries[t]})
    if not items:
        return
    payload = {
        "date": datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d"),
        "topic": "오늘의 증시",
        "items": items,
        # Fallback single script (top ticker) for consumers that don't recompose.
        "ticker": items[0]["ticker"],
        "script": items[0]["summary"],
    }
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
        print(f"[info] wrote {path} for shorts ({len(items)} ticker(s)).")
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] could not write {path}: {exc}")


def gemini_ping():
    """TEST-ONLY: make one tiny Gemini call to check the key/quota (429?).

    Sends nothing, publishes nothing, and does not touch seen.json. Always
    returns 0 (the log line tells the story). Triggered by GEMINI_PING=1.
    """
    api_key = (
        os.environ.get("GEMINI_API_KEY", "").strip()
        or os.environ.get("GOOGLE_API_KEY", "").strip()
    )
    if not api_key:
        print("[test] GEMINI_API_KEY 없음 - 키를 먼저 등록하세요.")
        return 0
    models = [SUMMARY_MODEL] + [
        m for m in SUMMARY_FALLBACK_MODELS if m != SUMMARY_MODEL
    ]
    for model in models:
        try:
            _gemini_batch_call(model, api_key, "한 단어로 'OK'만 답하세요.", 50)
            print(f"[test] ✅ Gemini 호출 성공 (모델: {model}) - 429 없음, 키/한도 정상.")
            return 0
        except Exception as exc:  # noqa: BLE001
            detail = ""
            try:
                detail = exc.read().decode("utf-8")[:200]
            except Exception:  # noqa: BLE001
                pass
            tag = "429 (사용량 초과)" if "429" in str(exc) else str(exc)
            print(f"[test] ❌ {model} 실패: {tag} {detail}".strip())
    print("[test] 모든 모델 실패 - 아직 이 프로젝트 한도가 소진된 상태로 보입니다.")
    return 0


def main():
    if os.environ.get("GEMINI_PING") == "1":
        return gemini_ping()

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

    force_send = os.environ.get("FORCE_SEND") == "1"
    now_iso = datetime.now(timezone.utc).isoformat()
    new_items = []
    for item in items:
        if force_send or item["key"] not in seen:
            new_items.append(item)
        seen[item["key"]] = seen.get(item["key"], now_iso)

    # Always persist state so the next run knows what we have seen.
    save_seen(seen)

    if force_send:
        print(
            f"[info] FORCE_SEND on: sending {len(new_items)} recent article(s) "
            "regardless of seen state."
        )
    elif first_run and os.environ.get("EMAIL_ON_FIRST_RUN") != "1":
        print(
            f"[info] first run: recorded {len(items)} article(s) as baseline. "
            "No email sent (future runs email only NEW articles)."
        )
        return 0

    if not new_items:
        print("[info] no new/recent articles. Nothing to email.")
        return 0

    try:
        per_ticker = int(os.environ.get("MAX_PER_TICKER", str(MAX_PER_TICKER)))
    except ValueError:
        per_ticker = MAX_PER_TICKER
    total_new = len(new_items)
    new_items = select_for_email(new_items, per_ticker)
    if total_new > len(new_items):
        print(
            f"[info] {total_new} new item(s) after filters; trimmed to "
            f"{len(new_items)} (<= {per_ticker} per ticker), newest first."
        )

    print(f"[info] {len(new_items)} new article(s) -> sending email.")
    summaries = summarize_all(new_items)
    write_shorts_json(new_items, summaries)   # bridge: feed the shorts-generator
    tickers_line = ", ".join(sorted({i["ticker"] for i in new_items}))
    subject = f"📰 오늘의 종목 뉴스 브리핑 ({len(new_items)}건): {tickers_line}"
    body = build_email_html(new_items, summaries, LAST_SUMMARY_ERROR)
    ok = send_email(subject, body)

    # Auto-publish the same briefing to Blogger (API v3) titled
    # "YYYY년 M월 D일 포트폴리오 뉴스", when Blogger secrets are configured.
    post_to_blogger(kst_today_title(), body)

    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main())
