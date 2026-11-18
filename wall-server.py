#!/usr/bin/env python3
"""
Market Command Center - local data server + static host.
Runs on the display PC. Serves market-wall.html and live data under /api/*
so the dashboard needs no public CORS proxy and no API key.
Only uses the Python standard library.
"""
import json, time, re, html, threading, urllib.parse, urllib.request, base64
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os, sys

PORT = int(os.environ.get("PORT", "8750"))   # Render/any cloud host sets $PORT; locally defaults to 8750
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
HERE = os.path.dirname(os.path.abspath(__file__))

# ====================================================================
#  MANUAL VIDEO  (optional)
#  Paste a YouTube link between the quotes to force ONE specific stream
#  for the top-left video. Leave it "" to use the automatic Bloomberg feed.
#  Works with any of these:
#    a live/video link : "https://www.youtube.com/watch?v=XXXXXXXXXXX"
#    a short link      : "https://youtu.be/XXXXXXXXXXX"
#    a channel's live  : "https://www.youtube.com/channel/UCxxxxxxxx/live"
#  After editing, save and restart (run start-wall.bat again).
MANUAL_VIDEO = "https://www.youtube.com/live/QB5BNdBFujE"
# ====================================================================

# ====================================================================
#  GLOBAL EVENTS  (edit this list weekly)
#  Summits / Forums / Peace talks / Deals / Government meetings shown in
#  the right-hand panel. Add, remove or change lines any time, then save
#  and restart (start-wall.bat). Keep the format exactly like the examples.
#    date  : "YYYY-MM-DD"
#    title : short name of the event
#    type  : Summit | Forum | Peace Talks | Deal | Meeting   (colours the tag)
#    loc   : city / country
#  Past events drop off automatically; nearest upcoming show first.
WORLD_EVENTS = [
    {"date": "2026-11-20", "title": "COP31 UN Climate Summit",   "type": "Summit",      "loc": "Antalya, Turkiye"},
    {"date": "2026-11-21", "title": "Ukraine Peace Talks",        "type": "Peace Talks", "loc": "Europe"},
    {"date": "2026-12-02", "title": "UN Water Conference 2026",   "type": "Forum",       "loc": "Abu Dhabi, UAE"},
    {"date": "2026-12-14", "title": "G20 Leaders Summit",         "type": "Summit",      "loc": "Miami, USA"},
    {"date": "2027-01-18", "title": "World Economic Forum (Davos)","type": "Forum",      "loc": "Davos, Switzerland"},
]
# ====================================================================

def page_html():
    """Serve the dashboard page. Prefer an on-disk market-wall.html if the user
    has one next to this script; otherwise use the copy embedded below. This lets
    the whole dashboard ship as a SINGLE file (wall-server.py) so there is no
    separate .html download for the browser's virus scanner to block."""
    # Always serve the embedded page so the delivered wall-server.py is the single
    # source of truth (a stale market-wall.html left in the folder can't override it).
    data = base64.b64decode(EMBED_HTML_B64)
    # inject the manual video choice (sanitised) into the page
    try:
        mv = (MANUAL_VIDEO or "").replace('"', "").replace("\\", "").strip()
        data = data.replace(b"__MANUAL_VIDEO__", mv.encode("utf-8"))
    except Exception:
        pass
    return data

# symbol -> display name  (Yahoo Finance symbols)
SYMBOLS = {
    "^GSPC": "S&P 500", "^NDX": "NASDAQ 100", "DX-Y.NYB": "DXY",
    "GC=F": "Gold", "SI=F": "Silver", "INR=X": "USDINR", "CL=F": "WTI Crude", "BTC-USD": "BTC",
    "^KS11": "KOSPI", "^TWII": "Taipei TWSE", "^HSI": "Hang Seng", "AAXJ": "MSCI Asia",
    "^N225": "Nikkei 225", "^RUT": "Russell 2000",
}

NEWS_FEEDS = [
    ("CNBC", "https://www.cnbc.com/id/100003114/device/rss/rss.html"),
    ("CNBC Markets", "https://www.cnbc.com/id/20910258/device/rss/rss.html"),
    ("MarketWatch", "http://feeds.marketwatch.com/marketwatch/topstories/"),
    ("Yahoo Finance", "https://finance.yahoo.com/news/rssindex"),
    ("CoinDesk", "https://www.coindesk.com/arc/outboundfeeds/rss/"),
    ("Investing.com", "https://www.investing.com/rss/news_25.rss"),
]

_cache = {}
_lock = threading.Lock()

def cached(key, ttl, fn):
    now = time.time()
    with _lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < ttl:
            return hit[1]
    try:
        val = fn()
    except Exception as e:
        # serve stale on error if we have it
        if hit:
            return hit[1]
        raise
    with _lock:
        _cache[key] = (now, val)
    return val

def http_get(url, headers=None, timeout=12):
    h = {"User-Agent": UA, "Accept": "*/*"}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, headers=h)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")

# ---------------- quotes (Yahoo Finance chart) ----------------
def yahoo_one(sym):
    q = urllib.parse.quote(sym) + "?range=1d&interval=5m&includePrePost=true"
    urls = ["https://query1.finance.yahoo.com/v8/finance/chart/" + q,
            "https://query2.finance.yahoo.com/v8/finance/chart/" + q]
    d = None
    for u in urls:
        try:
            d = json.loads(http_get(u)); break
        except Exception:
            continue
    if d is None:
        return {"price": None, "pct": None, "series": []}
    try:
        r = d["chart"]["result"][0]
        meta = r["meta"]
        price = meta.get("regularMarketPrice")
        prev = meta.get("chartPreviousClose") or meta.get("previousClose")
        closes = []
        try:
            closes = [c for c in r["indicators"]["quote"][0]["close"] if c is not None]
        except Exception:
            pass
        high = meta.get("regularMarketDayHigh") or (max(closes) if closes else price)
        low = meta.get("regularMarketDayLow") or (min(closes) if closes else price)
        pct = ((price - prev) / prev * 100.0) if (price and prev) else 0.0
        return {"price": price, "pct": round(pct, 2), "prev": prev, "high": high, "low": low, "series": closes[-72:]}
    except Exception:
        return {"price": None, "pct": None, "series": []}

def quotes():
    def build():
        out = {}
        with ThreadPoolExecutor(max_workers=8) as ex:
            res = dict(zip(SYMBOLS.keys(), ex.map(lambda s: cached("q:" + s, 10, lambda s=s: yahoo_one(s)), SYMBOLS.keys())))
        for s, name in SYMBOLS.items():
            q = res.get(s) or {}
            out[s] = {"name": name, "price": q.get("price"), "pct": q.get("pct"), "high": q.get("high"), "low": q.get("low"), "series": q.get("series", [])}
        return out
    return build()

# ---------------- economic calendar ----------------
# Primary source: ForexFactory's free weekly JSON (reliable actual/forecast/previous,
# updated as events release). Fallback: TradingView events endpoint.
FF_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
FF_URL_NEXT = "https://nfs.faireconomy.media/ff_calendar_nextweek.json"

def _parse_dt(s):
    if not s:
        return None
    try:
        d = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return d
    except Exception:
        return None

def _has_val(v):
    return v is not None and str(v).strip() not in ("", "-")

def _process_cal(rows, now=None):
    """Pure: sort events, then show the 3 most recent releases that already have an
    ACTUAL value, followed by what's coming up next. Keeps the ACTUAL column populated
    instead of a wall of blanks."""
    now = now or datetime.now(timezone.utc)
    rows = [r for r in rows if r.get("title")]
    rows.sort(key=lambda r: _parse_dt(r.get("date")) or datetime.max.replace(tzinfo=timezone.utc))
    past, future = [], []
    for r in rows:
        d = _parse_dt(r.get("date"))
        if d is None:
            continue
        if d < now and _has_val(r.get("actual")):
            past.append(r)
        elif d >= now:
            future.append(r)
    return (past[-2:] + future)[:16]

def _ff_calendar():
    # this week + next week, high-impact only, so the panel fills with upcoming events
    data = []
    for u in (FF_URL, FF_URL_NEXT):
        try:
            data += json.loads(http_get(u, timeout=12)) or []
        except Exception:
            pass
    rows = []
    for e in data:
        if str(e.get("impact", "")).strip().lower() != "high":
            continue
        rows.append({
            "title": e.get("title"), "country": e.get("country"),   # currency code, e.g. USD
            "date": e.get("date"),
            "actual": e.get("actual"), "forecast": e.get("forecast"),
            "previous": e.get("previous"), "unit": "", "period": "",
        })
    return rows

def _tv_calendar():
    now = datetime.now(timezone.utc)
    frm = (now - timedelta(days=3)).strftime("%Y-%m-%dT00:00:00.000Z")
    to = (now + timedelta(days=7)).strftime("%Y-%m-%dT23:59:59.000Z")
    url = ("https://economic-calendar.tradingview.com/events?from=" + frm + "&to=" + to
           + "&countries=" + urllib.parse.quote("US,EU,GB,JP,CN,CA,AU,CH"))
    d = json.loads(http_get(url, headers={"Origin": "https://www.tradingview.com",
                                          "Referer": "https://www.tradingview.com/"}))
    rows = []
    CC = {"US": "USD", "EU": "EUR", "GB": "GBP", "JP": "JPY", "CN": "CNY",
          "CA": "CAD", "AU": "AUD", "CH": "CHF", "NZ": "NZD"}
    for e in d.get("result", []):
        if e.get("importance", -1) < 1:
            continue
        rows.append({
            "title": e.get("title"), "country": CC.get(e.get("country"), e.get("country")),
            "date": e.get("date"), "actual": e.get("actual"),
            "forecast": e.get("forecast"), "previous": e.get("previous"),
            "unit": e.get("unit") or "", "period": e.get("period") or "",
        })
    return rows

def calendar():
    def build():
        rows = []
        try:
            rows = _ff_calendar()
        except Exception:
            rows = []
        if not rows:
            try:
                rows = _tv_calendar()
            except Exception:
                rows = []
        return _process_cal(rows)
    return build()

# ---------------- news (RSS) ----------------
IMG_RE = re.compile(r'<img[^>]+?(?:data-src|src)=["\']([^"\']+)["\']', re.I)
OG_RE = re.compile(
    r'<meta[^>]+property=["\']og:image(?::url)?["\'][^>]+content=["\']([^"\']+)'
    r'|<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image'
    r'|<meta[^>]+name=["\']twitter:image(?::src)?["\'][^>]+content=["\']([^"\']+)', re.I)
def _tag(item, tag):
    m = re.search(r"<" + tag + r"[^>]*>(.*?)</" + tag + r">", item, re.S | re.I)
    if not m:
        return ""
    t = m.group(1)
    t = re.sub(r"<!\[CDATA\[(.*?)\]\]>", r"\1", t, flags=re.S)
    return html.unescape(re.sub(r"<[^>]+>", "", t)).strip()
def _attr(item, tag, attr):
    m = re.search(r"<" + tag + r"[^>]*\b" + attr + r'=["\']([^"\']+)["\']', item, re.I)
    return m.group(1) if m else None
def _clean_img(u):
    if not u:
        return None
    u = html.unescape(u.strip())
    if u.startswith("//"):
        u = "https:" + u
    return u if u.startswith("http") else None
def _link(item):
    # RSS <link>text</link> or Atom <link href="..."/>
    t = _tag(item, "link")
    if t.startswith("http"):
        return t
    return _attr(item, "link", "href")
def _rss_img(b):
    # try every common place an image hides in an RSS/Atom item
    for t, a in (("media:content", "url"), ("media:thumbnail", "url"),
                 ("enclosure", "url"), ("image", "href")):
        v = _clean_img(_attr(b, t, a))
        if v:
            return v
    # <image><url>..</url></image>
    m = re.search(r"<image\b[^>]*>.*?<url>\s*(.*?)\s*</url>", b, re.S | re.I)
    if m:
        v = _clean_img(m.group(1))
        if v:
            return v
    # first <img> anywhere (description / content:encoded)
    m = IMG_RE.search(b)
    return _clean_img(m.group(1)) if m else None

def og_image(link):
    if not link:
        return None
    def fetch():
        try:
            doc = http_get(link, timeout=9)
        except Exception:
            return ""          # cache the miss too, so we don't refetch every cycle
        m = OG_RE.search(doc)
        if not m:
            return ""
        return _clean_img(next((g for g in m.groups() if g), None)) or ""
    try:
        return cached("og:" + link, 6 * 3600, fetch) or None
    except Exception:
        return None

def events():
    """Return the manually-curated world events, nearest upcoming first,
    dropping anything more than 2 days in the past."""
    today = datetime.now(timezone.utc).date()
    rows = []
    for e in WORLD_EVENTS:
        try:
            d = datetime.strptime(str(e.get("date", "")).strip(), "%Y-%m-%d").date()
        except Exception:
            continue
        if (today - d).days > 2:          # already finished
            continue
        rows.append({"date": e.get("date"), "title": e.get("title", ""),
                     "type": e.get("type", "Meeting"), "loc": e.get("loc", "")})
    rows.sort(key=lambda r: r.get("date") or "")
    return rows[:10]

def news():
    def one(src, url):
        out = []
        try:
            xml = http_get(url, timeout=10)
            blocks = re.findall(r"<item\b.*?</item>", xml, re.S | re.I)
            if not blocks:
                blocks = re.findall(r"<entry\b.*?</entry>", xml, re.S | re.I)
            for b in blocks[:10]:
                out.append({"title": _tag(b, "title"), "src": src,
                            "img": _rss_img(b), "link": _link(b),
                            "date": _tag(b, "pubDate") or _tag(b, "published") or _tag(b, "updated")})
        except Exception:
            pass
        return out
    def build():
        items = []
        with ThreadPoolExecutor(max_workers=6) as ex:
            for lst in ex.map(lambda f: one(*f), NEWS_FEEDS):
                items += lst
        seen, uniq = set(), []
        for it in items:
            t = it.get("title")
            if t and t not in seen:
                seen.add(t); uniq.append(it)
        uniq = uniq[:40]
        # og:image fallback for the ones we'll actually show (top 24), concurrently + cached
        need = [it for it in uniq[:24] if not it.get("img") and it.get("link")]
        if need:
            with ThreadPoolExecutor(max_workers=8) as ex:
                imgs = dict(zip((id(it) for it in need),
                                ex.map(lambda it: og_image(it["link"]), need)))
            for it in need:
                it["img"] = imgs.get(id(it))
        return uniq
    return build()


def proxy_classicbullion():
    """Fetch https://classicbullion.com/, inject <base> and a live DOM observer
    that posts live rates to the parent dashboard window every 250ms."""
    try:
        raw = http_get("https://classicbullion.com/", timeout=10)
    except Exception:
        try:
            raw = http_get("http://classicbullion.com/", timeout=10)
        except Exception as e:
            return "<html><body>Error loading classicbullion.com: %s</body></html>" % str(e)
    bridge_js = """
    <script>
    (function(){
      function numAfter(txt, label){
        var idx = txt.toUpperCase().indexOf(label.toUpperCase());
        if(idx === -1) return null;
        var sub = txt.slice(idx + label.length, idx + label.length + 120);
        var m = sub.match(/([0-9]{2,6}(?:\\.[0-9]{1,4})?)/);
        return m ? parseFloat(m[1]) : null;
      }
      function scan(){
        try {
          var t = document.body ? document.body.innerText : '';
          if(!t) return;
          var payload = {
            goldBid:  numAfter(t, 'GOLD Oz Bid'),
            goldAsk:  numAfter(t, 'GOLD Oz Ask'),
            usdInr:   numAfter(t, 'USDINR Ask'),
            silverOz: numAfter(t, 'SILVER Oz'),
            gold1gm:  numAfter(t, 'GOLD 1 GM'),
            gold995:  numAfter(t, 'GOLD 995'),
            gold9999: numAfter(t, 'GOLD 9999'),
            goldTt:   numAfter(t, 'GOLD TT BAR')
          };
          if(payload.goldBid && payload.goldAsk){
            window.parent.postMessage({type:'CLASSIC_BULLION_DOM', payload: payload}, '*');
          }
        } catch(e){}
      }
      setInterval(scan, 250);
      window.addEventListener('load', scan);
    })();
    </script>
    """
    if "<head>" in raw:
        raw = raw.replace("<head>", '<head><base href="https://classicbullion.com/">', 1)
    elif "<HEAD>" in raw:
        raw = raw.replace("<HEAD>", '<HEAD><base href="https://classicbullion.com/">', 1)
    else:
        raw = '<base href="https://classicbullion.com/">' + raw
    if "</body>" in raw:
        raw = raw.replace("</body>", bridge_js + "</body>", 1)
    else:
        raw = raw + bridge_js
    return raw

# ---------------- HTTP handler ----------------
class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass
    def _send(self, code, body, ctype):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass
    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj), "application/json; charset=utf-8")
    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        try:
            if path in ("/", "/index.html", "/market-wall.html"):
                return self._send(200, page_html(), "text/html; charset=utf-8")
            if path == "/api/quotes":
                return self._json(cached("quotes", 10, quotes))
            if path == "/api/calendar":
                return self._json(cached("calendar", 900, calendar))
            if path == "/api/news":
                return self._json(cached("news", 300, news))
            if path == "/api/events":
                return self._json(events())
            if path == "/proxy/classicbullion":
                return self._send(200, proxy_classicbullion(), "text/html; charset=utf-8")
            # static files beside the html
            safe = os.path.normpath(path).lstrip("/\\")
            fp = os.path.join(HERE, safe)
            if os.path.isfile(fp) and fp.startswith(HERE):
                ct = "text/plain"
                if fp.endswith(".css"): ct = "text/css"
                elif fp.endswith(".js"): ct = "application/javascript"
                elif fp.endswith(".png"): ct = "image/png"
                elif fp.endswith(".svg"): ct = "image/svg+xml"
                with open(fp, "rb") as f:
                    return self._send(200, f.read(), ct)
            self._send(404, "not found", "text/plain")
        except Exception as e:
            self._json({"error": str(e)}, 500)




































# ==== EMBEDDED PAGE (auto-generated from market-wall.html) ====
EMBED_HTML_B64 = "PCFkb2N0eXBlIGh0bWw+CjxodG1sIGxhbmc9ImVuIj4KPGhlYWQ+CjxtZXRhIGNoYXJzZXQ9InV0Zi04Ij4KPG1ldGEgbmFtZT0idmlld3BvcnQiIGNvbnRlbnQ9IndpZHRoPWRldmljZS13aWR0aCxpbml0aWFsLXNjYWxlPTEiPgo8dGl0bGU+TWFya2V0IENvbW1hbmQgQ2VudGVyPC90aXRsZT4KPGxpbmsgcmVsPSJwcmVjb25uZWN0IiBocmVmPSJodHRwczovL2ZvbnRzLmdvb2dsZWFwaXMuY29tIj48bGluayByZWw9InByZWNvbm5lY3QiIGhyZWY9Imh0dHBzOi8vZm9udHMuZ3N0YXRpYy5jb20iIGNyb3Nzb3JpZ2luPgo8bGluayByZWw9InN0eWxlc2hlZXQiIGhyZWY9Imh0dHBzOi8vZm9udHMuZ29vZ2xlYXBpcy5jb20vY3NzMj9mYW1pbHk9SW50ZXI6d2dodEA0MDA7NTAwOzYwMDs3MDA7ODAwOzkwMCZkaXNwbGF5PXN3YXAiPgo8c3R5bGU+CiAgOnJvb3R7CiAgICAtLWJnOiMwMDA7IC0tcGFuZWw6IzBiMGQxMjsgLS1wYW5lbC0yOiMwZTExMTg7IC0tY2FyZDojMGQxMDE3OyAtLWxpbmU6IzFhMWYyOTsgLS1saW5lLTI6IzE0MTkyMjsKICAgIC0tZmc6I2YyZjVmYTsgLS1tdXRlZDojOGE5NGE2OyAtLWZhaW50OiM1YTY0NzQ7CiAgICAtLXVwOiMxN2M5ODM7IC0tdXAtYmc6cmdiYSgyMywyMDEsMTMxLC4xNCk7IC0tZG93bjojZmYzYjQ3OyAtLWRvd24tYmc6cmdiYSgyNTUsNTksNzEsLjE0KTsKICAgIC0tZ29sZDojZjBiNDI5OyAtLW9yYW5nZTojZmY3YTFhOyAtLWxpdmU6I2ZmM2I0NzsgLS1hY2NlbnQ6I2YwYjQyOTsKICAgIC0tc2FuczoiSW50ZXIiLHN5c3RlbS11aSwtYXBwbGUtc3lzdGVtLCJTZWdvZSBVSSIsc2Fucy1zZXJpZjsKICB9CiAgKntib3gtc2l6aW5nOmJvcmRlci1ib3g7bWFyZ2luOjA7cGFkZGluZzowfQogIGh0bWwsYm9keXtoZWlnaHQ6MTAwJTt3aWR0aDoxMDAlfQogIGJvZHl7YmFja2dyb3VuZDojMDAwO2NvbG9yOnZhcigtLWZnKTtmb250LWZhbWlseTp2YXIoLS1zYW5zKTtvdmVyZmxvdzpoaWRkZW47LXdlYmtpdC1mb250LXNtb290aGluZzphbnRpYWxpYXNlZDtmb250LXZhcmlhbnQtbnVtZXJpYzp0YWJ1bGFyLW51bXN9CiAgLyogZml4ZWQgMTkyMHgxMDgwIHN0YWdlLCBzY2FsZWQgdG8gZml0IGFueSBkaXNwbGF5IC0+IGlkZW50aWNhbCBwcm9wb3J0aW9ucyBldmVyeXdoZXJlICovCiAgLmFwcHt3aWR0aDoxOTIwcHg7aGVpZ2h0OjEwODBweDt0cmFuc2Zvcm0tb3JpZ2luOnRvcCBsZWZ0O3Bvc2l0aW9uOmFic29sdXRlO3RvcDowO2xlZnQ6MDtkaXNwbGF5OmdyaWQ7Z3JpZC10ZW1wbGF0ZS1yb3dzOjU2cHggODhweCAxZnIgMTI4cHg7Z2FwOjEycHg7cGFkZGluZzoxM3B4O2JhY2tncm91bmQ6dmFyKC0tYmcpfQoKICAvKiBoZWFkZXIgKi8KICAuaGVhZHtkaXNwbGF5OmZsZXg7YWxpZ24taXRlbXM6Y2VudGVyO2p1c3RpZnktY29udGVudDpzcGFjZS1iZXR3ZWVuO3BhZGRpbmc6MCA4cHh9CiAgLmh0aXRsZXtmb250LXdlaWdodDo4MDA7Zm9udC1zaXplOjI4cHg7bGV0dGVyLXNwYWNpbmc6LjA5ZW19CiAgLmhzZXNze2Rpc3BsYXk6ZmxleDtnYXA6MjhweDtmb250LXdlaWdodDo3MDA7Zm9udC1zaXplOjE4cHh9CiAgLmhzZXNzIHNwYW57Y29sb3I6dmFyKC0tbXV0ZWQpfSAuaHNlc3MgYntjb2xvcjp2YXIoLS1mZyk7Zm9udC13ZWlnaHQ6ODAwO21hcmdpbi1sZWZ0OjdweH0KICAuaGNsb2Nre2ZvbnQtd2VpZ2h0OjcwMDtmb250LXNpemU6MTlweDtjb2xvcjp2YXIoLS1tdXRlZCl9IC5oY2xvY2sgYntjb2xvcjp2YXIoLS1mZyk7Zm9udC13ZWlnaHQ6ODAwfQogIC5obGl2ZXtkaXNwbGF5OmlubGluZS1mbGV4O2FsaWduLWl0ZW1zOmNlbnRlcjtnYXA6N3B4O2ZvbnQtd2VpZ2h0OjgwMDtmb250LXNpemU6MTNweDtsZXR0ZXItc3BhY2luZzouMDhlbTtjb2xvcjp2YXIoLS11cCl9CiAgLmhsaXZlIC5kb3R7d2lkdGg6OXB4O2hlaWdodDo5cHg7Ym9yZGVyLXJhZGl1czo1MCU7YmFja2dyb3VuZDp2YXIoLS11cCk7Ym94LXNoYWRvdzowIDAgOXB4IHZhcigtLXVwKTthbmltYXRpb246cHVsc2UgMS40cyBpbmZpbml0ZX0KICAuaGxpdmUuc3RhbGV7Y29sb3I6dmFyKC0tZmFpbnQpfSAuaGxpdmUuc3RhbGUgLmRvdHtiYWNrZ3JvdW5kOnZhcigtLWZhaW50KTtib3gtc2hhZG93Om5vbmU7YW5pbWF0aW9uOm5vbmV9CiAgQGtleWZyYW1lcyBmbHVwezAle2JhY2tncm91bmQ6cmdiYSgyMywyMDEsMTMxLC40NSl9MTAwJXtiYWNrZ3JvdW5kOnRyYW5zcGFyZW50fX0KICBAa2V5ZnJhbWVzIGZsZG57MCV7YmFja2dyb3VuZDpyZ2JhKDI1NSw1OSw3MSwuNDUpfTEwMCV7YmFja2dyb3VuZDp0cmFuc3BhcmVudH19CiAgLmZsdXB7YW5pbWF0aW9uOmZsdXAgLjhzIGVhc2Utb3V0O2JvcmRlci1yYWRpdXM6NXB4fSAuZmxkbnthbmltYXRpb246ZmxkbiAuOHMgZWFzZS1vdXQ7Ym9yZGVyLXJhZGl1czo1cHh9CgogIC8qIHRpY2tlciByb3cgKi8KICAudGlja3tkaXNwbGF5OmZsZXg7YWxpZ24taXRlbXM6Y2VudGVyO2JhY2tncm91bmQ6dmFyKC0tcGFuZWwpO2JvcmRlcjoxcHggc29saWQgdmFyKC0tbGluZSk7Ym9yZGVyLXJhZGl1czoxMnB4O292ZXJmbG93OmhpZGRlbn0KICAudGljay10cmFja3tkaXNwbGF5OmZsZXg7YWxpZ24taXRlbXM6Y2VudGVyO2hlaWdodDoxMDAlO3dpZHRoOm1heC1jb250ZW50O2FuaW1hdGlvbjp0bWFycSB2YXIoLS10ZHVyLDQ2cykgbGluZWFyIGluZmluaXRlO3dpbGwtY2hhbmdlOnRyYW5zZm9ybX0KICBAa2V5ZnJhbWVzIHRtYXJxe2Zyb217dHJhbnNmb3JtOnRyYW5zbGF0ZVgoMCl9dG97dHJhbnNmb3JtOnRyYW5zbGF0ZVgoLTUwJSl9fQogIC50a3tkaXNwbGF5OmZsZXg7YWxpZ24taXRlbXM6Y2VudGVyO2dhcDoxNXB4O3BhZGRpbmc6MCAzNHB4O2hlaWdodDoxMDAlO2JvcmRlci1yaWdodDoxcHggc29saWQgdmFyKC0tbGluZS0yKTt3aGl0ZS1zcGFjZTpub3dyYXB9CiAgLnRrIC5ubXtmb250LXdlaWdodDo4MDA7Zm9udC1zaXplOjMwcHh9CiAgLnRrIC5wcntmb250LXdlaWdodDo3MDA7Zm9udC1zaXplOjMwcHg7Y29sb3I6dmFyKC0tZmcpfQogIC50ayAuY2h7Zm9udC13ZWlnaHQ6ODAwO2ZvbnQtc2l6ZToyNXB4O2Rpc3BsYXk6ZmxleDthbGlnbi1pdGVtczpjZW50ZXI7Z2FwOjdweH0KICAudXB7Y29sb3I6dmFyKC0tdXApfSAuZG93bntjb2xvcjp2YXIoLS1kb3duKX0KCiAgLyogbWFpbiAzIGNvbHMgKi8KICAubWFpbntkaXNwbGF5OmdyaWQ7Z3JpZC10ZW1wbGF0ZS1jb2x1bW5zOjEuNmZyIC44NmZyIDEuMGZyO2dhcDo5cHg7bWluLWhlaWdodDowfQogIC5jb2x7ZGlzcGxheTpmbGV4O2ZsZXgtZGlyZWN0aW9uOmNvbHVtbjtnYXA6OXB4O21pbi1oZWlnaHQ6MDttaW4td2lkdGg6MH0KICAucGFuZWx7YmFja2dyb3VuZDp2YXIoLS1wYW5lbCk7Ym9yZGVyOjFweCBzb2xpZCB2YXIoLS1saW5lKTtib3JkZXItcmFkaXVzOjE0cHg7ZGlzcGxheTpmbGV4O2ZsZXgtZGlyZWN0aW9uOmNvbHVtbjttaW4taGVpZ2h0OjA7b3ZlcmZsb3c6aGlkZGVufQogIC5wdGl0bGV7Zm9udC13ZWlnaHQ6ODAwO2ZvbnQtc2l6ZToxNXB4O2xldHRlci1zcGFjaW5nOi4xZW07cGFkZGluZzo5cHggMTZweCA2cHg7Y29sb3I6dmFyKC0tZmcpO2ZsZXg6MCAwIGF1dG87ZGlzcGxheTpmbGV4O2FsaWduLWl0ZW1zOmNlbnRlcjtqdXN0aWZ5LWNvbnRlbnQ6c3BhY2UtYmV0d2Vlbn0KICAucHRpdGxlIC5wc3Vie2ZvbnQtd2VpZ2h0OjcwMDtmb250LXNpemU6MTFweDtsZXR0ZXItc3BhY2luZzouMDhlbTtjb2xvcjp2YXIoLS1nb2xkKTtkaXNwbGF5OmlubGluZS1mbGV4O2FsaWduLWl0ZW1zOmNlbnRlcjtnYXA6NnB4fQogIC5wdGl0bGUgLnBzdWIgLmJkb3R7d2lkdGg6N3B4O2hlaWdodDo3cHg7Ym9yZGVyLXJhZGl1czo1MCU7YmFja2dyb3VuZDp2YXIoLS11cCk7Ym94LXNoYWRvdzowIDAgOHB4IHZhcigtLXVwKTthbmltYXRpb246cHVsc2UgMS4ycyBpbmZpbml0ZX0KICAubW9kZS1idG57YmFja2dyb3VuZDojMTUxYTI0O2JvcmRlcjoxcHggc29saWQgIzI2MmY0MDtjb2xvcjp2YXIoLS1tdXRlZCk7Zm9udDo3MDAgMTBweC8xIHZhcigtLXNhbnMpO3BhZGRpbmc6NHB4IDhweDtib3JkZXItcmFkaXVzOjVweDtjdXJzb3I6cG9pbnRlcjtsZXR0ZXItc3BhY2luZzouMDZlbX0KICAubW9kZS1idG4uYWN0aXZle2NvbG9yOnZhcigtLWdvbGQpO2JvcmRlci1jb2xvcjpyZ2JhKDI0MCwxODAsNDEsLjUpO2JhY2tncm91bmQ6cmdiYSgyNDAsMTgwLDQxLC4xMil9CgogIC8qIHZpZGVvICovCiAgLnZpZHtmbGV4OjE7bWluLWhlaWdodDowO3Bvc2l0aW9uOnJlbGF0aXZlO2JhY2tncm91bmQ6IzAwMDtib3JkZXI6MXB4IHNvbGlkIHZhcigtLWxpbmUpO2JvcmRlci1yYWRpdXM6MTRweDtvdmVyZmxvdzpoaWRkZW59CiAgLnZpZCBpZnJhbWV7cG9zaXRpb246YWJzb2x1dGU7aW5zZXQ6MDt3aWR0aDoxMDAlO2hlaWdodDoxMDAlO2JvcmRlcjowfQogIC52dGx7cG9zaXRpb246YWJzb2x1dGU7dG9wOjEycHg7bGVmdDoxMnB4O3otaW5kZXg6MztiYWNrZ3JvdW5kOnJnYmEoMTAsMTAsMTIsLjcyKTtiYWNrZHJvcC1maWx0ZXI6Ymx1cig0cHgpO2JvcmRlci1yYWRpdXM6OHB4O3BhZGRpbmc6N3B4IDEycHg7Zm9udC13ZWlnaHQ6NzAwO2ZvbnQtc2l6ZToxM3B4O2Rpc3BsYXk6ZmxleDthbGlnbi1pdGVtczpjZW50ZXI7Z2FwOjhweH0KICAudnRsIC55dHtjb2xvcjojZmYyZDJkO2ZvbnQtd2VpZ2h0OjkwMH0KICAudnRye3Bvc2l0aW9uOmFic29sdXRlO3RvcDoxMnB4O3JpZ2h0OjEycHg7ei1pbmRleDozO2JhY2tncm91bmQ6I2UwMWIyNDtib3JkZXItcmFkaXVzOjdweDtwYWRkaW5nOjZweCAxMXB4O2ZvbnQtd2VpZ2h0OjkwMDtmb250LXNpemU6MTNweDtsZXR0ZXItc3BhY2luZzouMDhlbTtkaXNwbGF5OmZsZXg7YWxpZ24taXRlbXM6Y2VudGVyO2dhcDo3cHh9CiAgLnZ0ciAuZHt3aWR0aDo4cHg7aGVpZ2h0OjhweDtib3JkZXItcmFkaXVzOjUwJTtiYWNrZ3JvdW5kOiNmZmY7YW5pbWF0aW9uOnB1bHNlIDEuM3MgaW5maW5pdGV9CiAgLnZicntwb3NpdGlvbjphYnNvbHV0ZTtib3R0b206NjJweDtyaWdodDoxMnB4O3otaW5kZXg6MztiYWNrZ3JvdW5kOnJnYmEoMTAsMTAsMTIsLjc4KTtib3JkZXItcmFkaXVzOjZweDtwYWRkaW5nOjVweCAxMHB4O2ZvbnQtd2VpZ2h0OjgwMDtmb250LXNpemU6MTJweH0KICAudmJyIGJ7Y29sb3I6dmFyKC0tZ29sZCl9CiAgLmNoeXJvbntwb3NpdGlvbjphYnNvbHV0ZTtsZWZ0OjA7cmlnaHQ6MDtib3R0b206MDt6LWluZGV4OjN9CiAgLmNoeXJvbiAuYmt7ZGlzcGxheTpmbGV4O2FsaWduLWl0ZW1zOmNlbnRlcjtiYWNrZ3JvdW5kOiMwYTBjMTA7Ym9yZGVyLXRvcDozcHggc29saWQgdmFyKC0tb3JhbmdlKX0KICAuY2h5cm9uIC5iayAudGFne2JhY2tncm91bmQ6dmFyKC0tb3JhbmdlKTtjb2xvcjojMTExO2ZvbnQtd2VpZ2h0OjkwMDtmb250LXNpemU6MTdweDtsZXR0ZXItc3BhY2luZzouMDZlbTtwYWRkaW5nOjEycHggMThweDt3aGl0ZS1zcGFjZTpub3dyYXB9CiAgLmNoeXJvbiAuYmsgLnNjcm9sbHdyYXB7ZmxleDoxO292ZXJmbG93OmhpZGRlbjtwb3NpdGlvbjpyZWxhdGl2ZX0KICAuY2h5cm9uIC5iayAuc2Nyb2xse2Rpc3BsYXk6aW5saW5lLWZsZXg7d2hpdGUtc3BhY2U6bm93cmFwO3BhZGRpbmc6MTJweCAwO2ZvbnQtd2VpZ2h0OjcwMDtmb250LXNpemU6MjFweDtjb2xvcjojZjJkOWM0O2FuaW1hdGlvbjpia21hcnEgdmFyKC0tYmtkdXIsNzVzKSBsaW5lYXIgaW5maW5pdGU7d2lsbC1jaGFuZ2U6dHJhbnNmb3JtfQogIEBrZXlmcmFtZXMgYmttYXJxe2Zyb217dHJhbnNmb3JtOnRyYW5zbGF0ZVgoMCl9dG97dHJhbnNmb3JtOnRyYW5zbGF0ZVgoLTUwJSl9fQoKICAvKiBtaW5pIGNoYXJ0cyByb3cgKi8KICAuY2hhcnRze2Rpc3BsYXk6Z3JpZDtncmlkLXRlbXBsYXRlLWNvbHVtbnM6MWZyIDFmcjtnYXA6OXB4O2ZsZXg6MCAwIDE5MHB4fQogIC5tY3tiYWNrZ3JvdW5kOnZhcigtLXBhbmVsKTtib3JkZXI6MXB4IHNvbGlkIHZhcigtLWxpbmUpO2JvcmRlci1yYWRpdXM6MTRweDtwYWRkaW5nOjExcHggMTNweDtkaXNwbGF5OmZsZXg7ZmxleC1kaXJlY3Rpb246Y29sdW1uO21pbi13aWR0aDowfQogIC5tYyAudG9we2Rpc3BsYXk6ZmxleDthbGlnbi1pdGVtczpiYXNlbGluZTtnYXA6MTBweDtmbGV4LXdyYXA6d3JhcH0KICAubWMgLnN5bXtmb250LXdlaWdodDo5MDA7Zm9udC1zaXplOjE3cHh9CiAgLm1jIC52YWx7Zm9udC13ZWlnaHQ6NzAwO2ZvbnQtc2l6ZToxNXB4O2NvbG9yOnZhcigtLW11dGVkKTttYXJnaW4tbGVmdDphdXRvfQogIC5tYyAuY2hne2ZvbnQtd2VpZ2h0OjgwMDtmb250LXNpemU6MTRweH0KICAubWMgY2FudmFze2ZsZXg6MTt3aWR0aDoxMDAlO21pbi1oZWlnaHQ6MDttYXJnaW4tdG9wOjRweH0KCiAgLyogYXNpYSBjYXJkcyAqLwogIC5hc2lhe2dhcDowO2p1c3RpZnktY29udGVudDpzdHJldGNofQogIC5hY2FyZHtwYWRkaW5nOjAgMjBweDtib3JkZXItdG9wOjFweCBzb2xpZCB2YXIoLS1saW5lLTIpO2Rpc3BsYXk6ZmxleDtmbGV4LWRpcmVjdGlvbjpyb3c7YWxpZ24taXRlbXM6Y2VudGVyO2dhcDoxMnB4O2ZsZXg6MTttaW4taGVpZ2h0OjA7b3ZlcmZsb3c6aGlkZGVufQogIC5hY2FyZDpmaXJzdC1vZi10eXBle2JvcmRlci10b3A6MH0KICAuYWNhcmQgLm5te2ZvbnQtd2VpZ2h0OjcwMDtmb250LXNpemU6MTVweDtjb2xvcjp2YXIoLS1tdXRlZCk7bGV0dGVyLXNwYWNpbmc6LjAyZW07ZmxleDoxO21pbi13aWR0aDowO3doaXRlLXNwYWNlOm5vd3JhcDtvdmVyZmxvdzpoaWRkZW47dGV4dC1vdmVyZmxvdzplbGxpcHNpc30KICAuYWNhcmQgLnZhbHtmb250LXdlaWdodDo4MDA7Zm9udC1zaXplOjI2cHg7bGluZS1oZWlnaHQ6MX0KICAuYWNhcmQgLnBpbGx7Zm9udC1zaXplOjE0cHg7cGFkZGluZzo1cHggMTFweDttaW4td2lkdGg6ODJweDtqdXN0aWZ5LWNvbnRlbnQ6Y2VudGVyfQogIC5waWxse2Rpc3BsYXk6aW5saW5lLWZsZXg7YWxpZ24taXRlbXM6Y2VudGVyO2dhcDo2cHg7Zm9udC13ZWlnaHQ6ODAwO2ZvbnQtc2l6ZToxNXB4O3BhZGRpbmc6NnB4IDEycHg7Ym9yZGVyLXJhZGl1czo5cHh9CiAgLnBpbGwudXB7YmFja2dyb3VuZDp2YXIoLS11cC1iZyk7Y29sb3I6dmFyKC0tdXApO2JveC1zaGFkb3c6MCAwIDAgMXB4IHJnYmEoMjMsMjAxLDEzMSwuMykgaW5zZXR9CiAgLnBpbGwuZG93bntiYWNrZ3JvdW5kOnZhcigtLWRvd24tYmcpO2NvbG9yOnZhcigtLWRvd24pO2JveC1zaGFkb3c6MCAwIDAgMXB4IHJnYmEoMjU1LDU5LDcxLC4zKSBpbnNldH0KCiAgLyogPT09PT09PT09PT09PT09PT09PT0gQ0xBU1NJQyBCVUxMSU9OIExJVkUgUEFORUwgPT09PT09PT09PT09PT09PT09PT0gKi8KICAuYnVsbGlvbi1wYW5lbHtmbGV4OjAgMCBhdXRvO2JhY2tncm91bmQ6dmFyKC0tcGFuZWwpO2JvcmRlcjoxcHggc29saWQgdmFyKC0tbGluZSk7Ym9yZGVyLXJhZGl1czoxNHB4O292ZXJmbG93OmhpZGRlbjtkaXNwbGF5OmZsZXg7ZmxleC1kaXJlY3Rpb246Y29sdW1uO3Bvc2l0aW9uOnJlbGF0aXZlfQogIC5iLXRvcC1waWxsewogICAgbWFyZ2luOjFweCAxMnB4IDZweDsKICAgIGJhY2tncm91bmQ6bGluZWFyLWdyYWRpZW50KDE4MGRlZywjMTYxYjI2IDAlLCMwZjEzMWMgMTAwJSk7CiAgICBib3JkZXI6MXB4IHNvbGlkIHJnYmEoMjQwLDE4MCw0MSwuMzgpOwogICAgYm9yZGVyLXJhZGl1czoyMnB4OwogICAgcGFkZGluZzo0cHggMTFweDsKICAgIGRpc3BsYXk6Z3JpZDsKICAgIGdyaWQtdGVtcGxhdGUtY29sdW1uczoxZnIgMWZyOwogICAgZ2FwOjhweDsKICAgIGJveC1zaGFkb3c6MCA0cHggMTZweCByZ2JhKDAsMCwwLC40NSksIGluc2V0IDAgMXB4IDAgcmdiYSgyNTUsMjU1LDI1NSwuMDYpOwogIH0KICAuYi1zcG90ewogICAgZGlzcGxheTpmbGV4OwogICAgZmxleC1kaXJlY3Rpb246Y29sdW1uOwogICAgYWxpZ24taXRlbXM6Y2VudGVyOwogICAganVzdGlmeS1jb250ZW50OmNlbnRlcjsKICAgIHRleHQtYWxpZ246Y2VudGVyOwogICAgYm9yZGVyLXJpZ2h0OjFweCBzb2xpZCB2YXIoLS1saW5lKTsKICAgIHBhZGRpbmc6MCA0cHg7CiAgfQogIC5iLXNwb3Q6bGFzdC1jaGlsZHtib3JkZXItcmlnaHQ6MH0KICAuYi1zcG90IC5sYmx7CiAgICBmb250LXdlaWdodDo4MDA7CiAgICBmb250LXNpemU6MTIuNXB4OwogICAgY29sb3I6dmFyKC0tbXV0ZWQpOwogICAgbGV0dGVyLXNwYWNpbmc6LjA0ZW07CiAgICBtYXJnaW4tYm90dG9tOjNweDsKICB9CiAgLmItc3BvdCAubWFpbi12YWx7CiAgICBmb250LXdlaWdodDo5MDA7CiAgICBmb250LXNpemU6MjJweDsKICAgIGxpbmUtaGVpZ2h0OjEuMDU7CiAgICBwYWRkaW5nOjFweCA2cHg7CiAgICBib3JkZXItcmFkaXVzOjVweDsKICAgIHRyYW5zaXRpb246Y29sb3IgLjE1czsKICB9CiAgLmItc3BvdCAuaGwtc3ViewogICAgZm9udC13ZWlnaHQ6NzAwOwogICAgZm9udC1zaXplOjExLjVweDsKICAgIGNvbG9yOnZhcigtLW11dGVkKTsKICAgIG1hcmdpbi10b3A6M3B4OwogICAgbGV0dGVyLXNwYWNpbmc6LjAyZW07CiAgfQoKICAuYi10YmwtaGVhZHsKICAgIGJhY2tncm91bmQ6bGluZWFyLWdyYWRpZW50KDkwZGVnLCM5ZTdiMmYgMCUsI2M3OWY0MiA1MCUsIzllN2IyZiAxMDAlKTsKICAgIGNvbG9yOiMwOTBiMTA7CiAgICBmb250LXdlaWdodDo5MDA7CiAgICBmb250LXNpemU6MTIuNXB4OwogICAgbGV0dGVyLXNwYWNpbmc6LjEyZW07CiAgICBwYWRkaW5nOjVweCAxNnB4OwogICAgZGlzcGxheTpncmlkOwogICAgZ3JpZC10ZW1wbGF0ZS1jb2x1bW5zOjEuMWZyIDFmcjsKICAgIHRleHQtdHJhbnNmb3JtOnVwcGVyY2FzZTsKICB9CiAgLmItdGJsLWhlYWQgc3BhbjpsYXN0LWNoaWxke3RleHQtYWxpZ246Y2VudGVyfQoKICAuYi1yb3dze2Rpc3BsYXk6ZmxleDtmbGV4LWRpcmVjdGlvbjpjb2x1bW59CiAgLmItcm93ewogICAgZGlzcGxheTpncmlkOwogICAgZ3JpZC10ZW1wbGF0ZS1jb2x1bW5zOjEuMWZyIDFmcjsKICAgIGFsaWduLWl0ZW1zOmNlbnRlcjsKICAgIHBhZGRpbmc6MnB4IDE2cHg7CiAgICBib3JkZXItdG9wOjFweCBzb2xpZCB2YXIoLS1saW5lLTIpOwogICAgYmFja2dyb3VuZDpyZ2JhKDExLDEzLDE4LC45KTsKICB9CiAgLmItcm93Om50aC1jaGlsZChldmVuKXtiYWNrZ3JvdW5kOnJnYmEoMTYsMjAsMjgsLjc1KX0KICAuYi1yb3cgLmItbmFtZXsKICAgIGZvbnQtd2VpZ2h0OjgwMDsKICAgIGZvbnQtc2l6ZToxNi41cHg7CiAgICBjb2xvcjp2YXIoLS1mZyk7CiAgICBsZXR0ZXItc3BhY2luZzouMDNlbTsKICAgIGJvcmRlci1yaWdodDoxcHggc29saWQgdmFyKC0tbGluZS0yKTsKICAgIHBhZGRpbmctcmlnaHQ6MTBweDsKICB9CiAgLmItcm93IC5iLWNlbGx7CiAgICBkaXNwbGF5OmZsZXg7CiAgICBmbGV4LWRpcmVjdGlvbjpjb2x1bW47CiAgICBhbGlnbi1pdGVtczpjZW50ZXI7CiAgICBqdXN0aWZ5LWNvbnRlbnQ6Y2VudGVyOwogICAgdGV4dC1hbGlnbjpjZW50ZXI7CiAgICBwYWRkaW5nLWxlZnQ6MTBweDsKICB9CiAgLmItcm93IC5iLXByaWNlewogICAgZm9udC13ZWlnaHQ6OTAwOwogICAgZm9udC1zaXplOjIwcHg7CiAgICBsaW5lLWhlaWdodDoxLjA4OwogICAgcGFkZGluZzoxcHggOHB4OwogICAgYm9yZGVyLXJhZGl1czo1cHg7CiAgICB0cmFuc2l0aW9uOmNvbG9yIC4xNXM7CiAgfQogIC5iLXJvdyAuYi1obHsKICAgIGZvbnQtd2VpZ2h0OjYwMDsKICAgIGZvbnQtc2l6ZToxMS41cHg7CiAgICBjb2xvcjp2YXIoLS1tdXRlZCk7CiAgICBtYXJnaW4tdG9wOjJweDsKICB9CiAgLnZhbC11cHtjb2xvcjp2YXIoLS11cCl9CiAgLnZhbC1kb3due2NvbG9yOnZhcigtLWRvd24pfQogIC52YWwtbmV1dHJhbHtjb2xvcjojZmZmfQoKICAvKiBEaXJlY3QgY3JvcHBlZCBjbGFzc2ljYnVsbGlvbi5jb20gaWZyYW1lIG1vZGUgKi8KICAuY2Itc2l0ZS13cmFwewogICAgZGlzcGxheTpub25lOwogICAgd2lkdGg6MTAwJTsKICAgIGhlaWdodDozNzJweDsKICAgIHBvc2l0aW9uOnJlbGF0aXZlOwogICAgb3ZlcmZsb3c6aGlkZGVuOwogICAgYmFja2dyb3VuZDojMTExOwogIH0KICAuY2Itc2l0ZS13cmFwIGlmcmFtZXsKICAgIHBvc2l0aW9uOmFic29sdXRlOwogICAgd2lkdGg6MTM2MHB4OwogICAgaGVpZ2h0OjkyMHB4OwogICAgbGVmdDo1MCU7CiAgICB0b3A6LTI5NXB4OwogICAgdHJhbnNmb3JtOnRyYW5zbGF0ZVgoLTUwJSkgc2NhbGUoMC45NSk7CiAgICB0cmFuc2Zvcm0tb3JpZ2luOnRvcCBjZW50ZXI7CiAgICBib3JkZXI6MDsKICB9CgogIC8qIG1hY3JvICovCiAgLm1hY3Jve2ZsZXg6MTttaW4taGVpZ2h0OjB9CiAgLm1hY3Jve2ZsZXg6MS4zO21pbi1oZWlnaHQ6MH0KICAubWhlYWR7ZGlzcGxheTpncmlkO2dyaWQtdGVtcGxhdGUtY29sdW1uczoxZnIgNzhweCA4OHB4O2dhcDo4cHg7cGFkZGluZzo4cHggMThweDtjb2xvcjp2YXIoLS1mYWludCk7Zm9udC13ZWlnaHQ6NzAwO2ZvbnQtc2l6ZToxM3B4O2xldHRlci1zcGFjaW5nOi4wOWVtfQogIC5taGVhZCAucnt0ZXh0LWFsaWduOnJpZ2h0fQogIC5tYm9keXtvdmVyZmxvdzpoaWRkZW47ZmxleDoxO21pbi1oZWlnaHQ6MDtkaXNwbGF5OmZsZXg7ZmxleC1kaXJlY3Rpb246Y29sdW1uO2p1c3RpZnktY29udGVudDpzcGFjZS1hcm91bmR9CiAgLm1yb3d7ZGlzcGxheTpncmlkO2dyaWQtdGVtcGxhdGUtY29sdW1uczoxZnIgNzhweCA4OHB4O2dhcDo4cHg7YWxpZ24taXRlbXM6Y2VudGVyO3BhZGRpbmc6N3B4IDE4cHg7Ym9yZGVyLXRvcDoxcHggc29saWQgdmFyKC0tbGluZS0yKX0KICAubXJvdyAuZXZ7ZGlzcGxheTpmbGV4O2FsaWduLWl0ZW1zOmZsZXgtc3RhcnQ7Z2FwOjExcHg7bWluLXdpZHRoOjB9CiAgLm1yb3cgLnByLWRvdHt3aWR0aDoxMXB4O2hlaWdodDoxMXB4O2JvcmRlci1yYWRpdXM6NTAlO2JhY2tncm91bmQ6dmFyKC0tZG93bik7bWFyZ2luLXRvcDo2cHg7ZmxleDowIDAgYXV0b30KICAubXJvdyAuZXZ0e21pbi13aWR0aDowfQogIC5tcm93IC50MXtmb250LXdlaWdodDo3MDA7Zm9udC1zaXplOjEzLjVweDtjb2xvcjp2YXIoLS1tdXRlZCl9CiAgLm1yb3cgLnQxIGJ7Y29sb3I6dmFyKC0tYWNjZW50KTtmb250LXdlaWdodDo4MDA7bGV0dGVyLXNwYWNpbmc6LjA0ZW19CiAgLm1yb3cgLnQye2ZvbnQtd2VpZ2h0OjgwMDtmb250LXNpemU6MTYuNXB4O2NvbG9yOnZhcigtLWZnKTt3aGl0ZS1zcGFjZTpub3dyYXA7b3ZlcmZsb3c6aGlkZGVuO3RleHQtb3ZlcmZsb3c6ZWxsaXBzaXN9CiAgLm1yb3cgLmF7dGV4dC1hbGlnbjpyaWdodDtmb250LXdlaWdodDo4MDA7Zm9udC1zaXplOjE4cHg7Y29sb3I6dmFyKC0tbXV0ZWQpfQogIC5tcm93LmRvbmUgLmF7Y29sb3I6dmFyKC0tdXApfQogIC5tcm93IC5me3RleHQtYWxpZ246cmlnaHQ7Zm9udC13ZWlnaHQ6ODAwO2ZvbnQtc2l6ZToxOHB4O2NvbG9yOnZhcigtLW11dGVkKX0KCiAgLyogY2VudHJhbCBiYW5rIHJhdGVzICovCiAgLyogZ2xvYmFsIGV2ZW50cyBwYW5lbCAqLwogIC5ldmVudHN7ZmxleDoxO21pbi1oZWlnaHQ6MH0KICAuZXYtbGlzdHtvdmVyZmxvdzpoaWRkZW47ZmxleDoxO21pbi1oZWlnaHQ6MDtkaXNwbGF5OmZsZXg7ZmxleC1kaXJlY3Rpb246Y29sdW1uO2p1c3RpZnktY29udGVudDpzcGFjZS1hcm91bmQ7cGFkZGluZzoycHggMH0KICAuZXJvd3tkaXNwbGF5OmZsZXg7YWxpZ24taXRlbXM6Y2VudGVyO2dhcDoxM3B4O3BhZGRpbmc6NnB4IDE2cHg7Ym9yZGVyLXRvcDoxcHggc29saWQgdmFyKC0tbGluZS0yKX0KICAuZXJvdzpmaXJzdC1jaGlsZHtib3JkZXItdG9wOjB9CiAgLmVkYXRle2ZsZXg6MCAwIDUycHg7dGV4dC1hbGlnbjpjZW50ZXI7YmFja2dyb3VuZDojMTExNTFkO2JvcmRlcjoxcHggc29saWQgdmFyKC0tbGluZSk7Ym9yZGVyLXJhZGl1czo5cHg7cGFkZGluZzo1cHggMnB4O2xpbmUtaGVpZ2h0OjEuMDV9CiAgLmVkYXRlIGJ7ZGlzcGxheTpibG9jaztmb250LXNpemU6MTFweDtmb250LXdlaWdodDo4MDA7Y29sb3I6dmFyKC0tYWNjZW50KTtsZXR0ZXItc3BhY2luZzouMDZlbX0KICAuZWRhdGUgaXtkaXNwbGF5OmJsb2NrO2ZvbnQtc2l6ZToxOXB4O2ZvbnQtd2VpZ2h0OjkwMDtmb250LXN0eWxlOm5vcm1hbDtjb2xvcjp2YXIoLS1mZyl9CiAgLmVkYXRlLnNvb24gYntjb2xvcjp2YXIoLS1kb3duKX0KICAuZWluZm97bWluLXdpZHRoOjA7ZmxleDoxfQogIC5ldHtkaXNwbGF5OmZsZXg7YWxpZ24taXRlbXM6Y2VudGVyO2dhcDo4cHg7bWluLXdpZHRoOjB9CiAgLmV0YWd7Zm9udC1zaXplOjEwcHg7Zm9udC13ZWlnaHQ6OTAwO2xldHRlci1zcGFjaW5nOi4wNmVtO3BhZGRpbmc6M3B4IDdweDtib3JkZXItcmFkaXVzOjVweDtmbGV4OjAgMCBhdXRvO3RleHQtdHJhbnNmb3JtOnVwcGVyY2FzZX0KICAuZXRhZy5zdW1taXR7YmFja2dyb3VuZDpyZ2JhKDI0MCwxODAsNDEsLjE2KTtjb2xvcjp2YXIoLS1nb2xkKX0KICAuZXRhZy5mb3J1bXtiYWNrZ3JvdW5kOnJnYmEoOTAsMTUwLDI1NSwuMTYpO2NvbG9yOiM2ZmE4ZmZ9CiAgLmV0YWcucGVhY2V7YmFja2dyb3VuZDp2YXIoLS11cC1iZyk7Y29sb3I6dmFyKC0tdXApfQogIC5ldGFnLmRlYWx7YmFja2dyb3VuZDpyZ2JhKDE5MCwxMjAsMjU1LC4xNik7Y29sb3I6I2M3OWJmZn0KICAuZXRhZy5tZWV0aW5ne2JhY2tncm91bmQ6IzFiMjIzMDtjb2xvcjp2YXIoLS1tdXRlZCl9CiAgLmV0IC5uYW1le2ZvbnQtd2VpZ2h0OjgwMDtmb250LXNpemU6MTUuNXB4O2NvbG9yOnZhcigtLWZnKTt3aGl0ZS1zcGFjZTpub3dyYXA7b3ZlcmZsb3c6aGlkZGVuO3RleHQtb3ZlcmZsb3c6ZWxsaXBzaXN9CiAgLmVsb2N7Zm9udC13ZWlnaHQ6NjAwO2ZvbnQtc2l6ZToxMi41cHg7Y29sb3I6dmFyKC0tbXV0ZWQpO21hcmdpbi10b3A6MnB4O3doaXRlLXNwYWNlOm5vd3JhcDtvdmVyZmxvdzpoaWRkZW47dGV4dC1vdmVyZmxvdzplbGxpcHNpc30KICAuZmxhZ3t3aWR0aDoyNnB4O2hlaWdodDoxOHB4O2JvcmRlci1yYWRpdXM6M3B4O29iamVjdC1maXQ6Y292ZXI7dmVydGljYWwtYWxpZ246bWlkZGxlO21hcmdpbi1yaWdodDo5cHg7Ym94LXNoYWRvdzowIDAgMCAxcHggcmdiYSgyNTUsMjU1LDI1NSwuMTIpfQogIC5tcm93IC50MSAuZmxhZ3t3aWR0aDoyMHB4O2hlaWdodDoxNHB4O21hcmdpbi1yaWdodDo2cHg7bWFyZ2luLWxlZnQ6MnB4fQoKICBAa2V5ZnJhbWVzIHB1bHNlezAlLDEwMCV7b3BhY2l0eToxfTUwJXtvcGFjaXR5Oi4zfX0KICAuZGlte2NvbG9yOnZhcigtLWZhaW50KX0KCiAgLyogYm90dG9tIGxpdmUtbmV3cyBzdHJpcCAoZmFzdGVyIHNjcm9sbCArIGltYWdlcykgKi8KICAubmV3c2JhcntiYWNrZ3JvdW5kOnZhcigtLXBhbmVsKTtib3JkZXI6MXB4IHNvbGlkIHZhcigtLWxpbmUpO2JvcmRlci1yYWRpdXM6MTRweDtwb3NpdGlvbjpyZWxhdGl2ZTtvdmVyZmxvdzpoaWRkZW59CiAgLm5ld3NiYXI6OmFmdGVye2NvbnRlbnQ6IiI7cG9zaXRpb246YWJzb2x1dGU7dG9wOjA7Ym90dG9tOjA7cmlnaHQ6MDt3aWR0aDo3MHB4O3otaW5kZXg6MztiYWNrZ3JvdW5kOmxpbmVhci1ncmFkaWVudCgyNzBkZWcsdmFyKC0tcGFuZWwpLHRyYW5zcGFyZW50KTtwb2ludGVyLWV2ZW50czpub25lfQogIC5uZXdzYmFyIC5sYmx7cG9zaXRpb246YWJzb2x1dGU7dG9wOjA7Ym90dG9tOjA7bGVmdDowO3otaW5kZXg6NDtkaXNwbGF5OmZsZXg7YWxpZ24taXRlbXM6Y2VudGVyO2dhcDo5cHg7cGFkZGluZzowIDIwcHggMCAxOHB4O2JhY2tncm91bmQ6bGluZWFyLWdyYWRpZW50KDkwZGVnLHZhcigtLXBhbmVsKSA3OCUsdHJhbnNwYXJlbnQpO2ZvbnQtd2VpZ2h0OjgwMDtmb250LXNpemU6MTNweDtsZXR0ZXItc3BhY2luZzouMTJlbTtjb2xvcjp2YXIoLS1saXZlKX0KICAubmV3c2JhciAubGJsIC5ke3dpZHRoOjlweDtoZWlnaHQ6OXB4O2JvcmRlci1yYWRpdXM6NTAlO2JhY2tncm91bmQ6dmFyKC0tbGl2ZSk7Ym94LXNoYWRvdzowIDAgOXB4IHZhcigtLWxpdmUpO2FuaW1hdGlvbjpwdWxzZSAxLjRzIGluZmluaXRlfQogIC5uZXdzLXRyYWNre2Rpc3BsYXk6ZmxleDtnYXA6MTJweDthbGlnbi1pdGVtczpjZW50ZXI7aGVpZ2h0OjEwMCU7cGFkZGluZzowIDE4cHggMCAxNTBweDt3aWR0aDptYXgtY29udGVudDt3aWxsLWNoYW5nZTp0cmFuc2Zvcm19CiAgLm5ld3MtdHJhY2sucnVue2FuaW1hdGlvbjpubWFycSB2YXIoLS1uZHVyLDcwcykgbGluZWFyIGluZmluaXRlfQogIC5uY2FyZHtmbGV4OjAgMCA0ODBweDtkaXNwbGF5OmZsZXg7Z2FwOjEzcHg7YmFja2dyb3VuZDp2YXIoLS1wYW5lbC0yKTtib3JkZXI6MXB4IHNvbGlkIHZhcigtLWxpbmUtMik7Ym9yZGVyLXJhZGl1czoxMXB4O3BhZGRpbmc6OXB4O2hlaWdodDoxMDRweDthbGlnbi1pdGVtczpjZW50ZXJ9CiAgLm5jYXJkIC5uaW1ne2ZsZXg6MCAwIDEzNHB4O2hlaWdodDoxMDAlO2JvcmRlci1yYWRpdXM6OHB4O292ZXJmbG93OmhpZGRlbjtiYWNrZ3JvdW5kOmxpbmVhci1ncmFkaWVudCgxMzVkZWcsIzFjMjYzNCwjMGUxNDIwKTtkaXNwbGF5OmZsZXg7YWxpZ24taXRlbXM6Y2VudGVyO2p1c3RpZnktY29udGVudDpjZW50ZXJ9CiAgLm5jYXJkIC5uaW1nIGltZ3t3aWR0aDoxMDAlO2hlaWdodDoxMDAlO29iamVjdC1maXQ6Y292ZXJ9CiAgLm5jYXJkIC5uaW1nIC5waHtmb250LXdlaWdodDo4MDA7Zm9udC1zaXplOjIycHg7Y29sb3I6dmFyKC0tZmFpbnQpfQogIC5uY2FyZCAubmJ7bWluLXdpZHRoOjA7ZGlzcGxheTpmbGV4O2ZsZXgtZGlyZWN0aW9uOmNvbHVtbjtnYXA6NXB4fQogIC5uY2FyZCAubnN7Zm9udC13ZWlnaHQ6ODAwO2ZvbnQtc2l6ZToxMnB4O2NvbG9yOnZhcigtLWFjY2VudCk7dGV4dC10cmFuc2Zvcm06dXBwZXJjYXNlO2xldHRlci1zcGFjaW5nOi4wNmVtfQogIC5uY2FyZCAubnMgLnRte2NvbG9yOnZhcigtLWZhaW50KX0KICAubmNhcmQgLm50e2ZvbnQtd2VpZ2h0OjcwMDtmb250LXNpemU6MTZweDtsaW5lLWhlaWdodDoxLjM7ZGlzcGxheTotd2Via2l0LWJveDstd2Via2l0LWxpbmUtY2xhbXA6Mjstd2Via2l0LWJveC1vcmllbnQ6dmVydGljYWw7b3ZlcmZsb3c6aGlkZGVufQogIEBrZXlmcmFtZXMgbm1hcnF7ZnJvbXt0cmFuc2Zvcm06dHJhbnNsYXRlWCgwKX10b3t0cmFuc2Zvcm06dHJhbnNsYXRlWCgtNTAlKX19Cjwvc3R5bGU+CjwvaGVhZD4KPGJvZHk+CjxkaXYgY2xhc3M9ImFwcCI+CgogIDwhLS0gSEVBREVSIC0tPgogIDxoZWFkZXIgY2xhc3M9ImhlYWQiPgogICAgPGRpdiBjbGFzcz0iaHNlc3MiIGlkPSJzZXNzIj48L2Rpdj4KICAgIDxkaXYgc3R5bGU9ImRpc3BsYXk6ZmxleDthbGlnbi1pdGVtczpjZW50ZXI7Z2FwOjE4cHgiPgogICAgICA8c3BhbiBjbGFzcz0iaGxpdmUgc3RhbGUiIGlkPSJobGl2ZSI+PHNwYW4gY2xhc3M9ImRvdCI+PC9zcGFuPjxzcGFuIGlkPSJ1cGR0eHQiPkNPTk5FQ1RJTkc8L3NwYW4+PC9zcGFuPgogICAgICA8ZGl2IGNsYXNzPSJoY2xvY2siIGlkPSJjbG9jayI+4oCUPC9kaXY+CiAgICA8L2Rpdj4KICA8L2hlYWRlcj4KCiAgPCEtLSBUSUNLRVIgKHNjcm9sbGluZyBtYXJxdWVlKSAtLT4KICA8ZGl2IGNsYXNzPSJ0aWNrIj48ZGl2IGNsYXNzPSJ0aWNrLXRyYWNrIiBpZD0idGlja1RyYWNrIj48L2Rpdj48L2Rpdj4KCiAgPCEtLSBNQUlOIC0tPgogIDxkaXYgY2xhc3M9Im1haW4iPgoKICAgIDwhLS0gQ09MIDE6IHZpZGVvICsgY2hhcnRzIC0tPgogICAgPGRpdiBjbGFzcz0iY29sIj4KICAgICAgPGRpdiBjbGFzcz0idmlkIj4KICAgICAgICA8ZGl2IGNsYXNzPSJ2dGwiPjxzcGFuIGNsYXNzPSJ5dCI+JiM5NjU0OyBZb3VUdWJlIExJVkU8L3NwYW4+ICZidWxsOyBCbG9vbWJlcmcgQnVzaW5lc3MgTmV3czwvZGl2PgogICAgICAgIDxkaXYgY2xhc3M9InZ0ciI+PHNwYW4gY2xhc3M9ImQiPjwvc3Bhbj5MSVZFPC9kaXY+CiAgICAgICAgPGRpdiBjbGFzcz0idmJyIj5CTE9PTUJFUkcgPGI+TElWRTwvYj48L2Rpdj4KICAgICAgICA8ZGl2IGlkPSJ2ZnJhbWUiIHN0eWxlPSJwb3NpdGlvbjphYnNvbHV0ZTtpbnNldDowIj48L2Rpdj4KICAgICAgICA8ZGl2IGNsYXNzPSJjaHlyb24iPgogICAgICAgICAgPGRpdiBjbGFzcz0iYmsiPjxkaXYgY2xhc3M9InRhZyI+QlJFQUtJTkcgTkVXUzwvZGl2PjxkaXYgY2xhc3M9InNjcm9sbHdyYXAiPjxkaXYgY2xhc3M9InNjcm9sbCIgaWQ9ImNoeVNjcm9sbCI+PC9kaXY+PC9kaXY+PC9kaXY+CiAgICAgICAgPC9kaXY+CiAgICAgIDwvZGl2PgogICAgPC9kaXY+CgogICAgPCEtLSBDT0wgMjogbWFjcm8gKyBjZW50cmFsIGJhbmsgcmF0ZXMgLS0+CiAgICA8ZGl2IGNsYXNzPSJjb2wiPgogICAgICA8ZGl2IGNsYXNzPSJwYW5lbCBtYWNybyIgc3R5bGU9ImZsZXg6MS4yNSI+CiAgICAgICAgPGRpdiBjbGFzcz0icHRpdGxlIj5NQUNSTyBFQ09OT01JQyBDQUxFTkRBUjwvZGl2PgogICAgICAgIDxkaXYgY2xhc3M9Im1oZWFkIj48c3Bhbj5QUklPUklUWTwvc3Bhbj48c3BhbiBjbGFzcz0iciI+QUNUVUFMPC9zcGFuPjxzcGFuIGNsYXNzPSJyIj5GT1JFQ0FTVDwvc3Bhbj48L2Rpdj4KICAgICAgICA8ZGl2IGNsYXNzPSJtYm9keSIgaWQ9Im1hY3JvIj48L2Rpdj4KICAgICAgPC9kaXY+CiAgICAgIDxkaXYgY2xhc3M9InBhbmVsIGV2ZW50cyIgc3R5bGU9ImZsZXg6MSI+CiAgICAgICAgPGRpdiBjbGFzcz0icHRpdGxlIj5HTE9CQUwgRVZFTlRTICZtaWRkb3Q7IFNVTU1JVFMgJmFtcDsgVEFMS1M8L2Rpdj4KICAgICAgICA8ZGl2IGNsYXNzPSJldi1saXN0IiBpZD0iZXZlbnRzIj48L2Rpdj4KICAgICAgPC9kaXY+CiAgICA8L2Rpdj4KCiAgICA8IS0tIENPTCAzOiBDTEFTU0lDIEJVTExJT04gKyBBU0lBIFNUT0NLUyAtLT4KICAgIDxkaXYgY2xhc3M9ImNvbCI+CiAgICAgIDxkaXYgY2xhc3M9ImJ1bGxpb24tcGFuZWwiPgogICAgICAgIDxkaXYgY2xhc3M9InB0aXRsZSI+CiAgICAgICAgICA8c3Bhbj5DTEFTU0lDIEJVTExJT04gJm1pZGRvdDsgTElWRSBSQVRFUzwvc3Bhbj4KICAgICAgICAgIDxkaXYgc3R5bGU9ImRpc3BsYXk6ZmxleDthbGlnbi1pdGVtczpjZW50ZXI7Z2FwOjhweCI+CiAgICAgICAgICAgIDxzcGFuIGNsYXNzPSJwc3ViIj48c3BhbiBjbGFzcz0iYmRvdCI+PC9zcGFuPjxzcGFuIGlkPSJjYlN0YXR1cyI+TElWRSBTWU5DPC9zcGFuPjwvc3Bhbj4KICAgICAgICAgICAgPGJ1dHRvbiBjbGFzcz0ibW9kZS1idG4gYWN0aXZlIiBpZD0iYnRuQ2JEYXJrIiBvbmNsaWNrPSJzZXRDYlZpZXcoJ2RhcmsnKSI+REFSSyBVSTwvYnV0dG9uPgogICAgICAgICAgICA8YnV0dG9uIGNsYXNzPSJtb2RlLWJ0biIgaWQ9ImJ0bkNiU2l0ZSIgb25jbGljaz0ic2V0Q2JWaWV3KCdzaXRlJykiPk9SSUdJTkFMIFNJVEU8L2J1dHRvbj4KICAgICAgICAgIDwvZGl2PgogICAgICAgIDwvZGl2PgoKICAgICAgICA8IS0tIE1PREUgMTogREFSSyBVSSBQQU5FTCAoMTAwJSBDQUxJQlJBVEVEICsgRElSRUNUIFBST1hZIEJSSURHRSkgLS0+CiAgICAgICAgPGRpdiBpZD0iY2JEYXJrQm94Ij4KICAgICAgICAgIDxkaXYgY2xhc3M9ImItdG9wLXBpbGwiPgogICAgICAgICAgICA8ZGl2IGNsYXNzPSJiLXNwb3QiPgogICAgICAgICAgICAgIDxkaXYgY2xhc3M9ImxibCI+R09MRCBPeiBCaWQ8L2Rpdj4KICAgICAgICAgICAgICA8ZGl2IGNsYXNzPSJtYWluLXZhbCB2YWwtbmV1dHJhbCIgaWQ9ImNiLWJpZCI+MzkwMC41NTwvZGl2PgogICAgICAgICAgICAgIDxkaXYgY2xhc3M9ImhsLXN1YiIgaWQ9ImNiLWJpZC1oIj5IOiAzOTI0LjA0PC9kaXY+CiAgICAgICAgICAgIDwvZGl2PgogICAgICAgICAgICA8ZGl2IGNsYXNzPSJiLXNwb3QiPgogICAgICAgICAgICAgIDxkaXYgY2xhc3M9ImxibCI+R09MRCBPeiBBc2s8L2Rpdj4KICAgICAgICAgICAgICA8ZGl2IGNsYXNzPSJtYWluLXZhbCB2YWwtbmV1dHJhbCIgaWQ9ImNiLWFzayI+MzkwMi4wNTwvZGl2PgogICAgICAgICAgICAgIDxkaXYgY2xhc3M9ImhsLXN1YiIgaWQ9ImNiLWFzay1sIj5MOiAzODczLjEyPC9kaXY+CiAgICAgICAgICAgIDwvZGl2PgogICAgICAgICAgPC9kaXY+CgogICAgICAgICAgPGRpdiBjbGFzcz0iYi10YmwtaGVhZCI+CiAgICAgICAgICAgIDxzcGFuPkJVTExJT048L3NwYW4+CiAgICAgICAgICAgIDxzcGFuPlBSSUNFPC9zcGFuPgogICAgICAgICAgPC9kaXY+CgogICAgICAgICAgPGRpdiBjbGFzcz0iYi1yb3dzIj4KICAgICAgICAgICAgPGRpdiBjbGFzcz0iYi1yb3ciPgogICAgICAgICAgICAgIDxkaXYgY2xhc3M9ImItbmFtZSI+U0lMVkVSIE96PC9kaXY+CiAgICAgICAgICAgICAgPGRpdiBjbGFzcz0iYi1jZWxsIj4KICAgICAgICAgICAgICAgIDxkaXYgY2xhc3M9ImItcHJpY2UgdmFsLW5ldXRyYWwiIGlkPSJjYi1zaWwiPjUyLjA5PC9kaXY+CiAgICAgICAgICAgICAgICA8ZGl2IGNsYXNzPSJiLWhsIiBpZD0iY2Itc2lsLWhsIj5IOiA1Mi44MSB8IEw6IDUxLjIxPC9kaXY+CiAgICAgICAgICAgICAgPC9kaXY+CiAgICAgICAgICAgIDwvZGl2PgogICAgICAgICAgICA8ZGl2IGNsYXNzPSJiLXJvdyI+CiAgICAgICAgICAgICAgPGRpdiBjbGFzcz0iYi1uYW1lIj5HT0xEIDEgR008L2Rpdj4KICAgICAgICAgICAgICA8ZGl2IGNsYXNzPSJiLWNlbGwiPgogICAgICAgICAgICAgICAgPGRpdiBjbGFzcz0iYi1wcmljZSB2YWwtbmV1dHJhbCIgaWQ9ImNiLTFnbSI+NDYzLjE4PC9kaXY+CiAgICAgICAgICAgICAgICA8ZGl2IGNsYXNzPSJiLWhsIiBpZD0iY2ItMWdtLWhsIj5IOiA0NjUuNzggfCBMOiA0NTkuNzc8L2Rpdj4KICAgICAgICAgICAgICA8L2Rpdj4KICAgICAgICAgICAgPC9kaXY+CiAgICAgICAgICAgIDxkaXYgY2xhc3M9ImItcm93Ij4KICAgICAgICAgICAgICA8ZGl2IGNsYXNzPSJiLW5hbWUiPkdPTEQgOTk5OTwvZGl2PgogICAgICAgICAgICAgIDxkaXYgY2xhc3M9ImItY2VsbCI+CiAgICAgICAgICAgICAgICA8ZGl2IGNsYXNzPSJiLXByaWNlIHZhbC1uZXV0cmFsIiBpZD0iY2ItOTk5OSI+NDYxOTYzPC9kaXY+CiAgICAgICAgICAgICAgICA8ZGl2IGNsYXNzPSJiLWhsIiBpZD0iY2ItOTk5OS1obCI+SDogNDY0NTYwIHwgTDogNDU4NTQ2PC9kaXY+CiAgICAgICAgICAgICAgPC9kaXY+CiAgICAgICAgICAgIDwvZGl2PgogICAgICAgICAgPC9kaXY+CiAgICAgICAgPC9kaXY+CgogICAgICAgIDwhLS0gTU9ERSAyOiBESVJFQ1QgQ1JPUFBFRCBDTEFTU0lDQlVMTElPTi5DT00gTElWRSBFTUJFRCAtLT4KICAgICAgICA8ZGl2IGNsYXNzPSJjYi1zaXRlLXdyYXAiIGlkPSJjYlNpdGVCb3giPgogICAgICAgICAgPGlmcmFtZSBpZD0iY2JEaXJlY3RJZnJhbWUiIHNyYz0iL3Byb3h5L2NsYXNzaWNidWxsaW9uIj48L2lmcmFtZT4KICAgICAgICA8L2Rpdj4KICAgICAgPC9kaXY+CgogICAgICA8ZGl2IGNsYXNzPSJwYW5lbCBhc2lhIiBzdHlsZT0iZmxleDoxIj4KICAgICAgICA8ZGl2IGNsYXNzPSJwdGl0bGUiPkFTSUEgU1RPQ0tTIElOVFJBREFZPC9kaXY+CiAgICAgICAgPGRpdiBpZD0iYXNpYSIgc3R5bGU9ImRpc3BsYXk6ZmxleDtmbGV4LWRpcmVjdGlvbjpjb2x1bW47ZmxleDoxO21pbi1oZWlnaHQ6MCI+PC9kaXY+CiAgICAgIDwvZGl2PgogICAgPC9kaXY+CiAgPC9kaXY+CgogIDwhLS0gQk9UVE9NIExJVkUgTkVXUyBTVFJJUCAtLT4KICA8ZGl2IGNsYXNzPSJuZXdzYmFyIj4KICAgIDxkaXYgY2xhc3M9ImxibCI+PHNwYW4gY2xhc3M9ImQiPjwvc3Bhbj5MSVZFIE5FV1M8L2Rpdj4KICAgIDxkaXYgY2xhc3M9Im5ld3MtdHJhY2siIGlkPSJuZXdzU3RyaXAiPjwvZGl2PgogIDwvZGl2Pgo8L2Rpdj4KCjxzY3JpcHQ+Ci8qIC0tLS0tLS0tLS0tLS0tLS0gaGVscGVycyAtLS0tLS0tLS0tLS0tLS0tICovCnZhciAkPWZ1bmN0aW9uKGlkKXtyZXR1cm4gZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoaWQpfTsKZnVuY3Rpb24gZGVjKHN5bSx2KXsgaWYoc3ltPT09J0RYLVkuTllCJylyZXR1cm4gMzsgaWYoc3ltPT09J0JUQy1VU0QnKXJldHVybiAodj49MTAwMD8wOjIpOyBpZihzeW09PT0nR0M9Rid8fHN5bT09PSdDTD1GJylyZXR1cm4gMjsgaWYodj49MTAwMClyZXR1cm4gMjsgcmV0dXJuIDI7IH0KZnVuY3Rpb24gZm10KHYsZCl7IGlmKHY9PW51bGx8fGlzTmFOKHYpKXJldHVybiAn4oCUJzsgcmV0dXJuIE51bWJlcih2KS50b0xvY2FsZVN0cmluZygnZW4tVVMnLHttaW5pbXVtRnJhY3Rpb25EaWdpdHM6ZCxtYXhpbXVtRnJhY3Rpb25EaWdpdHM6ZH0pOyB9CmZ1bmN0aW9uIHBjdFR4dChwKXsgaWYocD09bnVsbHx8aXNOYU4ocCkpcmV0dXJuICcnOyByZXR1cm4gKHA+PTA/JysnOicnKStwLnRvRml4ZWQoMikrJyUnOyB9CmZ1bmN0aW9uIGFycm93KHApeyByZXR1cm4gcD49MD8n4payJzon4pa8JzsgfQpmdW5jdGlvbiBlc2Mocyl7dmFyIGQ9ZG9jdW1lbnQuY3JlYXRlRWxlbWVudCgnZGl2Jyk7ZC50ZXh0Q29udGVudD1zfHwnJztyZXR1cm4gZC5pbm5lckhUTUw7fQoKZnVuY3Rpb24gc2V0Q2JWaWV3KG1vZGUpewogICQoJ2NiRGFya0JveCcpLnN0eWxlLmRpc3BsYXkgPSAobW9kZT09PSdkYXJrJykgPyAnYmxvY2snIDogJ25vbmUnOwogICQoJ2NiU2l0ZUJveCcpLnN0eWxlLmRpc3BsYXkgPSAobW9kZT09PSdzaXRlJykgPyAnYmxvY2snIDogJ25vbmUnOwogICQoJ2J0bkNiRGFyaycpLmNsYXNzTmFtZSA9ICdtb2RlLWJ0bicgKyAobW9kZT09PSdkYXJrJyA/ICcgYWN0aXZlJyA6ICcnKTsKICAkKCdidG5DYlNpdGUnKS5jbGFzc05hbWUgPSAnbW9kZS1idG4nICsgKG1vZGU9PT0nc2l0ZScgPyAnIGFjdGl2ZScgOiAnJyk7Cn0KCi8qIC0tLS0tLS0tLS0tLS0tLS0gbGl2ZSB2aWRlbyB3aXRoIGF1dG8tZmFsbGJhY2sgLS0tLS0tLS0tLS0tLS0tLQogICBUcmllcyBCbG9vbWJlcmcgZmlyc3Q7IGlmIGEgc3RyZWFtIGlzIHVuYXZhaWxhYmxlIC8gbm90IGVtYmVkZGFibGUgLyBub3QgbGl2ZSwKICAgdGhlIFlvdVR1YmUgSUZyYW1lIEFQSSBmaXJlcyBvbkVycm9yIChvciBpdCBuZXZlciBzdGFydHMgd2l0aGluIDlzKSBhbmQgd2UKICAgYXV0b21hdGljYWxseSByb3RhdGUgdG8gdGhlIG5leHQgMjQvNyBmaW5hbmNlIGxpdmUgY2hhbm5lbC4gKi8KLyogRGVmYXVsdCB0byBhIGZpbmFuY2UgbGl2ZSB0aGF0IGFjdHVhbGx5IHdvcmtzIHJpZ2h0IG5vdyAoQmxvb21iZXJnJ3MgZnJlZSBsaXZlCiAgIGlzIGN1cnJlbnRseSBkb3duKS4gVHJ5IFlhaG9vIEZpbmFuY2UsIHRoZW4gQ05CQywgdGhlbiBCbG9vbWJlcmc7IHdoaWNoZXZlciBpcwogICBsaXZlICYgZW1iZWRkYWJsZSBwbGF5cyBhbmQgc3RheXMuICovCnZhciBCQj0nVUNJQUxNS3ZPYlpOdEo2QW1kQ0xQN0xnJyxZRj0nVUNFQVplVUllSnMwSWpRaXFUQ2RWU0lnJyxDTkI9J1VDcnBfVUk4WHR1WWZwaXFsdVdMRDdMdyc7CnZhciBZVF9DSD1bWydZYWhvbyBGaW5hbmNlJyxZRiwnbm9jb29raWUnXSwKICAgICAgICAgICBbJ0NOQkMnLENOQiwnbm9jb29raWUnXSwKICAgICAgICAgICBbJ0Jsb29tYmVyZycsQkIsJ25vY29va2llJ10sCiAgICAgICAgICAgWydZYWhvbyBGaW5hbmNlJyxZRiwnd3d3J10sCiAgICAgICAgICAgWydDTkJDJyxDTkIsJ3d3dyddLAogICAgICAgICAgIFsnQmxvb21iZXJnJyxCQiwnd3d3J11dOwp2YXIgeXRJZHg9MCx5dFBsYXllcj1udWxsLHl0VGltZXI9bnVsbCx5dE9LPWZhbHNlLHl0QXBpUmVhZHk9ZmFsc2U7CmZ1bmN0aW9uIHl0U3JjKGUpe3ZhciBkb209KGVbMl09PT0nd3d3Jyk/J3d3dy55b3V0dWJlLmNvbSc6J3d3dy55b3V0dWJlLW5vY29va2llLmNvbSc7CiAgcmV0dXJuICdodHRwczovLycrZG9tKycvZW1iZWQvbGl2ZV9zdHJlYW0/Y2hhbm5lbD0nK2VbMV0rJyZhdXRvcGxheT0xJm11dGU9MSZwbGF5c2lubGluZT0xJnJlbD0wJmVuYWJsZWpzYXBpPTEnO30KZnVuY3Rpb24geXRMYWJlbCgpe3ZhciBlbD1kb2N1bWVudC5xdWVyeVNlbGVjdG9yKCcudnRsJyk7aWYoZWwpZWwuaW5uZXJIVE1MPSc8c3BhbiBjbGFzcz0ieXQiPiYjOTY1NDsgWW91VHViZSBMSVZFPC9zcGFuPiAmYnVsbDsgJytlc2MoWVRfQ0hbeXRJZHhdWzBdKSsnIEJ1c2luZXNzIE5ld3MnO30KZnVuY3Rpb24geXRCaW5kKCl7IHRyeXsgeXRQbGF5ZXI9bmV3IFlULlBsYXllcigndHYnLHtldmVudHM6ewogIG9uU3RhdGVDaGFuZ2U6ZnVuY3Rpb24oZSl7IGlmKGUuZGF0YT09PTF8fGUuZGF0YT09PTMpe3l0T0s9dHJ1ZTtjbGVhclRpbWVvdXQoeXRUaW1lcik7fSB9LAogIG9uRXJyb3I6ZnVuY3Rpb24oKXsgeXROZXh0KCk7IH0KfX0pOyB9Y2F0Y2goZSl7fSB9CmZ1bmN0aW9uIHl0TG9hZCgpewogIHZhciBib3g9JCgndmZyYW1lJyk7aWYoIWJveClyZXR1cm47CiAgaWYoeXRQbGF5ZXImJnl0UGxheWVyLmRlc3Ryb3kpe3RyeXt5dFBsYXllci5kZXN0cm95KCk7fWNhdGNoKGUpe319CiAgYm94LmlubmVySFRNTD0nPGlmcmFtZSBpZD0idHYiIHNyYz0iJyt5dFNyYyhZVF9DSFt5dElkeF0pKyciIGFsbG93PSJhdXRvcGxheTsgZW5jcnlwdGVkLW1lZGlhOyBwaWN0dXJlLWluLXBpY3R1cmUiIGFsbG93ZnVsbHNjcmVlbiByZWZlcnJlcnBvbGljeT0ib3JpZ2luLXdoZW4tY3Jvc3Mtb3JpZ2luIj48L2lmcmFtZT4nOwogIHl0TGFiZWwoKTsKICB5dE9LPWZhbHNlOyBjbGVhclRpbWVvdXQoeXRUaW1lcik7CiAgeXRUaW1lcj1zZXRUaW1lb3V0KGZ1bmN0aW9uKCl7IGlmKCF5dE9LKSB5dE5leHQoKTsgfSw5MDAwKTsgIC8qIG5vIHBsYXliYWNrIGluIDlzIC0+IHRyeSBuZXh0ICovCiAgaWYoeXRBcGlSZWFkeSkgeXRCaW5kKCk7Cn0KZnVuY3Rpb24geXROZXh0KCl7CiAgeXRJZHgrKzsKICBpZih5dElkeD49WVRfQ0gubGVuZ3RoKXsgeXRJZHg9MDsgY2xlYXJUaW1lb3V0KHl0VGltZXIpOyB5dFRpbWVyPXNldFRpbWVvdXQoeXRMb2FkLDQ1MDAwKTsgcmV0dXJuOyB9IC8qIGFsbCB2YXJpYW50cyBmYWlsZWQgLT4gYmFjayBvZmYsIHRoZW4gcmV0cnkgQmxvb21iZXJnICovCiAgeXRMb2FkKCk7Cn0Kd2luZG93Lm9uWW91VHViZUlmcmFtZUFQSVJlYWR5PWZ1bmN0aW9uKCl7IHl0QXBpUmVhZHk9dHJ1ZTsgeXRCaW5kKCk7IH07CihmdW5jdGlvbigpe3ZhciBzPWRvY3VtZW50LmNyZWF0ZUVsZW1lbnQoJ3NjcmlwdCcpO3Muc3JjPSdodHRwczovL3d3dy55b3V0dWJlLmNvbS9pZnJhbWVfYXBpJztkb2N1bWVudC5oZWFkLmFwcGVuZENoaWxkKHMpO30pKCk7Ci8qIE1BTlVBTCBvdmVycmlkZTogd2FsbC1zZXJ2ZXIucHkgaW5qZWN0cyBhIFlvdVR1YmUgbGluayBoZXJlLiBJZiBzZXQsIHdlIGVtYmVkCiAgIGV4YWN0bHkgdGhhdCBzdHJlYW0gYW5kIHNraXAgdGhlIGF1dG8tZmFsbGJhY2suICovCnZhciBNQU5VQUxfVklERU89Il9fTUFOVUFMX1ZJREVPX18iOwpmdW5jdGlvbiB5dE1hbnVhbFNyYygpewogIHZhciB1PShNQU5VQUxfVklERU98fCcnKS50cmltKCk7CiAgaWYoIXV8fHUuY2hhckF0KDApPT09J18nKSByZXR1cm4gbnVsbDsgICAgICAgICAgICAgICAgIC8qIGVtcHR5IC8gdW5yZXBsYWNlZCBwbGFjZWhvbGRlciAqLwogIHZhciBjaD11Lm1hdGNoKC9jaGFubmVsXC8oVUNbXHctXSspLyk7CiAgaWYoY2gpIHJldHVybiAnaHR0cHM6Ly93d3cueW91dHViZS1ub2Nvb2tpZS5jb20vZW1iZWQvbGl2ZV9zdHJlYW0/Y2hhbm5lbD0nK2NoWzFdKycmYXV0b3BsYXk9MSZtdXRlPTEmcGxheXNpbmxpbmU9MSZyZWw9MCc7CiAgdmFyIHY9KHUubWF0Y2goL1s/Jl12PShbXHctXXsxMX0pLyl8fHUubWF0Y2goL3lvdXR1XC5iZVwvKFtcdy1dezExfSkvKXx8dS5tYXRjaCgvbGl2ZVwvKFtcdy1dezExfSkvKXx8dS5tYXRjaCgvZW1iZWRcLyhbXHctXXsxMX0pLyl8fHUubWF0Y2goL14oW1x3LV17MTF9KSQvKSk7CiAgaWYodikgcmV0dXJuICdodHRwczovL3d3dy55b3V0dWJlLW5vY29va2llLmNvbS9lbWJlZC8nK3ZbMV0rJz9hdXRvcGxheT0xJm11dGU9MSZwbGF5c2lubGluZT0xJnJlbD0wJzsKICByZXR1cm4gbnVsbDsgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgIC8qIHVucmVjb2duaXNlZCAtPiB1c2UgYXV0byBmZWVkICovCn0KKGZ1bmN0aW9uKCl7CiAgdmFyIG1zPXl0TWFudWFsU3JjKCk7CiAgaWYobXMpewogICAgdmFyIGJveD0kKCd2ZnJhbWUnKTsgaWYoYm94KSBib3guaW5uZXJIVE1MPSc8aWZyYW1lIGlkPSJ0diIgc3JjPSInK21zKyciIGFsbG93PSJhdXRvcGxheTsgZW5jcnlwdGVkLW1lZGlhOyBwaWN0dXJlLWluLXBpY3R1cmUiIGFsbG93ZnVsbHNjcmVlbiByZWZlcnJlcnBvbGljeT0ib3JpZ2luLXdoZW4tY3Jvc3Mtb3JpZ2luIj48L2lmcmFtZT4nOwogICAgdmFyIGVsPWRvY3VtZW50LnF1ZXJ5U2VsZWN0b3IoJy52dGwnKTsgaWYoZWwpIGVsLmlubmVySFRNTD0nPHNwYW4gY2xhc3M9Inl0Ij4mIzk2NTQ7IFlvdVR1YmUgTElWRTwvc3Bhbj4gJmJ1bGw7IExpdmUgU3RyZWFtJzsKICB9ZWxzZXsKICAgIHl0TG9hZCgpOwogICAgLyogc2VsZi1oZWFsaW5nOiBldmVyeSAzIG1pbiwgaWYgbm90aGluZyBpcyBwbGF5aW5nLCByZXN0YXJ0IHRoZSB3aG9sZSBjYXNjYWRlCiAgICAgICBmcm9tIHRoZSB0b3Agc28gYW4gdW5hdHRlbmRlZCBUViBhbHdheXMgcmVjb3ZlcnMgdG8gYSB3b3JraW5nIHN0cmVhbSAqLwogICAgc2V0SW50ZXJ2YWwoZnVuY3Rpb24oKXsgaWYoIXl0T0speyB5dElkeD0wOyB5dExvYWQoKTsgfSB9LDMqNjAqMTAwMCk7CiAgfQp9KSgpOwoKLyogLS0tLS0tLS0tLS0tLS0tLSBjbG9jayArIHNlc3Npb25zIC0tLS0tLS0tLS0tLS0tLS0gKi8KdmFyIENJVFk9W1snTlknLCdBbWVyaWNhL05ld19Zb3JrJ10sWydMRE4nLCdFdXJvcGUvTG9uZG9uJ10sWydUWU8nLCdBc2lhL1Rva3lvJ10sWydTWUQnLCdBdXN0cmFsaWEvU3lkbmV5J11dOwpmdW5jdGlvbiBjbG9jaygpewogIHZhciBuPW5ldyBEYXRlKCk7CiAgJCgnY2xvY2snKS5pbm5lckhUTUw9JzxiPicrbi50b0xvY2FsZVRpbWVTdHJpbmcoJ2VuLVVTJyx7aG91cjonMi1kaWdpdCcsbWludXRlOicyLWRpZ2l0JyxzZWNvbmQ6JzItZGlnaXQnfSkrJzwvYj4gLyAnK24udG9Mb2NhbGVEYXRlU3RyaW5nKCdlbi1HQicse3dlZWtkYXk6J3Nob3J0JyxkYXk6JzItZGlnaXQnLG1vbnRoOidzaG9ydCcseWVhcjonbnVtZXJpYyd9KS50b1VwcGVyQ2FzZSgpOwogICQoJ3Nlc3MnKS5pbm5lckhUTUw9Q0lUWS5tYXAoZnVuY3Rpb24oYyl7CiAgICB2YXIgdD1uZXcgRGF0ZSgpLnRvTG9jYWxlVGltZVN0cmluZygnZW4tR0InLHt0aW1lWm9uZTpjWzFdLGhvdXI6JzItZGlnaXQnLG1pbnV0ZTonMi1kaWdpdCd9KTsKICAgIHJldHVybiAnPHNwYW4+JytjWzBdKyc8Yj4nK3QrJzwvYj48L3NwYW4+JzsKICB9KS5qb2luKCcnKTsKICB2YXIgaGw9JCgnaGxpdmUnKSx1dD0kKCd1cGR0eHQnKTsKICBpZihobCYmdXQpewogICAgaWYobGFzdFVwZCYmKERhdGUubm93KCktbGFzdFVwZCk8NjAwMDApe2hsLmNsYXNzTmFtZT0naGxpdmUnO3V0LnRleHRDb250ZW50PSdMSVZFIMK3ICcrbmV3IERhdGUobGFzdFVwZCkudG9Mb2NhbGVUaW1lU3RyaW5nKCdlbi1VUycse2hvdXIxMjpmYWxzZX0pO30KICAgIGVsc2V7aGwuY2xhc3NOYW1lPSdobGl2ZSBzdGFsZSc7dXQudGV4dENvbnRlbnQ9bGFzdFVwZD8nUkVDT05ORUNUSU5HJzonQ09OTkVDVElORyc7fQogIH0KfQpjbG9jaygpO3NldEludGVydmFsKGNsb2NrLDEwMDApOwoKLyogLS0tLS0tLS0tLS0tLS0tLSBxdW90ZXMgLS0tLS0tLS0tLS0tLS0tLSAqLwp2YXIgUT17fSwgUFJFVj17fSwgbGFzdFVwZD0wOwpmdW5jdGlvbiBjaENscyhzLHByaWNlKXt2YXIgcD1QUkVWW3NdO2lmKHA9PW51bGx8fHByaWNlPT1udWxsfHxwcmljZT09PXApcmV0dXJuICcnO3JldHVybiBwcmljZT5wPycgZmx1cCc6JyBmbGRuJzt9CnZhciBUSUNLRVI9WydeR1NQQycsJ15ORFgnLCdEWC1ZLk5ZQicsJ0dDPUYnLCdDTD1GJywnQlRDLVVTRCddOwp2YXIgVE5BTUU9eydeR1NQQyc6J1MmUCA1MDAnLCdeTkRYJzonTkFTREFRIDEwMCcsJ0RYLVkuTllCJzonRFhZJywnR0M9Ric6J0dPTEQnLCdDTD1GJzonV1RJJywnQlRDLVVTRCc6J0JUQ1VTRCd9Owp2YXIgQVNJQT1bJ0FBWEonLCdeTjIyNScsJ15LUzExJywnXlRXSUknLCdeSFNJJywnXkdTUEMnLCdeUlVUJ107CnZhciBBTkFNRT17J0FBWEonOidNU0NJIEFDIEFTSUEgUEFDJywnXk4yMjUnOidOSUtLRUkgMjI1JywnXktTMTEnOidLT1NQSScsJ15UV0lJJzonVEFJUEVJIFRXU0UnLCdeSFNJJzonSEFORyBTRU5HJywnXkdTUEMnOidTJlAgNTAwJywnXlJVVCc6J1JVU1NFTEwgMjAwMCd9OwoKZnVuY3Rpb24gdGtJdGVtKHMpewogIHZhciBxPVFbc118fHt9O3ZhciB1cD0ocS5wY3R8fDApPj0wOwogIHJldHVybiAnPGRpdiBjbGFzcz0idGsiPjxzcGFuIGNsYXNzPSJubSAnKyh1cD8ndXAnOidkb3duJykrJyIgZGF0YS1ubT0iJytzKyciPicrVE5BTUVbc10rJzwvc3Bhbj4nKwogICAgJzxzcGFuIGNsYXNzPSJwciIgZGF0YS1wcj0iJytzKyciPicrZm10KHEucHJpY2UsZGVjKHMscS5wcmljZSkpKyc8L3NwYW4+JysKICAgICc8c3BhbiBjbGFzcz0iY2ggJysodXA/J3VwJzonZG93bicpKyciIGRhdGEtY2g9IicrcysnIj4nK2Fycm93KHEucGN0fHwwKSsnICcrcGN0VHh0KHEucGN0KSsnPC9zcGFuPjwvZGl2Pic7Cn0KZnVuY3Rpb24gYnVpbGRUaWNrZXIoKXsKICB2YXIgdHI9JCgndGlja1RyYWNrJyk7aWYoIXRyKXJldHVybjsKICB2YXIgb25lPVRJQ0tFUi5tYXAodGtJdGVtKS5qb2luKCcnKTsKICB0ci5pbm5lckhUTUw9b25lK29uZTsgIC8qIGR1cGxpY2F0ZSBmb3Igc2VhbWxlc3MgbG9vcCAqLwogIHZhciBjb250PXRyLnBhcmVudEVsZW1lbnQ/dHIucGFyZW50RWxlbWVudC5jbGllbnRXaWR0aDoxOTIwOwogIHZhciBoYWxmPXRyLnNjcm9sbFdpZHRoLzI7CiAgaWYoaGFsZj4wJiZoYWxmPGNvbnQpe3ZhciByZXBzPU1hdGguY2VpbChjb250L2hhbGYpKjI7dmFyIGZ1bGw9Jyc7Zm9yKHZhciBpPTA7aTxyZXBzO2krKylmdWxsKz1vbmU7dHIuaW5uZXJIVE1MPWZ1bGw7aGFsZj10ci5zY3JvbGxXaWR0aC8yO30KICB0ci5zdHlsZS5zZXRQcm9wZXJ0eSgnLS10ZHVyJyxNYXRoLm1heCgzMCxNYXRoLnJvdW5kKGhhbGYvNTUpKSsncycpOwp9Ci8qIHVwZGF0ZSBwcmljZXMvcGN0IGluIHBsYWNlIChubyBpbm5lckhUTUwgcmVidWlsZCkgc28gdGhlIG1hcnF1ZWUgbmV2ZXIganVtcHMgKi8KZnVuY3Rpb24gcmVuZGVyVGlja2VyKCl7CiAgdmFyIHRyPSQoJ3RpY2tUcmFjaycpO2lmKCF0cilyZXR1cm47CiAgaWYoIXRyLmNoaWxkcmVuLmxlbmd0aCl7YnVpbGRUaWNrZXIoKTtyZXR1cm47fQogIFRJQ0tFUi5mb3JFYWNoKGZ1bmN0aW9uKHMpewogICAgdmFyIHE9UVtzXXx8e307dmFyIHVwPShxLnBjdHx8MCk+PTA7dmFyIGZsPWNoQ2xzKHMscS5wcmljZSk7CiAgICB2YXIgcHY9Zm10KHEucHJpY2UsZGVjKHMscS5wcmljZSkpOwogICAgZG9jdW1lbnQucXVlcnlTZWxlY3RvckFsbCgnW2RhdGEtcHI9IicrcysnIl0nKS5mb3JFYWNoKGZ1bmN0aW9uKGUpewogICAgICBpZihlLnRleHRDb250ZW50IT09cHYpe2UudGV4dENvbnRlbnQ9cHY7ZS5jbGFzc0xpc3QucmVtb3ZlKCdmbHVwJywnZmxkbicpO2lmKGZsKXt2b2lkIGUub2Zmc2V0V2lkdGg7ZS5jbGFzc0xpc3QuYWRkKGZsLnRyaW0oKSk7fX0KICAgIH0pOwogICAgZG9jdW1lbnQucXVlcnlTZWxlY3RvckFsbCgnW2RhdGEtY2g9IicrcysnIl0nKS5mb3JFYWNoKGZ1bmN0aW9uKGUpe2UudGV4dENvbnRlbnQ9YXJyb3cocS5wY3R8fDApKycgJytwY3RUeHQocS5wY3QpO2UuY2xhc3NOYW1lPSdjaCAnKyh1cD8ndXAnOidkb3duJyk7fSk7CiAgICBkb2N1bWVudC5xdWVyeVNlbGVjdG9yQWxsKCdbZGF0YS1ubT0iJytzKyciXScpLmZvckVhY2goZnVuY3Rpb24oZSl7ZS5jbGFzc05hbWU9J25tICcrKHVwPyd1cCc6J2Rvd24nKTt9KTsKICB9KTsKICB0ci5zdHlsZS5zZXRQcm9wZXJ0eSgnLS10ZHVyJyxNYXRoLm1heCg0MCxNYXRoLnJvdW5kKHRyLnNjcm9sbFdpZHRoLzIvNTUpKSsncycpOyAgLyogY29uc3RhbnQgc3BlZWQgYXMgd2lkdGhzIHNldHRsZSAqLwp9CmZ1bmN0aW9uIHJlbmRlckFzaWEoKXsKICAkKCdhc2lhJykuaW5uZXJIVE1MPUFTSUEubWFwKGZ1bmN0aW9uKHMpewogICAgdmFyIHE9UVtzXXx8e307dmFyIHVwPShxLnBjdHx8MCk+PTA7CiAgICByZXR1cm4gJzxkaXYgY2xhc3M9ImFjYXJkIj48ZGl2IGNsYXNzPSJubSI+JytBTkFNRVtzXSsnPC9kaXY+JysKICAgICAgJzxkaXYgY2xhc3M9InZhbCcrY2hDbHMocyxxLnByaWNlKSsnIj4nK2ZtdChxLnByaWNlLDIpKyc8L2Rpdj4nKwogICAgICAnPGRpdiBjbGFzcz0icGlsbCAnKyh1cD8ndXAnOidkb3duJykrJyI+JythcnJvdyhxLnBjdHx8MCkrJyAnK3BjdFR4dChxLnBjdCkrJzwvZGl2PjwvZGl2Pic7CiAgfSkuam9pbignJyk7Cn0KCi8qID09PT09PT09PT09PT09PT09PT09IENMQVNTSUMgQlVMTElPTiBFWEFDVCBMSVZFIEVOR0lORSA9PT09PT09PT09PT09PT09PT09PQogICAxLiBMaXN0ZW5zIHRvIGRpcmVjdCBET00gc3luYyBmcm9tIC9wcm94eS9jbGFzc2ljYnVsbGlvbiBpZnJhbWUgKGlmIGNsYXNzaWNidWxsaW9uLmNvbSBzb2NrZXQgc3RyZWFtcykKICAgMi4gUG9sbHMgcmVhbC10aW1lIFNwb3QgR29sZCAoVFZDOkdPTEQpLCBTcG90IFNpbHZlciAoVFZDOlNJTFZFUiksIGFuZCBTcG90IFVTRElOUiAoRlhfSURDOlVTRElOUikKICAgICAgZXZlcnkgMSBzZWNvbmQgd2l0aCBDbGFzc2ljIEJ1bGxpb24ncyBleGFjdCBEdWJhaSBkZWFsZXIgY2FsaWJyYXRpb246CiAgICAgIC0gR09MRCBPeiBCaWQgPSBTcG90X0dvbGQgLSAwLjcwCiAgICAgIC0gR09MRCBPeiBBc2sgPSBTcG90X0dvbGQgKyAwLjgwICgxLjUwIHNwcmVhZCkKICAgICAgLSBTSUxWRVIgT3ogICA9IFNwb3RfU2lsdmVyICsgMi4wMiAoQ09NRVggLyBEdWJhaSBCdWxsaW9uIGJhc2lzKQogICAgICAtIFVTRElOUiBBc2sgID0gU3BvdF9VU0RJTlIgKyAwLjExNQogICAgICAtIEtfQUVEX0dSQU0gID0gMy42NzQgLyAzMS4xMDM1CiAgICAgIC0gR09MRCAxIEdNICAgPSBBc2sgKiBLX0FFRF9HUkFNICsgMi4yNjMKICAgICAgLSBHT0xEIDk5NSAgICA9IEFzayAqIEtfQUVEX0dSQU0gKiA5OTUgKyAxNjc0LjMKICAgICAgLSBHT0xEIDk5OTkgICA9IEFzayAqIEtfQUVEX0dSQU0gKiA5OTkuOSArIDEwOTIuMQogICAgICAtIEdPTEQgVFQgQkFSID0gQXNrICogS19BRURfR1JBTSAqIDExNi41MjMzNiArIDE1NC4zCiAgID09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PSAqLwp2YXIgQlVMTElPTl9TVEFURSA9IHsKICBnb2xkQmlkOiAzOTAwLjU1LAogIGdvbGRBc2s6IDM5MDIuMDUsCiAgZ29sZEhpZ2g6IDM5MjQuMDQsCiAgZ29sZExvdzogMzg3My4xMiwKICB1c2RJbnI6IDk0LjMxMywKICBpbnJIaWdoOiA5NC4zNDksCiAgaW5yTG93OiA5NC4xMTgsCiAgc2lsdmVyT3o6IDUyLjA5LAogIHNpbEhpZ2g6IDUyLjgxLAogIHNpbExvdzogNTEuMjEKfTsKdmFyIEJVTExJT05fUFJFViA9IHt9Owp2YXIgQlVMTElPTl9USU1FUlMgPSB7fTsKdmFyIGRpcmVjdERvbUFjdGl2ZSA9IGZhbHNlOwoKdmFyIEtfQUVEID0gMy42NzQgLyAzMS4xMDM1OwoKZnVuY3Rpb24gc2V0QnVsbGlvblZhbChpZCwgbnVtVmFsLCBkZWNpbWFscykgewogIHZhciBlbCA9ICQoaWQpOwogIGlmICghZWwgfHwgbnVtVmFsID09IG51bGwgfHwgaXNOYU4obnVtVmFsKSkgcmV0dXJuOwogIHZhciByb3VuZGVkID0gTnVtYmVyKE51bWJlcihudW1WYWwpLnRvRml4ZWQoZGVjaW1hbHMpKTsKICB2YXIgdHh0ID0gZGVjaW1hbHMgPT09IDAgPyBTdHJpbmcoTWF0aC5yb3VuZChyb3VuZGVkKSkgOiByb3VuZGVkLnRvRml4ZWQoZGVjaW1hbHMpOwogIHZhciBwcmV2ID0gQlVMTElPTl9QUkVWW2lkXTsKICBlbC50ZXh0Q29udGVudCA9IHR4dDsKCiAgaWYgKHByZXYgIT09IHVuZGVmaW5lZCAmJiBwcmV2ICE9PSByb3VuZGVkKSB7CiAgICB2YXIgaXNVcCA9IHJvdW5kZWQgPiBwcmV2OwogICAgZWwuY2xhc3NOYW1lID0gZWwuY2xhc3NOYW1lLnJlcGxhY2UoL1xiKHZhbC11cHx2YWwtZG93bnx2YWwtbmV1dHJhbHxmbHVwfGZsZG4pXGIvZywgJycpLnRyaW0oKTsKICAgIHZvaWQgZWwub2Zmc2V0V2lkdGg7CiAgICBlbC5jbGFzc0xpc3QuYWRkKGlzVXAgPyAndmFsLXVwJyA6ICd2YWwtZG93bicsIGlzVXAgPyAnZmx1cCcgOiAnZmxkbicpOwogICAgaWYgKEJVTExJT05fVElNRVJTW2lkXSkgY2xlYXJUaW1lb3V0KEJVTExJT05fVElNRVJTW2lkXSk7CiAgICAvKiBSZXR1cm4gdG8gd2hpdGUvbmV1dHJhbCBhZnRlciA0cyBpZiBubyBmdXJ0aGVyIHRpY2tjaGFuZ2Ugb24gdGhhdCBpdGVtIChtYXRjaGVzIGNsYXNzaWNidWxsaW9uLmNvbSkgKi8KICAgIEJVTExJT05fVElNRVJTW2lkXSA9IHNldFRpbWVvdXQoZnVuY3Rpb24oKSB7CiAgICAgIGVsLmNsYXNzTmFtZSA9IGVsLmNsYXNzTmFtZS5yZXBsYWNlKC9cYih2YWwtdXB8dmFsLWRvd24pXGIvZywgJycpLnRyaW0oKSArICcgdmFsLW5ldXRyYWwnOwogICAgfSwgNDAwMCk7CiAgfQogIEJVTExJT05fUFJFVltpZF0gPSByb3VuZGVkOwp9CgpmdW5jdGlvbiByZW5kZXJCdWxsaW9uKCkgewogIHZhciBiaWQgPSBCVUxMSU9OX1NUQVRFLmdvbGRCaWQ7CiAgdmFyIGFzayA9IEJVTExJT05fU1RBVEUuZ29sZEFzazsKICB2YXIgZ2ggID0gTWF0aC5tYXgoQlVMTElPTl9TVEFURS5nb2xkSGlnaCwgYXNrKTsKICB2YXIgZ2wgID0gTWF0aC5taW4oQlVMTElPTl9TVEFURS5nb2xkTG93LCBiaWQpOwogIEJVTExJT05fU1RBVEUuZ29sZEhpZ2ggPSBnaDsKICBCVUxMSU9OX1NUQVRFLmdvbGRMb3cgID0gZ2w7CgogIHNldEJ1bGxpb25WYWwoJ2NiLWJpZCcsIGJpZCwgMik7CiAgc2V0QnVsbGlvblZhbCgnY2ItYXNrJywgYXNrLCAyKTsKICAkKCdjYi1iaWQtaCcpLnRleHRDb250ZW50ID0gJ0g6ICcgKyBnaC50b0ZpeGVkKDIpOwogICQoJ2NiLWFzay1sJykudGV4dENvbnRlbnQgPSAnTDogJyArIGdsLnRvRml4ZWQoMik7CgogIHNldEJ1bGxpb25WYWwoJ2NiLXNpbCcsIEJVTExJT05fU1RBVEUuc2lsdmVyT3osIDIpOwogICQoJ2NiLXNpbC1obCcpLnRleHRDb250ZW50ID0gJ0g6ICcgKyBCVUxMSU9OX1NUQVRFLnNpbEhpZ2gudG9GaXhlZCgyKSArICcgfCBMOiAnICsgQlVMTElPTl9TVEFURS5zaWxMb3cudG9GaXhlZCgyKTsKCiAgLyogRXhhY3QgQ2xhc3NpYyBCdWxsaW9uIER1YmFpIEFFRCBmb3JtdWxhcyAqLwogIHZhciBnbTEgICAgPSBhc2sgKiBLX0FFRCArIDIuMjYzOwogIHZhciBnbTFIICAgPSBnaCAgKiBLX0FFRCArIDIuMjYzOwogIHZhciBnbTFMICAgPSBnbCAgKiBLX0FFRCArIDIuMjYzOwoKICB2YXIgZzk5NSAgID0gYXNrICogS19BRUQgKiA5OTUgKyAxNjc0LjM7CiAgdmFyIGc5OTVIICA9IGdoICAqIEtfQUVEICogOTk1ICsgMTY3NC4zOwogIHZhciBnOTk1TCAgPSBnbCAgKiBLX0FFRCAqIDk5NSArIDE2NzQuMzsKCiAgdmFyIGc5OTk5ICA9IGFzayAqIEtfQUVEICogOTk5LjkgKyAxMDkyLjE7CiAgdmFyIGc5OTk5SCA9IGdoICAqIEtfQUVEICogOTk5LjkgKyAxMDkyLjE7CiAgdmFyIGc5OTk5TCA9IGdsICAqIEtfQUVEICogOTk5LjkgKyAxMDkyLjE7CgogIHZhciBndHQgICAgPSBhc2sgKiBLX0FFRCAqIDExNi41MjMzNiArIDE1NC4zOwogIHZhciBndHRIICAgPSBnaCAgKiBLX0FFRCAqIDExNi41MjMzNiArIDE1NC4zOwogIHZhciBndHRMICAgPSBnbCAgKiBLX0FFRCAqIDExNi41MjMzNiArIDE1NC4zOwoKICBzZXRCdWxsaW9uVmFsKCdjYi0xZ20nLCAgZ20xLCAgIDIpOwogICQoJ2NiLTFnbS1obCcpLnRleHRDb250ZW50ICA9ICdIOiAnICsgZ20xSC50b0ZpeGVkKDIpICsgJyB8IEw6ICcgKyBnbTFMLnRvRml4ZWQoMik7CgogIHNldEJ1bGxpb25WYWwoJ2NiLTk5OTknLCBnOTk5OSwgMCk7CiAgJCgnY2ItOTk5OS1obCcpLnRleHRDb250ZW50ID0gJ0g6ICcgKyBNYXRoLnJvdW5kKGc5OTk5SCkgKyAnIHwgTDogJyArIE1hdGgucm91bmQoZzk5OTlMKTsKfQoKLyogTGlzdGVuIGZvciBkaXJlY3QgbGl2ZSBET00gbWVzc2FnZXMgZnJvbSAvcHJveHkvY2xhc3NpY2J1bGxpb24gaWZyYW1lICovCndpbmRvdy5hZGRFdmVudExpc3RlbmVyKCdtZXNzYWdlJywgZnVuY3Rpb24oZXYpIHsKICBpZiAoIWV2LmRhdGEgfHwgZXYuZGF0YS50eXBlICE9PSAnQ0xBU1NJQ19CVUxMSU9OX0RPTScpIHJldHVybjsKICB2YXIgZCA9IGV2LmRhdGEucGF5bG9hZCB8fCB7fTsKICBpZiAoZC5nb2xkQmlkID4gMTAwMCAmJiBkLmdvbGRBc2sgPiAxMDAwKSB7CiAgICBkaXJlY3REb21BY3RpdmUgPSB0cnVlOwogICAgJCgnY2JTdGF0dXMnKS50ZXh0Q29udGVudCA9ICdDTEFTU0lDQlVMTElPTi5DT00gTElWRSc7CiAgICBzZXRCdWxsaW9uVmFsKCdjYi1iaWQnLCBkLmdvbGRCaWQsIDIpOwogICAgc2V0QnVsbGlvblZhbCgnY2ItYXNrJywgZC5nb2xkQXNrLCAyKTsKICAgIGlmIChkLmdvbGRIaWdoKSAkKCdjYi1iaWQtaCcpLnRleHRDb250ZW50ID0gZC5nb2xkSGlnaDsKICAgIGlmIChkLmdvbGRMb3cpICAkKCdjYi1hc2stbCcpLnRleHRDb250ZW50ID0gZC5nb2xkTG93OwogICAgaWYgKGQudXNkSW5yKSAgIHNldEJ1bGxpb25WYWwoJ2NiLWlucicsIGQudXNkSW5yLCAzKTsKICAgIGlmIChkLnNpbHZlck96KSBzZXRCdWxsaW9uVmFsKCdjYi1zaWwnLCBkLnNpbHZlck96LCAyKTsKICAgIGlmIChkLmdvbGQxZ20pICBzZXRCdWxsaW9uVmFsKCdjYi0xZ20nLCBkLmdvbGQxZ20sIDIpOwogICAgaWYgKGQuZ29sZDk5NSkgIHNldEJ1bGxpb25WYWwoJ2NiLTk5NScsIGQuZ29sZDk5NSwgMCk7CiAgICBpZiAoZC5nb2xkOTk5OSkgc2V0QnVsbGlvblZhbCgnY2ItOTk5OScsIGQuZ29sZDk5OTksIDApOwogICAgaWYgKGQuZ29sZFR0KSAgIHNldEJ1bGxpb25WYWwoJ2NiLXR0JywgZC5nb2xkVHQsIDApOwogIH0KfSk7CgovKiBQb2xsIFRyYWRpbmdWaWV3IHJlYWwtdGltZSBTcG90IFNjYW5uZXIgZXZlcnkgMSBzZWNvbmQgKDEwMDBtcykgd2l0aCBleGFjdCBDbGFzc2ljIEJ1bGxpb24gY2FsaWJyYXRpb24gKi8KYXN5bmMgZnVuY3Rpb24gcG9sbERpcmVjdEJ1bGxpb25TY2FubmVyKCkgewogIGlmIChkaXJlY3REb21BY3RpdmUpIHJldHVybjsKICB0cnkgewogICAgdmFyIGJvZHkgPSBKU09OLnN0cmluZ2lmeSh7CiAgICAgIHN5bWJvbHM6IHsgdGlja2VyczogWydUVkM6R09MRCcsICdUVkM6U0lMVkVSJywgJ0ZYX0lEQzpVU0RJTlInXSwgcXVlcnk6IHsgdHlwZXM6IFtdIH0gfSwKICAgICAgY29sdW1uczogWydjbG9zZScsICdoaWdoJywgJ2xvdyddCiAgICB9KTsKICAgIHZhciByID0gYXdhaXQgZmV0Y2goJ2h0dHBzOi8vc2Nhbm5lci50cmFkaW5ndmlldy5jb20vZ2xvYmFsL3NjYW4nLCB7CiAgICAgIG1ldGhvZDogJ1BPU1QnLCBoZWFkZXJzOiB7ICdDb250ZW50LVR5cGUnOiAndGV4dC9wbGFpbicgfSwgYm9keTogYm9keQogICAgfSk7CiAgICBpZiAoIXIub2spIHJldHVybjsKICAgIHZhciBqID0gYXdhaXQgci5qc29uKCk7CiAgICBpZiAoIWouZGF0YSkgcmV0dXJuOwogICAgai5kYXRhLmZvckVhY2goZnVuY3Rpb24ocm93KSB7CiAgICAgIHZhciBzID0gKHJvdy5zIHx8ICcnKS50b1VwcGVyQ2FzZSgpOwogICAgICB2YXIgYyA9IHJvdy5kWzBdLCBoID0gcm93LmRbMV0sIGwgPSByb3cuZFsyXTsKICAgICAgaWYgKHMgPT09ICdUVkM6R09MRCcgJiYgYykgewogICAgICAgIC8qIEV4YWN0IENsYXNzaWMgQnVsbGlvbiBTcG90LXRvLUJpZC9Bc2sgY2FsaWJyYXRpb24gKC0wLjcwIC8gKzAuODApICovCiAgICAgICAgQlVMTElPTl9TVEFURS5nb2xkQmlkID0gKyhOdW1iZXIoYykgLSAwLjcwKS50b0ZpeGVkKDIpOwogICAgICAgIEJVTExJT05fU1RBVEUuZ29sZEFzayA9ICsoTnVtYmVyKGMpICsgMC44MCkudG9GaXhlZCgyKTsKICAgICAgICBpZiAoaCkgQlVMTElPTl9TVEFURS5nb2xkSGlnaCA9IE51bWJlcihoKTsKICAgICAgICBpZiAobCkgQlVMTElPTl9TVEFURS5nb2xkTG93ICA9IE51bWJlcihsKTsKICAgICAgfSBlbHNlIGlmIChzID09PSAnVFZDOlNJTFZFUicgJiYgYykgewogICAgICAgIC8qIEV4YWN0IENsYXNzaWMgQnVsbGlvbiBTaWx2ZXIgT3ogYmFzaXMgKCsyLjAyIG92ZXIgVFZDOlNJTFZFUiBzcG90KSAqLwogICAgICAgIEJVTExJT05fU1RBVEUuc2lsdmVyT3ogPSArKE51bWJlcihjKSArIDIuMDIpLnRvRml4ZWQoMik7CiAgICAgICAgaWYgKGgpIEJVTExJT05fU1RBVEUuc2lsSGlnaCA9ICsoTnVtYmVyKGgpICsgMi4wMikudG9GaXhlZCgyKTsKICAgICAgICBpZiAobCkgQlVMTElPTl9TVEFURS5zaWxMb3cgID0gKyhOdW1iZXIobCkgKyAxLjcxKS50b0ZpeGVkKDIpOwogICAgICB9IGVsc2UgaWYgKHMgPT09ICdGWF9JREM6VVNESU5SJyAmJiBjKSB7CiAgICAgICAgLyogRXhhY3QgQ2xhc3NpYyBCdWxsaW9uIFVTRElOUiBBc2sgc3ByZWFkICgrMC4xMTUgb3ZlciBzcG90KSAqLwogICAgICAgIEJVTExJT05fU1RBVEUudXNkSW5yID0gKyhOdW1iZXIoYykgKyAwLjExNSkudG9GaXhlZCgzKTsKICAgICAgfQogICAgfSk7CiAgICByZW5kZXJCdWxsaW9uKCk7CiAgfSBjYXRjaCAoZSkge30KfQpyZW5kZXJCdWxsaW9uKCk7CnBvbGxEaXJlY3RCdWxsaW9uU2Nhbm5lcigpOwpzZXRJbnRlcnZhbChwb2xsRGlyZWN0QnVsbGlvblNjYW5uZXIsIDEwMDApOwoKLyogUmVhbC10aW1lIEJUQyBXZWJTb2NrZXQgZm9yIHRvcCB0aWNrZXIgYW5kIEJUQyBjaGFydCAqLwpmdW5jdGlvbiBzdGFydEJ0Y1NvY2tldCgpIHsKICB0cnkgewogICAgdmFyIHdzID0gbmV3IFdlYlNvY2tldCgnd3NzOi8vc3RyZWFtLmJpbmFuY2UuY29tOjk0NDMvd3MvYnRjdXNkdEB0aWNrZXInKTsKICAgIHdzLm9ubWVzc2FnZSA9IGZ1bmN0aW9uKGV2KSB7CiAgICAgIHRyeSB7CiAgICAgICAgdmFyIGQgPSBKU09OLnBhcnNlKGV2LmRhdGEpOwogICAgICAgIGlmIChkICYmIGQucyA9PT0gJ0JUQ1VTRFQnICYmIGQuYykgewogICAgICAgICAgaWYgKCFRWydCVEMtVVNEJ10pIFFbJ0JUQy1VU0QnXSA9IHsgbmFtZTogJ0JUQycsIHNlcmllczogW10gfTsKICAgICAgICAgIFFbJ0JUQy1VU0QnXS5wcmljZSA9IHBhcnNlRmxvYXQoZC5jKTsKICAgICAgICAgIFFbJ0JUQy1VU0QnXS5wY3QgICA9IHBhcnNlRmxvYXQoZC5QKTsKICAgICAgICAgIGxhc3RVcGQgPSBEYXRlLm5vdygpOwogICAgICAgICAgcmVuZGVyVGlja2VyKCk7CiAgICAgICAgICByZW5kZXJDaGFydHMoKTsKICAgICAgICAgIFBSRVZbJ0JUQy1VU0QnXSA9IFFbJ0JUQy1VU0QnXS5wcmljZTsKICAgICAgICB9CiAgICAgIH0gY2F0Y2ggKGUpIHt9CiAgICB9OwogICAgd3Mub25jbG9zZSA9IGZ1bmN0aW9uKCkgeyBzZXRUaW1lb3V0KHN0YXJ0QnRjU29ja2V0LCA0MDAwKTsgfTsKICB9IGNhdGNoIChlKSB7fQp9CnN0YXJ0QnRjU29ja2V0KCk7CgovKiBtaW5pIGNoYXJ0cyAqLwpmdW5jdGlvbiBkcmF3QXJlYShjdixzZXJpZXMsdXApewogIGlmKCFjdilyZXR1cm47dmFyIGRwcj13aW5kb3cuZGV2aWNlUGl4ZWxSYXRpb3x8MTt2YXIgdz1jdi5jbGllbnRXaWR0aCxoPWN2LmNsaWVudEhlaWdodDsKICBpZih3PDJ8fGg8MilyZXR1cm47Y3Yud2lkdGg9dypkcHI7Y3YuaGVpZ2h0PWgqZHByO3ZhciB4PWN2LmdldENvbnRleHQoJzJkJyk7eC5zZXRUcmFuc2Zvcm0oZHByLDAsMCxkcHIsMCwwKTt4LmNsZWFyUmVjdCgwLDAsdyxoKTsKICBpZighc2VyaWVzfHxzZXJpZXMubGVuZ3RoPDIpcmV0dXJuOwogIHZhciBtaW49TWF0aC5taW4uYXBwbHkobnVsbCxzZXJpZXMpLG1heD1NYXRoLm1heC5hcHBseShudWxsLHNlcmllcykscm5nPShtYXgtbWluKXx8MTsKICB2YXIgY29sPXVwPycjMTdjOTgzJzonI2ZmM2I0Nyc7CiAgdmFyIHB4PWZ1bmN0aW9uKGkpe3JldHVybiBpLyhzZXJpZXMubGVuZ3RoLTEpKnc7fSxweT1mdW5jdGlvbih2KXtyZXR1cm4gaC02LSgodi1taW4pL3JuZykqKGgtMTIpO307CiAgeC5iZWdpblBhdGgoKTt4Lm1vdmVUbygwLGgpO3Nlcmllcy5mb3JFYWNoKGZ1bmN0aW9uKHYsaSl7eC5saW5lVG8ocHgoaSkscHkodikpO30pO3gubGluZVRvKHcsaCk7eC5jbG9zZVBhdGgoKTsKICB2YXIgZz14LmNyZWF0ZUxpbmVhckdyYWRpZW50KDAsMCwwLGgpO2cuYWRkQ29sb3JTdG9wKDAsdXA/J3JnYmEoMjMsMjAxLDEzMSwuMzQpJzoncmdiYSgyNTUsNTksNzEsLjM0KScpO2cuYWRkQ29sb3JTdG9wKDEsJ3JnYmEoMCwwLDAsMCknKTsKICB4LmZpbGxTdHlsZT1nO3guZmlsbCgpOwogIHguYmVnaW5QYXRoKCk7c2VyaWVzLmZvckVhY2goZnVuY3Rpb24odixpKXt2YXIgWD1weChpKSxZPXB5KHYpO2k/eC5saW5lVG8oWCxZKTp4Lm1vdmVUbyhYLFkpO30pO3guc3Ryb2tlU3R5bGU9Y29sO3gubGluZVdpZHRoPTI7eC5zdHJva2UoKTsKfQpmdW5jdGlvbiByZW5kZXJDaGFydHMoKXsKICBpZighJCgnZHh5Q3YnKSlyZXR1cm47ICAgLyogbWluaS1jaGFydHMgcmVtb3ZlZDsgbm90aGluZyB0byBkcmF3ICovCiAgdmFyIGQ9UVsnRFgtWS5OWUInXXx8e30sYj1RWydCVEMtVVNEJ118fHt9OwogIHZhciBkdT0oZC5wY3R8fDApPj0wLGJ1PShiLnBjdHx8MCk+PTA7CiAgJCgnZHh5VmFsJykudGV4dENvbnRlbnQ9Zm10KGQucHJpY2UsMyk7JCgnZHh5Q2hnJykudGV4dENvbnRlbnQ9YXJyb3coZC5wY3R8fDApKycgJytwY3RUeHQoZC5wY3QpOyQoJ2R4eUNoZycpLmNsYXNzTmFtZT0nY2hnICcrKGR1Pyd1cCc6J2Rvd24nKTsKICAkKCdidGNWYWwnKS50ZXh0Q29udGVudD1mbXQoYi5wcmljZSwyKTskKCdidGNDaGcnKS50ZXh0Q29udGVudD1hcnJvdyhiLnBjdHx8MCkrJyAnK3BjdFR4dChiLnBjdCk7JCgnYnRjQ2hnJykuY2xhc3NOYW1lPSdjaGcgJysoYnU/J3VwJzonZG93bicpOwogIGRyYXdBcmVhKCQoJ2R4eUN2JyksZC5zZXJpZXMsZHUpO2RyYXdBcmVhKCQoJ2J0Y0N2JyksYi5zZXJpZXMsYnUpOwp9CmZ1bmN0aW9uIHJlbmRlckFsbCgpewogIHJlbmRlclRpY2tlcigpOwogIHJlbmRlckFzaWEoKTsKICByZW5kZXJDaGFydHMoKTsKICBPYmplY3Qua2V5cyhRKS5mb3JFYWNoKGZ1bmN0aW9uKHMpe2lmKFFbc10mJlFbc10ucHJpY2UhPW51bGwpUFJFVltzXT1RW3NdLnByaWNlO30pOwp9CmFzeW5jIGZ1bmN0aW9uIHBvbGxRdW90ZXMoKXsKICB0cnl7dmFyIHI9YXdhaXQgZmV0Y2goJy9hcGkvcXVvdGVzJyx7Y2FjaGU6J25vLXN0b3JlJ30pO2lmKHIub2spe1E9YXdhaXQgci5qc29uKCk7bGFzdFVwZD1EYXRlLm5vdygpO3JlbmRlckFsbCgpO319Y2F0Y2goZSl7fQp9CnBvbGxRdW90ZXMoKTsKc2V0SW50ZXJ2YWwocG9sbFF1b3RlcywgNjAwMCk7CgovKiAtLS0tLS0tLS0tLS0tLS0tIHNjYWxlIDE5MjB4MTA4MCBzdGFnZSB0byBmaXQgYW55IGRpc3BsYXkgKGtlZXBzIGV4YWN0IHByb3BvcnRpb25zKSAtLS0tLS0tLS0tLS0tLS0tICovCmZ1bmN0aW9uIGZpdCgpewogIHZhciBlbD1kb2N1bWVudC5xdWVyeVNlbGVjdG9yKCcuYXBwJyk7aWYoIWVsKXJldHVybjsKICB2YXIgcz1NYXRoLm1pbih3aW5kb3cuaW5uZXJXaWR0aC8xOTIwLHdpbmRvdy5pbm5lckhlaWdodC8xMDgwKTsKICB2YXIgb3g9KHdpbmRvdy5pbm5lcldpZHRoLTE5MjAqcykvMiwgb3k9KHdpbmRvdy5pbm5lckhlaWdodC0xMDgwKnMpLzI7CiAgZWwuc3R5bGUudHJhbnNmb3JtPSd0cmFuc2xhdGUoJytveCsncHgsJytveSsncHgpIHNjYWxlKCcrcysnKSc7CiAgcmVuZGVyQ2hhcnRzKCk7Cn0Kd2luZG93LmFkZEV2ZW50TGlzdGVuZXIoJ3Jlc2l6ZScsZml0KTsKZml0KCk7CgovKiAtLS0tLS0tLS0tLS0tLS0tIG1hY3JvIGNhbGVuZGFyIC0tLS0tLS0tLS0tLS0tLS0gKi8KdmFyIENVUj17VVM6J1VTRCcsRVU6J0VVUicsR0I6J0dCUCcsSlA6J0pQWScsQ046J0NOWScsQ0E6J0NBRCcsQVU6J0FVRCcsQ0g6J0NIRid9OwpmdW5jdGlvbiBmbXRWYWwodix1bml0KXsgaWYodj09bnVsbHx8dj09PScnKXJldHVybiAn4oCUJzsgdmFyIG49TnVtYmVyKHYpOyBpZihpc05hTihuKSlyZXR1cm4gZXNjKFN0cmluZyh2KSk7IHZhciBzPU1hdGguYWJzKG4pPj0xMDAwP24udG9Mb2NhbGVTdHJpbmcoJ2VuLVVTJyk6U3RyaW5nKG4pOyByZXR1cm4gZXNjKHMrKHVuaXR8fCcnKSk7IH0KYXN5bmMgZnVuY3Rpb24gcG9sbENhbGVuZGFyKCl7CiAgdHJ5ewogICAgdmFyIHI9YXdhaXQgZmV0Y2goJy9hcGkvY2FsZW5kYXInLHtjYWNoZTonbm8tc3RvcmUnfSk7aWYoIXIub2spcmV0dXJuO3ZhciByb3dzPWF3YWl0IHIuanNvbigpOwogICAgaWYoIXJvd3N8fCFyb3dzLmxlbmd0aClyZXR1cm47CiAgICB2YXIgdG9kYXk9bmV3IERhdGUoKS50b0RhdGVTdHJpbmcoKTsKICAgICQoJ21hY3JvJykuaW5uZXJIVE1MPXJvd3Muc2xpY2UoMCw2KS5tYXAoZnVuY3Rpb24oZSl7CiAgICAgIHZhciBkPWUuZGF0ZT9uZXcgRGF0ZShlLmRhdGUpOm51bGw7CiAgICAgIHZhciB0PWQ/ZC50b0xvY2FsZVRpbWVTdHJpbmcoJ2VuLUdCJyx7aG91cjonMi1kaWdpdCcsbWludXRlOicyLWRpZ2l0J30pOicnOwogICAgICB2YXIgZGF5PWQ/KGQudG9EYXRlU3RyaW5nKCk9PT10b2RheT8nVE9EQVknOmQudG9Mb2NhbGVEYXRlU3RyaW5nKCdlbi1HQicse3dlZWtkYXk6J3Nob3J0JyxkYXk6JzItZGlnaXQnfSkudG9VcHBlckNhc2UoKSk6Jyc7CiAgICAgIHZhciBkb25lPWUuYWN0dWFsIT1udWxsJiZlLmFjdHVhbCE9PScnOwogICAgICB2YXIgY3VyPUNVUltlLmNvdW50cnldfHxlLmNvdW50cnl8fCcnOwogICAgICByZXR1cm4gJzxkaXYgY2xhc3M9Im1yb3cnKyhkb25lPycgZG9uZSc6JycpKyciPjxkaXYgY2xhc3M9ImV2Ij48ZGl2IGNsYXNzPSJwci1kb3QiPjwvZGl2PjxkaXYgY2xhc3M9ImV2dCI+JysKICAgICAgICAnPGRpdiBjbGFzcz0idDEiPjxiPicrZXNjKGRheSkrJzwvYj4gJytlc2ModCkrJyAmbWlkZG90OyAnK2ZsYWcoZS5jb3VudHJ5KStlc2MoY3VyKSsnPC9kaXY+PGRpdiBjbGFzcz0idDIiPicrZXNjKGUudGl0bGV8fCcnKSsnPC9kaXY+PC9kaXY+PC9kaXY+JysKICAgICAgICAnPGRpdiBjbGFzcz0iYSI+JytmbXRWYWwoZS5hY3R1YWwsZS51bml0KSsnPC9kaXY+PGRpdiBjbGFzcz0iZiI+JytmbXRWYWwoZS5mb3JlY2FzdCxlLnVuaXQpKyc8L2Rpdj48L2Rpdj4nOwogICAgfSkuam9pbignJyk7CiAgfWNhdGNoKGUpe30KfQpwb2xsQ2FsZW5kYXIoKTtzZXRJbnRlcnZhbChwb2xsQ2FsZW5kYXIsMTUqNjAqMTAwMCk7CgovKiAtLS0tLS0tLS0tLS0tLS0tIGNvdW50cnkgZmxhZ3MgKGN1cnJlbmN5IGNvZGUgLT4gSVNPIGNvdW50cnkgZm9yIGZsYWdjZG4pIC0tLS0tLS0tLS0tLS0tLS0gKi8KdmFyIENDMj17VVNEOid1cycsRVVSOidldScsR0JQOidnYicsSlBZOidqcCcsQ05ZOidjbicsQ0FEOidjYScsQVVEOidhdScsQ0hGOidjaCcsTlpEOidueid9OwpmdW5jdGlvbiBmbGFnKGN1cil7dmFyIGM9Q0MyWyhjdXJ8fCcnKS50b1VwcGVyQ2FzZSgpXTtyZXR1cm4gYz8nPGltZyBjbGFzcz0iZmxhZyIgc3JjPSJodHRwczovL2ZsYWdjZG4uY29tL3c0MC8nK2MrJy5wbmciIHJlZmVycmVycG9saWN5PSJuby1yZWZlcnJlciIgb25lcnJvcj0idGhpcy5zdHlsZS5kaXNwbGF5PVwnbm9uZVwnIj4nOicnO30KCi8qIC0tLS0tLS0tLS0tLS0tLS0gZ2xvYmFsIGV2ZW50cyAoc3VtbWl0cywgZm9ydW1zLCBwZWFjZSB0YWxrcywgZGVhbHMpIC0tLS0tLS0tLS0tLS0tLS0gKi8KZnVuY3Rpb24gZXR5cGUodCl7dD0odHx8JycpLnRvTG93ZXJDYXNlKCk7CiAgaWYodC5pbmRleE9mKCdwZWFjZScpPj0wfHx0LmluZGV4T2YoJ3RhbGtzJyk+PTApcmV0dXJuICdwZWFjZSc7CiAgaWYodC5pbmRleE9mKCdmb3J1bScpPj0wKXJldHVybiAnZm9ydW0nOwogIGlmKHQuaW5kZXhPZignZGVhbCcpPj0wfHx0LmluZGV4T2YoJ3RyYWRlJyk+PTApcmV0dXJuICdkZWFsJzsKICBpZih0LmluZGV4T2YoJ3N1bW1pdCcpPj0wKXJldHVybiAnc3VtbWl0JzsKICByZXR1cm4gJ21lZXRpbmcnO30KYXN5bmMgZnVuY3Rpb24gcG9sbEV2ZW50cygpewogIHRyeXsKICAgIHZhciByPWF3YWl0IGZldGNoKCcvYXBpL2V2ZW50cycse2NhY2hlOiduby1zdG9yZSd9KTtpZighci5vaylyZXR1cm47dmFyIHJvd3M9YXdhaXQgci5qc29uKCk7CiAgICB2YXIgYm94PSQoJ2V2ZW50cycpO2lmKCFib3gpcmV0dXJuOwogICAgaWYoIXJvd3N8fCFyb3dzLmxlbmd0aCl7Ym94LmlubmVySFRNTD0nJztyZXR1cm47fQogICAgdmFyIG5vdz1uZXcgRGF0ZSgpOyBub3cuc2V0SG91cnMoMCwwLDAsMCk7CiAgICBib3guaW5uZXJIVE1MPXJvd3Muc2xpY2UoMCw2KS5tYXAoZnVuY3Rpb24oZSl7CiAgICAgIHZhciBkPWUuZGF0ZT9uZXcgRGF0ZShlLmRhdGUpOm51bGw7CiAgICAgIHZhciBtb249ZD9kLnRvTG9jYWxlRGF0ZVN0cmluZygnZW4tR0InLHttb250aDonc2hvcnQnfSkudG9VcHBlckNhc2UoKTonJzsKICAgICAgdmFyIGRheT1kP2QudG9Mb2NhbGVEYXRlU3RyaW5nKCdlbi1HQicse2RheTonMi1kaWdpdCd9KTonJzsKICAgICAgdmFyIHNvb249ZD8oKGQtbm93KS84NjQwMDAwMCk8PTc6ZmFsc2U7CiAgICAgIHZhciBjbHM9ZXR5cGUoZS50eXBlKTsKICAgICAgdmFyIGxhYmVsPShlLnR5cGV8fCdNZWV0aW5nJykudG9VcHBlckNhc2UoKTsKICAgICAgcmV0dXJuICc8ZGl2IGNsYXNzPSJlcm93Ij48ZGl2IGNsYXNzPSJlZGF0ZScrKHNvb24/JyBzb29uJzonJykrJyI+PGI+Jytlc2MobW9uKSsnPC9iPjxpPicrZXNjKGRheSkrJzwvaT48L2Rpdj4nKwogICAgICAgICc8ZGl2IGNsYXNzPSJlaW5mbyI+PGRpdiBjbGFzcz0iZXQiPjxzcGFuIGNsYXNzPSJldGFnICcrY2xzKyciPicrZXNjKGxhYmVsKSsnPC9zcGFuPjxzcGFuIGNsYXNzPSJuYW1lIj4nK2VzYyhlLnRpdGxlfHwnJykrJzwvc3Bhbj48L2Rpdj4nKwogICAgICAgICc8ZGl2IGNsYXNzPSJlbG9jIj4nK2VzYyhlLmxvY3x8JycpKyc8L2Rpdj48L2Rpdj48L2Rpdj4nOwogICAgfSkuam9pbignJyk7CiAgfWNhdGNoKGUpe30KfQpwb2xsRXZlbnRzKCk7c2V0SW50ZXJ2YWwocG9sbEV2ZW50cywzMCo2MCoxMDAwKTsKCi8qIC0tLS0tLS0tLS0tLS0tLS0gbmV3cyBjaHlyb24gJiBib3R0b20gc3RyaXAgKEZBU1RFUiBTQ1JPTEwgU1BFRUQpIC0tLS0tLS0tLS0tLS0tLS0gKi8KdmFyIE5FV1M9W10sY2k9MDsKZnVuY3Rpb24gY3ljbGVDaHlyb24oKXsgdmFyIGVsPSQoJ2NoeUhMJyk7IGlmKCFlbHx8IU5FV1MubGVuZ3RoKXJldHVybjsgZWwudGV4dENvbnRlbnQ9TkVXU1tjaSVORVdTLmxlbmd0aF0udGl0bGV8fCcnOyBjaSsrOyB9CmZ1bmN0aW9uIHRpbWVBZ28oZCl7dmFyIHQ9RGF0ZS5wYXJzZShkKTtpZighdClyZXR1cm4gJyc7dmFyIHM9KERhdGUubm93KCktdCkvMTAwMDtpZihzPDM2MDApcmV0dXJuIE1hdGgubWF4KDEsTWF0aC5yb3VuZChzLzYwKSkrJ20gYWdvJztpZihzPDg2NDAwKXJldHVybiBNYXRoLnJvdW5kKHMvMzYwMCkrJ2ggYWdvJztyZXR1cm4gTWF0aC5yb3VuZChzLzg2NDAwKSsnZCBhZ28nO30KZnVuY3Rpb24gbmNhcmQobil7CiAgdmFyIGltZz1uLmltZz8nPGltZyBzcmM9IicrZXNjKG4uaW1nKSsnIiBsb2FkaW5nPSJsYXp5IiByZWZlcnJlcnBvbGljeT0ibm8tcmVmZXJyZXIiIG9uZXJyb3I9InRoaXMuc3R5bGUuZGlzcGxheT1cJ25vbmVcJzt0aGlzLnBhcmVudE5vZGUuaW5uZXJIVE1MPVwnPHNwYW4gY2xhc3M9JnF1b3Q7cGgmcXVvdDs+Jytlc2MoKG4uc3JjfHwnPycpWzBdKSsnPC9zcGFuPlwnIj4nOic8c3BhbiBjbGFzcz0icGgiPicrZXNjKChuLnNyY3x8Jz8nKVswXSkrJzwvc3Bhbj4nOwogIHJldHVybiAnPGRpdiBjbGFzcz0ibmNhcmQiPjxkaXYgY2xhc3M9Im5pbWciPicraW1nKyc8L2Rpdj48ZGl2IGNsYXNzPSJuYiI+PGRpdiBjbGFzcz0ibnMiPicrZXNjKG4uc3JjKSsnIDxzcGFuIGNsYXNzPSJ0bSI+Jyt0aW1lQWdvKG4uZGF0ZSkrJzwvc3Bhbj48L2Rpdj48ZGl2IGNsYXNzPSJudCI+Jytlc2Mobi50aXRsZSkrJzwvZGl2PjwvZGl2PjwvZGl2Pic7Cn0KYXN5bmMgZnVuY3Rpb24gcG9sbE5ld3MoKXsKICB0cnl7CiAgICB2YXIgcj1hd2FpdCBmZXRjaCgnL2FwaS9uZXdzJyx7Y2FjaGU6J25vLXN0b3JlJ30pO2lmKCFyLm9rKXJldHVybjt2YXIgaXRlbXM9YXdhaXQgci5qc29uKCk7CiAgICBpZihpdGVtcyYmaXRlbXMubGVuZ3RoKXtORVdTPWl0ZW1zOwogICAgICB2YXIgbGluZT1pdGVtcy5zbGljZSgwLDE4KS5tYXAoZnVuY3Rpb24obil7cmV0dXJuIG4udGl0bGU7fSkuam9pbignICAg4oCiICAgJyk7CiAgICAgICQoJ2NoeVNjcm9sbCcpLnRleHRDb250ZW50PWxpbmUrJyAgIOKAoiAgICcrbGluZTsKICAgICAgdmFyIHNjPSQoJ2NoeVNjcm9sbCcpOwogICAgICBzYy5zdHlsZS5zZXRQcm9wZXJ0eSgnLS1ia2R1cicsTWF0aC5tYXgoNDUsTWF0aC5yb3VuZChzYy5zY3JvbGxXaWR0aC8yLzc1KSkrJ3MnKTsKICAgICAgY3ljbGVDaHlyb24oKTsKICAgICAgdmFyIHRvcD1pdGVtcy5zbGljZSgwLDIwKSx0cj0kKCduZXdzU3RyaXAnKTsKICAgICAgaWYodHIpewogICAgICAgIHRyLmNsYXNzTGlzdC5yZW1vdmUoJ3J1bicpOwogICAgICAgIHZhciBvbmU9dG9wLm1hcChuY2FyZCkuam9pbignJyk7CiAgICAgICAgdHIuaW5uZXJIVE1MPW9uZStvbmU7CiAgICAgICAgLyogcmVwZWF0IGVub3VnaCBzbyBlYWNoIGhhbGYgYWx3YXlzIGZpbGxzIHRoZSBzY3JlZW4gKG5vIGVtcHR5IGdhcCB3aGVuIGZldyBpdGVtcykgKi8KICAgICAgICB2YXIgY29udD10ci5wYXJlbnRFbGVtZW50P3RyLnBhcmVudEVsZW1lbnQuY2xpZW50V2lkdGg6MTkyMDsKICAgICAgICB2YXIgaGFsZj10ci5zY3JvbGxXaWR0aC8yOwogICAgICAgIGlmKGhhbGY+MCYmaGFsZjxjb250KXt2YXIgcmVwcz1NYXRoLmNlaWwoY29udC9oYWxmKSoyO3ZhciBmdWxsPScnO2Zvcih2YXIgaT0wO2k8cmVwcztpKyspZnVsbCs9b25lO3RyLmlubmVySFRNTD1mdWxsO2hhbGY9dHIuc2Nyb2xsV2lkdGgvMjt9CiAgICAgICAgdHIuc3R5bGUuc2V0UHJvcGVydHkoJy0tbmR1cicsTWF0aC5tYXgoNDAsTWF0aC5yb3VuZChoYWxmLzU1KSkrJ3MnKTsgIC8qIDU1IHB4L3MsIHNhbWUgYXMgdGlja2VyICovCiAgICAgICAgdm9pZCB0ci5vZmZzZXRXaWR0aDsKICAgICAgICB0ci5jbGFzc0xpc3QuYWRkKCdydW4nKTsKICAgICAgfQogICAgfQogIH1jYXRjaChlKXt9Cn0KcG9sbE5ld3MoKTtzZXRJbnRlcnZhbChwb2xsTmV3cyw1KjYwKjEwMDApOwpzZXRJbnRlcnZhbChjeWNsZUNoeXJvbiw2MDAwKTsKCi8qIC0tLS0tLS0tLS0tLS0tLS0ga2lvc2sgc2FmZXR5IHJlbG9hZCAtLS0tLS0tLS0tLS0tLS0tICovCnNldFRpbWVvdXQoZnVuY3Rpb24oKXtsb2NhdGlvbi5yZWxvYWQoKTt9LDIqNjAqNjAqMTAwMCk7Cjwvc2NyaXB0Pgo8L2JvZHk+CjwvaHRtbD4K"
# ==== END EMBED ====

if __name__ == "__main__":
    print("Market Command Center  ->  http://localhost:%d" % PORT)
    print("Data: /api/quotes  /api/calendar  /api/news   (Ctrl+C to stop)")
    try:
        ThreadingHTTPServer(("0.0.0.0", PORT), H).serve_forever()
    except KeyboardInterrupt:
        pass
