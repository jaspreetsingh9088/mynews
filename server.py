#!/usr/bin/env python3
"""
India Finance Brief
- Fetches finance/market news from free RSS feeds
- Fetches live market prices from free APIs
- Serves a beautiful single-page app
- Runs locally OR on Render/Railway for free
"""

import http.server
import json
import urllib.request
import urllib.parse
import xml.etree.ElementTree as ET
import re, gzip, ssl, threading, time, os
from datetime import datetime

PORT = int(os.environ.get("PORT", 8765))

# ── FINANCE RSS FEEDS (free, no key) ─────────────────────────────────────────
FEEDS = [
    {"name": "Economic Times Markets",  "short": "ET",    "url": "https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms"},
    {"name": "Economic Times Economy",  "short": "ET",    "url": "https://economictimes.indiatimes.com/news/economy/rssfeeds/1373380680.cms"},
    {"name": "Moneycontrol",            "short": "MC",    "url": "https://www.moneycontrol.com/rss/homepage.xml"},
    {"name": "LiveMint Markets",        "short": "Mint",  "url": "https://www.livemint.com/rss/markets"},
    {"name": "Business Standard Mkt",  "short": "BS",    "url": "https://www.business-standard.com/rss/markets-106.rss"},
    {"name": "Financial Express",       "short": "FE",    "url": "https://www.financialexpress.com/market/feed/"},
    {"name": "Zee Business",            "short": "Zee",   "url": "https://www.zeebiz.com/feeds/rss/markets"},
    {"name": "CNBC TV18 Markets",       "short": "CNBC",  "url": "https://www.cnbctv18.com/commonfeeds/v1/eng/rss/market.xml"},
    {"name": "Google Finance News",     "short": "GNews", "url": "https://news.google.com/rss/search?q=india+stock+market+sensex+nifty&hl=en-IN&gl=IN&ceid=IN:en"},
    {"name": "Yahoo Finance",           "short": "Yahoo", "url": "https://finance.yahoo.com/rss/topstories"},
]

# ── MARKET DATA SOURCES (free, no key) ───────────────────────────────────────
# Using multiple free sources with fallbacks
MARKET_SYMBOLS = {
    "sensex":  {"yahoo": "^BSESN",   "label": "SENSEX",    "sub": "BSE Index"},
    "nifty":   {"yahoo": "^NSEI",    "label": "NIFTY 50",  "sub": "NSE Index"},
    "usdinr":  {"yahoo": "USDINR=X", "label": "USD/INR",   "sub": "Exchange Rate"},
    "gold":    {"yahoo": "GC=F",     "label": "Gold",      "sub": "USD/oz Futures"},
    "crude":   {"yahoo": "BZ=F",     "label": "Crude Oil", "sub": "Brent USD/bbl"},
    "bitcoin": {"yahoo": "BTC-USD",  "label": "Bitcoin",   "sub": "BTC/USD"},
}

# Simple in-memory cache
_cache = {}
CACHE_SECS = 1800  # 30 min

def cached(key, fn):
    now = time.time()
    if key in _cache and now - _cache[key]["ts"] < CACHE_SECS:
        return _cache[key]["data"]
    data = fn()
    _cache[key] = {"data": data, "ts": now}
    return data

# ── HTTP HELPERS ──────────────────────────────────────────────────────────────
def make_ctx():
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-IN,en-US;q=0.9,en;q=0.8",
    "Accept-Encoding": "gzip, deflate",
    "Cache-Control": "no-cache",
}

def fetch(url, timeout=12, extra_headers=None):
    req = urllib.request.Request(url, headers={**HEADERS, **(extra_headers or {})})
    with urllib.request.urlopen(req, timeout=timeout, context=make_ctx()) as r:
        raw = r.read()
        try:    return gzip.decompress(raw).decode("utf-8", "ignore")
        except: return raw.decode("utf-8", "ignore")

# ── RSS PARSER ────────────────────────────────────────────────────────────────
def strip_html(text):
    if not text: return ""
    text = re.sub(r"<[^>]+>", " ", text)
    for e, r in [("&amp;","&"),("&lt;","<"),("&gt;",">"),("&quot;",'"'),("&#39;","'"),("&nbsp;"," ")]:
        text = text.replace(e, r)
    return re.sub(r"\s+", " ", text).strip()

def rel_time(s):
    fmts = ["%a, %d %b %Y %H:%M:%S %z", "%a, %d %b %Y %H:%M:%S GMT",
            "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%SZ"]
    for f in fmts:
        try:
            dt = datetime.strptime(s.strip(), f).replace(tzinfo=None)
            m = int((datetime.utcnow() - dt).total_seconds() / 60)
            if m < 1:   return "Just now"
            if m < 60:  return f"{m} min ago"
            if m < 1440:return f"{m//60} hr ago"
            return f"{m//1440}d ago"
        except: pass
    return "Today"

def tag_category(text):
    t = text.lower()
    if any(w in t for w in ["sensex","nifty","bse","nse","stock","share","equity","ipo","fii","dii"]): return "Markets"
    if any(w in t for w in ["rbi","repo","rate","inflation","gdp","fiscal","budget","revenue","tax"]): return "Economy"
    if any(w in t for w in ["rupee","dollar","forex","currency","usd","inr","euro","pound"]): return "Currency"
    if any(w in t for w in ["gold","silver","crude","oil","commodity","metal","copper"]): return "Commodity"
    if any(w in t for w in ["bitcoin","crypto","ethereum","blockchain","web3","defi"]): return "Crypto"
    if any(w in t for w in ["bank","loan","emi","nbfc","credit","insurance","mutual fund","sip","mf"]): return "Banking"
    if any(w in t for w in ["startup","vc","funding","unicorn","ipo","sebi","merger","acquisition"]): return "Corporate"
    return "Finance"

def parse_feed(xml_text, feed, seen):
    items = []
    try:
        xml_text = re.sub(r'xmlns[^"]*"[^"]*"', '', xml_text)
        xml_text = re.sub(r'</?[a-z]+:[a-z]+[^>]*>', '', xml_text)
        root = ET.fromstring(xml_text)
        for item in root.findall(".//item")[:6]:
            title   = strip_html(item.findtext("title",""))
            desc    = strip_html(item.findtext("description",""))
            link    = item.findtext("link","") or item.findtext("guid","")
            pubdate = item.findtext("pubDate","") or item.findtext("updated","")
            if not title or len(title) < 12: continue
            key = title[:45].lower()
            if key in seen: continue
            seen.add(key)
            summary = desc[:280] if desc else title
            # Keep only 2 sentences
            sents = re.split(r'(?<=[.!?])\s+', summary)
            summary = " ".join(sents[:2])
            if len(summary) < 15: summary = title
            items.append({
                "headline": title,
                "summary": summary[:280],
                "source": feed["short"],
                "link": link,
                "time": rel_time(pubdate),
                "category": tag_category(title + " " + desc),
            })
    except Exception as e:
        print(f"[PARSE] {feed['short']}: {e}")
    return items

# ── FETCH ALL NEWS ────────────────────────────────────────────────────────────
def get_news():
    def _fetch():
        all_items, seen, errors = [], set(), []
        lock = threading.Lock()

        def one(feed):
            try:
                xml = fetch(feed["url"], timeout=10)
                items = parse_feed(xml, feed, seen)
                with lock:
                    all_items.extend(items)
                print(f"[RSS] ✅ {feed['short']}: {len(items)} items")
            except Exception as e:
                with lock: errors.append(feed["short"])
                print(f"[RSS] ❌ {feed['short']}: {str(e)[:60]}")

        ts = [threading.Thread(target=one, args=(f,)) for f in FEEDS]
        for t in ts: t.daemon = True; t.start()
        for t in ts: t.join(timeout=14)

        # Deduplicate and pick top 10 with source variety
        result, src_count = [], {}
        for item in all_items:
            s = item["source"]
            if src_count.get(s, 0) < 2 and len(result) < 10:
                result.append(item)
                src_count[s] = src_count.get(s, 0) + 1
        for item in all_items:
            if len(result) >= 10: break
            if item not in result: result.append(item)

        for i, item in enumerate(result[:10], 1):
            item["id"] = i

        return {
            "date": datetime.now().strftime("%A, %d %B %Y"),
            "fetchTime": datetime.now().strftime("%I:%M %p"),
            "news": result[:10],
            "errors": errors,
            "sources": list(set(n["source"] for n in result)),
        }
    return cached("news", _fetch)

# ── FETCH MARKET DATA ─────────────────────────────────────────────────────────
def fetch_yahoo(symbol):
    """Fetch single quote from Yahoo Finance (free, no key)."""
    encoded = urllib.parse.quote(symbol)
    url = f"https://query2.finance.yahoo.com/v8/finance/chart/{encoded}?interval=1d&range=1d"
    try:
        text = fetch(url, timeout=10, extra_headers={"Accept": "application/json"})
        j = json.loads(text)
        meta = j["chart"]["result"][0]["meta"]
        price = meta.get("regularMarketPrice", 0)
        prev  = meta.get("previousClose") or meta.get("chartPreviousClose") or price
        pct   = ((price - prev) / prev * 100) if prev else 0
        sign  = "+" if pct >= 0 else ""
        return {"value": f"{price:,.2f}", "change": f"{sign}{pct:.2f}%", "up": pct >= 0}
    except Exception as e:
        print(f"[MKT] ❌ {symbol}: {e}")
        return None

def fetch_coinbase_btc():
    """CoinBase free public API for BTC price."""
    try:
        text = fetch("https://api.coinbase.com/v2/prices/BTC-USD/spot", timeout=8,
                     extra_headers={"Accept":"application/json"})
        j = json.loads(text)
        price = float(j["data"]["amount"])
        return {"value": f"{price:,.0f}", "change": "live", "up": True}
    except Exception as e:
        print(f"[MKT] Coinbase fallback failed: {e}")
        return None

def fetch_coingecko():
    """CoinGecko free API for crypto prices."""
    try:
        text = fetch("https://api.coingecko.com/api/v3/simple/price?ids=bitcoin,ethereum&vs_currencies=usd",
                     timeout=8, extra_headers={"Accept":"application/json"})
        j = json.loads(text)
        btc = j.get("bitcoin",{}).get("usd",0)
        if btc:
            return {"value": f"{btc:,.0f}", "change": "live", "up": True}
    except Exception as e:
        print(f"[MKT] CoinGecko failed: {e}")
    return None

def fetch_open_exchange():
    """Open exchange rates - USD/INR free."""
    try:
        text = fetch("https://open.er-api.com/v6/latest/USD", timeout=8,
                     extra_headers={"Accept":"application/json"})
        j = json.loads(text)
        inr = j["rates"].get("INR", 0)
        if inr:
            return {"value": f"₹{inr:.2f}", "change": "live", "up": False}
    except Exception as e:
        print(f"[MKT] OpenExchange failed: {e}")
    return None

def get_markets():
    def _fetch():
        result = {}
        lock = threading.Lock()

        def fetch_sym(key, symbol):
            data = fetch_yahoo(symbol)
            if data:
                with lock: result[key] = data

        threads = []
        for key, info in MARKET_SYMBOLS.items():
            t = threading.Thread(target=fetch_sym, args=(key, info["yahoo"]))
            t.daemon = True
            threads.append(t)
        for t in threads: t.start()
        for t in threads: t.join(timeout=12)

        # Fallbacks for missing data
        if "bitcoin" not in result:
            btc = fetch_coinbase_btc() or fetch_coingecko()
            if btc: result["bitcoin"] = btc
        if "usdinr" not in result:
            inr = fetch_open_exchange()
            if inr: result["usdinr"] = inr

        # Build simple analysis text
        lines = []
        if "sensex" in result:
            s = result["sensex"]
            dir_ = "rose" if s["up"] else "fell"
            lines.append(f"SENSEX {dir_} to {s['value']} ({s['change']}).")
        if "nifty" in result:
            n = result["nifty"]
            lines.append(f"NIFTY 50 at {n['value']} ({n['change']}).")
        if "usdinr" in result:
            r = result["usdinr"]
            lines.append(f"Rupee at {r['value']} per USD.")
        if "gold" in result:
            g = result["gold"]
            lines.append(f"Gold at ${g['value']}/oz ({g['change']}).")
        if "crude" in result:
            c = result["crude"]
            lines.append(f"Brent Crude at ${c['value']}/bbl ({c['change']}).")
        if "bitcoin" in result:
            b = result["bitcoin"]
            lines.append(f"Bitcoin at ${b['value']}.")
        analysis = " ".join(lines) if lines else "Market data temporarily unavailable. Please try again shortly."

        return {"market": result, "marketAnalysis": analysis, "labels": MARKET_SYMBOLS}

    return cached("market", _fetch)

# ── HTTP SERVER ───────────────────────────────────────────────────────────────
class Handler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print(f"[{datetime.now().strftime('%H:%M:%S')}] {fmt % args}")

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def do_OPTIONS(self):
        self.send_response(200); self._cors(); self.end_headers()

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/api/news":      self._json(get_news())
        elif path == "/api/market":  self._json(get_markets())
        elif path == "/api/status":  self._json({"ok": True, "time": datetime.now().strftime("%H:%M")})
        elif path == "/" or path == "/index.html":
            self._serve_file("index.html", "text/html")
        else:
            self.send_response(404); self.end_headers()

    def _serve_file(self, filename, ctype):
        try:
            with open(filename, "rb") as f:
                data = f.read()
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except FileNotFoundError:
            self.send_response(404); self.end_headers()

    def _json(self, data):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._cors()
        self.end_headers()
        self.wfile.write(body)

if __name__ == "__main__":
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    print(f"\n{'='*50}")
    print(f"  🇮🇳  INDIA FINANCE BRIEF")
    print(f"{'='*50}")
    print(f"  ✅  Free — No API key needed")
    print(f"  ✅  http://localhost:{PORT}")
    print(f"  ⛔  Ctrl+C to stop")
    print(f"{'='*50}\n")
    server = http.server.HTTPServer(("0.0.0.0", PORT), Handler)
    try:    server.serve_forever()
    except KeyboardInterrupt: print("\nStopped.")
