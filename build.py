#!/usr/bin/env python3
"""PULSE data builder. Fetches free public sources, ranks them, writes site/data.json.

Standard library only. Every source is isolated: a failure is recorded in
meta.sources and never breaks the build.
"""
import concurrent.futures as cf
import email.utils
import hashlib
import html
import json
import math
import os
import re
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

# SEC asks automated clients to identify themselves with a contact email.
# Override with the CONTACT_EMAIL env var (e.g. a GitHub Actions secret).
CONTACT = os.environ.get("CONTACT_EMAIL") or "pulse-feed@users.noreply.github.com"
UA = f"Mozilla/5.0 (compatible; PulseWorldFeed/1.0; +mailto:{CONTACT})"
SEC_UA = f"WorldFeed {CONTACT}"
TIMEOUT = 15
WINDOW_H = 72
CAP = 600
MIN_PER_CAT = 30
HALF_LIFE_H = 6.0
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "site", "data.json")
NOW = datetime.now(timezone.utc)

CATEGORIES = [
    ("ai", "AI"), ("tech", "Tech"), ("money", "Money"), ("crypto", "Crypto"),
    ("people", "People"), ("world", "World"), ("health", "Health"),
    ("science", "Science"), ("culture", "Culture"), ("hype", "Hype"),
]

# How much each kind of source counts toward heat (engagement-backed sources weigh more).
WEIGHT = {"gtopic": 1.0, "gnews": 0.72, "hn": 0.95, "reddit": 0.95, "ape": 0.85, "coingecko": 0.75,
          "hf": 0.75, "producthunt": 0.65, "sec": 0.8}
# Snapshot sources (trending lists) have no real publish time; give them a fixed recency.
SNAPSHOT = {"ape": 0.6, "coingecko": 0.55, "hf": 0.5}

AI_RE = re.compile(r"\b(ai|a\.i\.|llm|llms|gpt|chatgpt|openai|anthropic|claude|gemini|xai|grok|"
                   r"deepseek|mistral|llama|machine learning|neural|agi|transformer|diffusion|"
                   r"nvidia|copilot|agents?|model)\b", re.I)


# ---------------------------------------------------------------- helpers
def get(url, ua=UA, accept="*/*"):
    req = urllib.request.Request(url, headers={"User-Agent": ua, "Accept": accept,
                                               "Accept-Language": "en-US,en;q=0.9"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return r.read()


def get_json(url, ua=UA):
    return json.loads(get(url, ua, "application/json"))


def iso(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_date(s):
    if not s:
        return None
    s = s.strip()
    try:
        d = email.utils.parsedate_to_datetime(s)
    except (TypeError, ValueError):
        try:
            d = datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError:
            return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return min(d, NOW)


def clean(s, n=None):
    s = html.unescape(re.sub(r"<[^>]+>", " ", s or ""))
    s = re.sub(r"\s+", " ", s).strip()
    if n and len(s) > n:
        s = s[: n - 1].rsplit(" ", 1)[0] + "…"
    return s


def item(title, url, source, category, published, eng, kind, **extra):
    d = dict(title=clean(title, 220), url=url, source=source, category=category,
             published=published or NOW, eng=float(eng or 0), kind=kind)
    d.update({k: v for k, v in extra.items() if v})
    return d


# ---------------------------------------------------------------- sources
GN = "https://news.google.com/rss"
GN_TAIL = "hl=en-US&gl=US&ceid=US:en"


def gnews_search(q):
    return f"{GN}/search?q={urllib.parse.quote_plus(q + ' when:1d')}&{GN_TAIL}"


def gnews_topic(t):
    return f"{GN}/headlines/section/topic/{t}?{GN_TAIL}"


def gnews(url, category, who=None):
    kind = "gtopic" if "/topic/" in url else "gnews"
    root = ET.fromstring(get(url))
    rows = root.findall("channel/item")
    out = []
    for i, it in enumerate(rows):
        title = it.findtext("title") or ""
        src_el = it.find("source")
        src = (src_el.text or "").strip() if src_el is not None else "Google News"
        if src and title.endswith(" - " + src):
            title = title[: -len(src) - 3]
        coverage = (it.findtext("description") or "").count("<li")
        eng = max(coverage, 1) * 2 + (len(rows) - i) / max(len(rows), 1)  # Google's own ordering + cluster size
        out.append(item(title, it.findtext("link"), src, category,
                        parse_date(it.findtext("pubDate")), eng, kind, who=who))
    return out


def hackernews():
    ids = get_json("https://hacker-news.firebaseio.com/v0/topstories.json")[:120]

    def one(i):
        try:
            return get_json(f"https://hacker-news.firebaseio.com/v0/item/{i}.json")
        except Exception:
            return None

    with cf.ThreadPoolExecutor(24) as ex:
        stories = [s for s in ex.map(one, ids) if s and s.get("title") and not s.get("dead")]
    out = []
    for s in stories:
        hn = f"https://news.ycombinator.com/item?id={s['id']}"
        cat = "ai" if AI_RE.search(s["title"]) else "tech"
        pts, com = s.get("score", 0), s.get("descendants", 0)
        out.append(item(s["title"], s.get("url") or hn, "Hacker News", cat,
                        datetime.fromtimestamp(s.get("time", 0), timezone.utc), pts, "hn",
                        summary=f"{pts:,} points · {com:,} comments", discuss=hn))
    return out


def huggingface():
    data = get_json("https://huggingface.co/api/trending?type=model&limit=20")
    out = []
    for i, r in enumerate(data.get("recentlyTrending", [])):
        m = r.get("repoData", {})
        mid = m.get("id")
        if not mid:
            continue
        task = (m.get("pipeline_tag") or "model").replace("-", " ")
        out.append(item(f"Trending on Hugging Face: {mid}", f"https://huggingface.co/{mid}",
                        "Hugging Face", "ai", parse_date(m.get("lastModified")),
                        m.get("likes", 0) + (20 - i) * 50, "hf",
                        summary=f"{task} · {m.get('likes', 0):,} likes · {m.get('downloads', 0):,} downloads",
                        image=(m.get("authorData") or {}).get("avatarUrl")))
    return out


def producthunt():
    ns = {"a": "http://www.w3.org/2005/Atom"}
    root = ET.fromstring(get("https://www.producthunt.com/feed"))
    rows = root.findall("a:entry", ns)
    out = []
    for i, e in enumerate(rows):
        link = e.find("a:link", ns)
        tagline = clean(e.findtext("a:content", "", ns)).replace("Discussion | Link", "").strip()
        name = e.findtext("a:title", "", ns)
        out.append(item(f"{name}: {tagline}" if tagline else name, link.get("href") if link is not None else None,
                        "Product Hunt", "tech", parse_date(e.findtext("a:published", "", ns)),
                        len(rows) - i, "producthunt"))
    return out


def apewisdom():
    res = get_json("https://apewisdom.io/api/v1.0/filter/all-stocks/page/1").get("results", [])
    out = []
    for r in res:
        m, prev = int(r.get("mentions") or 0), int(r.get("mentions_24h_ago") or 0)
        if m < 15:
            continue
        pct = (m - prev) / max(prev, 1) * 100
        rank, was = int(r.get("rank") or 99), r.get("rank_24h_ago")
        if pct < 25 and rank > 10:
            continue
        name, tk = html.unescape(r.get("name") or ""), r.get("ticker")
        verb = f"up {pct:,.0f}%" if pct >= 0 else f"down {abs(pct):,.0f}%"
        eng = math.log1p(m) * (1 + min(max(pct, 0), 800) / 100) + (100 - rank) / 25
        out.append(item(f"${tk} mentions {verb} on Reddit · {name}", f"https://apewisdom.io/stocks/{tk}/",
                        "ApeWisdom", "money", NOW, eng, "ape", also=["hype"],
                        summary=f"{m:,} mentions in 24h (was {prev:,}) · rank #{rank}" + (f" (was #{was})" if was else "")))
    return out


def coingecko():
    data = get_json("https://api.coingecko.com/api/v3/search/trending")
    out = []
    for i, c in enumerate(data.get("coins", [])):
        c = c.get("item", {})
        ch = ((c.get("data") or {}).get("price_change_percentage_24h") or {}).get("usd")
        chs = f" · {ch:+.1f}% 24h" if isinstance(ch, (int, float)) else ""
        rank = c.get("market_cap_rank")
        out.append(item(f"{c.get('name')} (${c.get('symbol')}) is trending{chs}",
                        f"https://www.coingecko.com/en/coins/{c.get('id')}", "CoinGecko", "crypto", NOW,
                        (20 - i) + abs(ch or 0) / 5, "coingecko", also=["hype"], image=c.get("small"),
                        summary=f"Trending #{i + 1} on CoinGecko" + (f" · market cap rank #{rank}" if rank else "")))
    return out


def sec_13d():
    start = (NOW - timedelta(days=5)).strftime("%Y-%m-%d")
    url = ("https://efts.sec.gov/LATEST/search-index?forms=SCHEDULE%2013D,SC%2013D"
           f"&dateRange=custom&startdt={start}&enddt={NOW.strftime('%Y-%m-%d')}")
    hits = get_json(url, SEC_UA).get("hits", {}).get("hits", [])
    out, seen = [], set()
    for h in hits:
        s = h.get("_source", {})
        if s.get("form") not in ("SCHEDULE 13D", "SC 13D"):  # originals only, skip /A amendments
            continue
        adsh = s.get("adsh")
        if not adsh or adsh in seen:
            continue
        seen.add(adsh)
        names = s.get("display_names") or []
        subj = next((n for n in names if re.search(r"\)\s+\(CIK", n) and re.search(r"\(([A-Z0-9.\-, ]+)\)\s+\(CIK", n)), None)
        if not subj:
            continue  # no ticker: not a listed company, mostly noise
        tk = re.search(r"\(([A-Z0-9.\-, ]+)\)\s+\(CIK", subj).group(1).split(",")[0].strip()
        company = clean(subj.split("(")[0])
        filer = next((clean(n.split("(")[0]) for n in names if n != subj), "An investor")
        cik = int((s.get("ciks") or ["0"])[0])
        link = f"https://www.sec.gov/Archives/edgar/data/{cik}/{adsh.replace('-', '')}/{adsh}-index.htm"
        d = datetime.fromisoformat(s["file_date"]).replace(hour=20, tzinfo=timezone.utc)
        out.append(item(f"New 13D: {filer} takes a 5%+ stake in {company} (${tk})", link, "SEC EDGAR",
                        "money", min(d, NOW), 1, "sec",
                        summary="Original Schedule 13D: an investor crossed 5% ownership and may push for change."))
    return out


def reddit(sub):
    data = get_json(f"https://www.reddit.com/r/{sub}/hot.json?limit=25&raw_json=1")
    posts = [c["data"] for c in data["data"]["children"]]
    out = []
    for p in posts:
        if p.get("stickied"):
            continue
        thumb = p.get("thumbnail")
        out.append(item(p.get("title"), p.get("url_overridden_by_dest") or "https://www.reddit.com" + p.get("permalink", ""),
                        f"r/{sub}", "hype", datetime.fromtimestamp(p.get("created_utc", 0), timezone.utc),
                        p.get("ups", 0), "reddit", discuss="https://www.reddit.com" + p.get("permalink", ""),
                        summary=f"{p.get('ups', 0):,} upvotes · {p.get('num_comments', 0):,} comments",
                        image=thumb if thumb and thumb.startswith("http") else None))
    return out


SOURCES = {
    # AI
    "gn:ai": ("Google News · AI", lambda: gnews(gnews_search("artificial intelligence OR OpenAI OR Anthropic OR Gemini OR xAI"), "ai")),
    "hn": ("Hacker News", hackernews),
    "hf": ("Hugging Face trending", huggingface),
    # Tech
    "gn:tech": ("Google News · Technology", lambda: gnews(gnews_topic("TECHNOLOGY"), "tech")),
    "ph": ("Product Hunt", producthunt),
    # Money
    "gn:business": ("Google News · Business", lambda: gnews(gnews_topic("BUSINESS"), "money")),
    "gn:stocks": ("Google News · stock market", lambda: gnews(gnews_search("stock market"), "money")),
    "gn:ipo": ("Google News · IPO", lambda: gnews(gnews_search("IPO"), "money")),
    "ape": ("ApeWisdom (Reddit tickers)", apewisdom),
    "sec": ("SEC EDGAR 13D", sec_13d),
    # Crypto
    "cg": ("CoinGecko trending", coingecko),
    "gn:crypto": ("Google News · crypto", lambda: gnews(gnews_search("crypto OR bitcoin OR memecoin"), "crypto")),
    # People
    "gn:elon": ("Google News · Elon Musk", lambda: gnews(gnews_search('"Elon Musk"'), "people", "Elon")),
    "gn:trump": ("Google News · Donald Trump", lambda: gnews(gnews_search('"Donald Trump"'), "people", "Trump")),
    # World
    "gn:world": ("Google News · World", lambda: gnews(gnews_topic("WORLD"), "world")),
    "gn:china": ("Google News · China tech", lambda: gnews(gnews_search("China technology"), "world", "China")),
    "gn:india": ("Google News · India tech", lambda: gnews(gnews_search("India technology"), "world", "India")),
    # Health
    "gn:longevity": ("Google News · longevity", lambda: gnews(gnews_search("longevity OR anti-aging research"), "health", "Longevity")),
    "gn:fda": ("Google News · FDA approval", lambda: gnews(gnews_search('"FDA approval" OR "FDA approves"'), "health", "FDA")),
    "gn:breakthrough": ("Google News · medical breakthrough", lambda: gnews(gnews_search("medical breakthrough"), "health")),
    # Science
    "gn:science": ("Google News · Science", lambda: gnews(gnews_topic("SCIENCE"), "science")),
    "gn:space": ("Google News · SpaceX/NASA", lambda: gnews(gnews_search("SpaceX OR NASA"), "science", "Space")),
    # Culture
    "gn:ent": ("Google News · Entertainment", lambda: gnews(gnews_topic("ENTERTAINMENT"), "culture")),
    "gn:celeb": ("Google News · celebrity", lambda: gnews(gnews_search("celebrity"), "culture")),
    "gn:died": ("Google News · notable deaths", lambda: gnews(gnews_search('"dies at" OR "has died" OR obituary legend'), "culture", "RIP")),
    "gn:food": ("Google News · food trends", lambda: gnews(gnews_search('"food trend" OR viral food OR new menu item'), "culture", "Food")),
    "gn:fashion": ("Google News · fashion", lambda: gnews(gnews_search("streetwear OR sneaker release OR fashion drop"), "culture", "Fashion")),
    # Hype
    **{f"reddit:{s}": (f"Reddit r/{s}", (lambda s=s: reddit(s)))
       for s in ("wallstreetbets", "technology", "Futurology", "singularity", "worldnews")},
}


# ---------------------------------------------------------------- ranking
STOP = set("a an the of to in on for and or is are was be by with at from as it its this that how why what "
           "new says say after over into about will can could his her their they he she you your we our "
           "has have had not but more than up out vs".split())


def tokens(title):
    return {w for w in re.findall(r"[a-z0-9$]+", title.lower()) if w not in STOP and len(w) > 1}


def cluster(items):
    """Greedy near-duplicate grouping: each title joins the first cluster whose lead title
    shares >= 50% of its content words (Jaccard), or the same URL. No transitive chaining."""
    order = sorted(items, key=lambda x: x["pct"] * WEIGHT.get(x["kind"], 0.8), reverse=True)
    leads, index, by_url = [], {}, {}
    for it in order:
        tk = tokens(it["title"])
        home = by_url.get(it["url"])
        if home is None and len(tk) >= 3:
            shared = {}
            for t in tk:
                for c in index.get(t, ()):
                    shared[c] = shared.get(c, 0) + 1
            best = 0.0
            for c, n in shared.items():
                if n >= 3:
                    j = n / len(tk | leads[c][0])
                    if j >= 0.5 and j > best:
                        best, home = j, c
        if home is None:
            home = len(leads)
            leads.append((tk, []))
            for t in tk:
                index.setdefault(t, []).append(home)
        leads[home][1].append(it)
        by_url.setdefault(it["url"], home)
    return [g for _, g in leads]


def rank(items):
    # 1) engagement percentile within each source
    by_src = {}
    for it in items:
        by_src.setdefault(it["_sid"], []).append(it)
    for group in by_src.values():
        group.sort(key=lambda x: x["eng"])
        n = len(group)
        for k, it in enumerate(group):
            it["pct"] = (k + 1) / n if n > 1 else 0.75
    # 2) cluster duplicates, keep the strongest representative
    out = []
    for g in cluster(items):
        g.sort(key=lambda x: x["pct"] * WEIGHT.get(x["kind"], 0.8), reverse=True)
        rep = dict(g[0])
        sources = []
        for x in g:
            if x["source"] not in sources:
                sources.append(x["source"])
        rep["published"] = max(x["published"] for x in g)
        also = set(rep.get("also") or [])
        also.update(x["category"] for x in g if x["category"] != rep["category"])
        rep["also"] = sorted(also)
        rep["n"] = len(sources)
        rep["more"] = sources[1:4]
        out.append(rep)
    # 3) heat = engagement x recency x cross-source boost
    for it in out:
        age_h = max((NOW - it["published"]).total_seconds() / 3600, 0)
        rec = SNAPSHOT.get(it["kind"], 0.5 ** (age_h / HALF_LIFE_H))
        boost = 1 + 0.18 * min(it["n"] - 1, 6)
        eng = it["pct"] * WEIGHT.get(it["kind"], 0.8)
        it["top"] = round(100 * eng * boost / 2.08, 2)
        it["score"] = round(100 * (0.2 + 0.8 * eng) * (0.3 + 0.7 * rec) * boost / 2.08, 2)
        it["age_h"] = age_h
    return out


def main():
    t0 = time.time()
    meta_sources, items = [], []

    def run(sid):
        name, fn = SOURCES[sid]
        s = time.time()
        try:
            got = [x for x in fn() if x.get("title") and x.get("url")]
            return sid, name, got, None, time.time() - s
        except Exception as e:  # noqa: BLE001 - isolate every source
            return sid, name, [], f"{type(e).__name__}: {str(e)[:120]}", time.time() - s

    with cf.ThreadPoolExecutor(12) as ex:
        for sid, name, got, err, dt in ex.map(run, SOURCES):
            for x in got:
                x["_sid"] = sid
            items += got
            meta_sources.append(dict(id=sid, name=name, ok=err is None and len(got) > 0,
                                     count=len(got), ms=int(dt * 1000), error=err or (None if got else "no items")))

    cutoff = NOW - timedelta(hours=WINDOW_H)
    items = [x for x in items if x["published"] >= cutoff]
    ranked = sorted(rank(items), key=lambda x: x["score"], reverse=True)

    # keep a floor per category, then fill by heat
    keep, ids = [], set()
    for key, _ in CATEGORIES:
        for x in [x for x in ranked if x["category"] == key][:MIN_PER_CAT]:
            keep.append(x); ids.add(id(x))
    for x in ranked:
        if len(keep) >= CAP:
            break
        if id(x) not in ids:
            keep.append(x); ids.add(id(x))
    keep.sort(key=lambda x: x["score"], reverse=True)

    # heat level (1-3) from global distribution
    scores = sorted((x["score"] for x in keep), reverse=True)
    t3 = scores[max(int(len(scores) * 0.08) - 1, 0)] if scores else 0
    t2 = scores[max(int(len(scores) * 0.30) - 1, 0)] if scores else 0

    data_items = []
    for x in keep:
        d = dict(id=hashlib.sha1(x["url"].encode()).hexdigest()[:12], title=x["title"], url=x["url"],
                 source=x["source"], category=x["category"], published_utc=iso(x["published"]),
                 score=x["score"], top=x["top"], heat=3 if x["score"] >= t3 else 2 if x["score"] >= t2 else 1)
        for k in ("summary", "image", "who", "discuss", "also", "more"):
            if x.get(k):
                d[k] = x[k]
        if x.get("n", 1) > 1:
            d["n"] = x["n"]
        data_items.append(d)

    counts = {k: sum(1 for d in data_items if d["category"] == k or k in d.get("also", [])) for k, _ in CATEGORIES}
    data = dict(meta=dict(generated_utc=iso(NOW), build_seconds=round(time.time() - t0, 2),
                          total=len(data_items), categories=[dict(key=k, label=l, count=counts[k]) for k, l in CATEGORIES],
                          sources=sorted(meta_sources, key=lambda s: s["id"])),
                items=data_items)
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    # Safety net: if nearly everything failed (network outage), keep the last good feed.
    if len(data_items) < 50 and os.path.exists(OUT):
        try:
            with open(OUT, encoding="utf-8") as f:
                prev = len(json.load(f).get("items", []))
        except (OSError, ValueError):
            prev = 0
        if prev > len(data_items):
            print(f"PULSE: only {len(data_items)} items this run; keeping previous data.json ({prev} items).")
            return
    tmp = OUT + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, OUT)

    ok = sum(s["ok"] for s in meta_sources)
    print(f"PULSE: {len(data_items)} items from {ok}/{len(meta_sources)} sources in {time.time() - t0:.1f}s")
    for s in sorted(meta_sources, key=lambda s: s["id"]):
        print(f"  {'ok ' if s['ok'] else 'ERR'} {s['id']:<22} {s['count']:>4}  {s['ms']:>5}ms  {s['error'] or ''}")
    print("  per category:", ", ".join(f"{k}={v}" for k, v in counts.items()))


if __name__ == "__main__":
    main()
