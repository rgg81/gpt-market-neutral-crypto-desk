from __future__ import annotations

import html
import re
import xml.etree.ElementTree as ET
from datetime import UTC, datetime

from pydantic import BaseModel

FNG_URL = "https://api.alternative.me/fng/"
FRED_URL = "https://api.stlouisfed.org/fred/series/observations"


class FearGreed(BaseModel):
    value: int
    classification: str
    ts: datetime


class NewsItem(BaseModel):
    title: str
    url: str
    published_at: str
    source: str
    kind: str
    instruments: list[str]
    summary: str = ""               # HTML-stripped article body/snippet (not just the title)
    votes_positive: int = 0
    votes_negative: int = 0


class SocialPost(BaseModel):
    """A reddit post the Sentiment analyst reads to gauge crowd CONTENT (not just an index number).
    `score` = net upvotes (the crowd's weight on the post); `summary` = the self-text snippet."""
    title: str
    summary: str = ""
    score: int = 0
    num_comments: int = 0
    source: str = ""                # the subreddit, e.g. 'CryptoCurrency'
    instruments: list[str] = []


def parse_fear_greed(payload: dict) -> FearGreed:
    d = payload["data"][0]
    return FearGreed(
        value=int(d["value"]),
        classification=d["value_classification"],
        ts=datetime.fromtimestamp(int(d["timestamp"]), tz=UTC),
    )


_ATOM = "{http://www.w3.org/2005/Atom}"
_ALIASES = {
    "BTC": ("btc", "bitcoin"), "ETH": ("eth", "ethereum"), "SOL": ("sol", "solana"),
    "BNB": ("bnb", "binance coin"), "XRP": ("xrp", "ripple"), "DOGE": ("doge", "dogecoin"),
    "ADA": ("ada", "cardano"), "AVAX": ("avax", "avalanche"),
}


def _base(symbol: str) -> str:
    # "BTC/USDT:USDT" -> "BTC"; "BTCUSDT" -> "BTC"
    s = symbol.split("/")[0]
    return s[:-4] if s.endswith("USDT") else s


def tag_instruments(title: str, symbols: list[str]) -> list[str]:
    """Which of `symbols` (bases or unified) a headline mentions, by ticker or full name.

    Matches on WORD BOUNDARIES (not raw substrings) so common words don't produce spurious
    tags: 'method' must not match 'eth', 'console'/'absolute' must not match 'sol',
    'canada'/'nevada' must not match 'ada', 'ethernet' must not match 'eth'."""
    t = title.lower()
    out: list[str] = []
    for sym in symbols:
        b = _base(sym)
        kws = (b.lower(),) + _ALIASES.get(b, ())
        if any(re.search(rf"\b{re.escape(k)}\b", t) for k in kws) and b not in out:
            out.append(b)
    return out


_CONTENT = "{http://purl.org/rss/1.0/modules/content/}"  # <content:encoded> full-body namespace
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def _clean_html(s: str | None, limit: int = 500) -> str:
    """Strip HTML tags, decode entities, collapse whitespace, truncate — turn an RSS body snippet
    into a plain-text summary the News analyst can read. Empty string on None."""
    if not s:
        return ""
    text = _WS_RE.sub(" ", _TAG_RE.sub(" ", html.unescape(s))).strip()
    return text[:limit].rstrip() + ("…" if len(text) > limit else "")


def _rss_text(el, tag: str) -> str | None:
    for cand in (tag, _ATOM + tag):
        e = el.find(cand)
        if e is not None:
            if e.text and e.text.strip():
                return e.text.strip()
            if e.get("href"):
                return e.get("href")
    return None


def parse_rss(content: bytes, source: str, symbols: list[str]) -> list[NewsItem]:
    """Parse an RSS/Atom feed (namespace-aware) into NewsItems. Returns [] on malformed XML."""
    try:
        root = ET.fromstring(content)
    except ET.ParseError:
        return []
    nodes = root.findall(".//item") or root.findall(f".//{_ATOM}entry")
    items: list[NewsItem] = []
    for n in nodes:
        title = _rss_text(n, "title")
        if not title:
            continue
        # Body: RSS <content:encoded> (full) or <description>; Atom <content>/<summary>. The body
        # often names coins the title doesn't, so tag instruments on title + body, and hand the
        # analyst the HTML-stripped snippet — not just the headline.
        raw_body = (_rss_text(n, _CONTENT + "encoded") or _rss_text(n, "encoded")
                    or _rss_text(n, "description") or _rss_text(n, "content")
                    or _rss_text(n, "summary"))
        summary = _clean_html(raw_body)
        items.append(NewsItem(
            title=title,
            url=_rss_text(n, "link") or "",
            published_at=_rss_text(n, "pubDate") or _rss_text(n, "published")
            or _rss_text(n, "updated") or "",
            source=source,
            kind="news",
            instruments=tag_instruments(f"{title} {summary}", symbols),
            summary=summary,
        ))
    return items


def fetch_news(
    client, sources: list[str], symbols: list[str], per_source: int = 10
) -> list[NewsItem]:
    """Fetch + parse multiple keyless RSS news feeds; skip any source that errors; dedupe by
    title."""
    seen: set[str] = set()
    out: list[NewsItem] = []
    for url in sources:
        try:
            r = client.get(url, headers={"User-Agent": "Mozilla/5.0"})
            r.raise_for_status()
            src = url.split("//")[-1].split("/")[0]
            for item in parse_rss(r.content, source=src, symbols=symbols)[:per_source]:
                if item.title not in seen:
                    seen.add(item.title)
                    out.append(item)
        except Exception:
            continue  # graceful: a dead/blocked source must not break the cycle
    return out


_REDDIT_UA = "Mozilla/5.0 (TempestDesk research; keyless public-json read)"


def parse_reddit(payload: dict, subreddit: str, symbols: list[str]) -> list[SocialPost]:
    """Parse reddit's public listing JSON ({data:{children:[{data:{title,selftext,score,...}}]}})
    into SocialPosts, tagging instruments from title + self-text. Returns [] on any shape error."""
    try:
        children = (payload or {}).get("data", {}).get("children", [])
    except (AttributeError, TypeError):
        return []
    out: list[SocialPost] = []
    for ch in children:
        d = ch.get("data", {}) if isinstance(ch, dict) else {}
        title = (d.get("title") or "").strip()
        if not title:
            continue
        body = _clean_html(d.get("selftext") or "")
        out.append(SocialPost(
            title=title, summary=body,
            score=int(d.get("score") or 0), num_comments=int(d.get("num_comments") or 0),
            source=subreddit, instruments=tag_instruments(f"{title} {body}", symbols)))
    return out


def _posts_for_sub(client, sub: str, symbols: list[str], per_sub: int) -> list[SocialPost]:
    """One subreddit's posts. Tries /hot.json first (richer — carries upvote `score`), which reddit
    OFTEN 403s for keyless/datacenter reads; falls back to the /.rss Atom feed (works keyless but
    has no score). Returns [] if both fail."""
    try:
        r = client.get(f"https://www.reddit.com/r/{sub}/hot.json",
                       params={"limit": per_sub}, headers={"User-Agent": _REDDIT_UA})
        r.raise_for_status()
        posts = parse_reddit(r.json(), subreddit=sub, symbols=symbols)
        if posts:
            return posts[:per_sub]
    except Exception:
        pass
    try:
        r = client.get(f"https://www.reddit.com/r/{sub}/.rss", headers={"User-Agent": _REDDIT_UA})
        r.raise_for_status()
        return [SocialPost(title=i.title, summary=i.summary, source=sub, instruments=i.instruments)
                for i in parse_rss(r.content, source=sub, symbols=symbols)[:per_sub]]
    except Exception:
        return []


def fetch_reddit(client, subreddits: list[str], symbols: list[str], per_sub: int = 40) -> dict:
    """Keyless reddit social-sentiment scrape. Aggregates the top posts and a per-symbol mention
    count + score-weighted sum (the crowd's attention/weight per coin), so the Sentiment analyst
    reads real crowd CONTENT, not just a Fear&Greed number. Per sub it tries /hot.json then falls
    back to the /.rss Atom feed (reddit 403s the keyless JSON but serves the RSS). Graceful: a
    blocked sub is skipped; if all fail, returns {'posts': [], 'mentions': {}} and the desk caps
    conviction (the persona handles the degraded read)."""
    seen: set[str] = set()
    posts: list[SocialPost] = []
    for sub in subreddits:
        for p in _posts_for_sub(client, sub, symbols, per_sub):
            if p.title not in seen:
                seen.add(p.title)
                posts.append(p)
    posts.sort(key=lambda p: p.score, reverse=True)
    mentions: dict[str, dict] = {}
    for p in posts:
        for sym in p.instruments:
            m = mentions.setdefault(sym, {"count": 0, "score_sum": 0})
            m["count"] += 1
            m["score_sum"] += p.score
    return {"posts": [p.model_dump() for p in posts[:30]], "mentions": mentions}


def fetch_macro(client, series: list[str], api_key: str | None) -> dict[str, float]:
    """Latest value per FRED series (DXY/yields/Fed/CPI). Empty dict if no key (graceful)."""
    if not api_key:
        return {}
    out: dict[str, float] = {}
    for sid in series:
        try:
            r = client.get(FRED_URL, params={"series_id": sid, "api_key": api_key,
                                              "file_type": "json", "sort_order": "desc",
                                              "limit": 1})
            r.raise_for_status()
            # pick the latest observation by ISO date — order-independent (don't trust API order)
            vals = parse_fred(r.json())  # [(date, value)], skips "."
            if vals:
                out[sid] = max(vals, key=lambda dv: dv[0])[1]
        except Exception:
            continue
    return out


def parse_fred(payload: dict) -> list[tuple[str, float]]:
    out: list[tuple[str, float]] = []
    for o in payload.get("observations", []):
        if o["value"] == ".":  # FRED missing-value sentinel
            continue
        out.append((o["date"], float(o["value"])))
    return out


def fetch_fear_greed(client, limit: int = 1) -> FearGreed:
    r = client.get(FNG_URL, params={"limit": limit, "format": "json"})
    r.raise_for_status()
    return parse_fear_greed(r.json())
