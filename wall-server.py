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

def page_html():
    """Serve the dashboard page. Prefer an on-disk market-wall.html if the user
    has one next to this script; otherwise use the copy embedded below. This lets
    the whole dashboard ship as a SINGLE file (wall-server.py) so there is no
    separate .html download for the browser's virus scanner to block."""
    p = os.path.join(HERE, "market-wall.html")
    try:
        if os.path.isfile(p):
            with open(p, "rb") as f:
                return f.read()
    except Exception:
        pass
    return base64.b64decode(EMBED_HTML_B64)

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
EMBED_HTML_B64 = "PCFkb2N0eXBlIGh0bWw+CjxodG1sIGxhbmc9ImVuIj4KPGhlYWQ+CjxtZXRhIGNoYXJzZXQ9InV0Zi04Ij4KPG1ldGEgbmFtZT0idmlld3BvcnQiIGNvbnRlbnQ9IndpZHRoPWRldmljZS13aWR0aCxpbml0aWFsLXNjYWxlPTEiPgo8dGl0bGU+TWFya2V0IENvbW1hbmQgQ2VudGVyPC90aXRsZT4KPGxpbmsgcmVsPSJwcmVjb25uZWN0IiBocmVmPSJodHRwczovL2ZvbnRzLmdvb2dsZWFwaXMuY29tIj48bGluayByZWw9InByZWNvbm5lY3QiIGhyZWY9Imh0dHBzOi8vZm9udHMuZ3N0YXRpYy5jb20iIGNyb3Nzb3JpZ2luPgo8bGluayByZWw9InN0eWxlc2hlZXQiIGhyZWY9Imh0dHBzOi8vZm9udHMuZ29vZ2xlYXBpcy5jb20vY3NzMj9mYW1pbHk9SW50ZXI6d2dodEA0MDA7NTAwOzYwMDs3MDA7ODAwOzkwMCZkaXNwbGF5PXN3YXAiPgo8c3R5bGU+CiAgOnJvb3R7CiAgICAtLWJnOiMwMDA7IC0tcGFuZWw6IzBiMGQxMjsgLS1wYW5lbC0yOiMwZTExMTg7IC0tY2FyZDojMGQxMDE3OyAtLWxpbmU6IzFhMWYyOTsgLS1saW5lLTI6IzE0MTkyMjsKICAgIC0tZmc6I2YyZjVmYTsgLS1tdXRlZDojOGE5NGE2OyAtLWZhaW50OiM1YTY0NzQ7CiAgICAtLXVwOiMxN2M5ODM7IC0tdXAtYmc6cmdiYSgyMywyMDEsMTMxLC4xNCk7IC0tZG93bjojZmYzYjQ3OyAtLWRvd24tYmc6cmdiYSgyNTUsNTksNzEsLjE0KTsKICAgIC0tZ29sZDojZjBiNDI5OyAtLW9yYW5nZTojZmY3YTFhOyAtLWxpdmU6I2ZmM2I0NzsgLS1hY2NlbnQ6I2YwYjQyOTsKICAgIC0tc2FuczoiSW50ZXIiLHN5c3RlbS11aSwtYXBwbGUtc3lzdGVtLCJTZWdvZSBVSSIsc2Fucy1zZXJpZjsKICB9CiAgKntib3gtc2l6aW5nOmJvcmRlci1ib3g7bWFyZ2luOjA7cGFkZGluZzowfQogIGh0bWwsYm9keXtoZWlnaHQ6MTAwJTt3aWR0aDoxMDAlfQogIGJvZHl7YmFja2dyb3VuZDojMDAwO2NvbG9yOnZhcigtLWZnKTtmb250LWZhbWlseTp2YXIoLS1zYW5zKTtvdmVyZmxvdzpoaWRkZW47LXdlYmtpdC1mb250LXNtb290aGluZzphbnRpYWxpYXNlZDtmb250LXZhcmlhbnQtbnVtZXJpYzp0YWJ1bGFyLW51bXN9CiAgLyogZml4ZWQgMTkyMHgxMDgwIHN0YWdlLCBzY2FsZWQgdG8gZml0IGFueSBkaXNwbGF5IC0+IGlkZW50aWNhbCBwcm9wb3J0aW9ucyBldmVyeXdoZXJlICovCiAgLmFwcHt3aWR0aDoxOTIwcHg7aGVpZ2h0OjEwODBweDt0cmFuc2Zvcm0tb3JpZ2luOnRvcCBsZWZ0O3Bvc2l0aW9uOmFic29sdXRlO3RvcDowO2xlZnQ6MDtkaXNwbGF5OmdyaWQ7Z3JpZC10ZW1wbGF0ZS1yb3dzOjU2cHggODhweCAxZnIgMTI4cHg7Z2FwOjEycHg7cGFkZGluZzoxM3B4O2JhY2tncm91bmQ6dmFyKC0tYmcpfQoKICAvKiBoZWFkZXIgKi8KICAuaGVhZHtkaXNwbGF5OmZsZXg7YWxpZ24taXRlbXM6Y2VudGVyO2p1c3RpZnktY29udGVudDpzcGFjZS1iZXR3ZWVuO3BhZGRpbmc6MCA4cHh9CiAgLmh0aXRsZXtmb250LXdlaWdodDo4MDA7Zm9udC1zaXplOjI4cHg7bGV0dGVyLXNwYWNpbmc6LjA5ZW19CiAgLmhzZXNze2Rpc3BsYXk6ZmxleDtnYXA6MjhweDtmb250LXdlaWdodDo3MDA7Zm9udC1zaXplOjE4cHh9CiAgLmhzZXNzIHNwYW57Y29sb3I6dmFyKC0tbXV0ZWQpfSAuaHNlc3MgYntjb2xvcjp2YXIoLS1mZyk7Zm9udC13ZWlnaHQ6ODAwO21hcmdpbi1sZWZ0OjdweH0KICAuaGNsb2Nre2ZvbnQtd2VpZ2h0OjcwMDtmb250LXNpemU6MTlweDtjb2xvcjp2YXIoLS1tdXRlZCl9IC5oY2xvY2sgYntjb2xvcjp2YXIoLS1mZyk7Zm9udC13ZWlnaHQ6ODAwfQogIC5obGl2ZXtkaXNwbGF5OmlubGluZS1mbGV4O2FsaWduLWl0ZW1zOmNlbnRlcjtnYXA6N3B4O2ZvbnQtd2VpZ2h0OjgwMDtmb250LXNpemU6MTNweDtsZXR0ZXItc3BhY2luZzouMDhlbTtjb2xvcjp2YXIoLS11cCl9CiAgLmhsaXZlIC5kb3R7d2lkdGg6OXB4O2hlaWdodDo5cHg7Ym9yZGVyLXJhZGl1czo1MCU7YmFja2dyb3VuZDp2YXIoLS11cCk7Ym94LXNoYWRvdzowIDAgOXB4IHZhcigtLXVwKTthbmltYXRpb246cHVsc2UgMS40cyBpbmZpbml0ZX0KICAuaGxpdmUuc3RhbGV7Y29sb3I6dmFyKC0tZmFpbnQpfSAuaGxpdmUuc3RhbGUgLmRvdHtiYWNrZ3JvdW5kOnZhcigtLWZhaW50KTtib3gtc2hhZG93Om5vbmU7YW5pbWF0aW9uOm5vbmV9CiAgQGtleWZyYW1lcyBmbHVwezAle2JhY2tncm91bmQ6cmdiYSgyMywyMDEsMTMxLC40NSl9MTAwJXtiYWNrZ3JvdW5kOnRyYW5zcGFyZW50fX0KICBAa2V5ZnJhbWVzIGZsZG57MCV7YmFja2dyb3VuZDpyZ2JhKDI1NSw1OSw3MSwuNDUpfTEwMCV7YmFja2dyb3VuZDp0cmFuc3BhcmVudH19CiAgLmZsdXB7YW5pbWF0aW9uOmZsdXAgLjhzIGVhc2Utb3V0O2JvcmRlci1yYWRpdXM6NXB4fSAuZmxkbnthbmltYXRpb246ZmxkbiAuOHMgZWFzZS1vdXQ7Ym9yZGVyLXJhZGl1czo1cHh9CgogIC8qIHRpY2tlciByb3cgKi8KICAudGlja3tkaXNwbGF5OmZsZXg7YWxpZ24taXRlbXM6Y2VudGVyO2JhY2tncm91bmQ6dmFyKC0tcGFuZWwpO2JvcmRlcjoxcHggc29saWQgdmFyKC0tbGluZSk7Ym9yZGVyLXJhZGl1czoxMnB4O292ZXJmbG93OmhpZGRlbn0KICAudGljay10cmFja3tkaXNwbGF5OmZsZXg7YWxpZ24taXRlbXM6Y2VudGVyO2hlaWdodDoxMDAlO3dpZHRoOm1heC1jb250ZW50O2FuaW1hdGlvbjp0bWFycSB2YXIoLS10ZHVyLDQ2cykgbGluZWFyIGluZmluaXRlO3dpbGwtY2hhbmdlOnRyYW5zZm9ybX0KICBAa2V5ZnJhbWVzIHRtYXJxe2Zyb217dHJhbnNmb3JtOnRyYW5zbGF0ZVgoMCl9dG97dHJhbnNmb3JtOnRyYW5zbGF0ZVgoLTUwJSl9fQogIC50a3tkaXNwbGF5OmZsZXg7YWxpZ24taXRlbXM6Y2VudGVyO2dhcDoxNXB4O3BhZGRpbmc6MCAzNHB4O2hlaWdodDoxMDAlO2JvcmRlci1yaWdodDoxcHggc29saWQgdmFyKC0tbGluZS0yKTt3aGl0ZS1zcGFjZTpub3dyYXB9CiAgLnRrIC5ubXtmb250LXdlaWdodDo4MDA7Zm9udC1zaXplOjMwcHh9CiAgLnRrIC5wcntmb250LXdlaWdodDo3MDA7Zm9udC1zaXplOjMwcHg7Y29sb3I6dmFyKC0tZmcpfQogIC50ayAuY2h7Zm9udC13ZWlnaHQ6ODAwO2ZvbnQtc2l6ZToyNXB4O2Rpc3BsYXk6ZmxleDthbGlnbi1pdGVtczpjZW50ZXI7Z2FwOjdweH0KICAudXB7Y29sb3I6dmFyKC0tdXApfSAuZG93bntjb2xvcjp2YXIoLS1kb3duKX0KCiAgLyogbWFpbiAzIGNvbHMgKi8KICAubWFpbntkaXNwbGF5OmdyaWQ7Z3JpZC10ZW1wbGF0ZS1jb2x1bW5zOjEuNmZyIC44NmZyIDEuMGZyO2dhcDo5cHg7bWluLWhlaWdodDowfQogIC5jb2x7ZGlzcGxheTpmbGV4O2ZsZXgtZGlyZWN0aW9uOmNvbHVtbjtnYXA6OXB4O21pbi1oZWlnaHQ6MDttaW4td2lkdGg6MH0KICAucGFuZWx7YmFja2dyb3VuZDp2YXIoLS1wYW5lbCk7Ym9yZGVyOjFweCBzb2xpZCB2YXIoLS1saW5lKTtib3JkZXItcmFkaXVzOjE0cHg7ZGlzcGxheTpmbGV4O2ZsZXgtZGlyZWN0aW9uOmNvbHVtbjttaW4taGVpZ2h0OjA7b3ZlcmZsb3c6aGlkZGVufQogIC5wdGl0bGV7Zm9udC13ZWlnaHQ6ODAwO2ZvbnQtc2l6ZToxNXB4O2xldHRlci1zcGFjaW5nOi4xZW07cGFkZGluZzo5cHggMTZweCA2cHg7Y29sb3I6dmFyKC0tZmcpO2ZsZXg6MCAwIGF1dG87ZGlzcGxheTpmbGV4O2FsaWduLWl0ZW1zOmNlbnRlcjtqdXN0aWZ5LWNvbnRlbnQ6c3BhY2UtYmV0d2Vlbn0KICAucHRpdGxlIC5wc3Vie2ZvbnQtd2VpZ2h0OjcwMDtmb250LXNpemU6MTFweDtsZXR0ZXItc3BhY2luZzouMDhlbTtjb2xvcjp2YXIoLS1nb2xkKTtkaXNwbGF5OmlubGluZS1mbGV4O2FsaWduLWl0ZW1zOmNlbnRlcjtnYXA6NnB4fQogIC5wdGl0bGUgLnBzdWIgLmJkb3R7d2lkdGg6N3B4O2hlaWdodDo3cHg7Ym9yZGVyLXJhZGl1czo1MCU7YmFja2dyb3VuZDp2YXIoLS11cCk7Ym94LXNoYWRvdzowIDAgOHB4IHZhcigtLXVwKTthbmltYXRpb246cHVsc2UgMS4ycyBpbmZpbml0ZX0KICAubW9kZS1idG57YmFja2dyb3VuZDojMTUxYTI0O2JvcmRlcjoxcHggc29saWQgIzI2MmY0MDtjb2xvcjp2YXIoLS1tdXRlZCk7Zm9udDo3MDAgMTBweC8xIHZhcigtLXNhbnMpO3BhZGRpbmc6NHB4IDhweDtib3JkZXItcmFkaXVzOjVweDtjdXJzb3I6cG9pbnRlcjtsZXR0ZXItc3BhY2luZzouMDZlbX0KICAubW9kZS1idG4uYWN0aXZle2NvbG9yOnZhcigtLWdvbGQpO2JvcmRlci1jb2xvcjpyZ2JhKDI0MCwxODAsNDEsLjUpO2JhY2tncm91bmQ6cmdiYSgyNDAsMTgwLDQxLC4xMil9CgogIC8qIHZpZGVvICovCiAgLnZpZHtmbGV4OjE7bWluLWhlaWdodDowO3Bvc2l0aW9uOnJlbGF0aXZlO2JhY2tncm91bmQ6IzAwMDtib3JkZXI6MXB4IHNvbGlkIHZhcigtLWxpbmUpO2JvcmRlci1yYWRpdXM6MTRweDtvdmVyZmxvdzpoaWRkZW59CiAgLnZpZCBpZnJhbWV7cG9zaXRpb246YWJzb2x1dGU7aW5zZXQ6MDt3aWR0aDoxMDAlO2hlaWdodDoxMDAlO2JvcmRlcjowfQogIC52dGx7cG9zaXRpb246YWJzb2x1dGU7dG9wOjEycHg7bGVmdDoxMnB4O3otaW5kZXg6MztiYWNrZ3JvdW5kOnJnYmEoMTAsMTAsMTIsLjcyKTtiYWNrZHJvcC1maWx0ZXI6Ymx1cig0cHgpO2JvcmRlci1yYWRpdXM6OHB4O3BhZGRpbmc6N3B4IDEycHg7Zm9udC13ZWlnaHQ6NzAwO2ZvbnQtc2l6ZToxM3B4O2Rpc3BsYXk6ZmxleDthbGlnbi1pdGVtczpjZW50ZXI7Z2FwOjhweH0KICAudnRsIC55dHtjb2xvcjojZmYyZDJkO2ZvbnQtd2VpZ2h0OjkwMH0KICAudnRye3Bvc2l0aW9uOmFic29sdXRlO3RvcDoxMnB4O3JpZ2h0OjEycHg7ei1pbmRleDozO2JhY2tncm91bmQ6I2UwMWIyNDtib3JkZXItcmFkaXVzOjdweDtwYWRkaW5nOjZweCAxMXB4O2ZvbnQtd2VpZ2h0OjkwMDtmb250LXNpemU6MTNweDtsZXR0ZXItc3BhY2luZzouMDhlbTtkaXNwbGF5OmZsZXg7YWxpZ24taXRlbXM6Y2VudGVyO2dhcDo3cHh9CiAgLnZ0ciAuZHt3aWR0aDo4cHg7aGVpZ2h0OjhweDtib3JkZXItcmFkaXVzOjUwJTtiYWNrZ3JvdW5kOiNmZmY7YW5pbWF0aW9uOnB1bHNlIDEuM3MgaW5maW5pdGV9CiAgLnZicntwb3NpdGlvbjphYnNvbHV0ZTtib3R0b206NjJweDtyaWdodDoxMnB4O3otaW5kZXg6MztiYWNrZ3JvdW5kOnJnYmEoMTAsMTAsMTIsLjc4KTtib3JkZXItcmFkaXVzOjZweDtwYWRkaW5nOjVweCAxMHB4O2ZvbnQtd2VpZ2h0OjgwMDtmb250LXNpemU6MTJweH0KICAudmJyIGJ7Y29sb3I6dmFyKC0tZ29sZCl9CiAgLmNoeXJvbntwb3NpdGlvbjphYnNvbHV0ZTtsZWZ0OjA7cmlnaHQ6MDtib3R0b206MDt6LWluZGV4OjN9CiAgLmNoeXJvbiAuYmt7ZGlzcGxheTpmbGV4O2FsaWduLWl0ZW1zOmNlbnRlcjtiYWNrZ3JvdW5kOiMwYTBjMTA7Ym9yZGVyLXRvcDozcHggc29saWQgdmFyKC0tb3JhbmdlKX0KICAuY2h5cm9uIC5iayAudGFne2JhY2tncm91bmQ6dmFyKC0tb3JhbmdlKTtjb2xvcjojMTExO2ZvbnQtd2VpZ2h0OjkwMDtmb250LXNpemU6MTdweDtsZXR0ZXItc3BhY2luZzouMDZlbTtwYWRkaW5nOjEycHggMThweDt3aGl0ZS1zcGFjZTpub3dyYXB9CiAgLmNoeXJvbiAuYmsgLnNjcm9sbHdyYXB7ZmxleDoxO292ZXJmbG93OmhpZGRlbjtwb3NpdGlvbjpyZWxhdGl2ZX0KICAuY2h5cm9uIC5iayAuc2Nyb2xse2Rpc3BsYXk6aW5saW5lLWZsZXg7d2hpdGUtc3BhY2U6bm93cmFwO3BhZGRpbmc6MTJweCAwO2ZvbnQtd2VpZ2h0OjcwMDtmb250LXNpemU6MjFweDtjb2xvcjojZjJkOWM0O2FuaW1hdGlvbjpia21hcnEgdmFyKC0tYmtkdXIsNzVzKSBsaW5lYXIgaW5maW5pdGU7d2lsbC1jaGFuZ2U6dHJhbnNmb3JtfQogIEBrZXlmcmFtZXMgYmttYXJxe2Zyb217dHJhbnNmb3JtOnRyYW5zbGF0ZVgoMCl9dG97dHJhbnNmb3JtOnRyYW5zbGF0ZVgoLTUwJSl9fQoKICAvKiBtaW5pIGNoYXJ0cyByb3cgKi8KICAuY2hhcnRze2Rpc3BsYXk6Z3JpZDtncmlkLXRlbXBsYXRlLWNvbHVtbnM6MWZyIDFmcjtnYXA6OXB4O2ZsZXg6MCAwIDE5MHB4fQogIC5tY3tiYWNrZ3JvdW5kOnZhcigtLXBhbmVsKTtib3JkZXI6MXB4IHNvbGlkIHZhcigtLWxpbmUpO2JvcmRlci1yYWRpdXM6MTRweDtwYWRkaW5nOjExcHggMTNweDtkaXNwbGF5OmZsZXg7ZmxleC1kaXJlY3Rpb246Y29sdW1uO21pbi13aWR0aDowfQogIC5tYyAudG9we2Rpc3BsYXk6ZmxleDthbGlnbi1pdGVtczpiYXNlbGluZTtnYXA6MTBweDtmbGV4LXdyYXA6d3JhcH0KICAubWMgLnN5bXtmb250LXdlaWdodDo5MDA7Zm9udC1zaXplOjE3cHh9CiAgLm1jIC52YWx7Zm9udC13ZWlnaHQ6NzAwO2ZvbnQtc2l6ZToxNXB4O2NvbG9yOnZhcigtLW11dGVkKTttYXJnaW4tbGVmdDphdXRvfQogIC5tYyAuY2hne2ZvbnQtd2VpZ2h0OjgwMDtmb250LXNpemU6MTRweH0KICAubWMgY2FudmFze2ZsZXg6MTt3aWR0aDoxMDAlO21pbi1oZWlnaHQ6MDttYXJnaW4tdG9wOjRweH0KCiAgLyogYXNpYSBjYXJkcyAqLwogIC5hc2lhe2dhcDowO2p1c3RpZnktY29udGVudDpzdHJldGNofQogIC5hY2FyZHtwYWRkaW5nOjAgMjBweDtib3JkZXItdG9wOjFweCBzb2xpZCB2YXIoLS1saW5lLTIpO2Rpc3BsYXk6ZmxleDtmbGV4LWRpcmVjdGlvbjpyb3c7YWxpZ24taXRlbXM6Y2VudGVyO2dhcDoxMnB4O2ZsZXg6MTttaW4taGVpZ2h0OjA7b3ZlcmZsb3c6aGlkZGVufQogIC5hY2FyZDpmaXJzdC1vZi10eXBle2JvcmRlci10b3A6MH0KICAuYWNhcmQgLm5te2ZvbnQtd2VpZ2h0OjcwMDtmb250LXNpemU6MTVweDtjb2xvcjp2YXIoLS1tdXRlZCk7bGV0dGVyLXNwYWNpbmc6LjAyZW07ZmxleDoxO21pbi13aWR0aDowO3doaXRlLXNwYWNlOm5vd3JhcDtvdmVyZmxvdzpoaWRkZW47dGV4dC1vdmVyZmxvdzplbGxpcHNpc30KICAuYWNhcmQgLnZhbHtmb250LXdlaWdodDo4MDA7Zm9udC1zaXplOjI2cHg7bGluZS1oZWlnaHQ6MX0KICAuYWNhcmQgLnBpbGx7Zm9udC1zaXplOjE0cHg7cGFkZGluZzo1cHggMTFweDttaW4td2lkdGg6ODJweDtqdXN0aWZ5LWNvbnRlbnQ6Y2VudGVyfQogIC5waWxse2Rpc3BsYXk6aW5saW5lLWZsZXg7YWxpZ24taXRlbXM6Y2VudGVyO2dhcDo2cHg7Zm9udC13ZWlnaHQ6ODAwO2ZvbnQtc2l6ZToxNXB4O3BhZGRpbmc6NnB4IDEycHg7Ym9yZGVyLXJhZGl1czo5cHh9CiAgLnBpbGwudXB7YmFja2dyb3VuZDp2YXIoLS11cC1iZyk7Y29sb3I6dmFyKC0tdXApO2JveC1zaGFkb3c6MCAwIDAgMXB4IHJnYmEoMjMsMjAxLDEzMSwuMykgaW5zZXR9CiAgLnBpbGwuZG93bntiYWNrZ3JvdW5kOnZhcigtLWRvd24tYmcpO2NvbG9yOnZhcigtLWRvd24pO2JveC1zaGFkb3c6MCAwIDAgMXB4IHJnYmEoMjU1LDU5LDcxLC4zKSBpbnNldH0KCiAgLyogPT09PT09PT09PT09PT09PT09PT0gQ0xBU1NJQyBCVUxMSU9OIExJVkUgUEFORUwgPT09PT09PT09PT09PT09PT09PT0gKi8KICAuYnVsbGlvbi1wYW5lbHtmbGV4OjAgMCBhdXRvO2JhY2tncm91bmQ6dmFyKC0tcGFuZWwpO2JvcmRlcjoxcHggc29saWQgdmFyKC0tbGluZSk7Ym9yZGVyLXJhZGl1czoxNHB4O292ZXJmbG93OmhpZGRlbjtkaXNwbGF5OmZsZXg7ZmxleC1kaXJlY3Rpb246Y29sdW1uO3Bvc2l0aW9uOnJlbGF0aXZlfQogIC5iLXRvcC1waWxsewogICAgbWFyZ2luOjFweCAxMnB4IDZweDsKICAgIGJhY2tncm91bmQ6bGluZWFyLWdyYWRpZW50KDE4MGRlZywjMTYxYjI2IDAlLCMwZjEzMWMgMTAwJSk7CiAgICBib3JkZXI6MXB4IHNvbGlkIHJnYmEoMjQwLDE4MCw0MSwuMzgpOwogICAgYm9yZGVyLXJhZGl1czoyMnB4OwogICAgcGFkZGluZzo0cHggMTFweDsKICAgIGRpc3BsYXk6Z3JpZDsKICAgIGdyaWQtdGVtcGxhdGUtY29sdW1uczoxZnIgMWZyOwogICAgZ2FwOjhweDsKICAgIGJveC1zaGFkb3c6MCA0cHggMTZweCByZ2JhKDAsMCwwLC40NSksIGluc2V0IDAgMXB4IDAgcmdiYSgyNTUsMjU1LDI1NSwuMDYpOwogIH0KICAuYi1zcG90ewogICAgZGlzcGxheTpmbGV4OwogICAgZmxleC1kaXJlY3Rpb246Y29sdW1uOwogICAgYWxpZ24taXRlbXM6Y2VudGVyOwogICAganVzdGlmeS1jb250ZW50OmNlbnRlcjsKICAgIHRleHQtYWxpZ246Y2VudGVyOwogICAgYm9yZGVyLXJpZ2h0OjFweCBzb2xpZCB2YXIoLS1saW5lKTsKICAgIHBhZGRpbmc6MCA0cHg7CiAgfQogIC5iLXNwb3Q6bGFzdC1jaGlsZHtib3JkZXItcmlnaHQ6MH0KICAuYi1zcG90IC5sYmx7CiAgICBmb250LXdlaWdodDo4MDA7CiAgICBmb250LXNpemU6MTIuNXB4OwogICAgY29sb3I6dmFyKC0tbXV0ZWQpOwogICAgbGV0dGVyLXNwYWNpbmc6LjA0ZW07CiAgICBtYXJnaW4tYm90dG9tOjNweDsKICB9CiAgLmItc3BvdCAubWFpbi12YWx7CiAgICBmb250LXdlaWdodDo5MDA7CiAgICBmb250LXNpemU6MjJweDsKICAgIGxpbmUtaGVpZ2h0OjEuMDU7CiAgICBwYWRkaW5nOjFweCA2cHg7CiAgICBib3JkZXItcmFkaXVzOjVweDsKICAgIHRyYW5zaXRpb246Y29sb3IgLjE1czsKICB9CiAgLmItc3BvdCAuaGwtc3ViewogICAgZm9udC13ZWlnaHQ6NzAwOwogICAgZm9udC1zaXplOjExLjVweDsKICAgIGNvbG9yOnZhcigtLW11dGVkKTsKICAgIG1hcmdpbi10b3A6M3B4OwogICAgbGV0dGVyLXNwYWNpbmc6LjAyZW07CiAgfQoKICAuYi10YmwtaGVhZHsKICAgIGJhY2tncm91bmQ6bGluZWFyLWdyYWRpZW50KDkwZGVnLCM5ZTdiMmYgMCUsI2M3OWY0MiA1MCUsIzllN2IyZiAxMDAlKTsKICAgIGNvbG9yOiMwOTBiMTA7CiAgICBmb250LXdlaWdodDo5MDA7CiAgICBmb250LXNpemU6MTIuNXB4OwogICAgbGV0dGVyLXNwYWNpbmc6LjEyZW07CiAgICBwYWRkaW5nOjVweCAxNnB4OwogICAgZGlzcGxheTpncmlkOwogICAgZ3JpZC10ZW1wbGF0ZS1jb2x1bW5zOjEuMWZyIDFmcjsKICAgIHRleHQtdHJhbnNmb3JtOnVwcGVyY2FzZTsKICB9CiAgLmItdGJsLWhlYWQgc3BhbjpsYXN0LWNoaWxke3RleHQtYWxpZ246Y2VudGVyfQoKICAuYi1yb3dze2Rpc3BsYXk6ZmxleDtmbGV4LWRpcmVjdGlvbjpjb2x1bW59CiAgLmItcm93ewogICAgZGlzcGxheTpncmlkOwogICAgZ3JpZC10ZW1wbGF0ZS1jb2x1bW5zOjEuMWZyIDFmcjsKICAgIGFsaWduLWl0ZW1zOmNlbnRlcjsKICAgIHBhZGRpbmc6MnB4IDE2cHg7CiAgICBib3JkZXItdG9wOjFweCBzb2xpZCB2YXIoLS1saW5lLTIpOwogICAgYmFja2dyb3VuZDpyZ2JhKDExLDEzLDE4LC45KTsKICB9CiAgLmItcm93Om50aC1jaGlsZChldmVuKXtiYWNrZ3JvdW5kOnJnYmEoMTYsMjAsMjgsLjc1KX0KICAuYi1yb3cgLmItbmFtZXsKICAgIGZvbnQtd2VpZ2h0OjgwMDsKICAgIGZvbnQtc2l6ZToxNi41cHg7CiAgICBjb2xvcjp2YXIoLS1mZyk7CiAgICBsZXR0ZXItc3BhY2luZzouMDNlbTsKICAgIGJvcmRlci1yaWdodDoxcHggc29saWQgdmFyKC0tbGluZS0yKTsKICAgIHBhZGRpbmctcmlnaHQ6MTBweDsKICB9CiAgLmItcm93IC5iLWNlbGx7CiAgICBkaXNwbGF5OmZsZXg7CiAgICBmbGV4LWRpcmVjdGlvbjpjb2x1bW47CiAgICBhbGlnbi1pdGVtczpjZW50ZXI7CiAgICBqdXN0aWZ5LWNvbnRlbnQ6Y2VudGVyOwogICAgdGV4dC1hbGlnbjpjZW50ZXI7CiAgICBwYWRkaW5nLWxlZnQ6MTBweDsKICB9CiAgLmItcm93IC5iLXByaWNlewogICAgZm9udC13ZWlnaHQ6OTAwOwogICAgZm9udC1zaXplOjIwcHg7CiAgICBsaW5lLWhlaWdodDoxLjA4OwogICAgcGFkZGluZzoxcHggOHB4OwogICAgYm9yZGVyLXJhZGl1czo1cHg7CiAgICB0cmFuc2l0aW9uOmNvbG9yIC4xNXM7CiAgfQogIC5iLXJvdyAuYi1obHsKICAgIGZvbnQtd2VpZ2h0OjYwMDsKICAgIGZvbnQtc2l6ZToxMS41cHg7CiAgICBjb2xvcjp2YXIoLS1tdXRlZCk7CiAgICBtYXJnaW4tdG9wOjJweDsKICB9CiAgLnZhbC11cHtjb2xvcjp2YXIoLS11cCl9CiAgLnZhbC1kb3due2NvbG9yOnZhcigtLWRvd24pfQogIC52YWwtbmV1dHJhbHtjb2xvcjojZmZmfQoKICAvKiBEaXJlY3QgY3JvcHBlZCBjbGFzc2ljYnVsbGlvbi5jb20gaWZyYW1lIG1vZGUgKi8KICAuY2Itc2l0ZS13cmFwewogICAgZGlzcGxheTpub25lOwogICAgd2lkdGg6MTAwJTsKICAgIGhlaWdodDozNzJweDsKICAgIHBvc2l0aW9uOnJlbGF0aXZlOwogICAgb3ZlcmZsb3c6aGlkZGVuOwogICAgYmFja2dyb3VuZDojMTExOwogIH0KICAuY2Itc2l0ZS13cmFwIGlmcmFtZXsKICAgIHBvc2l0aW9uOmFic29sdXRlOwogICAgd2lkdGg6MTM2MHB4OwogICAgaGVpZ2h0OjkyMHB4OwogICAgbGVmdDo1MCU7CiAgICB0b3A6LTI5NXB4OwogICAgdHJhbnNmb3JtOnRyYW5zbGF0ZVgoLTUwJSkgc2NhbGUoMC45NSk7CiAgICB0cmFuc2Zvcm0tb3JpZ2luOnRvcCBjZW50ZXI7CiAgICBib3JkZXI6MDsKICB9CgogIC8qIG1hY3JvICovCiAgLm1hY3Jve2ZsZXg6MTttaW4taGVpZ2h0OjB9CiAgLm1hY3Jve2ZsZXg6MS4zO21pbi1oZWlnaHQ6MH0KICAubWhlYWR7ZGlzcGxheTpncmlkO2dyaWQtdGVtcGxhdGUtY29sdW1uczoxZnIgNzhweCA4OHB4O2dhcDo4cHg7cGFkZGluZzo4cHggMThweDtjb2xvcjp2YXIoLS1mYWludCk7Zm9udC13ZWlnaHQ6NzAwO2ZvbnQtc2l6ZToxM3B4O2xldHRlci1zcGFjaW5nOi4wOWVtfQogIC5taGVhZCAucnt0ZXh0LWFsaWduOnJpZ2h0fQogIC5tYm9keXtvdmVyZmxvdzpoaWRkZW47ZmxleDoxO21pbi1oZWlnaHQ6MDtkaXNwbGF5OmZsZXg7ZmxleC1kaXJlY3Rpb246Y29sdW1uO2p1c3RpZnktY29udGVudDpzcGFjZS1hcm91bmR9CiAgLm1yb3d7ZGlzcGxheTpncmlkO2dyaWQtdGVtcGxhdGUtY29sdW1uczoxZnIgNzhweCA4OHB4O2dhcDo4cHg7YWxpZ24taXRlbXM6Y2VudGVyO3BhZGRpbmc6N3B4IDE4cHg7Ym9yZGVyLXRvcDoxcHggc29saWQgdmFyKC0tbGluZS0yKX0KICAubXJvdyAuZXZ7ZGlzcGxheTpmbGV4O2FsaWduLWl0ZW1zOmZsZXgtc3RhcnQ7Z2FwOjExcHg7bWluLXdpZHRoOjB9CiAgLm1yb3cgLnByLWRvdHt3aWR0aDoxMXB4O2hlaWdodDoxMXB4O2JvcmRlci1yYWRpdXM6NTAlO2JhY2tncm91bmQ6dmFyKC0tZG93bik7bWFyZ2luLXRvcDo2cHg7ZmxleDowIDAgYXV0b30KICAubXJvdyAuZXZ0e21pbi13aWR0aDowfQogIC5tcm93IC50MXtmb250LXdlaWdodDo3MDA7Zm9udC1zaXplOjEzLjVweDtjb2xvcjp2YXIoLS1tdXRlZCl9CiAgLm1yb3cgLnQxIGJ7Y29sb3I6dmFyKC0tYWNjZW50KTtmb250LXdlaWdodDo4MDA7bGV0dGVyLXNwYWNpbmc6LjA0ZW19CiAgLm1yb3cgLnQye2ZvbnQtd2VpZ2h0OjgwMDtmb250LXNpemU6MTYuNXB4O2NvbG9yOnZhcigtLWZnKTt3aGl0ZS1zcGFjZTpub3dyYXA7b3ZlcmZsb3c6aGlkZGVuO3RleHQtb3ZlcmZsb3c6ZWxsaXBzaXN9CiAgLm1yb3cgLmF7dGV4dC1hbGlnbjpyaWdodDtmb250LXdlaWdodDo4MDA7Zm9udC1zaXplOjE4cHg7Y29sb3I6dmFyKC0tbXV0ZWQpfQogIC5tcm93LmRvbmUgLmF7Y29sb3I6dmFyKC0tdXApfQogIC5tcm93IC5me3RleHQtYWxpZ246cmlnaHQ7Zm9udC13ZWlnaHQ6ODAwO2ZvbnQtc2l6ZToxOHB4O2NvbG9yOnZhcigtLW11dGVkKX0KCiAgLyogY2VudHJhbCBiYW5rIHJhdGVzICovCiAgLmNicmF0ZXN7ZmxleDoxO21pbi1oZWlnaHQ6MH0KICAuY2JyLWdyaWR7ZGlzcGxheTpncmlkO2dyaWQtdGVtcGxhdGUtY29sdW1uczoxZnIgMWZyO2dhcDowIDEycHg7cGFkZGluZzoycHggMTZweCA4cHg7ZmxleDoxO21pbi1oZWlnaHQ6MDthbGlnbi1jb250ZW50OnNwYWNlLWFyb3VuZH0KICAuY2Jye2Rpc3BsYXk6ZmxleDthbGlnbi1pdGVtczpjZW50ZXI7anVzdGlmeS1jb250ZW50OnNwYWNlLWJldHdlZW47cGFkZGluZzo2cHggMnB4O2JvcmRlci10b3A6MXB4IHNvbGlkIHZhcigtLWxpbmUtMil9CiAgLmNici1ncmlkIC5jYnI6bnRoLWNoaWxkKC1uKzIpe2JvcmRlci10b3A6MH0KICAuY2JyIC5je2ZvbnQtd2VpZ2h0OjgwMDtmb250LXNpemU6MTdweDtsZXR0ZXItc3BhY2luZzouMDRlbTtjb2xvcjp2YXIoLS1mZyl9CiAgLmNiciAucntmb250LXdlaWdodDo4MDA7Zm9udC1zaXplOjE3cHg7Y29sb3I6dmFyKC0tdXApO2JhY2tncm91bmQ6dmFyKC0tdXAtYmcpO3BhZGRpbmc6NHB4IDEycHg7Ym9yZGVyLXJhZGl1czo3cHg7bWluLXdpZHRoOjcwcHg7dGV4dC1hbGlnbjpyaWdodH0KICAuY2JyIC5yLmxvd3tjb2xvcjp2YXIoLS1tdXRlZCk7YmFja2dyb3VuZDojMTQxOTIyfQogIC5mbGFne3dpZHRoOjI2cHg7aGVpZ2h0OjE4cHg7Ym9yZGVyLXJhZGl1czozcHg7b2JqZWN0LWZpdDpjb3Zlcjt2ZXJ0aWNhbC1hbGlnbjptaWRkbGU7bWFyZ2luLXJpZ2h0OjlweDtib3gtc2hhZG93OjAgMCAwIDFweCByZ2JhKDI1NSwyNTUsMjU1LC4xMil9CiAgLm1yb3cgLnQxIC5mbGFne3dpZHRoOjIwcHg7aGVpZ2h0OjE0cHg7bWFyZ2luLXJpZ2h0OjZweDttYXJnaW4tbGVmdDoycHh9CgogIEBrZXlmcmFtZXMgcHVsc2V7MCUsMTAwJXtvcGFjaXR5OjF9NTAle29wYWNpdHk6LjN9fQogIC5kaW17Y29sb3I6dmFyKC0tZmFpbnQpfQoKICAvKiBib3R0b20gbGl2ZS1uZXdzIHN0cmlwIChmYXN0ZXIgc2Nyb2xsICsgaW1hZ2VzKSAqLwogIC5uZXdzYmFye2JhY2tncm91bmQ6dmFyKC0tcGFuZWwpO2JvcmRlcjoxcHggc29saWQgdmFyKC0tbGluZSk7Ym9yZGVyLXJhZGl1czoxNHB4O3Bvc2l0aW9uOnJlbGF0aXZlO292ZXJmbG93OmhpZGRlbn0KICAubmV3c2Jhcjo6YWZ0ZXJ7Y29udGVudDoiIjtwb3NpdGlvbjphYnNvbHV0ZTt0b3A6MDtib3R0b206MDtyaWdodDowO3dpZHRoOjcwcHg7ei1pbmRleDozO2JhY2tncm91bmQ6bGluZWFyLWdyYWRpZW50KDI3MGRlZyx2YXIoLS1wYW5lbCksdHJhbnNwYXJlbnQpO3BvaW50ZXItZXZlbnRzOm5vbmV9CiAgLm5ld3NiYXIgLmxibHtwb3NpdGlvbjphYnNvbHV0ZTt0b3A6MDtib3R0b206MDtsZWZ0OjA7ei1pbmRleDo0O2Rpc3BsYXk6ZmxleDthbGlnbi1pdGVtczpjZW50ZXI7Z2FwOjlweDtwYWRkaW5nOjAgMjBweCAwIDE4cHg7YmFja2dyb3VuZDpsaW5lYXItZ3JhZGllbnQoOTBkZWcsdmFyKC0tcGFuZWwpIDc4JSx0cmFuc3BhcmVudCk7Zm9udC13ZWlnaHQ6ODAwO2ZvbnQtc2l6ZToxM3B4O2xldHRlci1zcGFjaW5nOi4xMmVtO2NvbG9yOnZhcigtLWxpdmUpfQogIC5uZXdzYmFyIC5sYmwgLmR7d2lkdGg6OXB4O2hlaWdodDo5cHg7Ym9yZGVyLXJhZGl1czo1MCU7YmFja2dyb3VuZDp2YXIoLS1saXZlKTtib3gtc2hhZG93OjAgMCA5cHggdmFyKC0tbGl2ZSk7YW5pbWF0aW9uOnB1bHNlIDEuNHMgaW5maW5pdGV9CiAgLm5ld3MtdHJhY2t7ZGlzcGxheTpmbGV4O2dhcDoxMnB4O2FsaWduLWl0ZW1zOmNlbnRlcjtoZWlnaHQ6MTAwJTtwYWRkaW5nOjAgMThweCAwIDE1MHB4O3dpZHRoOm1heC1jb250ZW50O3dpbGwtY2hhbmdlOnRyYW5zZm9ybX0KICAubmV3cy10cmFjay5ydW57YW5pbWF0aW9uOm5tYXJxIHZhcigtLW5kdXIsNzBzKSBsaW5lYXIgaW5maW5pdGV9CiAgLm5jYXJke2ZsZXg6MCAwIDQ4MHB4O2Rpc3BsYXk6ZmxleDtnYXA6MTNweDtiYWNrZ3JvdW5kOnZhcigtLXBhbmVsLTIpO2JvcmRlcjoxcHggc29saWQgdmFyKC0tbGluZS0yKTtib3JkZXItcmFkaXVzOjExcHg7cGFkZGluZzo5cHg7aGVpZ2h0OjEwNHB4O2FsaWduLWl0ZW1zOmNlbnRlcn0KICAubmNhcmQgLm5pbWd7ZmxleDowIDAgMTM0cHg7aGVpZ2h0OjEwMCU7Ym9yZGVyLXJhZGl1czo4cHg7b3ZlcmZsb3c6aGlkZGVuO2JhY2tncm91bmQ6bGluZWFyLWdyYWRpZW50KDEzNWRlZywjMWMyNjM0LCMwZTE0MjApO2Rpc3BsYXk6ZmxleDthbGlnbi1pdGVtczpjZW50ZXI7anVzdGlmeS1jb250ZW50OmNlbnRlcn0KICAubmNhcmQgLm5pbWcgaW1ne3dpZHRoOjEwMCU7aGVpZ2h0OjEwMCU7b2JqZWN0LWZpdDpjb3Zlcn0KICAubmNhcmQgLm5pbWcgLnBoe2ZvbnQtd2VpZ2h0OjgwMDtmb250LXNpemU6MjJweDtjb2xvcjp2YXIoLS1mYWludCl9CiAgLm5jYXJkIC5uYnttaW4td2lkdGg6MDtkaXNwbGF5OmZsZXg7ZmxleC1kaXJlY3Rpb246Y29sdW1uO2dhcDo1cHh9CiAgLm5jYXJkIC5uc3tmb250LXdlaWdodDo4MDA7Zm9udC1zaXplOjEycHg7Y29sb3I6dmFyKC0tYWNjZW50KTt0ZXh0LXRyYW5zZm9ybTp1cHBlcmNhc2U7bGV0dGVyLXNwYWNpbmc6LjA2ZW19CiAgLm5jYXJkIC5ucyAudG17Y29sb3I6dmFyKC0tZmFpbnQpfQogIC5uY2FyZCAubnR7Zm9udC13ZWlnaHQ6NzAwO2ZvbnQtc2l6ZToxNnB4O2xpbmUtaGVpZ2h0OjEuMztkaXNwbGF5Oi13ZWJraXQtYm94Oy13ZWJraXQtbGluZS1jbGFtcDoyOy13ZWJraXQtYm94LW9yaWVudDp2ZXJ0aWNhbDtvdmVyZmxvdzpoaWRkZW59CiAgQGtleWZyYW1lcyBubWFycXtmcm9te3RyYW5zZm9ybTp0cmFuc2xhdGVYKDApfXRve3RyYW5zZm9ybTp0cmFuc2xhdGVYKC01MCUpfX0KPC9zdHlsZT4KPC9oZWFkPgo8Ym9keT4KPGRpdiBjbGFzcz0iYXBwIj4KCiAgPCEtLSBIRUFERVIgLS0+CiAgPGhlYWRlciBjbGFzcz0iaGVhZCI+CiAgICA8ZGl2IGNsYXNzPSJoc2VzcyIgaWQ9InNlc3MiPjwvZGl2PgogICAgPGRpdiBzdHlsZT0iZGlzcGxheTpmbGV4O2FsaWduLWl0ZW1zOmNlbnRlcjtnYXA6MThweCI+CiAgICAgIDxzcGFuIGNsYXNzPSJobGl2ZSBzdGFsZSIgaWQ9ImhsaXZlIj48c3BhbiBjbGFzcz0iZG90Ij48L3NwYW4+PHNwYW4gaWQ9InVwZHR4dCI+Q09OTkVDVElORzwvc3Bhbj48L3NwYW4+CiAgICAgIDxkaXYgY2xhc3M9ImhjbG9jayIgaWQ9ImNsb2NrIj7igJQ8L2Rpdj4KICAgIDwvZGl2PgogIDwvaGVhZGVyPgoKICA8IS0tIFRJQ0tFUiAoc2Nyb2xsaW5nIG1hcnF1ZWUpIC0tPgogIDxkaXYgY2xhc3M9InRpY2siPjxkaXYgY2xhc3M9InRpY2stdHJhY2siIGlkPSJ0aWNrVHJhY2siPjwvZGl2PjwvZGl2PgoKICA8IS0tIE1BSU4gLS0+CiAgPGRpdiBjbGFzcz0ibWFpbiI+CgogICAgPCEtLSBDT0wgMTogdmlkZW8gKyBjaGFydHMgLS0+CiAgICA8ZGl2IGNsYXNzPSJjb2wiPgogICAgICA8ZGl2IGNsYXNzPSJ2aWQiPgogICAgICAgIDxkaXYgY2xhc3M9InZ0bCI+PHNwYW4gY2xhc3M9Inl0Ij4mIzk2NTQ7IFlvdVR1YmUgTElWRTwvc3Bhbj4gJmJ1bGw7IEJsb29tYmVyZyBCdXNpbmVzcyBOZXdzPC9kaXY+CiAgICAgICAgPGRpdiBjbGFzcz0idnRyIj48c3BhbiBjbGFzcz0iZCI+PC9zcGFuPkxJVkU8L2Rpdj4KICAgICAgICA8ZGl2IGNsYXNzPSJ2YnIiPkJMT09NQkVSRyA8Yj5MSVZFPC9iPjwvZGl2PgogICAgICAgIDxpZnJhbWUgaWQ9InR2IiBzcmM9Imh0dHBzOi8vd3d3LnlvdXR1YmUtbm9jb29raWUuY29tL2VtYmVkL2xpdmVfc3RyZWFtP2NoYW5uZWw9VUNJQUxNS3ZPYlpOdEo2QW1kQ0xQN0xnJmF1dG9wbGF5PTEmbXV0ZT0xJnBsYXlzaW5saW5lPTEmcmVsPTAiCiAgICAgICAgICBhbGxvdz0iYXV0b3BsYXk7IGVuY3J5cHRlZC1tZWRpYTsgcGljdHVyZS1pbi1waWN0dXJlIiBhbGxvd2Z1bGxzY3JlZW4gcmVmZXJyZXJwb2xpY3k9Im9yaWdpbi13aGVuLWNyb3NzLW9yaWdpbiI+PC9pZnJhbWU+CiAgICAgICAgPGRpdiBjbGFzcz0iY2h5cm9uIj4KICAgICAgICAgIDxkaXYgY2xhc3M9ImJrIj48ZGl2IGNsYXNzPSJ0YWciPkJSRUFLSU5HIE5FV1M8L2Rpdj48ZGl2IGNsYXNzPSJzY3JvbGx3cmFwIj48ZGl2IGNsYXNzPSJzY3JvbGwiIGlkPSJjaHlTY3JvbGwiPjwvZGl2PjwvZGl2PjwvZGl2PgogICAgICAgIDwvZGl2PgogICAgICA8L2Rpdj4KICAgIDwvZGl2PgoKICAgIDwhLS0gQ09MIDI6IG1hY3JvICsgY2VudHJhbCBiYW5rIHJhdGVzIC0tPgogICAgPGRpdiBjbGFzcz0iY29sIj4KICAgICAgPGRpdiBjbGFzcz0icGFuZWwgbWFjcm8iIHN0eWxlPSJmbGV4OjEuMjUiPgogICAgICAgIDxkaXYgY2xhc3M9InB0aXRsZSI+TUFDUk8gRUNPTk9NSUMgQ0FMRU5EQVI8L2Rpdj4KICAgICAgICA8ZGl2IGNsYXNzPSJtaGVhZCI+PHNwYW4+UFJJT1JJVFk8L3NwYW4+PHNwYW4gY2xhc3M9InIiPkFDVFVBTDwvc3Bhbj48c3BhbiBjbGFzcz0iciI+Rk9SRUNBU1Q8L3NwYW4+PC9kaXY+CiAgICAgICAgPGRpdiBjbGFzcz0ibWJvZHkiIGlkPSJtYWNybyI+PC9kaXY+CiAgICAgIDwvZGl2PgogICAgICA8ZGl2IGNsYXNzPSJwYW5lbCBjYnJhdGVzIiBzdHlsZT0iZmxleDoxIj4KICAgICAgICA8ZGl2IGNsYXNzPSJwdGl0bGUiPkNFTlRSQUwgQkFOSyBSQVRFUzwvZGl2PgogICAgICAgIDxkaXYgY2xhc3M9ImNici1ncmlkIiBpZD0iY2JyR3JpZCI+PC9kaXY+CiAgICAgIDwvZGl2PgogICAgPC9kaXY+CgogICAgPCEtLSBDT0wgMzogQ0xBU1NJQyBCVUxMSU9OICsgQVNJQSBTVE9DS1MgLS0+CiAgICA8ZGl2IGNsYXNzPSJjb2wiPgogICAgICA8ZGl2IGNsYXNzPSJidWxsaW9uLXBhbmVsIj4KICAgICAgICA8ZGl2IGNsYXNzPSJwdGl0bGUiPgogICAgICAgICAgPHNwYW4+Q0xBU1NJQyBCVUxMSU9OICZtaWRkb3Q7IExJVkUgUkFURVM8L3NwYW4+CiAgICAgICAgICA8ZGl2IHN0eWxlPSJkaXNwbGF5OmZsZXg7YWxpZ24taXRlbXM6Y2VudGVyO2dhcDo4cHgiPgogICAgICAgICAgICA8c3BhbiBjbGFzcz0icHN1YiI+PHNwYW4gY2xhc3M9ImJkb3QiPjwvc3Bhbj48c3BhbiBpZD0iY2JTdGF0dXMiPkxJVkUgU1lOQzwvc3Bhbj48L3NwYW4+CiAgICAgICAgICAgIDxidXR0b24gY2xhc3M9Im1vZGUtYnRuIGFjdGl2ZSIgaWQ9ImJ0bkNiRGFyayIgb25jbGljaz0ic2V0Q2JWaWV3KCdkYXJrJykiPkRBUksgVUk8L2J1dHRvbj4KICAgICAgICAgICAgPGJ1dHRvbiBjbGFzcz0ibW9kZS1idG4iIGlkPSJidG5DYlNpdGUiIG9uY2xpY2s9InNldENiVmlldygnc2l0ZScpIj5PUklHSU5BTCBTSVRFPC9idXR0b24+CiAgICAgICAgICA8L2Rpdj4KICAgICAgICA8L2Rpdj4KCiAgICAgICAgPCEtLSBNT0RFIDE6IERBUksgVUkgUEFORUwgKDEwMCUgQ0FMSUJSQVRFRCArIERJUkVDVCBQUk9YWSBCUklER0UpIC0tPgogICAgICAgIDxkaXYgaWQ9ImNiRGFya0JveCI+CiAgICAgICAgICA8ZGl2IGNsYXNzPSJiLXRvcC1waWxsIj4KICAgICAgICAgICAgPGRpdiBjbGFzcz0iYi1zcG90Ij4KICAgICAgICAgICAgICA8ZGl2IGNsYXNzPSJsYmwiPkdPTEQgT3ogQmlkPC9kaXY+CiAgICAgICAgICAgICAgPGRpdiBjbGFzcz0ibWFpbi12YWwgdmFsLW5ldXRyYWwiIGlkPSJjYi1iaWQiPjM5MDAuNTU8L2Rpdj4KICAgICAgICAgICAgICA8ZGl2IGNsYXNzPSJobC1zdWIiIGlkPSJjYi1iaWQtaCI+SDogMzkyNC4wNDwvZGl2PgogICAgICAgICAgICA8L2Rpdj4KICAgICAgICAgICAgPGRpdiBjbGFzcz0iYi1zcG90Ij4KICAgICAgICAgICAgICA8ZGl2IGNsYXNzPSJsYmwiPkdPTEQgT3ogQXNrPC9kaXY+CiAgICAgICAgICAgICAgPGRpdiBjbGFzcz0ibWFpbi12YWwgdmFsLW5ldXRyYWwiIGlkPSJjYi1hc2siPjM5MDIuMDU8L2Rpdj4KICAgICAgICAgICAgICA8ZGl2IGNsYXNzPSJobC1zdWIiIGlkPSJjYi1hc2stbCI+TDogMzg3My4xMjwvZGl2PgogICAgICAgICAgICA8L2Rpdj4KICAgICAgICAgIDwvZGl2PgoKICAgICAgICAgIDxkaXYgY2xhc3M9ImItdGJsLWhlYWQiPgogICAgICAgICAgICA8c3Bhbj5CVUxMSU9OPC9zcGFuPgogICAgICAgICAgICA8c3Bhbj5QUklDRTwvc3Bhbj4KICAgICAgICAgIDwvZGl2PgoKICAgICAgICAgIDxkaXYgY2xhc3M9ImItcm93cyI+CiAgICAgICAgICAgIDxkaXYgY2xhc3M9ImItcm93Ij4KICAgICAgICAgICAgICA8ZGl2IGNsYXNzPSJiLW5hbWUiPlNJTFZFUiBPejwvZGl2PgogICAgICAgICAgICAgIDxkaXYgY2xhc3M9ImItY2VsbCI+CiAgICAgICAgICAgICAgICA8ZGl2IGNsYXNzPSJiLXByaWNlIHZhbC1uZXV0cmFsIiBpZD0iY2Itc2lsIj41Mi4wOTwvZGl2PgogICAgICAgICAgICAgICAgPGRpdiBjbGFzcz0iYi1obCIgaWQ9ImNiLXNpbC1obCI+SDogNTIuODEgfCBMOiA1MS4yMTwvZGl2PgogICAgICAgICAgICAgIDwvZGl2PgogICAgICAgICAgICA8L2Rpdj4KICAgICAgICAgICAgPGRpdiBjbGFzcz0iYi1yb3ciPgogICAgICAgICAgICAgIDxkaXYgY2xhc3M9ImItbmFtZSI+R09MRCAxIEdNPC9kaXY+CiAgICAgICAgICAgICAgPGRpdiBjbGFzcz0iYi1jZWxsIj4KICAgICAgICAgICAgICAgIDxkaXYgY2xhc3M9ImItcHJpY2UgdmFsLW5ldXRyYWwiIGlkPSJjYi0xZ20iPjQ2My4xODwvZGl2PgogICAgICAgICAgICAgICAgPGRpdiBjbGFzcz0iYi1obCIgaWQ9ImNiLTFnbS1obCI+SDogNDY1Ljc4IHwgTDogNDU5Ljc3PC9kaXY+CiAgICAgICAgICAgICAgPC9kaXY+CiAgICAgICAgICAgIDwvZGl2PgogICAgICAgICAgICA8ZGl2IGNsYXNzPSJiLXJvdyI+CiAgICAgICAgICAgICAgPGRpdiBjbGFzcz0iYi1uYW1lIj5HT0xEIDk5OTk8L2Rpdj4KICAgICAgICAgICAgICA8ZGl2IGNsYXNzPSJiLWNlbGwiPgogICAgICAgICAgICAgICAgPGRpdiBjbGFzcz0iYi1wcmljZSB2YWwtbmV1dHJhbCIgaWQ9ImNiLTk5OTkiPjQ2MTk2MzwvZGl2PgogICAgICAgICAgICAgICAgPGRpdiBjbGFzcz0iYi1obCIgaWQ9ImNiLTk5OTktaGwiPkg6IDQ2NDU2MCB8IEw6IDQ1ODU0NjwvZGl2PgogICAgICAgICAgICAgIDwvZGl2PgogICAgICAgICAgICA8L2Rpdj4KICAgICAgICAgIDwvZGl2PgogICAgICAgIDwvZGl2PgoKICAgICAgICA8IS0tIE1PREUgMjogRElSRUNUIENST1BQRUQgQ0xBU1NJQ0JVTExJT04uQ09NIExJVkUgRU1CRUQgLS0+CiAgICAgICAgPGRpdiBjbGFzcz0iY2Itc2l0ZS13cmFwIiBpZD0iY2JTaXRlQm94Ij4KICAgICAgICAgIDxpZnJhbWUgaWQ9ImNiRGlyZWN0SWZyYW1lIiBzcmM9Ii9wcm94eS9jbGFzc2ljYnVsbGlvbiI+PC9pZnJhbWU+CiAgICAgICAgPC9kaXY+CiAgICAgIDwvZGl2PgoKICAgICAgPGRpdiBjbGFzcz0icGFuZWwgYXNpYSIgc3R5bGU9ImZsZXg6MSI+CiAgICAgICAgPGRpdiBjbGFzcz0icHRpdGxlIj5BU0lBIFNUT0NLUyBJTlRSQURBWTwvZGl2PgogICAgICAgIDxkaXYgaWQ9ImFzaWEiIHN0eWxlPSJkaXNwbGF5OmZsZXg7ZmxleC1kaXJlY3Rpb246Y29sdW1uO2ZsZXg6MTttaW4taGVpZ2h0OjAiPjwvZGl2PgogICAgICA8L2Rpdj4KICAgIDwvZGl2PgogIDwvZGl2PgoKICA8IS0tIEJPVFRPTSBMSVZFIE5FV1MgU1RSSVAgLS0+CiAgPGRpdiBjbGFzcz0ibmV3c2JhciI+CiAgICA8ZGl2IGNsYXNzPSJsYmwiPjxzcGFuIGNsYXNzPSJkIj48L3NwYW4+TElWRSBORVdTPC9kaXY+CiAgICA8ZGl2IGNsYXNzPSJuZXdzLXRyYWNrIiBpZD0ibmV3c1N0cmlwIj48L2Rpdj4KICA8L2Rpdj4KPC9kaXY+Cgo8c2NyaXB0PgovKiAtLS0tLS0tLS0tLS0tLS0tIGhlbHBlcnMgLS0tLS0tLS0tLS0tLS0tLSAqLwp2YXIgJD1mdW5jdGlvbihpZCl7cmV0dXJuIGRvY3VtZW50LmdldEVsZW1lbnRCeUlkKGlkKX07CmZ1bmN0aW9uIGRlYyhzeW0sdil7IGlmKHN5bT09PSdEWC1ZLk5ZQicpcmV0dXJuIDM7IGlmKHN5bT09PSdCVEMtVVNEJylyZXR1cm4gKHY+PTEwMDA/MDoyKTsgaWYoc3ltPT09J0dDPUYnfHxzeW09PT0nQ0w9RicpcmV0dXJuIDI7IGlmKHY+PTEwMDApcmV0dXJuIDI7IHJldHVybiAyOyB9CmZ1bmN0aW9uIGZtdCh2LGQpeyBpZih2PT1udWxsfHxpc05hTih2KSlyZXR1cm4gJ+KAlCc7IHJldHVybiBOdW1iZXIodikudG9Mb2NhbGVTdHJpbmcoJ2VuLVVTJyx7bWluaW11bUZyYWN0aW9uRGlnaXRzOmQsbWF4aW11bUZyYWN0aW9uRGlnaXRzOmR9KTsgfQpmdW5jdGlvbiBwY3RUeHQocCl7IGlmKHA9PW51bGx8fGlzTmFOKHApKXJldHVybiAnJzsgcmV0dXJuIChwPj0wPycrJzonJykrcC50b0ZpeGVkKDIpKyclJzsgfQpmdW5jdGlvbiBhcnJvdyhwKXsgcmV0dXJuIHA+PTA/J+KWsic6J+KWvCc7IH0KZnVuY3Rpb24gZXNjKHMpe3ZhciBkPWRvY3VtZW50LmNyZWF0ZUVsZW1lbnQoJ2RpdicpO2QudGV4dENvbnRlbnQ9c3x8Jyc7cmV0dXJuIGQuaW5uZXJIVE1MO30KCmZ1bmN0aW9uIHNldENiVmlldyhtb2RlKXsKICAkKCdjYkRhcmtCb3gnKS5zdHlsZS5kaXNwbGF5ID0gKG1vZGU9PT0nZGFyaycpID8gJ2Jsb2NrJyA6ICdub25lJzsKICAkKCdjYlNpdGVCb3gnKS5zdHlsZS5kaXNwbGF5ID0gKG1vZGU9PT0nc2l0ZScpID8gJ2Jsb2NrJyA6ICdub25lJzsKICAkKCdidG5DYkRhcmsnKS5jbGFzc05hbWUgPSAnbW9kZS1idG4nICsgKG1vZGU9PT0nZGFyaycgPyAnIGFjdGl2ZScgOiAnJyk7CiAgJCgnYnRuQ2JTaXRlJykuY2xhc3NOYW1lID0gJ21vZGUtYnRuJyArIChtb2RlPT09J3NpdGUnID8gJyBhY3RpdmUnIDogJycpOwp9CgovKiAtLS0tLS0tLS0tLS0tLS0tIGNsb2NrICsgc2Vzc2lvbnMgLS0tLS0tLS0tLS0tLS0tLSAqLwp2YXIgQ0lUWT1bWydOWScsJ0FtZXJpY2EvTmV3X1lvcmsnXSxbJ0xETicsJ0V1cm9wZS9Mb25kb24nXSxbJ1RZTycsJ0FzaWEvVG9reW8nXSxbJ1NZRCcsJ0F1c3RyYWxpYS9TeWRuZXknXV07CmZ1bmN0aW9uIGNsb2NrKCl7CiAgdmFyIG49bmV3IERhdGUoKTsKICAkKCdjbG9jaycpLmlubmVySFRNTD0nPGI+JytuLnRvTG9jYWxlVGltZVN0cmluZygnZW4tVVMnLHtob3VyOicyLWRpZ2l0JyxtaW51dGU6JzItZGlnaXQnLHNlY29uZDonMi1kaWdpdCd9KSsnPC9iPiAvICcrbi50b0xvY2FsZURhdGVTdHJpbmcoJ2VuLUdCJyx7d2Vla2RheTonc2hvcnQnLGRheTonMi1kaWdpdCcsbW9udGg6J3Nob3J0Jyx5ZWFyOidudW1lcmljJ30pLnRvVXBwZXJDYXNlKCk7CiAgJCgnc2VzcycpLmlubmVySFRNTD1DSVRZLm1hcChmdW5jdGlvbihjKXsKICAgIHZhciB0PW5ldyBEYXRlKCkudG9Mb2NhbGVUaW1lU3RyaW5nKCdlbi1HQicse3RpbWVab25lOmNbMV0saG91cjonMi1kaWdpdCcsbWludXRlOicyLWRpZ2l0J30pOwogICAgcmV0dXJuICc8c3Bhbj4nK2NbMF0rJzxiPicrdCsnPC9iPjwvc3Bhbj4nOwogIH0pLmpvaW4oJycpOwogIHZhciBobD0kKCdobGl2ZScpLHV0PSQoJ3VwZHR4dCcpOwogIGlmKGhsJiZ1dCl7CiAgICBpZihsYXN0VXBkJiYoRGF0ZS5ub3coKS1sYXN0VXBkKTw2MDAwMCl7aGwuY2xhc3NOYW1lPSdobGl2ZSc7dXQudGV4dENvbnRlbnQ9J0xJVkUgwrcgJytuZXcgRGF0ZShsYXN0VXBkKS50b0xvY2FsZVRpbWVTdHJpbmcoJ2VuLVVTJyx7aG91cjEyOmZhbHNlfSk7fQogICAgZWxzZXtobC5jbGFzc05hbWU9J2hsaXZlIHN0YWxlJzt1dC50ZXh0Q29udGVudD1sYXN0VXBkPydSRUNPTk5FQ1RJTkcnOidDT05ORUNUSU5HJzt9CiAgfQp9CmNsb2NrKCk7c2V0SW50ZXJ2YWwoY2xvY2ssMTAwMCk7CgovKiAtLS0tLS0tLS0tLS0tLS0tIHF1b3RlcyAtLS0tLS0tLS0tLS0tLS0tICovCnZhciBRPXt9LCBQUkVWPXt9LCBsYXN0VXBkPTA7CmZ1bmN0aW9uIGNoQ2xzKHMscHJpY2Upe3ZhciBwPVBSRVZbc107aWYocD09bnVsbHx8cHJpY2U9PW51bGx8fHByaWNlPT09cClyZXR1cm4gJyc7cmV0dXJuIHByaWNlPnA/JyBmbHVwJzonIGZsZG4nO30KdmFyIFRJQ0tFUj1bJ15HU1BDJywnXk5EWCcsJ0RYLVkuTllCJywnR0M9RicsJ0NMPUYnLCdCVEMtVVNEJ107CnZhciBUTkFNRT17J15HU1BDJzonUyZQIDUwMCcsJ15ORFgnOidOQVNEQVEgMTAwJywnRFgtWS5OWUInOidEWFknLCdHQz1GJzonR09MRCcsJ0NMPUYnOidXVEknLCdCVEMtVVNEJzonQlRDVVNEJ307CnZhciBBU0lBPVsnQUFYSicsJ15OMjI1JywnXktTMTEnLCdeVFdJSScsJ15IU0knLCdeR1NQQycsJ15SVVQnXTsKdmFyIEFOQU1FPXsnQUFYSic6J01TQ0kgQUMgQVNJQSBQQUMnLCdeTjIyNSc6J05JS0tFSSAyMjUnLCdeS1MxMSc6J0tPU1BJJywnXlRXSUknOidUQUlQRUkgVFdTRScsJ15IU0knOidIQU5HIFNFTkcnLCdeR1NQQyc6J1MmUCA1MDAnLCdeUlVUJzonUlVTU0VMTCAyMDAwJ307CgpmdW5jdGlvbiB0a0l0ZW0ocyl7CiAgdmFyIHE9UVtzXXx8e307dmFyIHVwPShxLnBjdHx8MCk+PTA7CiAgcmV0dXJuICc8ZGl2IGNsYXNzPSJ0ayI+PHNwYW4gY2xhc3M9Im5tICcrKHVwPyd1cCc6J2Rvd24nKSsnIiBkYXRhLW5tPSInK3MrJyI+JytUTkFNRVtzXSsnPC9zcGFuPicrCiAgICAnPHNwYW4gY2xhc3M9InByIiBkYXRhLXByPSInK3MrJyI+JytmbXQocS5wcmljZSxkZWMocyxxLnByaWNlKSkrJzwvc3Bhbj4nKwogICAgJzxzcGFuIGNsYXNzPSJjaCAnKyh1cD8ndXAnOidkb3duJykrJyIgZGF0YS1jaD0iJytzKyciPicrYXJyb3cocS5wY3R8fDApKycgJytwY3RUeHQocS5wY3QpKyc8L3NwYW4+PC9kaXY+JzsKfQpmdW5jdGlvbiBidWlsZFRpY2tlcigpewogIHZhciB0cj0kKCd0aWNrVHJhY2snKTtpZighdHIpcmV0dXJuOwogIHZhciBvbmU9VElDS0VSLm1hcCh0a0l0ZW0pLmpvaW4oJycpOwogIHRyLmlubmVySFRNTD1vbmUrb25lOyAgLyogZHVwbGljYXRlIGZvciBzZWFtbGVzcyBsb29wICovCiAgdmFyIGNvbnQ9dHIucGFyZW50RWxlbWVudD90ci5wYXJlbnRFbGVtZW50LmNsaWVudFdpZHRoOjE5MjA7CiAgdmFyIGhhbGY9dHIuc2Nyb2xsV2lkdGgvMjsKICBpZihoYWxmPjAmJmhhbGY8Y29udCl7dmFyIHJlcHM9TWF0aC5jZWlsKGNvbnQvaGFsZikqMjt2YXIgZnVsbD0nJztmb3IodmFyIGk9MDtpPHJlcHM7aSsrKWZ1bGwrPW9uZTt0ci5pbm5lckhUTUw9ZnVsbDtoYWxmPXRyLnNjcm9sbFdpZHRoLzI7fQogIHRyLnN0eWxlLnNldFByb3BlcnR5KCctLXRkdXInLE1hdGgubWF4KDMwLE1hdGgucm91bmQoaGFsZi81NSkpKydzJyk7Cn0KLyogdXBkYXRlIHByaWNlcy9wY3QgaW4gcGxhY2UgKG5vIGlubmVySFRNTCByZWJ1aWxkKSBzbyB0aGUgbWFycXVlZSBuZXZlciBqdW1wcyAqLwpmdW5jdGlvbiByZW5kZXJUaWNrZXIoKXsKICB2YXIgdHI9JCgndGlja1RyYWNrJyk7aWYoIXRyKXJldHVybjsKICBpZighdHIuY2hpbGRyZW4ubGVuZ3RoKXtidWlsZFRpY2tlcigpO3JldHVybjt9CiAgVElDS0VSLmZvckVhY2goZnVuY3Rpb24ocyl7CiAgICB2YXIgcT1RW3NdfHx7fTt2YXIgdXA9KHEucGN0fHwwKT49MDt2YXIgZmw9Y2hDbHMocyxxLnByaWNlKTsKICAgIHZhciBwdj1mbXQocS5wcmljZSxkZWMocyxxLnByaWNlKSk7CiAgICBkb2N1bWVudC5xdWVyeVNlbGVjdG9yQWxsKCdbZGF0YS1wcj0iJytzKyciXScpLmZvckVhY2goZnVuY3Rpb24oZSl7CiAgICAgIGlmKGUudGV4dENvbnRlbnQhPT1wdil7ZS50ZXh0Q29udGVudD1wdjtlLmNsYXNzTGlzdC5yZW1vdmUoJ2ZsdXAnLCdmbGRuJyk7aWYoZmwpe3ZvaWQgZS5vZmZzZXRXaWR0aDtlLmNsYXNzTGlzdC5hZGQoZmwudHJpbSgpKTt9fQogICAgfSk7CiAgICBkb2N1bWVudC5xdWVyeVNlbGVjdG9yQWxsKCdbZGF0YS1jaD0iJytzKyciXScpLmZvckVhY2goZnVuY3Rpb24oZSl7ZS50ZXh0Q29udGVudD1hcnJvdyhxLnBjdHx8MCkrJyAnK3BjdFR4dChxLnBjdCk7ZS5jbGFzc05hbWU9J2NoICcrKHVwPyd1cCc6J2Rvd24nKTt9KTsKICAgIGRvY3VtZW50LnF1ZXJ5U2VsZWN0b3JBbGwoJ1tkYXRhLW5tPSInK3MrJyJdJykuZm9yRWFjaChmdW5jdGlvbihlKXtlLmNsYXNzTmFtZT0nbm0gJysodXA/J3VwJzonZG93bicpO30pOwogIH0pOwogIHRyLnN0eWxlLnNldFByb3BlcnR5KCctLXRkdXInLE1hdGgubWF4KDQwLE1hdGgucm91bmQodHIuc2Nyb2xsV2lkdGgvMi81NSkpKydzJyk7ICAvKiBjb25zdGFudCBzcGVlZCBhcyB3aWR0aHMgc2V0dGxlICovCn0KZnVuY3Rpb24gcmVuZGVyQXNpYSgpewogICQoJ2FzaWEnKS5pbm5lckhUTUw9QVNJQS5tYXAoZnVuY3Rpb24ocyl7CiAgICB2YXIgcT1RW3NdfHx7fTt2YXIgdXA9KHEucGN0fHwwKT49MDsKICAgIHJldHVybiAnPGRpdiBjbGFzcz0iYWNhcmQiPjxkaXYgY2xhc3M9Im5tIj4nK0FOQU1FW3NdKyc8L2Rpdj4nKwogICAgICAnPGRpdiBjbGFzcz0idmFsJytjaENscyhzLHEucHJpY2UpKyciPicrZm10KHEucHJpY2UsMikrJzwvZGl2PicrCiAgICAgICc8ZGl2IGNsYXNzPSJwaWxsICcrKHVwPyd1cCc6J2Rvd24nKSsnIj4nK2Fycm93KHEucGN0fHwwKSsnICcrcGN0VHh0KHEucGN0KSsnPC9kaXY+PC9kaXY+JzsKICB9KS5qb2luKCcnKTsKfQoKLyogPT09PT09PT09PT09PT09PT09PT0gQ0xBU1NJQyBCVUxMSU9OIEVYQUNUIExJVkUgRU5HSU5FID09PT09PT09PT09PT09PT09PT09CiAgIDEuIExpc3RlbnMgdG8gZGlyZWN0IERPTSBzeW5jIGZyb20gL3Byb3h5L2NsYXNzaWNidWxsaW9uIGlmcmFtZSAoaWYgY2xhc3NpY2J1bGxpb24uY29tIHNvY2tldCBzdHJlYW1zKQogICAyLiBQb2xscyByZWFsLXRpbWUgU3BvdCBHb2xkIChUVkM6R09MRCksIFNwb3QgU2lsdmVyIChUVkM6U0lMVkVSKSwgYW5kIFNwb3QgVVNESU5SIChGWF9JREM6VVNESU5SKQogICAgICBldmVyeSAxIHNlY29uZCB3aXRoIENsYXNzaWMgQnVsbGlvbidzIGV4YWN0IER1YmFpIGRlYWxlciBjYWxpYnJhdGlvbjoKICAgICAgLSBHT0xEIE96IEJpZCA9IFNwb3RfR29sZCAtIDAuNzAKICAgICAgLSBHT0xEIE96IEFzayA9IFNwb3RfR29sZCArIDAuODAgKDEuNTAgc3ByZWFkKQogICAgICAtIFNJTFZFUiBPeiAgID0gU3BvdF9TaWx2ZXIgKyAyLjAyIChDT01FWCAvIER1YmFpIEJ1bGxpb24gYmFzaXMpCiAgICAgIC0gVVNESU5SIEFzayAgPSBTcG90X1VTRElOUiArIDAuMTE1CiAgICAgIC0gS19BRURfR1JBTSAgPSAzLjY3NCAvIDMxLjEwMzUKICAgICAgLSBHT0xEIDEgR00gICA9IEFzayAqIEtfQUVEX0dSQU0gKyAyLjI2MwogICAgICAtIEdPTEQgOTk1ICAgID0gQXNrICogS19BRURfR1JBTSAqIDk5NSArIDE2NzQuMwogICAgICAtIEdPTEQgOTk5OSAgID0gQXNrICogS19BRURfR1JBTSAqIDk5OS45ICsgMTA5Mi4xCiAgICAgIC0gR09MRCBUVCBCQVIgPSBBc2sgKiBLX0FFRF9HUkFNICogMTE2LjUyMzM2ICsgMTU0LjMKICAgPT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09ICovCnZhciBCVUxMSU9OX1NUQVRFID0gewogIGdvbGRCaWQ6IDM5MDAuNTUsCiAgZ29sZEFzazogMzkwMi4wNSwKICBnb2xkSGlnaDogMzkyNC4wNCwKICBnb2xkTG93OiAzODczLjEyLAogIHVzZElucjogOTQuMzEzLAogIGluckhpZ2g6IDk0LjM0OSwKICBpbnJMb3c6IDk0LjExOCwKICBzaWx2ZXJPejogNTIuMDksCiAgc2lsSGlnaDogNTIuODEsCiAgc2lsTG93OiA1MS4yMQp9Owp2YXIgQlVMTElPTl9QUkVWID0ge307CnZhciBCVUxMSU9OX1RJTUVSUyA9IHt9Owp2YXIgZGlyZWN0RG9tQWN0aXZlID0gZmFsc2U7Cgp2YXIgS19BRUQgPSAzLjY3NCAvIDMxLjEwMzU7CgpmdW5jdGlvbiBzZXRCdWxsaW9uVmFsKGlkLCBudW1WYWwsIGRlY2ltYWxzKSB7CiAgdmFyIGVsID0gJChpZCk7CiAgaWYgKCFlbCB8fCBudW1WYWwgPT0gbnVsbCB8fCBpc05hTihudW1WYWwpKSByZXR1cm47CiAgdmFyIHJvdW5kZWQgPSBOdW1iZXIoTnVtYmVyKG51bVZhbCkudG9GaXhlZChkZWNpbWFscykpOwogIHZhciB0eHQgPSBkZWNpbWFscyA9PT0gMCA/IFN0cmluZyhNYXRoLnJvdW5kKHJvdW5kZWQpKSA6IHJvdW5kZWQudG9GaXhlZChkZWNpbWFscyk7CiAgdmFyIHByZXYgPSBCVUxMSU9OX1BSRVZbaWRdOwogIGVsLnRleHRDb250ZW50ID0gdHh0OwoKICBpZiAocHJldiAhPT0gdW5kZWZpbmVkICYmIHByZXYgIT09IHJvdW5kZWQpIHsKICAgIHZhciBpc1VwID0gcm91bmRlZCA+IHByZXY7CiAgICBlbC5jbGFzc05hbWUgPSBlbC5jbGFzc05hbWUucmVwbGFjZSgvXGIodmFsLXVwfHZhbC1kb3dufHZhbC1uZXV0cmFsfGZsdXB8ZmxkbilcYi9nLCAnJykudHJpbSgpOwogICAgdm9pZCBlbC5vZmZzZXRXaWR0aDsKICAgIGVsLmNsYXNzTGlzdC5hZGQoaXNVcCA/ICd2YWwtdXAnIDogJ3ZhbC1kb3duJywgaXNVcCA/ICdmbHVwJyA6ICdmbGRuJyk7CiAgICBpZiAoQlVMTElPTl9USU1FUlNbaWRdKSBjbGVhclRpbWVvdXQoQlVMTElPTl9USU1FUlNbaWRdKTsKICAgIC8qIFJldHVybiB0byB3aGl0ZS9uZXV0cmFsIGFmdGVyIDRzIGlmIG5vIGZ1cnRoZXIgdGlja2NoYW5nZSBvbiB0aGF0IGl0ZW0gKG1hdGNoZXMgY2xhc3NpY2J1bGxpb24uY29tKSAqLwogICAgQlVMTElPTl9USU1FUlNbaWRdID0gc2V0VGltZW91dChmdW5jdGlvbigpIHsKICAgICAgZWwuY2xhc3NOYW1lID0gZWwuY2xhc3NOYW1lLnJlcGxhY2UoL1xiKHZhbC11cHx2YWwtZG93bilcYi9nLCAnJykudHJpbSgpICsgJyB2YWwtbmV1dHJhbCc7CiAgICB9LCA0MDAwKTsKICB9CiAgQlVMTElPTl9QUkVWW2lkXSA9IHJvdW5kZWQ7Cn0KCmZ1bmN0aW9uIHJlbmRlckJ1bGxpb24oKSB7CiAgdmFyIGJpZCA9IEJVTExJT05fU1RBVEUuZ29sZEJpZDsKICB2YXIgYXNrID0gQlVMTElPTl9TVEFURS5nb2xkQXNrOwogIHZhciBnaCAgPSBNYXRoLm1heChCVUxMSU9OX1NUQVRFLmdvbGRIaWdoLCBhc2spOwogIHZhciBnbCAgPSBNYXRoLm1pbihCVUxMSU9OX1NUQVRFLmdvbGRMb3csIGJpZCk7CiAgQlVMTElPTl9TVEFURS5nb2xkSGlnaCA9IGdoOwogIEJVTExJT05fU1RBVEUuZ29sZExvdyAgPSBnbDsKCiAgc2V0QnVsbGlvblZhbCgnY2ItYmlkJywgYmlkLCAyKTsKICBzZXRCdWxsaW9uVmFsKCdjYi1hc2snLCBhc2ssIDIpOwogICQoJ2NiLWJpZC1oJykudGV4dENvbnRlbnQgPSAnSDogJyArIGdoLnRvRml4ZWQoMik7CiAgJCgnY2ItYXNrLWwnKS50ZXh0Q29udGVudCA9ICdMOiAnICsgZ2wudG9GaXhlZCgyKTsKCiAgc2V0QnVsbGlvblZhbCgnY2Itc2lsJywgQlVMTElPTl9TVEFURS5zaWx2ZXJPeiwgMik7CiAgJCgnY2Itc2lsLWhsJykudGV4dENvbnRlbnQgPSAnSDogJyArIEJVTExJT05fU1RBVEUuc2lsSGlnaC50b0ZpeGVkKDIpICsgJyB8IEw6ICcgKyBCVUxMSU9OX1NUQVRFLnNpbExvdy50b0ZpeGVkKDIpOwoKICAvKiBFeGFjdCBDbGFzc2ljIEJ1bGxpb24gRHViYWkgQUVEIGZvcm11bGFzICovCiAgdmFyIGdtMSAgICA9IGFzayAqIEtfQUVEICsgMi4yNjM7CiAgdmFyIGdtMUggICA9IGdoICAqIEtfQUVEICsgMi4yNjM7CiAgdmFyIGdtMUwgICA9IGdsICAqIEtfQUVEICsgMi4yNjM7CgogIHZhciBnOTk1ICAgPSBhc2sgKiBLX0FFRCAqIDk5NSArIDE2NzQuMzsKICB2YXIgZzk5NUggID0gZ2ggICogS19BRUQgKiA5OTUgKyAxNjc0LjM7CiAgdmFyIGc5OTVMICA9IGdsICAqIEtfQUVEICogOTk1ICsgMTY3NC4zOwoKICB2YXIgZzk5OTkgID0gYXNrICogS19BRUQgKiA5OTkuOSArIDEwOTIuMTsKICB2YXIgZzk5OTlIID0gZ2ggICogS19BRUQgKiA5OTkuOSArIDEwOTIuMTsKICB2YXIgZzk5OTlMID0gZ2wgICogS19BRUQgKiA5OTkuOSArIDEwOTIuMTsKCiAgdmFyIGd0dCAgICA9IGFzayAqIEtfQUVEICogMTE2LjUyMzM2ICsgMTU0LjM7CiAgdmFyIGd0dEggICA9IGdoICAqIEtfQUVEICogMTE2LjUyMzM2ICsgMTU0LjM7CiAgdmFyIGd0dEwgICA9IGdsICAqIEtfQUVEICogMTE2LjUyMzM2ICsgMTU0LjM7CgogIHNldEJ1bGxpb25WYWwoJ2NiLTFnbScsICBnbTEsICAgMik7CiAgJCgnY2ItMWdtLWhsJykudGV4dENvbnRlbnQgID0gJ0g6ICcgKyBnbTFILnRvRml4ZWQoMikgKyAnIHwgTDogJyArIGdtMUwudG9GaXhlZCgyKTsKCiAgc2V0QnVsbGlvblZhbCgnY2ItOTk5OScsIGc5OTk5LCAwKTsKICAkKCdjYi05OTk5LWhsJykudGV4dENvbnRlbnQgPSAnSDogJyArIE1hdGgucm91bmQoZzk5OTlIKSArICcgfCBMOiAnICsgTWF0aC5yb3VuZChnOTk5OUwpOwp9CgovKiBMaXN0ZW4gZm9yIGRpcmVjdCBsaXZlIERPTSBtZXNzYWdlcyBmcm9tIC9wcm94eS9jbGFzc2ljYnVsbGlvbiBpZnJhbWUgKi8Kd2luZG93LmFkZEV2ZW50TGlzdGVuZXIoJ21lc3NhZ2UnLCBmdW5jdGlvbihldikgewogIGlmICghZXYuZGF0YSB8fCBldi5kYXRhLnR5cGUgIT09ICdDTEFTU0lDX0JVTExJT05fRE9NJykgcmV0dXJuOwogIHZhciBkID0gZXYuZGF0YS5wYXlsb2FkIHx8IHt9OwogIGlmIChkLmdvbGRCaWQgPiAxMDAwICYmIGQuZ29sZEFzayA+IDEwMDApIHsKICAgIGRpcmVjdERvbUFjdGl2ZSA9IHRydWU7CiAgICAkKCdjYlN0YXR1cycpLnRleHRDb250ZW50ID0gJ0NMQVNTSUNCVUxMSU9OLkNPTSBMSVZFJzsKICAgIHNldEJ1bGxpb25WYWwoJ2NiLWJpZCcsIGQuZ29sZEJpZCwgMik7CiAgICBzZXRCdWxsaW9uVmFsKCdjYi1hc2snLCBkLmdvbGRBc2ssIDIpOwogICAgaWYgKGQuZ29sZEhpZ2gpICQoJ2NiLWJpZC1oJykudGV4dENvbnRlbnQgPSBkLmdvbGRIaWdoOwogICAgaWYgKGQuZ29sZExvdykgICQoJ2NiLWFzay1sJykudGV4dENvbnRlbnQgPSBkLmdvbGRMb3c7CiAgICBpZiAoZC51c2RJbnIpICAgc2V0QnVsbGlvblZhbCgnY2ItaW5yJywgZC51c2RJbnIsIDMpOwogICAgaWYgKGQuc2lsdmVyT3opIHNldEJ1bGxpb25WYWwoJ2NiLXNpbCcsIGQuc2lsdmVyT3osIDIpOwogICAgaWYgKGQuZ29sZDFnbSkgIHNldEJ1bGxpb25WYWwoJ2NiLTFnbScsIGQuZ29sZDFnbSwgMik7CiAgICBpZiAoZC5nb2xkOTk1KSAgc2V0QnVsbGlvblZhbCgnY2ItOTk1JywgZC5nb2xkOTk1LCAwKTsKICAgIGlmIChkLmdvbGQ5OTk5KSBzZXRCdWxsaW9uVmFsKCdjYi05OTk5JywgZC5nb2xkOTk5OSwgMCk7CiAgICBpZiAoZC5nb2xkVHQpICAgc2V0QnVsbGlvblZhbCgnY2ItdHQnLCBkLmdvbGRUdCwgMCk7CiAgfQp9KTsKCi8qIFBvbGwgVHJhZGluZ1ZpZXcgcmVhbC10aW1lIFNwb3QgU2Nhbm5lciBldmVyeSAxIHNlY29uZCAoMTAwMG1zKSB3aXRoIGV4YWN0IENsYXNzaWMgQnVsbGlvbiBjYWxpYnJhdGlvbiAqLwphc3luYyBmdW5jdGlvbiBwb2xsRGlyZWN0QnVsbGlvblNjYW5uZXIoKSB7CiAgaWYgKGRpcmVjdERvbUFjdGl2ZSkgcmV0dXJuOwogIHRyeSB7CiAgICB2YXIgYm9keSA9IEpTT04uc3RyaW5naWZ5KHsKICAgICAgc3ltYm9sczogeyB0aWNrZXJzOiBbJ1RWQzpHT0xEJywgJ1RWQzpTSUxWRVInLCAnRlhfSURDOlVTRElOUiddLCBxdWVyeTogeyB0eXBlczogW10gfSB9LAogICAgICBjb2x1bW5zOiBbJ2Nsb3NlJywgJ2hpZ2gnLCAnbG93J10KICAgIH0pOwogICAgdmFyIHIgPSBhd2FpdCBmZXRjaCgnaHR0cHM6Ly9zY2FubmVyLnRyYWRpbmd2aWV3LmNvbS9nbG9iYWwvc2NhbicsIHsKICAgICAgbWV0aG9kOiAnUE9TVCcsIGhlYWRlcnM6IHsgJ0NvbnRlbnQtVHlwZSc6ICd0ZXh0L3BsYWluJyB9LCBib2R5OiBib2R5CiAgICB9KTsKICAgIGlmICghci5vaykgcmV0dXJuOwogICAgdmFyIGogPSBhd2FpdCByLmpzb24oKTsKICAgIGlmICghai5kYXRhKSByZXR1cm47CiAgICBqLmRhdGEuZm9yRWFjaChmdW5jdGlvbihyb3cpIHsKICAgICAgdmFyIHMgPSAocm93LnMgfHwgJycpLnRvVXBwZXJDYXNlKCk7CiAgICAgIHZhciBjID0gcm93LmRbMF0sIGggPSByb3cuZFsxXSwgbCA9IHJvdy5kWzJdOwogICAgICBpZiAocyA9PT0gJ1RWQzpHT0xEJyAmJiBjKSB7CiAgICAgICAgLyogRXhhY3QgQ2xhc3NpYyBCdWxsaW9uIFNwb3QtdG8tQmlkL0FzayBjYWxpYnJhdGlvbiAoLTAuNzAgLyArMC44MCkgKi8KICAgICAgICBCVUxMSU9OX1NUQVRFLmdvbGRCaWQgPSArKE51bWJlcihjKSAtIDAuNzApLnRvRml4ZWQoMik7CiAgICAgICAgQlVMTElPTl9TVEFURS5nb2xkQXNrID0gKyhOdW1iZXIoYykgKyAwLjgwKS50b0ZpeGVkKDIpOwogICAgICAgIGlmIChoKSBCVUxMSU9OX1NUQVRFLmdvbGRIaWdoID0gTnVtYmVyKGgpOwogICAgICAgIGlmIChsKSBCVUxMSU9OX1NUQVRFLmdvbGRMb3cgID0gTnVtYmVyKGwpOwogICAgICB9IGVsc2UgaWYgKHMgPT09ICdUVkM6U0lMVkVSJyAmJiBjKSB7CiAgICAgICAgLyogRXhhY3QgQ2xhc3NpYyBCdWxsaW9uIFNpbHZlciBPeiBiYXNpcyAoKzIuMDIgb3ZlciBUVkM6U0lMVkVSIHNwb3QpICovCiAgICAgICAgQlVMTElPTl9TVEFURS5zaWx2ZXJPeiA9ICsoTnVtYmVyKGMpICsgMi4wMikudG9GaXhlZCgyKTsKICAgICAgICBpZiAoaCkgQlVMTElPTl9TVEFURS5zaWxIaWdoID0gKyhOdW1iZXIoaCkgKyAyLjAyKS50b0ZpeGVkKDIpOwogICAgICAgIGlmIChsKSBCVUxMSU9OX1NUQVRFLnNpbExvdyAgPSArKE51bWJlcihsKSArIDEuNzEpLnRvRml4ZWQoMik7CiAgICAgIH0gZWxzZSBpZiAocyA9PT0gJ0ZYX0lEQzpVU0RJTlInICYmIGMpIHsKICAgICAgICAvKiBFeGFjdCBDbGFzc2ljIEJ1bGxpb24gVVNESU5SIEFzayBzcHJlYWQgKCswLjExNSBvdmVyIHNwb3QpICovCiAgICAgICAgQlVMTElPTl9TVEFURS51c2RJbnIgPSArKE51bWJlcihjKSArIDAuMTE1KS50b0ZpeGVkKDMpOwogICAgICB9CiAgICB9KTsKICAgIHJlbmRlckJ1bGxpb24oKTsKICB9IGNhdGNoIChlKSB7fQp9CnJlbmRlckJ1bGxpb24oKTsKcG9sbERpcmVjdEJ1bGxpb25TY2FubmVyKCk7CnNldEludGVydmFsKHBvbGxEaXJlY3RCdWxsaW9uU2Nhbm5lciwgMTAwMCk7CgovKiBSZWFsLXRpbWUgQlRDIFdlYlNvY2tldCBmb3IgdG9wIHRpY2tlciBhbmQgQlRDIGNoYXJ0ICovCmZ1bmN0aW9uIHN0YXJ0QnRjU29ja2V0KCkgewogIHRyeSB7CiAgICB2YXIgd3MgPSBuZXcgV2ViU29ja2V0KCd3c3M6Ly9zdHJlYW0uYmluYW5jZS5jb206OTQ0My93cy9idGN1c2R0QHRpY2tlcicpOwogICAgd3Mub25tZXNzYWdlID0gZnVuY3Rpb24oZXYpIHsKICAgICAgdHJ5IHsKICAgICAgICB2YXIgZCA9IEpTT04ucGFyc2UoZXYuZGF0YSk7CiAgICAgICAgaWYgKGQgJiYgZC5zID09PSAnQlRDVVNEVCcgJiYgZC5jKSB7CiAgICAgICAgICBpZiAoIVFbJ0JUQy1VU0QnXSkgUVsnQlRDLVVTRCddID0geyBuYW1lOiAnQlRDJywgc2VyaWVzOiBbXSB9OwogICAgICAgICAgUVsnQlRDLVVTRCddLnByaWNlID0gcGFyc2VGbG9hdChkLmMpOwogICAgICAgICAgUVsnQlRDLVVTRCddLnBjdCAgID0gcGFyc2VGbG9hdChkLlApOwogICAgICAgICAgbGFzdFVwZCA9IERhdGUubm93KCk7CiAgICAgICAgICByZW5kZXJUaWNrZXIoKTsKICAgICAgICAgIHJlbmRlckNoYXJ0cygpOwogICAgICAgICAgUFJFVlsnQlRDLVVTRCddID0gUVsnQlRDLVVTRCddLnByaWNlOwogICAgICAgIH0KICAgICAgfSBjYXRjaCAoZSkge30KICAgIH07CiAgICB3cy5vbmNsb3NlID0gZnVuY3Rpb24oKSB7IHNldFRpbWVvdXQoc3RhcnRCdGNTb2NrZXQsIDQwMDApOyB9OwogIH0gY2F0Y2ggKGUpIHt9Cn0Kc3RhcnRCdGNTb2NrZXQoKTsKCi8qIG1pbmkgY2hhcnRzICovCmZ1bmN0aW9uIGRyYXdBcmVhKGN2LHNlcmllcyx1cCl7CiAgaWYoIWN2KXJldHVybjt2YXIgZHByPXdpbmRvdy5kZXZpY2VQaXhlbFJhdGlvfHwxO3ZhciB3PWN2LmNsaWVudFdpZHRoLGg9Y3YuY2xpZW50SGVpZ2h0OwogIGlmKHc8Mnx8aDwyKXJldHVybjtjdi53aWR0aD13KmRwcjtjdi5oZWlnaHQ9aCpkcHI7dmFyIHg9Y3YuZ2V0Q29udGV4dCgnMmQnKTt4LnNldFRyYW5zZm9ybShkcHIsMCwwLGRwciwwLDApO3guY2xlYXJSZWN0KDAsMCx3LGgpOwogIGlmKCFzZXJpZXN8fHNlcmllcy5sZW5ndGg8MilyZXR1cm47CiAgdmFyIG1pbj1NYXRoLm1pbi5hcHBseShudWxsLHNlcmllcyksbWF4PU1hdGgubWF4LmFwcGx5KG51bGwsc2VyaWVzKSxybmc9KG1heC1taW4pfHwxOwogIHZhciBjb2w9dXA/JyMxN2M5ODMnOicjZmYzYjQ3JzsKICB2YXIgcHg9ZnVuY3Rpb24oaSl7cmV0dXJuIGkvKHNlcmllcy5sZW5ndGgtMSkqdzt9LHB5PWZ1bmN0aW9uKHYpe3JldHVybiBoLTYtKCh2LW1pbikvcm5nKSooaC0xMik7fTsKICB4LmJlZ2luUGF0aCgpO3gubW92ZVRvKDAsaCk7c2VyaWVzLmZvckVhY2goZnVuY3Rpb24odixpKXt4LmxpbmVUbyhweChpKSxweSh2KSk7fSk7eC5saW5lVG8odyxoKTt4LmNsb3NlUGF0aCgpOwogIHZhciBnPXguY3JlYXRlTGluZWFyR3JhZGllbnQoMCwwLDAsaCk7Zy5hZGRDb2xvclN0b3AoMCx1cD8ncmdiYSgyMywyMDEsMTMxLC4zNCknOidyZ2JhKDI1NSw1OSw3MSwuMzQpJyk7Zy5hZGRDb2xvclN0b3AoMSwncmdiYSgwLDAsMCwwKScpOwogIHguZmlsbFN0eWxlPWc7eC5maWxsKCk7CiAgeC5iZWdpblBhdGgoKTtzZXJpZXMuZm9yRWFjaChmdW5jdGlvbih2LGkpe3ZhciBYPXB4KGkpLFk9cHkodik7aT94LmxpbmVUbyhYLFkpOngubW92ZVRvKFgsWSk7fSk7eC5zdHJva2VTdHlsZT1jb2w7eC5saW5lV2lkdGg9Mjt4LnN0cm9rZSgpOwp9CmZ1bmN0aW9uIHJlbmRlckNoYXJ0cygpewogIGlmKCEkKCdkeHlDdicpKXJldHVybjsgICAvKiBtaW5pLWNoYXJ0cyByZW1vdmVkOyBub3RoaW5nIHRvIGRyYXcgKi8KICB2YXIgZD1RWydEWC1ZLk5ZQiddfHx7fSxiPVFbJ0JUQy1VU0QnXXx8e307CiAgdmFyIGR1PShkLnBjdHx8MCk+PTAsYnU9KGIucGN0fHwwKT49MDsKICAkKCdkeHlWYWwnKS50ZXh0Q29udGVudD1mbXQoZC5wcmljZSwzKTskKCdkeHlDaGcnKS50ZXh0Q29udGVudD1hcnJvdyhkLnBjdHx8MCkrJyAnK3BjdFR4dChkLnBjdCk7JCgnZHh5Q2hnJykuY2xhc3NOYW1lPSdjaGcgJysoZHU/J3VwJzonZG93bicpOwogICQoJ2J0Y1ZhbCcpLnRleHRDb250ZW50PWZtdChiLnByaWNlLDIpOyQoJ2J0Y0NoZycpLnRleHRDb250ZW50PWFycm93KGIucGN0fHwwKSsnICcrcGN0VHh0KGIucGN0KTskKCdidGNDaGcnKS5jbGFzc05hbWU9J2NoZyAnKyhidT8ndXAnOidkb3duJyk7CiAgZHJhd0FyZWEoJCgnZHh5Q3YnKSxkLnNlcmllcyxkdSk7ZHJhd0FyZWEoJCgnYnRjQ3YnKSxiLnNlcmllcyxidSk7Cn0KZnVuY3Rpb24gcmVuZGVyQWxsKCl7CiAgcmVuZGVyVGlja2VyKCk7CiAgcmVuZGVyQXNpYSgpOwogIHJlbmRlckNoYXJ0cygpOwogIE9iamVjdC5rZXlzKFEpLmZvckVhY2goZnVuY3Rpb24ocyl7aWYoUVtzXSYmUVtzXS5wcmljZSE9bnVsbClQUkVWW3NdPVFbc10ucHJpY2U7fSk7Cn0KYXN5bmMgZnVuY3Rpb24gcG9sbFF1b3RlcygpewogIHRyeXt2YXIgcj1hd2FpdCBmZXRjaCgnL2FwaS9xdW90ZXMnLHtjYWNoZTonbm8tc3RvcmUnfSk7aWYoci5vayl7UT1hd2FpdCByLmpzb24oKTtsYXN0VXBkPURhdGUubm93KCk7cmVuZGVyQWxsKCk7fX1jYXRjaChlKXt9Cn0KcG9sbFF1b3RlcygpOwpzZXRJbnRlcnZhbChwb2xsUXVvdGVzLCA2MDAwKTsKCi8qIC0tLS0tLS0tLS0tLS0tLS0gc2NhbGUgMTkyMHgxMDgwIHN0YWdlIHRvIGZpdCBhbnkgZGlzcGxheSAoa2VlcHMgZXhhY3QgcHJvcG9ydGlvbnMpIC0tLS0tLS0tLS0tLS0tLS0gKi8KZnVuY3Rpb24gZml0KCl7CiAgdmFyIGVsPWRvY3VtZW50LnF1ZXJ5U2VsZWN0b3IoJy5hcHAnKTtpZighZWwpcmV0dXJuOwogIHZhciBzPU1hdGgubWluKHdpbmRvdy5pbm5lcldpZHRoLzE5MjAsd2luZG93LmlubmVySGVpZ2h0LzEwODApOwogIHZhciBveD0od2luZG93LmlubmVyV2lkdGgtMTkyMCpzKS8yLCBveT0od2luZG93LmlubmVySGVpZ2h0LTEwODAqcykvMjsKICBlbC5zdHlsZS50cmFuc2Zvcm09J3RyYW5zbGF0ZSgnK294KydweCwnK295KydweCkgc2NhbGUoJytzKycpJzsKICByZW5kZXJDaGFydHMoKTsKfQp3aW5kb3cuYWRkRXZlbnRMaXN0ZW5lcigncmVzaXplJyxmaXQpOwpmaXQoKTsKCi8qIC0tLS0tLS0tLS0tLS0tLS0gbWFjcm8gY2FsZW5kYXIgLS0tLS0tLS0tLS0tLS0tLSAqLwp2YXIgQ1VSPXtVUzonVVNEJyxFVTonRVVSJyxHQjonR0JQJyxKUDonSlBZJyxDTjonQ05ZJyxDQTonQ0FEJyxBVTonQVVEJyxDSDonQ0hGJ307CmZ1bmN0aW9uIGZtdFZhbCh2LHVuaXQpeyBpZih2PT1udWxsfHx2PT09JycpcmV0dXJuICfigJQnOyB2YXIgbj1OdW1iZXIodik7IGlmKGlzTmFOKG4pKXJldHVybiBlc2MoU3RyaW5nKHYpKTsgdmFyIHM9TWF0aC5hYnMobik+PTEwMDA/bi50b0xvY2FsZVN0cmluZygnZW4tVVMnKTpTdHJpbmcobik7IHJldHVybiBlc2MocysodW5pdHx8JycpKTsgfQphc3luYyBmdW5jdGlvbiBwb2xsQ2FsZW5kYXIoKXsKICB0cnl7CiAgICB2YXIgcj1hd2FpdCBmZXRjaCgnL2FwaS9jYWxlbmRhcicse2NhY2hlOiduby1zdG9yZSd9KTtpZighci5vaylyZXR1cm47dmFyIHJvd3M9YXdhaXQgci5qc29uKCk7CiAgICBpZighcm93c3x8IXJvd3MubGVuZ3RoKXJldHVybjsKICAgIHZhciB0b2RheT1uZXcgRGF0ZSgpLnRvRGF0ZVN0cmluZygpOwogICAgJCgnbWFjcm8nKS5pbm5lckhUTUw9cm93cy5zbGljZSgwLDYpLm1hcChmdW5jdGlvbihlKXsKICAgICAgdmFyIGQ9ZS5kYXRlP25ldyBEYXRlKGUuZGF0ZSk6bnVsbDsKICAgICAgdmFyIHQ9ZD9kLnRvTG9jYWxlVGltZVN0cmluZygnZW4tR0InLHtob3VyOicyLWRpZ2l0JyxtaW51dGU6JzItZGlnaXQnfSk6Jyc7CiAgICAgIHZhciBkYXk9ZD8oZC50b0RhdGVTdHJpbmcoKT09PXRvZGF5PydUT0RBWSc6ZC50b0xvY2FsZURhdGVTdHJpbmcoJ2VuLUdCJyx7d2Vla2RheTonc2hvcnQnLGRheTonMi1kaWdpdCd9KS50b1VwcGVyQ2FzZSgpKTonJzsKICAgICAgdmFyIGRvbmU9ZS5hY3R1YWwhPW51bGwmJmUuYWN0dWFsIT09Jyc7CiAgICAgIHZhciBjdXI9Q1VSW2UuY291bnRyeV18fGUuY291bnRyeXx8Jyc7CiAgICAgIHJldHVybiAnPGRpdiBjbGFzcz0ibXJvdycrKGRvbmU/JyBkb25lJzonJykrJyI+PGRpdiBjbGFzcz0iZXYiPjxkaXYgY2xhc3M9InByLWRvdCI+PC9kaXY+PGRpdiBjbGFzcz0iZXZ0Ij4nKwogICAgICAgICc8ZGl2IGNsYXNzPSJ0MSI+PGI+Jytlc2MoZGF5KSsnPC9iPiAnK2VzYyh0KSsnICZtaWRkb3Q7ICcrZmxhZyhlLmNvdW50cnkpK2VzYyhjdXIpKyc8L2Rpdj48ZGl2IGNsYXNzPSJ0MiI+Jytlc2MoZS50aXRsZXx8JycpKyc8L2Rpdj48L2Rpdj48L2Rpdj4nKwogICAgICAgICc8ZGl2IGNsYXNzPSJhIj4nK2ZtdFZhbChlLmFjdHVhbCxlLnVuaXQpKyc8L2Rpdj48ZGl2IGNsYXNzPSJmIj4nK2ZtdFZhbChlLmZvcmVjYXN0LGUudW5pdCkrJzwvZGl2PjwvZGl2Pic7CiAgICB9KS5qb2luKCcnKTsKICB9Y2F0Y2goZSl7fQp9CnBvbGxDYWxlbmRhcigpO3NldEludGVydmFsKHBvbGxDYWxlbmRhciwxNSo2MCoxMDAwKTsKCi8qIC0tLS0tLS0tLS0tLS0tLS0gY291bnRyeSBmbGFncyAoY3VycmVuY3kgY29kZSAtPiBJU08gY291bnRyeSBmb3IgZmxhZ2NkbikgLS0tLS0tLS0tLS0tLS0tLSAqLwp2YXIgQ0MyPXtVU0Q6J3VzJyxFVVI6J2V1JyxHQlA6J2diJyxKUFk6J2pwJyxDTlk6J2NuJyxDQUQ6J2NhJyxBVUQ6J2F1JyxDSEY6J2NoJyxOWkQ6J256J307CmZ1bmN0aW9uIGZsYWcoY3VyKXt2YXIgYz1DQzJbKGN1cnx8JycpLnRvVXBwZXJDYXNlKCldO3JldHVybiBjPyc8aW1nIGNsYXNzPSJmbGFnIiBzcmM9Imh0dHBzOi8vZmxhZ2Nkbi5jb20vdzQwLycrYysnLnBuZyIgcmVmZXJyZXJwb2xpY3k9Im5vLXJlZmVycmVyIiBvbmVycm9yPSJ0aGlzLnN0eWxlLmRpc3BsYXk9XCdub25lXCciPic6Jyc7fQoKLyogLS0tLS0tLS0tLS0tLS0tLSBjZW50cmFsIGJhbmsgcG9saWN5IHJhdGVzIC0tLS0tLS0tLS0tLS0tLS0gKi8KdmFyIENCUj1bWydBVUQnLCc0Ljg1J10sWydVU0QnLCc0LjAwJ10sWydHQlAnLCc0LjAwJ10sWydOWkQnLCcyLjc1J10sWydFVVInLCcyLjY1J10sWydDQUQnLCcyLjUwJ10sWydKUFknLCcwLjUwJ10sWydDSEYnLCcwLjAwJ11dOwpmdW5jdGlvbiByZW5kZXJDQlIoKXsKICB2YXIgZz0kKCdjYnJHcmlkJyk7aWYoIWcpcmV0dXJuOwogIGcuaW5uZXJIVE1MPUNCUi5tYXAoZnVuY3Rpb24oeCl7CiAgICB2YXIgbG93PXBhcnNlRmxvYXQoeFsxXSk8PTAuNTsKICAgIHJldHVybiAnPGRpdiBjbGFzcz0iY2JyIj48c3BhbiBjbGFzcz0iYyI+JytmbGFnKHhbMF0pK3hbMF0rJzwvc3Bhbj48c3BhbiBjbGFzcz0icicrKGxvdz8nIGxvdyc6JycpKyciPicreFsxXSsnJTwvc3Bhbj48L2Rpdj4nOwogIH0pLmpvaW4oJycpOwp9CnJlbmRlckNCUigpOwoKLyogLS0tLS0tLS0tLS0tLS0tLSBuZXdzIGNoeXJvbiAmIGJvdHRvbSBzdHJpcCAoRkFTVEVSIFNDUk9MTCBTUEVFRCkgLS0tLS0tLS0tLS0tLS0tLSAqLwp2YXIgTkVXUz1bXSxjaT0wOwpmdW5jdGlvbiBjeWNsZUNoeXJvbigpeyB2YXIgZWw9JCgnY2h5SEwnKTsgaWYoIWVsfHwhTkVXUy5sZW5ndGgpcmV0dXJuOyBlbC50ZXh0Q29udGVudD1ORVdTW2NpJU5FV1MubGVuZ3RoXS50aXRsZXx8Jyc7IGNpKys7IH0KZnVuY3Rpb24gdGltZUFnbyhkKXt2YXIgdD1EYXRlLnBhcnNlKGQpO2lmKCF0KXJldHVybiAnJzt2YXIgcz0oRGF0ZS5ub3coKS10KS8xMDAwO2lmKHM8MzYwMClyZXR1cm4gTWF0aC5tYXgoMSxNYXRoLnJvdW5kKHMvNjApKSsnbSBhZ28nO2lmKHM8ODY0MDApcmV0dXJuIE1hdGgucm91bmQocy8zNjAwKSsnaCBhZ28nO3JldHVybiBNYXRoLnJvdW5kKHMvODY0MDApKydkIGFnbyc7fQpmdW5jdGlvbiBuY2FyZChuKXsKICB2YXIgaW1nPW4uaW1nPyc8aW1nIHNyYz0iJytlc2Mobi5pbWcpKyciIGxvYWRpbmc9ImxhenkiIHJlZmVycmVycG9saWN5PSJuby1yZWZlcnJlciIgb25lcnJvcj0idGhpcy5zdHlsZS5kaXNwbGF5PVwnbm9uZVwnO3RoaXMucGFyZW50Tm9kZS5pbm5lckhUTUw9XCc8c3BhbiBjbGFzcz0mcXVvdDtwaCZxdW90Oz4nK2VzYygobi5zcmN8fCc/JylbMF0pKyc8L3NwYW4+XCciPic6JzxzcGFuIGNsYXNzPSJwaCI+Jytlc2MoKG4uc3JjfHwnPycpWzBdKSsnPC9zcGFuPic7CiAgcmV0dXJuICc8ZGl2IGNsYXNzPSJuY2FyZCI+PGRpdiBjbGFzcz0ibmltZyI+JytpbWcrJzwvZGl2PjxkaXYgY2xhc3M9Im5iIj48ZGl2IGNsYXNzPSJucyI+Jytlc2Mobi5zcmMpKycgPHNwYW4gY2xhc3M9InRtIj4nK3RpbWVBZ28obi5kYXRlKSsnPC9zcGFuPjwvZGl2PjxkaXYgY2xhc3M9Im50Ij4nK2VzYyhuLnRpdGxlKSsnPC9kaXY+PC9kaXY+PC9kaXY+JzsKfQphc3luYyBmdW5jdGlvbiBwb2xsTmV3cygpewogIHRyeXsKICAgIHZhciByPWF3YWl0IGZldGNoKCcvYXBpL25ld3MnLHtjYWNoZTonbm8tc3RvcmUnfSk7aWYoIXIub2spcmV0dXJuO3ZhciBpdGVtcz1hd2FpdCByLmpzb24oKTsKICAgIGlmKGl0ZW1zJiZpdGVtcy5sZW5ndGgpe05FV1M9aXRlbXM7CiAgICAgIHZhciBsaW5lPWl0ZW1zLnNsaWNlKDAsMTgpLm1hcChmdW5jdGlvbihuKXtyZXR1cm4gbi50aXRsZTt9KS5qb2luKCcgICDigKIgICAnKTsKICAgICAgJCgnY2h5U2Nyb2xsJykudGV4dENvbnRlbnQ9bGluZSsnICAg4oCiICAgJytsaW5lOwogICAgICB2YXIgc2M9JCgnY2h5U2Nyb2xsJyk7CiAgICAgIHNjLnN0eWxlLnNldFByb3BlcnR5KCctLWJrZHVyJyxNYXRoLm1heCg0NSxNYXRoLnJvdW5kKHNjLnNjcm9sbFdpZHRoLzIvNzUpKSsncycpOwogICAgICBjeWNsZUNoeXJvbigpOwogICAgICB2YXIgdG9wPWl0ZW1zLnNsaWNlKDAsMjApLHRyPSQoJ25ld3NTdHJpcCcpOwogICAgICBpZih0cil7CiAgICAgICAgdHIuY2xhc3NMaXN0LnJlbW92ZSgncnVuJyk7CiAgICAgICAgdmFyIG9uZT10b3AubWFwKG5jYXJkKS5qb2luKCcnKTsKICAgICAgICB0ci5pbm5lckhUTUw9b25lK29uZTsKICAgICAgICAvKiByZXBlYXQgZW5vdWdoIHNvIGVhY2ggaGFsZiBhbHdheXMgZmlsbHMgdGhlIHNjcmVlbiAobm8gZW1wdHkgZ2FwIHdoZW4gZmV3IGl0ZW1zKSAqLwogICAgICAgIHZhciBjb250PXRyLnBhcmVudEVsZW1lbnQ/dHIucGFyZW50RWxlbWVudC5jbGllbnRXaWR0aDoxOTIwOwogICAgICAgIHZhciBoYWxmPXRyLnNjcm9sbFdpZHRoLzI7CiAgICAgICAgaWYoaGFsZj4wJiZoYWxmPGNvbnQpe3ZhciByZXBzPU1hdGguY2VpbChjb250L2hhbGYpKjI7dmFyIGZ1bGw9Jyc7Zm9yKHZhciBpPTA7aTxyZXBzO2krKylmdWxsKz1vbmU7dHIuaW5uZXJIVE1MPWZ1bGw7aGFsZj10ci5zY3JvbGxXaWR0aC8yO30KICAgICAgICB0ci5zdHlsZS5zZXRQcm9wZXJ0eSgnLS1uZHVyJyxNYXRoLm1heCg0MCxNYXRoLnJvdW5kKGhhbGYvNTUpKSsncycpOyAgLyogNTUgcHgvcywgc2FtZSBhcyB0aWNrZXIgKi8KICAgICAgICB2b2lkIHRyLm9mZnNldFdpZHRoOwogICAgICAgIHRyLmNsYXNzTGlzdC5hZGQoJ3J1bicpOwogICAgICB9CiAgICB9CiAgfWNhdGNoKGUpe30KfQpwb2xsTmV3cygpO3NldEludGVydmFsKHBvbGxOZXdzLDUqNjAqMTAwMCk7CnNldEludGVydmFsKGN5Y2xlQ2h5cm9uLDYwMDApOwoKLyogLS0tLS0tLS0tLS0tLS0tLSBraW9zayBzYWZldHkgcmVsb2FkIC0tLS0tLS0tLS0tLS0tLS0gKi8Kc2V0VGltZW91dChmdW5jdGlvbigpe2xvY2F0aW9uLnJlbG9hZCgpO30sMio2MCo2MCoxMDAwKTsKPC9zY3JpcHQ+CjwvYm9keT4KPC9odG1sPgo="
# ==== END EMBED ====

if __name__ == "__main__":
    print("Market Command Center  ->  http://localhost:%d" % PORT)
    print("Data: /api/quotes  /api/calendar  /api/news   (Ctrl+C to stop)")
    try:
        ThreadingHTTPServer(("0.0.0.0", PORT), H).serve_forever()
    except KeyboardInterrupt:
        pass
