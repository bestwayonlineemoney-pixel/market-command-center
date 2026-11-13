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
    return (past[-3:] + future)[:12]

def _ff_calendar():
    data = json.loads(http_get(FF_URL, timeout=12))
    rows = []
    for e in (data or []):
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
EMBED_HTML_B64 = "PCFkb2N0eXBlIGh0bWw+CjxodG1sIGxhbmc9ImVuIj4KPGhlYWQ+CjxtZXRhIGNoYXJzZXQ9InV0Zi04Ij4KPG1ldGEgbmFtZT0idmlld3BvcnQiIGNvbnRlbnQ9IndpZHRoPWRldmljZS13aWR0aCxpbml0aWFsLXNjYWxlPTEiPgo8dGl0bGU+TWFya2V0IENvbW1hbmQgQ2VudGVyPC90aXRsZT4KPGxpbmsgcmVsPSJwcmVjb25uZWN0IiBocmVmPSJodHRwczovL2ZvbnRzLmdvb2dsZWFwaXMuY29tIj48bGluayByZWw9InByZWNvbm5lY3QiIGhyZWY9Imh0dHBzOi8vZm9udHMuZ3N0YXRpYy5jb20iIGNyb3Nzb3JpZ2luPgo8bGluayByZWw9InN0eWxlc2hlZXQiIGhyZWY9Imh0dHBzOi8vZm9udHMuZ29vZ2xlYXBpcy5jb20vY3NzMj9mYW1pbHk9SW50ZXI6d2dodEA0MDA7NTAwOzYwMDs3MDA7ODAwOzkwMCZkaXNwbGF5PXN3YXAiPgo8c3R5bGU+CiAgOnJvb3R7CiAgICAtLWJnOiMwMDA7IC0tcGFuZWw6IzBiMGQxMjsgLS1wYW5lbC0yOiMwZTExMTg7IC0tY2FyZDojMGQxMDE3OyAtLWxpbmU6IzFhMWYyOTsgLS1saW5lLTI6IzE0MTkyMjsKICAgIC0tZmc6I2YyZjVmYTsgLS1tdXRlZDojOGE5NGE2OyAtLWZhaW50OiM1YTY0NzQ7CiAgICAtLXVwOiMxN2M5ODM7IC0tdXAtYmc6cmdiYSgyMywyMDEsMTMxLC4xNCk7IC0tZG93bjojZmYzYjQ3OyAtLWRvd24tYmc6cmdiYSgyNTUsNTksNzEsLjE0KTsKICAgIC0tZ29sZDojZjBiNDI5OyAtLW9yYW5nZTojZmY3YTFhOyAtLWxpdmU6I2ZmM2I0NzsgLS1hY2NlbnQ6I2YwYjQyOTsKICAgIC0tc2FuczoiSW50ZXIiLHN5c3RlbS11aSwtYXBwbGUtc3lzdGVtLCJTZWdvZSBVSSIsc2Fucy1zZXJpZjsKICB9CiAgKntib3gtc2l6aW5nOmJvcmRlci1ib3g7bWFyZ2luOjA7cGFkZGluZzowfQogIGh0bWwsYm9keXtoZWlnaHQ6MTAwJTt3aWR0aDoxMDAlfQogIGJvZHl7YmFja2dyb3VuZDojMDAwO2NvbG9yOnZhcigtLWZnKTtmb250LWZhbWlseTp2YXIoLS1zYW5zKTtvdmVyZmxvdzpoaWRkZW47LXdlYmtpdC1mb250LXNtb290aGluZzphbnRpYWxpYXNlZDtmb250LXZhcmlhbnQtbnVtZXJpYzp0YWJ1bGFyLW51bXN9CiAgLyogZml4ZWQgMTkyMHgxMDgwIHN0YWdlLCBzY2FsZWQgdG8gZml0IGFueSBkaXNwbGF5IC0+IGlkZW50aWNhbCBwcm9wb3J0aW9ucyBldmVyeXdoZXJlICovCiAgLmFwcHt3aWR0aDoxOTIwcHg7aGVpZ2h0OjEwODBweDt0cmFuc2Zvcm0tb3JpZ2luOnRvcCBsZWZ0O3Bvc2l0aW9uOmFic29sdXRlO3RvcDowO2xlZnQ6MDtkaXNwbGF5OmdyaWQ7Z3JpZC10ZW1wbGF0ZS1yb3dzOjYwcHggNzJweCAxZnIgMTU4cHg7Z2FwOjEycHg7cGFkZGluZzoxM3B4O2JhY2tncm91bmQ6dmFyKC0tYmcpfQoKICAvKiBoZWFkZXIgKi8KICAuaGVhZHtkaXNwbGF5OmZsZXg7YWxpZ24taXRlbXM6Y2VudGVyO2p1c3RpZnktY29udGVudDpzcGFjZS1iZXR3ZWVuO3BhZGRpbmc6MCA4cHh9CiAgLmh0aXRsZXtmb250LXdlaWdodDo4MDA7Zm9udC1zaXplOjI2cHg7bGV0dGVyLXNwYWNpbmc6LjA5ZW19CiAgLmhzZXNze2Rpc3BsYXk6ZmxleDtnYXA6MjhweDtmb250LXdlaWdodDo3MDA7Zm9udC1zaXplOjE3cHh9CiAgLmhzZXNzIHNwYW57Y29sb3I6dmFyKC0tbXV0ZWQpfSAuaHNlc3MgYntjb2xvcjp2YXIoLS1mZyk7Zm9udC13ZWlnaHQ6ODAwO21hcmdpbi1sZWZ0OjdweH0KICAuaGNsb2Nre2ZvbnQtd2VpZ2h0OjcwMDtmb250LXNpemU6MThweDtjb2xvcjp2YXIoLS1tdXRlZCl9IC5oY2xvY2sgYntjb2xvcjp2YXIoLS1mZyk7Zm9udC13ZWlnaHQ6ODAwfQogIC5obGl2ZXtkaXNwbGF5OmlubGluZS1mbGV4O2FsaWduLWl0ZW1zOmNlbnRlcjtnYXA6N3B4O2ZvbnQtd2VpZ2h0OjgwMDtmb250LXNpemU6MTNweDtsZXR0ZXItc3BhY2luZzouMDhlbTtjb2xvcjp2YXIoLS11cCl9CiAgLmhsaXZlIC5kb3R7d2lkdGg6OXB4O2hlaWdodDo5cHg7Ym9yZGVyLXJhZGl1czo1MCU7YmFja2dyb3VuZDp2YXIoLS11cCk7Ym94LXNoYWRvdzowIDAgOXB4IHZhcigtLXVwKTthbmltYXRpb246cHVsc2UgMS40cyBpbmZpbml0ZX0KICAuaGxpdmUuc3RhbGV7Y29sb3I6dmFyKC0tZmFpbnQpfSAuaGxpdmUuc3RhbGUgLmRvdHtiYWNrZ3JvdW5kOnZhcigtLWZhaW50KTtib3gtc2hhZG93Om5vbmU7YW5pbWF0aW9uOm5vbmV9CiAgQGtleWZyYW1lcyBmbHVwezAle2JhY2tncm91bmQ6cmdiYSgyMywyMDEsMTMxLC40NSl9MTAwJXtiYWNrZ3JvdW5kOnRyYW5zcGFyZW50fX0KICBAa2V5ZnJhbWVzIGZsZG57MCV7YmFja2dyb3VuZDpyZ2JhKDI1NSw1OSw3MSwuNDUpfTEwMCV7YmFja2dyb3VuZDp0cmFuc3BhcmVudH19CiAgLmZsdXB7YW5pbWF0aW9uOmZsdXAgLjhzIGVhc2Utb3V0O2JvcmRlci1yYWRpdXM6NXB4fSAuZmxkbnthbmltYXRpb246ZmxkbiAuOHMgZWFzZS1vdXQ7Ym9yZGVyLXJhZGl1czo1cHh9CgogIC8qIHRpY2tlciByb3cgKi8KICAudGlja3tkaXNwbGF5OmZsZXg7YWxpZ24taXRlbXM6Y2VudGVyO2JhY2tncm91bmQ6dmFyKC0tcGFuZWwpO2JvcmRlcjoxcHggc29saWQgdmFyKC0tbGluZSk7Ym9yZGVyLXJhZGl1czoxMnB4O292ZXJmbG93OmhpZGRlbn0KICAudGljay10cmFja3tkaXNwbGF5OmZsZXg7YWxpZ24taXRlbXM6Y2VudGVyO2hlaWdodDoxMDAlO3dpZHRoOm1heC1jb250ZW50O2FuaW1hdGlvbjp0bWFycSB2YXIoLS10ZHVyLDQ2cykgbGluZWFyIGluZmluaXRlO3dpbGwtY2hhbmdlOnRyYW5zZm9ybX0KICBAa2V5ZnJhbWVzIHRtYXJxe2Zyb217dHJhbnNmb3JtOnRyYW5zbGF0ZVgoMCl9dG97dHJhbnNmb3JtOnRyYW5zbGF0ZVgoLTUwJSl9fQogIC50a3tkaXNwbGF5OmZsZXg7YWxpZ24taXRlbXM6Y2VudGVyO2dhcDoxNHB4O3BhZGRpbmc6MCAzMHB4O2hlaWdodDoxMDAlO2JvcmRlci1yaWdodDoxcHggc29saWQgdmFyKC0tbGluZS0yKTt3aGl0ZS1zcGFjZTpub3dyYXB9CiAgLnRrIC5ubXtmb250LXdlaWdodDo4MDA7Zm9udC1zaXplOjIxcHh9CiAgLnRrIC5wcntmb250LXdlaWdodDo3MDA7Zm9udC1zaXplOjIxcHg7Y29sb3I6dmFyKC0tZmcpfQogIC50ayAuY2h7Zm9udC13ZWlnaHQ6ODAwO2ZvbnQtc2l6ZToxOHB4O2Rpc3BsYXk6ZmxleDthbGlnbi1pdGVtczpjZW50ZXI7Z2FwOjVweH0KICAudXB7Y29sb3I6dmFyKC0tdXApfSAuZG93bntjb2xvcjp2YXIoLS1kb3duKX0KCiAgLyogbWFpbiAzIGNvbHMgKi8KICAubWFpbntkaXNwbGF5OmdyaWQ7Z3JpZC10ZW1wbGF0ZS1jb2x1bW5zOjEuMzJmciAuOTJmciAxLjA2ZnI7Z2FwOjlweDttaW4taGVpZ2h0OjB9CiAgLmNvbHtkaXNwbGF5OmZsZXg7ZmxleC1kaXJlY3Rpb246Y29sdW1uO2dhcDo5cHg7bWluLWhlaWdodDowO21pbi13aWR0aDowfQogIC5wYW5lbHtiYWNrZ3JvdW5kOnZhcigtLXBhbmVsKTtib3JkZXI6MXB4IHNvbGlkIHZhcigtLWxpbmUpO2JvcmRlci1yYWRpdXM6MTRweDtkaXNwbGF5OmZsZXg7ZmxleC1kaXJlY3Rpb246Y29sdW1uO21pbi1oZWlnaHQ6MDtvdmVyZmxvdzpoaWRkZW59CiAgLnB0aXRsZXtmb250LXdlaWdodDo4MDA7Zm9udC1zaXplOjE1cHg7bGV0dGVyLXNwYWNpbmc6LjFlbTtwYWRkaW5nOjlweCAxNnB4IDZweDtjb2xvcjp2YXIoLS1mZyk7ZmxleDowIDAgYXV0bztkaXNwbGF5OmZsZXg7YWxpZ24taXRlbXM6Y2VudGVyO2p1c3RpZnktY29udGVudDpzcGFjZS1iZXR3ZWVufQogIC5wdGl0bGUgLnBzdWJ7Zm9udC13ZWlnaHQ6NzAwO2ZvbnQtc2l6ZToxMXB4O2xldHRlci1zcGFjaW5nOi4wOGVtO2NvbG9yOnZhcigtLWdvbGQpO2Rpc3BsYXk6aW5saW5lLWZsZXg7YWxpZ24taXRlbXM6Y2VudGVyO2dhcDo2cHh9CiAgLnB0aXRsZSAucHN1YiAuYmRvdHt3aWR0aDo3cHg7aGVpZ2h0OjdweDtib3JkZXItcmFkaXVzOjUwJTtiYWNrZ3JvdW5kOnZhcigtLXVwKTtib3gtc2hhZG93OjAgMCA4cHggdmFyKC0tdXApO2FuaW1hdGlvbjpwdWxzZSAxLjJzIGluZmluaXRlfQogIC5tb2RlLWJ0bntiYWNrZ3JvdW5kOiMxNTFhMjQ7Ym9yZGVyOjFweCBzb2xpZCAjMjYyZjQwO2NvbG9yOnZhcigtLW11dGVkKTtmb250OjcwMCAxMHB4LzEgdmFyKC0tc2Fucyk7cGFkZGluZzo0cHggOHB4O2JvcmRlci1yYWRpdXM6NXB4O2N1cnNvcjpwb2ludGVyO2xldHRlci1zcGFjaW5nOi4wNmVtfQogIC5tb2RlLWJ0bi5hY3RpdmV7Y29sb3I6dmFyKC0tZ29sZCk7Ym9yZGVyLWNvbG9yOnJnYmEoMjQwLDE4MCw0MSwuNSk7YmFja2dyb3VuZDpyZ2JhKDI0MCwxODAsNDEsLjEyKX0KCiAgLyogdmlkZW8gKi8KICAudmlke2ZsZXg6MTttaW4taGVpZ2h0OjA7cG9zaXRpb246cmVsYXRpdmU7YmFja2dyb3VuZDojMDAwO2JvcmRlcjoxcHggc29saWQgdmFyKC0tbGluZSk7Ym9yZGVyLXJhZGl1czoxNHB4O292ZXJmbG93OmhpZGRlbn0KICAudmlkIGlmcmFtZXtwb3NpdGlvbjphYnNvbHV0ZTtpbnNldDowO3dpZHRoOjEwMCU7aGVpZ2h0OjEwMCU7Ym9yZGVyOjB9CiAgLnZ0bHtwb3NpdGlvbjphYnNvbHV0ZTt0b3A6MTJweDtsZWZ0OjEycHg7ei1pbmRleDozO2JhY2tncm91bmQ6cmdiYSgxMCwxMCwxMiwuNzIpO2JhY2tkcm9wLWZpbHRlcjpibHVyKDRweCk7Ym9yZGVyLXJhZGl1czo4cHg7cGFkZGluZzo3cHggMTJweDtmb250LXdlaWdodDo3MDA7Zm9udC1zaXplOjEzcHg7ZGlzcGxheTpmbGV4O2FsaWduLWl0ZW1zOmNlbnRlcjtnYXA6OHB4fQogIC52dGwgLnl0e2NvbG9yOiNmZjJkMmQ7Zm9udC13ZWlnaHQ6OTAwfQogIC52dHJ7cG9zaXRpb246YWJzb2x1dGU7dG9wOjEycHg7cmlnaHQ6MTJweDt6LWluZGV4OjM7YmFja2dyb3VuZDojZTAxYjI0O2JvcmRlci1yYWRpdXM6N3B4O3BhZGRpbmc6NnB4IDExcHg7Zm9udC13ZWlnaHQ6OTAwO2ZvbnQtc2l6ZToxM3B4O2xldHRlci1zcGFjaW5nOi4wOGVtO2Rpc3BsYXk6ZmxleDthbGlnbi1pdGVtczpjZW50ZXI7Z2FwOjdweH0KICAudnRyIC5ke3dpZHRoOjhweDtoZWlnaHQ6OHB4O2JvcmRlci1yYWRpdXM6NTAlO2JhY2tncm91bmQ6I2ZmZjthbmltYXRpb246cHVsc2UgMS4zcyBpbmZpbml0ZX0KICAudmJye3Bvc2l0aW9uOmFic29sdXRlO2JvdHRvbTo3MnB4O3JpZ2h0OjEycHg7ei1pbmRleDozO2JhY2tncm91bmQ6cmdiYSgxMCwxMCwxMiwuNzgpO2JvcmRlci1yYWRpdXM6NnB4O3BhZGRpbmc6NXB4IDEwcHg7Zm9udC13ZWlnaHQ6ODAwO2ZvbnQtc2l6ZToxMnB4fQogIC52YnIgYntjb2xvcjp2YXIoLS1nb2xkKX0KICAuY2h5cm9ue3Bvc2l0aW9uOmFic29sdXRlO2xlZnQ6MDtyaWdodDowO2JvdHRvbTowO3otaW5kZXg6M30KICAuY2h5cm9uIC5obHtiYWNrZ3JvdW5kOmxpbmVhci1ncmFkaWVudCgwZGVnLHJnYmEoMCwwLDAsLjkyKSxyZ2JhKDAsMCwwLC41NSkpO3BhZGRpbmc6MTRweCAxNnB4IDEwcHg7Zm9udC13ZWlnaHQ6OTAwO2ZvbnQtc2l6ZTpjbGFtcCgxOHB4LDJ2dywzMHB4KTtsaW5lLWhlaWdodDoxLjA0O2xldHRlci1zcGFjaW5nOi4wMWVtO3RleHQtdHJhbnNmb3JtOnVwcGVyY2FzZTtjb2xvcjojZmZmO2Rpc3BsYXk6LXdlYmtpdC1ib3g7LXdlYmtpdC1saW5lLWNsYW1wOjI7LXdlYmtpdC1ib3gtb3JpZW50OnZlcnRpY2FsO292ZXJmbG93OmhpZGRlbjttaW4taGVpZ2h0Ojc2cHh9CiAgLmNoeXJvbiAuYmt7ZGlzcGxheTpmbGV4O2FsaWduLWl0ZW1zOmNlbnRlcjtiYWNrZ3JvdW5kOiMwYTBjMTA7Ym9yZGVyLXRvcDoycHggc29saWQgdmFyKC0tb3JhbmdlKX0KICAuY2h5cm9uIC5iayAudGFne2JhY2tncm91bmQ6dmFyKC0tb3JhbmdlKTtjb2xvcjojMTExO2ZvbnQtd2VpZ2h0OjkwMDtmb250LXNpemU6MTJweDtsZXR0ZXItc3BhY2luZzouMDZlbTtwYWRkaW5nOjhweCAxMnB4O3doaXRlLXNwYWNlOm5vd3JhcH0KICAuY2h5cm9uIC5iayAuc2Nyb2xsd3JhcHtmbGV4OjE7b3ZlcmZsb3c6aGlkZGVuO3Bvc2l0aW9uOnJlbGF0aXZlfQogIC5jaHlyb24gLmJrIC5zY3JvbGx7ZGlzcGxheTppbmxpbmUtZmxleDt3aGl0ZS1zcGFjZTpub3dyYXA7cGFkZGluZzo4cHggMDtmb250LXdlaWdodDo2MDA7Zm9udC1zaXplOjE0cHg7Y29sb3I6I2YyZDljNDthbmltYXRpb246YmttYXJxIHZhcigtLWJrZHVyLDc1cykgbGluZWFyIGluZmluaXRlO3dpbGwtY2hhbmdlOnRyYW5zZm9ybX0KICBAa2V5ZnJhbWVzIGJrbWFycXtmcm9te3RyYW5zZm9ybTp0cmFuc2xhdGVYKDApfXRve3RyYW5zZm9ybTp0cmFuc2xhdGVYKC01MCUpfX0KCiAgLyogbWluaSBjaGFydHMgcm93ICovCiAgLmNoYXJ0c3tkaXNwbGF5OmdyaWQ7Z3JpZC10ZW1wbGF0ZS1jb2x1bW5zOjFmciAxZnI7Z2FwOjlweDtmbGV4OjAgMCAxOTBweH0KICAubWN7YmFja2dyb3VuZDp2YXIoLS1wYW5lbCk7Ym9yZGVyOjFweCBzb2xpZCB2YXIoLS1saW5lKTtib3JkZXItcmFkaXVzOjE0cHg7cGFkZGluZzoxMXB4IDEzcHg7ZGlzcGxheTpmbGV4O2ZsZXgtZGlyZWN0aW9uOmNvbHVtbjttaW4td2lkdGg6MH0KICAubWMgLnRvcHtkaXNwbGF5OmZsZXg7YWxpZ24taXRlbXM6YmFzZWxpbmU7Z2FwOjEwcHg7ZmxleC13cmFwOndyYXB9CiAgLm1jIC5zeW17Zm9udC13ZWlnaHQ6OTAwO2ZvbnQtc2l6ZToxN3B4fQogIC5tYyAudmFse2ZvbnQtd2VpZ2h0OjcwMDtmb250LXNpemU6MTVweDtjb2xvcjp2YXIoLS1tdXRlZCk7bWFyZ2luLWxlZnQ6YXV0b30KICAubWMgLmNoZ3tmb250LXdlaWdodDo4MDA7Zm9udC1zaXplOjE0cHh9CiAgLm1jIGNhbnZhc3tmbGV4OjE7d2lkdGg6MTAwJTttaW4taGVpZ2h0OjA7bWFyZ2luLXRvcDo0cHh9CgogIC8qIGFzaWEgY2FyZHMgKi8KICAuYXNpYXtnYXA6MDtqdXN0aWZ5LWNvbnRlbnQ6c3RyZXRjaH0KICAuYWNhcmR7cGFkZGluZzo4cHggMjBweDtib3JkZXItdG9wOjFweCBzb2xpZCB2YXIoLS1saW5lLTIpO2Rpc3BsYXk6ZmxleDtmbGV4LWRpcmVjdGlvbjpjb2x1bW47anVzdGlmeS1jb250ZW50OmNlbnRlcjtnYXA6NXB4O2ZsZXg6MTttaW4taGVpZ2h0OjA7b3ZlcmZsb3c6aGlkZGVufQogIC5hY2FyZDpmaXJzdC1vZi10eXBle2JvcmRlci10b3A6MH0KICAuYWNhcmQgLm5te2ZvbnQtd2VpZ2h0OjcwMDtmb250LXNpemU6MTRweDtjb2xvcjp2YXIoLS1tdXRlZCk7bGV0dGVyLXNwYWNpbmc6LjAzZW19CiAgLmFjYXJkIC52YWx7Zm9udC13ZWlnaHQ6ODAwO2ZvbnQtc2l6ZTozMnB4O2xpbmUtaGVpZ2h0OjF9CiAgLmFjYXJkIC5waWxse2FsaWduLXNlbGY6ZmxleC1zdGFydDtmb250LXNpemU6MTRweDtwYWRkaW5nOjVweCAxMnB4fQogIC5waWxse2Rpc3BsYXk6aW5saW5lLWZsZXg7YWxpZ24taXRlbXM6Y2VudGVyO2dhcDo2cHg7Zm9udC13ZWlnaHQ6ODAwO2ZvbnQtc2l6ZToxNXB4O3BhZGRpbmc6NnB4IDEycHg7Ym9yZGVyLXJhZGl1czo5cHh9CiAgLnBpbGwudXB7YmFja2dyb3VuZDp2YXIoLS11cC1iZyk7Y29sb3I6dmFyKC0tdXApO2JveC1zaGFkb3c6MCAwIDAgMXB4IHJnYmEoMjMsMjAxLDEzMSwuMykgaW5zZXR9CiAgLnBpbGwuZG93bntiYWNrZ3JvdW5kOnZhcigtLWRvd24tYmcpO2NvbG9yOnZhcigtLWRvd24pO2JveC1zaGFkb3c6MCAwIDAgMXB4IHJnYmEoMjU1LDU5LDcxLC4zKSBpbnNldH0KCiAgLyogPT09PT09PT09PT09PT09PT09PT0gQ0xBU1NJQyBCVUxMSU9OIExJVkUgUEFORUwgPT09PT09PT09PT09PT09PT09PT0gKi8KICAuYnVsbGlvbi1wYW5lbHtmbGV4OjAgMCBhdXRvO2JhY2tncm91bmQ6dmFyKC0tcGFuZWwpO2JvcmRlcjoxcHggc29saWQgdmFyKC0tbGluZSk7Ym9yZGVyLXJhZGl1czoxNHB4O292ZXJmbG93OmhpZGRlbjtkaXNwbGF5OmZsZXg7ZmxleC1kaXJlY3Rpb246Y29sdW1uO3Bvc2l0aW9uOnJlbGF0aXZlfQogIC5iLXRvcC1waWxsewogICAgbWFyZ2luOjFweCAxMnB4IDZweDsKICAgIGJhY2tncm91bmQ6bGluZWFyLWdyYWRpZW50KDE4MGRlZywjMTYxYjI2IDAlLCMwZjEzMWMgMTAwJSk7CiAgICBib3JkZXI6MXB4IHNvbGlkIHJnYmEoMjQwLDE4MCw0MSwuMzgpOwogICAgYm9yZGVyLXJhZGl1czoyMnB4OwogICAgcGFkZGluZzo0cHggMTFweDsKICAgIGRpc3BsYXk6Z3JpZDsKICAgIGdyaWQtdGVtcGxhdGUtY29sdW1uczoxZnIgMWZyIDFmcjsKICAgIGdhcDo4cHg7CiAgICBib3gtc2hhZG93OjAgNHB4IDE2cHggcmdiYSgwLDAsMCwuNDUpLCBpbnNldCAwIDFweCAwIHJnYmEoMjU1LDI1NSwyNTUsLjA2KTsKICB9CiAgLmItc3BvdHsKICAgIGRpc3BsYXk6ZmxleDsKICAgIGZsZXgtZGlyZWN0aW9uOmNvbHVtbjsKICAgIGFsaWduLWl0ZW1zOmNlbnRlcjsKICAgIGp1c3RpZnktY29udGVudDpjZW50ZXI7CiAgICB0ZXh0LWFsaWduOmNlbnRlcjsKICAgIGJvcmRlci1yaWdodDoxcHggc29saWQgdmFyKC0tbGluZSk7CiAgICBwYWRkaW5nOjAgNHB4OwogIH0KICAuYi1zcG90Omxhc3QtY2hpbGR7Ym9yZGVyLXJpZ2h0OjB9CiAgLmItc3BvdCAubGJsewogICAgZm9udC13ZWlnaHQ6ODAwOwogICAgZm9udC1zaXplOjEyLjVweDsKICAgIGNvbG9yOnZhcigtLW11dGVkKTsKICAgIGxldHRlci1zcGFjaW5nOi4wNGVtOwogICAgbWFyZ2luLWJvdHRvbTozcHg7CiAgfQogIC5iLXNwb3QgLm1haW4tdmFsewogICAgZm9udC13ZWlnaHQ6OTAwOwogICAgZm9udC1zaXplOjIwcHg7CiAgICBsaW5lLWhlaWdodDoxLjA1OwogICAgcGFkZGluZzoxcHggNnB4OwogICAgYm9yZGVyLXJhZGl1czo1cHg7CiAgICB0cmFuc2l0aW9uOmNvbG9yIC4xNXM7CiAgfQogIC5iLXNwb3QgLmhsLXN1YnsKICAgIGZvbnQtd2VpZ2h0OjcwMDsKICAgIGZvbnQtc2l6ZToxMS41cHg7CiAgICBjb2xvcjp2YXIoLS1tdXRlZCk7CiAgICBtYXJnaW4tdG9wOjNweDsKICAgIGxldHRlci1zcGFjaW5nOi4wMmVtOwogIH0KCiAgLmItdGJsLWhlYWR7CiAgICBiYWNrZ3JvdW5kOmxpbmVhci1ncmFkaWVudCg5MGRlZywjOWU3YjJmIDAlLCNjNzlmNDIgNTAlLCM5ZTdiMmYgMTAwJSk7CiAgICBjb2xvcjojMDkwYjEwOwogICAgZm9udC13ZWlnaHQ6OTAwOwogICAgZm9udC1zaXplOjEyLjVweDsKICAgIGxldHRlci1zcGFjaW5nOi4xMmVtOwogICAgcGFkZGluZzo1cHggMTZweDsKICAgIGRpc3BsYXk6Z3JpZDsKICAgIGdyaWQtdGVtcGxhdGUtY29sdW1uczoxLjFmciAxZnI7CiAgICB0ZXh0LXRyYW5zZm9ybTp1cHBlcmNhc2U7CiAgfQogIC5iLXRibC1oZWFkIHNwYW46bGFzdC1jaGlsZHt0ZXh0LWFsaWduOmNlbnRlcn0KCiAgLmItcm93c3tkaXNwbGF5OmZsZXg7ZmxleC1kaXJlY3Rpb246Y29sdW1ufQogIC5iLXJvd3sKICAgIGRpc3BsYXk6Z3JpZDsKICAgIGdyaWQtdGVtcGxhdGUtY29sdW1uczoxLjFmciAxZnI7CiAgICBhbGlnbi1pdGVtczpjZW50ZXI7CiAgICBwYWRkaW5nOjJweCAxNnB4OwogICAgYm9yZGVyLXRvcDoxcHggc29saWQgdmFyKC0tbGluZS0yKTsKICAgIGJhY2tncm91bmQ6cmdiYSgxMSwxMywxOCwuOSk7CiAgfQogIC5iLXJvdzpudGgtY2hpbGQoZXZlbil7YmFja2dyb3VuZDpyZ2JhKDE2LDIwLDI4LC43NSl9CiAgLmItcm93IC5iLW5hbWV7CiAgICBmb250LXdlaWdodDo4MDA7CiAgICBmb250LXNpemU6MTUuNXB4OwogICAgY29sb3I6dmFyKC0tZmcpOwogICAgbGV0dGVyLXNwYWNpbmc6LjAzZW07CiAgICBib3JkZXItcmlnaHQ6MXB4IHNvbGlkIHZhcigtLWxpbmUtMik7CiAgICBwYWRkaW5nLXJpZ2h0OjEwcHg7CiAgfQogIC5iLXJvdyAuYi1jZWxsewogICAgZGlzcGxheTpmbGV4OwogICAgZmxleC1kaXJlY3Rpb246Y29sdW1uOwogICAgYWxpZ24taXRlbXM6Y2VudGVyOwogICAganVzdGlmeS1jb250ZW50OmNlbnRlcjsKICAgIHRleHQtYWxpZ246Y2VudGVyOwogICAgcGFkZGluZy1sZWZ0OjEwcHg7CiAgfQogIC5iLXJvdyAuYi1wcmljZXsKICAgIGZvbnQtd2VpZ2h0OjkwMDsKICAgIGZvbnQtc2l6ZToxOHB4OwogICAgbGluZS1oZWlnaHQ6MS4wODsKICAgIHBhZGRpbmc6MXB4IDhweDsKICAgIGJvcmRlci1yYWRpdXM6NXB4OwogICAgdHJhbnNpdGlvbjpjb2xvciAuMTVzOwogIH0KICAuYi1yb3cgLmItaGx7CiAgICBmb250LXdlaWdodDo2MDA7CiAgICBmb250LXNpemU6MTEuNXB4OwogICAgY29sb3I6dmFyKC0tbXV0ZWQpOwogICAgbWFyZ2luLXRvcDoycHg7CiAgfQogIC52YWwtdXB7Y29sb3I6dmFyKC0tdXApfQogIC52YWwtZG93bntjb2xvcjp2YXIoLS1kb3duKX0KICAudmFsLW5ldXRyYWx7Y29sb3I6I2ZmZn0KCiAgLyogRGlyZWN0IGNyb3BwZWQgY2xhc3NpY2J1bGxpb24uY29tIGlmcmFtZSBtb2RlICovCiAgLmNiLXNpdGUtd3JhcHsKICAgIGRpc3BsYXk6bm9uZTsKICAgIHdpZHRoOjEwMCU7CiAgICBoZWlnaHQ6MzcycHg7CiAgICBwb3NpdGlvbjpyZWxhdGl2ZTsKICAgIG92ZXJmbG93OmhpZGRlbjsKICAgIGJhY2tncm91bmQ6IzExMTsKICB9CiAgLmNiLXNpdGUtd3JhcCBpZnJhbWV7CiAgICBwb3NpdGlvbjphYnNvbHV0ZTsKICAgIHdpZHRoOjEzNjBweDsKICAgIGhlaWdodDo5MjBweDsKICAgIGxlZnQ6NTAlOwogICAgdG9wOi0yOTVweDsKICAgIHRyYW5zZm9ybTp0cmFuc2xhdGVYKC01MCUpIHNjYWxlKDAuOTUpOwogICAgdHJhbnNmb3JtLW9yaWdpbjp0b3AgY2VudGVyOwogICAgYm9yZGVyOjA7CiAgfQoKICAvKiBtYWNybyAqLwogIC5tYWNyb3tmbGV4OjE7bWluLWhlaWdodDowfQogIC5taGVhZHtkaXNwbGF5OmdyaWQ7Z3JpZC10ZW1wbGF0ZS1jb2x1bW5zOjFmciA3MHB4IDgwcHg7Z2FwOjhweDtwYWRkaW5nOjdweCAxNnB4O2NvbG9yOnZhcigtLWZhaW50KTtmb250LXdlaWdodDo3MDA7Zm9udC1zaXplOjEycHg7bGV0dGVyLXNwYWNpbmc6LjA5ZW19CiAgLm1oZWFkIC5ye3RleHQtYWxpZ246cmlnaHR9CiAgLm1ib2R5e292ZXJmbG93OmhpZGRlbjtmbGV4OjE7bWluLWhlaWdodDowfQogIC5tcm93e2Rpc3BsYXk6Z3JpZDtncmlkLXRlbXBsYXRlLWNvbHVtbnM6MWZyIDcwcHggODBweDtnYXA6OHB4O2FsaWduLWl0ZW1zOmNlbnRlcjtwYWRkaW5nOjRweCAxNnB4O2JvcmRlci10b3A6MXB4IHNvbGlkIHZhcigtLWxpbmUtMil9CiAgLm1yb3cgLmV2e2Rpc3BsYXk6ZmxleDthbGlnbi1pdGVtczpmbGV4LXN0YXJ0O2dhcDo5cHg7bWluLXdpZHRoOjB9CiAgLm1yb3cgLnByLWRvdHt3aWR0aDo5cHg7aGVpZ2h0OjlweDtib3JkZXItcmFkaXVzOjUwJTtiYWNrZ3JvdW5kOnZhcigtLWRvd24pO21hcmdpbi10b3A6NXB4O2ZsZXg6MCAwIGF1dG99CiAgLm1yb3cgLmV2dHttaW4td2lkdGg6MH0KICAubXJvdyAudDF7Zm9udC13ZWlnaHQ6NzAwO2ZvbnQtc2l6ZToxMnB4O2NvbG9yOnZhcigtLW11dGVkKX0KICAubXJvdyAudDEgYntjb2xvcjp2YXIoLS1hY2NlbnQpO2ZvbnQtd2VpZ2h0OjgwMDtsZXR0ZXItc3BhY2luZzouMDRlbX0KICAubXJvdyAudDJ7Zm9udC13ZWlnaHQ6ODAwO2ZvbnQtc2l6ZToxMy41cHg7Y29sb3I6dmFyKC0tZmcpO3doaXRlLXNwYWNlOm5vd3JhcDtvdmVyZmxvdzpoaWRkZW47dGV4dC1vdmVyZmxvdzplbGxpcHNpc30KICAubXJvdyAuYXt0ZXh0LWFsaWduOnJpZ2h0O2ZvbnQtd2VpZ2h0OjgwMDtmb250LXNpemU6MTVweDtjb2xvcjp2YXIoLS1tdXRlZCl9CiAgLm1yb3cuZG9uZSAuYXtjb2xvcjp2YXIoLS11cCl9CiAgLm1yb3cgLmZ7dGV4dC1hbGlnbjpyaWdodDtmb250LXdlaWdodDo4MDA7Zm9udC1zaXplOjE1cHg7Y29sb3I6dmFyKC0tbXV0ZWQpfQoKICAvKiBjZW50cmFsIGJhbmsgcmF0ZXMgKi8KICAuY2JyYXRlc3tmbGV4OjAgMCBhdXRvfQogIC5jYnItZ3JpZHtkaXNwbGF5OmdyaWQ7Z3JpZC10ZW1wbGF0ZS1jb2x1bW5zOjFmciAxZnI7Z2FwOjAgMTBweDtwYWRkaW5nOjJweCAxNHB4IDdweH0KICAuY2Jye2Rpc3BsYXk6ZmxleDthbGlnbi1pdGVtczpjZW50ZXI7anVzdGlmeS1jb250ZW50OnNwYWNlLWJldHdlZW47cGFkZGluZzo0cHggMnB4O2JvcmRlci10b3A6MXB4IHNvbGlkIHZhcigtLWxpbmUtMil9CiAgLmNici1ncmlkIC5jYnI6bnRoLWNoaWxkKC1uKzIpe2JvcmRlci10b3A6MH0KICAuY2JyIC5je2ZvbnQtd2VpZ2h0OjgwMDtmb250LXNpemU6MTRweDtsZXR0ZXItc3BhY2luZzouMDRlbTtjb2xvcjp2YXIoLS1mZyl9CiAgLmNiciAucntmb250LXdlaWdodDo4MDA7Zm9udC1zaXplOjE0cHg7Y29sb3I6dmFyKC0tdXApO2JhY2tncm91bmQ6dmFyKC0tdXAtYmcpO3BhZGRpbmc6M3B4IDEwcHg7Ym9yZGVyLXJhZGl1czo3cHg7bWluLXdpZHRoOjYwcHg7dGV4dC1hbGlnbjpyaWdodH0KICAuY2JyIC5yLmxvd3tjb2xvcjp2YXIoLS1tdXRlZCk7YmFja2dyb3VuZDojMTQxOTIyfQoKICBAa2V5ZnJhbWVzIHB1bHNlezAlLDEwMCV7b3BhY2l0eToxfTUwJXtvcGFjaXR5Oi4zfX0KICAuZGlte2NvbG9yOnZhcigtLWZhaW50KX0KCiAgLyogYm90dG9tIGxpdmUtbmV3cyBzdHJpcCAoZmFzdGVyIHNjcm9sbCArIGltYWdlcykgKi8KICAubmV3c2JhcntiYWNrZ3JvdW5kOnZhcigtLXBhbmVsKTtib3JkZXI6MXB4IHNvbGlkIHZhcigtLWxpbmUpO2JvcmRlci1yYWRpdXM6MTRweDtwb3NpdGlvbjpyZWxhdGl2ZTtvdmVyZmxvdzpoaWRkZW59CiAgLm5ld3NiYXI6OmFmdGVye2NvbnRlbnQ6IiI7cG9zaXRpb246YWJzb2x1dGU7dG9wOjA7Ym90dG9tOjA7cmlnaHQ6MDt3aWR0aDo3MHB4O3otaW5kZXg6MztiYWNrZ3JvdW5kOmxpbmVhci1ncmFkaWVudCgyNzBkZWcsdmFyKC0tcGFuZWwpLHRyYW5zcGFyZW50KTtwb2ludGVyLWV2ZW50czpub25lfQogIC5uZXdzYmFyIC5sYmx7cG9zaXRpb246YWJzb2x1dGU7dG9wOjA7Ym90dG9tOjA7bGVmdDowO3otaW5kZXg6NDtkaXNwbGF5OmZsZXg7YWxpZ24taXRlbXM6Y2VudGVyO2dhcDo5cHg7cGFkZGluZzowIDIwcHggMCAxOHB4O2JhY2tncm91bmQ6bGluZWFyLWdyYWRpZW50KDkwZGVnLHZhcigtLXBhbmVsKSA3OCUsdHJhbnNwYXJlbnQpO2ZvbnQtd2VpZ2h0OjgwMDtmb250LXNpemU6MTNweDtsZXR0ZXItc3BhY2luZzouMTJlbTtjb2xvcjp2YXIoLS1saXZlKX0KICAubmV3c2JhciAubGJsIC5ke3dpZHRoOjlweDtoZWlnaHQ6OXB4O2JvcmRlci1yYWRpdXM6NTAlO2JhY2tncm91bmQ6dmFyKC0tbGl2ZSk7Ym94LXNoYWRvdzowIDAgOXB4IHZhcigtLWxpdmUpO2FuaW1hdGlvbjpwdWxzZSAxLjRzIGluZmluaXRlfQogIC5uZXdzLXRyYWNre2Rpc3BsYXk6ZmxleDtnYXA6MTRweDthbGlnbi1pdGVtczpjZW50ZXI7aGVpZ2h0OjEwMCU7cGFkZGluZzowIDE4cHggMCAxNThweDt3aWR0aDptYXgtY29udGVudDt3aWxsLWNoYW5nZTp0cmFuc2Zvcm19CiAgLm5ld3MtdHJhY2sucnVue2FuaW1hdGlvbjpubWFycSB2YXIoLS1uZHVyLDcwcykgbGluZWFyIGluZmluaXRlfQogIC5uY2FyZHtmbGV4OjAgMCA0NjBweDtkaXNwbGF5OmZsZXg7Z2FwOjE0cHg7YmFja2dyb3VuZDp2YXIoLS1wYW5lbC0yKTtib3JkZXI6MXB4IHNvbGlkIHZhcigtLWxpbmUtMik7Ym9yZGVyLXJhZGl1czoxMXB4O3BhZGRpbmc6MTFweDtoZWlnaHQ6MTI4cHg7YWxpZ24taXRlbXM6Y2VudGVyfQogIC5uY2FyZCAubmltZ3tmbGV4OjAgMCAxNTBweDtoZWlnaHQ6MTAwJTtib3JkZXItcmFkaXVzOjhweDtvdmVyZmxvdzpoaWRkZW47YmFja2dyb3VuZDpsaW5lYXItZ3JhZGllbnQoMTM1ZGVnLCMxYzI2MzQsIzBlMTQyMCk7ZGlzcGxheTpmbGV4O2FsaWduLWl0ZW1zOmNlbnRlcjtqdXN0aWZ5LWNvbnRlbnQ6Y2VudGVyfQogIC5uY2FyZCAubmltZyBpbWd7d2lkdGg6MTAwJTtoZWlnaHQ6MTAwJTtvYmplY3QtZml0OmNvdmVyfQogIC5uY2FyZCAubmltZyAucGh7Zm9udC13ZWlnaHQ6ODAwO2ZvbnQtc2l6ZToyNHB4O2NvbG9yOnZhcigtLWZhaW50KX0KICAubmNhcmQgLm5ie21pbi13aWR0aDowO2Rpc3BsYXk6ZmxleDtmbGV4LWRpcmVjdGlvbjpjb2x1bW47Z2FwOjZweH0KICAubmNhcmQgLm5ze2ZvbnQtd2VpZ2h0OjgwMDtmb250LXNpemU6MTJweDtjb2xvcjp2YXIoLS1hY2NlbnQpO3RleHQtdHJhbnNmb3JtOnVwcGVyY2FzZTtsZXR0ZXItc3BhY2luZzouMDZlbX0KICAubmNhcmQgLm5zIC50bXtjb2xvcjp2YXIoLS1mYWludCl9CiAgLm5jYXJkIC5udHtmb250LXdlaWdodDo3MDA7Zm9udC1zaXplOjE2LjVweDtsaW5lLWhlaWdodDoxLjM7ZGlzcGxheTotd2Via2l0LWJveDstd2Via2l0LWxpbmUtY2xhbXA6Mzstd2Via2l0LWJveC1vcmllbnQ6dmVydGljYWw7b3ZlcmZsb3c6aGlkZGVufQogIEBrZXlmcmFtZXMgbm1hcnF7ZnJvbXt0cmFuc2Zvcm06dHJhbnNsYXRlWCgwKX10b3t0cmFuc2Zvcm06dHJhbnNsYXRlWCgtNTAlKX19Cjwvc3R5bGU+CjwvaGVhZD4KPGJvZHk+CjxkaXYgY2xhc3M9ImFwcCI+CgogIDwhLS0gSEVBREVSIC0tPgogIDxoZWFkZXIgY2xhc3M9ImhlYWQiPgogICAgPGRpdiBjbGFzcz0iaHRpdGxlIj5NQVJLRVQgQ09NTUFORCBDRU5URVI8L2Rpdj4KICAgIDxkaXYgY2xhc3M9ImhzZXNzIiBpZD0ic2VzcyI+PC9kaXY+CiAgICA8ZGl2IHN0eWxlPSJkaXNwbGF5OmZsZXg7YWxpZ24taXRlbXM6Y2VudGVyO2dhcDoxOHB4Ij4KICAgICAgPHNwYW4gY2xhc3M9ImhsaXZlIHN0YWxlIiBpZD0iaGxpdmUiPjxzcGFuIGNsYXNzPSJkb3QiPjwvc3Bhbj48c3BhbiBpZD0idXBkdHh0Ij5DT05ORUNUSU5HPC9zcGFuPjwvc3Bhbj4KICAgICAgPGRpdiBjbGFzcz0iaGNsb2NrIiBpZD0iY2xvY2siPuKAlDwvZGl2PgogICAgPC9kaXY+CiAgPC9oZWFkZXI+CgogIDwhLS0gVElDS0VSIChzY3JvbGxpbmcgbWFycXVlZSkgLS0+CiAgPGRpdiBjbGFzcz0idGljayI+PGRpdiBjbGFzcz0idGljay10cmFjayIgaWQ9InRpY2tUcmFjayI+PC9kaXY+PC9kaXY+CgogIDwhLS0gTUFJTiAtLT4KICA8ZGl2IGNsYXNzPSJtYWluIj4KCiAgICA8IS0tIENPTCAxOiB2aWRlbyArIGNoYXJ0cyAtLT4KICAgIDxkaXYgY2xhc3M9ImNvbCI+CiAgICAgIDxkaXYgY2xhc3M9InZpZCI+CiAgICAgICAgPGRpdiBjbGFzcz0idnRsIj48c3BhbiBjbGFzcz0ieXQiPiYjOTY1NDsgWW91VHViZSBMSVZFPC9zcGFuPiAmYnVsbDsgQmxvb21iZXJnIEJ1c2luZXNzIE5ld3M8L2Rpdj4KICAgICAgICA8ZGl2IGNsYXNzPSJ2dHIiPjxzcGFuIGNsYXNzPSJkIj48L3NwYW4+TElWRTwvZGl2PgogICAgICAgIDxkaXYgY2xhc3M9InZiciI+QkxPT01CRVJHIDxiPkxJVkU8L2I+PC9kaXY+CiAgICAgICAgPGlmcmFtZSBpZD0idHYiIHNyYz0iaHR0cHM6Ly93d3cueW91dHViZS1ub2Nvb2tpZS5jb20vZW1iZWQvbGl2ZV9zdHJlYW0/Y2hhbm5lbD1VQ0lBTE1Ldk9iWk50SjZBbWRDTFA3TGcmYXV0b3BsYXk9MSZtdXRlPTEmcGxheXNpbmxpbmU9MSZyZWw9MCIKICAgICAgICAgIGFsbG93PSJhdXRvcGxheTsgZW5jcnlwdGVkLW1lZGlhOyBwaWN0dXJlLWluLXBpY3R1cmUiIGFsbG93ZnVsbHNjcmVlbiByZWZlcnJlcnBvbGljeT0ib3JpZ2luLXdoZW4tY3Jvc3Mtb3JpZ2luIj48L2lmcmFtZT4KICAgICAgICA8ZGl2IGNsYXNzPSJjaHlyb24iPgogICAgICAgICAgPGRpdiBjbGFzcz0iaGwiIGlkPSJjaHlITCI+Jm5ic3A7PC9kaXY+CiAgICAgICAgICA8ZGl2IGNsYXNzPSJiayI+PGRpdiBjbGFzcz0idGFnIj5CUkVBS0lORyBORVdTPC9kaXY+PGRpdiBjbGFzcz0ic2Nyb2xsd3JhcCI+PGRpdiBjbGFzcz0ic2Nyb2xsIiBpZD0iY2h5U2Nyb2xsIj48L2Rpdj48L2Rpdj48L2Rpdj4KICAgICAgICA8L2Rpdj4KICAgICAgPC9kaXY+CiAgICAgIDxkaXYgY2xhc3M9ImNoYXJ0cyI+CiAgICAgICAgPGRpdiBjbGFzcz0ibWMiPjxkaXYgY2xhc3M9InRvcCI+PHNwYW4gY2xhc3M9InN5bSI+RFhZPC9zcGFuPjxzcGFuIGNsYXNzPSJ2YWwiIGlkPSJkeHlWYWwiPuKAlDwvc3Bhbj48c3BhbiBjbGFzcz0iY2hnIiBpZD0iZHh5Q2hnIj48L3NwYW4+PC9kaXY+PGNhbnZhcyBpZD0iZHh5Q3YiPjwvY2FudmFzPjwvZGl2PgogICAgICAgIDxkaXYgY2xhc3M9Im1jIj48ZGl2IGNsYXNzPSJ0b3AiPjxzcGFuIGNsYXNzPSJzeW0iPkJUQ1VTRDwvc3Bhbj48c3BhbiBjbGFzcz0idmFsIiBpZD0iYnRjVmFsIj7igJQ8L3NwYW4+PHNwYW4gY2xhc3M9ImNoZyIgaWQ9ImJ0Y0NoZyI+PC9zcGFuPjwvZGl2PjxjYW52YXMgaWQ9ImJ0Y0N2Ij48L2NhbnZhcz48L2Rpdj4KICAgICAgPC9kaXY+CiAgICA8L2Rpdj4KCiAgICA8IS0tIENPTCAyOiBhc2lhIC0tPgogICAgPGRpdiBjbGFzcz0iY29sIj4KICAgICAgPGRpdiBjbGFzcz0icGFuZWwgYXNpYSIgc3R5bGU9ImZsZXg6MSI+CiAgICAgICAgPGRpdiBjbGFzcz0icHRpdGxlIj5BU0lBIFNUT0NLUyBJTlRSQURBWTwvZGl2PgogICAgICAgIDxkaXYgaWQ9ImFzaWEiIHN0eWxlPSJkaXNwbGF5OmZsZXg7ZmxleC1kaXJlY3Rpb246Y29sdW1uO2ZsZXg6MTttaW4taGVpZ2h0OjAiPjwvZGl2PgogICAgICA8L2Rpdj4KICAgIDwvZGl2PgoKICAgIDwhLS0gQ09MIDM6IENMQVNTSUMgQlVMTElPTiBMSVZFIFJBVEVTICsgTUFDUk8gLS0+CiAgICA8ZGl2IGNsYXNzPSJjb2wiPgogICAgICA8ZGl2IGNsYXNzPSJidWxsaW9uLXBhbmVsIj4KICAgICAgICA8ZGl2IGNsYXNzPSJwdGl0bGUiPgogICAgICAgICAgPHNwYW4+Q0xBU1NJQyBCVUxMSU9OICZtaWRkb3Q7IExJVkUgUkFURVM8L3NwYW4+CiAgICAgICAgICA8ZGl2IHN0eWxlPSJkaXNwbGF5OmZsZXg7YWxpZ24taXRlbXM6Y2VudGVyO2dhcDo4cHgiPgogICAgICAgICAgICA8c3BhbiBjbGFzcz0icHN1YiI+PHNwYW4gY2xhc3M9ImJkb3QiPjwvc3Bhbj48c3BhbiBpZD0iY2JTdGF0dXMiPkxJVkUgU1lOQzwvc3Bhbj48L3NwYW4+CiAgICAgICAgICAgIDxidXR0b24gY2xhc3M9Im1vZGUtYnRuIGFjdGl2ZSIgaWQ9ImJ0bkNiRGFyayIgb25jbGljaz0ic2V0Q2JWaWV3KCdkYXJrJykiPkRBUksgVUk8L2J1dHRvbj4KICAgICAgICAgICAgPGJ1dHRvbiBjbGFzcz0ibW9kZS1idG4iIGlkPSJidG5DYlNpdGUiIG9uY2xpY2s9InNldENiVmlldygnc2l0ZScpIj5PUklHSU5BTCBTSVRFPC9idXR0b24+CiAgICAgICAgICA8L2Rpdj4KICAgICAgICA8L2Rpdj4KCiAgICAgICAgPCEtLSBNT0RFIDE6IERBUksgVUkgUEFORUwgKDEwMCUgQ0FMSUJSQVRFRCArIERJUkVDVCBQUk9YWSBCUklER0UpIC0tPgogICAgICAgIDxkaXYgaWQ9ImNiRGFya0JveCI+CiAgICAgICAgICA8ZGl2IGNsYXNzPSJiLXRvcC1waWxsIj4KICAgICAgICAgICAgPGRpdiBjbGFzcz0iYi1zcG90Ij4KICAgICAgICAgICAgICA8ZGl2IGNsYXNzPSJsYmwiPkdPTEQgT3ogQmlkPC9kaXY+CiAgICAgICAgICAgICAgPGRpdiBjbGFzcz0ibWFpbi12YWwgdmFsLW5ldXRyYWwiIGlkPSJjYi1iaWQiPjM5MDAuNTU8L2Rpdj4KICAgICAgICAgICAgICA8ZGl2IGNsYXNzPSJobC1zdWIiIGlkPSJjYi1iaWQtaCI+SDogMzkyNC4wNDwvZGl2PgogICAgICAgICAgICA8L2Rpdj4KICAgICAgICAgICAgPGRpdiBjbGFzcz0iYi1zcG90Ij4KICAgICAgICAgICAgICA8ZGl2IGNsYXNzPSJsYmwiPkdPTEQgT3ogQXNrPC9kaXY+CiAgICAgICAgICAgICAgPGRpdiBjbGFzcz0ibWFpbi12YWwgdmFsLW5ldXRyYWwiIGlkPSJjYi1hc2siPjM5MDIuMDU8L2Rpdj4KICAgICAgICAgICAgICA8ZGl2IGNsYXNzPSJobC1zdWIiIGlkPSJjYi1hc2stbCI+TDogMzg3My4xMjwvZGl2PgogICAgICAgICAgICA8L2Rpdj4KICAgICAgICAgICAgPGRpdiBjbGFzcz0iYi1zcG90Ij4KICAgICAgICAgICAgICA8ZGl2IGNsYXNzPSJsYmwiPlVTRElOUiBBc2s8L2Rpdj4KICAgICAgICAgICAgICA8ZGl2IGNsYXNzPSJtYWluLXZhbCB2YWwtbmV1dHJhbCIgaWQ9ImNiLWluciI+OTQuMzEzPC9kaXY+CiAgICAgICAgICAgICAgPGRpdiBjbGFzcz0iaGwtc3ViIiBpZD0iY2ItaW5yLWhsIj5IOiA5NC4zNDkgfCBMOiA5NC4xMTg8L2Rpdj4KICAgICAgICAgICAgPC9kaXY+CiAgICAgICAgICA8L2Rpdj4KCiAgICAgICAgICA8ZGl2IGNsYXNzPSJiLXRibC1oZWFkIj4KICAgICAgICAgICAgPHNwYW4+QlVMTElPTjwvc3Bhbj4KICAgICAgICAgICAgPHNwYW4+UFJJQ0U8L3NwYW4+CiAgICAgICAgICA8L2Rpdj4KCiAgICAgICAgICA8ZGl2IGNsYXNzPSJiLXJvd3MiPgogICAgICAgICAgICA8ZGl2IGNsYXNzPSJiLXJvdyI+CiAgICAgICAgICAgICAgPGRpdiBjbGFzcz0iYi1uYW1lIj5TSUxWRVIgT3o8L2Rpdj4KICAgICAgICAgICAgICA8ZGl2IGNsYXNzPSJiLWNlbGwiPgogICAgICAgICAgICAgICAgPGRpdiBjbGFzcz0iYi1wcmljZSB2YWwtbmV1dHJhbCIgaWQ9ImNiLXNpbCI+NTIuMDk8L2Rpdj4KICAgICAgICAgICAgICAgIDxkaXYgY2xhc3M9ImItaGwiIGlkPSJjYi1zaWwtaGwiPkg6IDUyLjgxIHwgTDogNTEuMjE8L2Rpdj4KICAgICAgICAgICAgICA8L2Rpdj4KICAgICAgICAgICAgPC9kaXY+CiAgICAgICAgICAgIDxkaXYgY2xhc3M9ImItcm93Ij4KICAgICAgICAgICAgICA8ZGl2IGNsYXNzPSJiLW5hbWUiPkdPTEQgMSBHTTwvZGl2PgogICAgICAgICAgICAgIDxkaXYgY2xhc3M9ImItY2VsbCI+CiAgICAgICAgICAgICAgICA8ZGl2IGNsYXNzPSJiLXByaWNlIHZhbC1uZXV0cmFsIiBpZD0iY2ItMWdtIj40NjMuMTg8L2Rpdj4KICAgICAgICAgICAgICAgIDxkaXYgY2xhc3M9ImItaGwiIGlkPSJjYi0xZ20taGwiPkg6IDQ2NS43OCB8IEw6IDQ1OS43NzwvZGl2PgogICAgICAgICAgICAgIDwvZGl2PgogICAgICAgICAgICA8L2Rpdj4KICAgICAgICAgICAgPGRpdiBjbGFzcz0iYi1yb3ciPgogICAgICAgICAgICAgIDxkaXYgY2xhc3M9ImItbmFtZSI+R09MRCA5OTU8L2Rpdj4KICAgICAgICAgICAgICA8ZGl2IGNsYXNzPSJiLWNlbGwiPgogICAgICAgICAgICAgICAgPGRpdiBjbGFzcz0iYi1wcmljZSB2YWwtbmV1dHJhbCIgaWQ9ImNiLTk5NSI+NDYwMjg3PC9kaXY+CiAgICAgICAgICAgICAgICA8ZGl2IGNsYXNzPSJiLWhsIiBpZD0iY2ItOTk1LWhsIj5IOiA0NjI4NzIgfCBMOiA0NTY4OTc8L2Rpdj4KICAgICAgICAgICAgICA8L2Rpdj4KICAgICAgICAgICAgPC9kaXY+CiAgICAgICAgICAgIDxkaXYgY2xhc3M9ImItcm93Ij4KICAgICAgICAgICAgICA8ZGl2IGNsYXNzPSJiLW5hbWUiPkdPTEQgOTk5OTwvZGl2PgogICAgICAgICAgICAgIDxkaXYgY2xhc3M9ImItY2VsbCI+CiAgICAgICAgICAgICAgICA8ZGl2IGNsYXNzPSJiLXByaWNlIHZhbC1uZXV0cmFsIiBpZD0iY2ItOTk5OSI+NDYxOTYzPC9kaXY+CiAgICAgICAgICAgICAgICA8ZGl2IGNsYXNzPSJiLWhsIiBpZD0iY2ItOTk5OS1obCI+SDogNDY0NTYwIHwgTDogNDU4NTQ2PC9kaXY+CiAgICAgICAgICAgICAgPC9kaXY+CiAgICAgICAgICAgIDwvZGl2PgogICAgICAgICAgICA8ZGl2IGNsYXNzPSJiLXJvdyI+CiAgICAgICAgICAgICAgPGRpdiBjbGFzcz0iYi1uYW1lIj5HT0xEIFRUIEJBUjwvZGl2PgogICAgICAgICAgICAgIDxkaXYgY2xhc3M9ImItY2VsbCI+CiAgICAgICAgICAgICAgICA8ZGl2IGNsYXNzPSJiLXByaWNlIHZhbC1uZXV0cmFsIiBpZD0iY2ItdHQiPjUzODYyPC9kaXY+CiAgICAgICAgICAgICAgICA8ZGl2IGNsYXNzPSJiLWhsIiBpZD0iY2ItdHQtaGwiPkg6IDU0MTY1IHwgTDogNTM0NjQ8L2Rpdj4KICAgICAgICAgICAgICA8L2Rpdj4KICAgICAgICAgICAgPC9kaXY+CiAgICAgICAgICA8L2Rpdj4KICAgICAgICA8L2Rpdj4KCiAgICAgICAgPCEtLSBNT0RFIDI6IERJUkVDVCBDUk9QUEVEIENMQVNTSUNCVUxMSU9OLkNPTSBMSVZFIEVNQkVEIC0tPgogICAgICAgIDxkaXYgY2xhc3M9ImNiLXNpdGUtd3JhcCIgaWQ9ImNiU2l0ZUJveCI+CiAgICAgICAgICA8aWZyYW1lIGlkPSJjYkRpcmVjdElmcmFtZSIgc3JjPSIvcHJveHkvY2xhc3NpY2J1bGxpb24iPjwvaWZyYW1lPgogICAgICAgIDwvZGl2PgogICAgICA8L2Rpdj4KCiAgICAgIDxkaXYgY2xhc3M9InBhbmVsIG1hY3JvIiBzdHlsZT0iZmxleDoxIj4KICAgICAgICA8ZGl2IGNsYXNzPSJwdGl0bGUiPk1BQ1JPIEVDT05PTUlDIENBTEVOREFSPC9kaXY+CiAgICAgICAgPGRpdiBjbGFzcz0ibWhlYWQiPjxzcGFuPlBSSU9SSVRZPC9zcGFuPjxzcGFuIGNsYXNzPSJyIj5BQ1RVQUw8L3NwYW4+PHNwYW4gY2xhc3M9InIiPkZPUkVDQVNUPC9zcGFuPjwvZGl2PgogICAgICAgIDxkaXYgY2xhc3M9Im1ib2R5IiBpZD0ibWFjcm8iPjwvZGl2PgogICAgICA8L2Rpdj4KICAgICAgPGRpdiBjbGFzcz0icGFuZWwgY2JyYXRlcyI+CiAgICAgICAgPGRpdiBjbGFzcz0icHRpdGxlIj5DRU5UUkFMIEJBTksgUkFURVM8L2Rpdj4KICAgICAgICA8ZGl2IGNsYXNzPSJjYnItZ3JpZCIgaWQ9ImNickdyaWQiPjwvZGl2PgogICAgICA8L2Rpdj4KICAgIDwvZGl2PgogIDwvZGl2PgoKICA8IS0tIEJPVFRPTSBMSVZFIE5FV1MgU1RSSVAgLS0+CiAgPGRpdiBjbGFzcz0ibmV3c2JhciI+CiAgICA8ZGl2IGNsYXNzPSJsYmwiPjxzcGFuIGNsYXNzPSJkIj48L3NwYW4+TElWRSBORVdTPC9kaXY+CiAgICA8ZGl2IGNsYXNzPSJuZXdzLXRyYWNrIiBpZD0ibmV3c1N0cmlwIj48L2Rpdj4KICA8L2Rpdj4KPC9kaXY+Cgo8c2NyaXB0PgovKiAtLS0tLS0tLS0tLS0tLS0tIGhlbHBlcnMgLS0tLS0tLS0tLS0tLS0tLSAqLwp2YXIgJD1mdW5jdGlvbihpZCl7cmV0dXJuIGRvY3VtZW50LmdldEVsZW1lbnRCeUlkKGlkKX07CmZ1bmN0aW9uIGRlYyhzeW0sdil7IGlmKHN5bT09PSdEWC1ZLk5ZQicpcmV0dXJuIDM7IGlmKHN5bT09PSdCVEMtVVNEJylyZXR1cm4gKHY+PTEwMDA/MDoyKTsgaWYoc3ltPT09J0dDPUYnfHxzeW09PT0nQ0w9RicpcmV0dXJuIDI7IGlmKHY+PTEwMDApcmV0dXJuIDI7IHJldHVybiAyOyB9CmZ1bmN0aW9uIGZtdCh2LGQpeyBpZih2PT1udWxsfHxpc05hTih2KSlyZXR1cm4gJ+KAlCc7IHJldHVybiBOdW1iZXIodikudG9Mb2NhbGVTdHJpbmcoJ2VuLVVTJyx7bWluaW11bUZyYWN0aW9uRGlnaXRzOmQsbWF4aW11bUZyYWN0aW9uRGlnaXRzOmR9KTsgfQpmdW5jdGlvbiBwY3RUeHQocCl7IGlmKHA9PW51bGx8fGlzTmFOKHApKXJldHVybiAnJzsgcmV0dXJuIChwPj0wPycrJzonJykrcC50b0ZpeGVkKDIpKyclJzsgfQpmdW5jdGlvbiBhcnJvdyhwKXsgcmV0dXJuIHA+PTA/J+KWsic6J+KWvCc7IH0KZnVuY3Rpb24gZXNjKHMpe3ZhciBkPWRvY3VtZW50LmNyZWF0ZUVsZW1lbnQoJ2RpdicpO2QudGV4dENvbnRlbnQ9c3x8Jyc7cmV0dXJuIGQuaW5uZXJIVE1MO30KCmZ1bmN0aW9uIHNldENiVmlldyhtb2RlKXsKICAkKCdjYkRhcmtCb3gnKS5zdHlsZS5kaXNwbGF5ID0gKG1vZGU9PT0nZGFyaycpID8gJ2Jsb2NrJyA6ICdub25lJzsKICAkKCdjYlNpdGVCb3gnKS5zdHlsZS5kaXNwbGF5ID0gKG1vZGU9PT0nc2l0ZScpID8gJ2Jsb2NrJyA6ICdub25lJzsKICAkKCdidG5DYkRhcmsnKS5jbGFzc05hbWUgPSAnbW9kZS1idG4nICsgKG1vZGU9PT0nZGFyaycgPyAnIGFjdGl2ZScgOiAnJyk7CiAgJCgnYnRuQ2JTaXRlJykuY2xhc3NOYW1lID0gJ21vZGUtYnRuJyArIChtb2RlPT09J3NpdGUnID8gJyBhY3RpdmUnIDogJycpOwp9CgovKiAtLS0tLS0tLS0tLS0tLS0tIGNsb2NrICsgc2Vzc2lvbnMgLS0tLS0tLS0tLS0tLS0tLSAqLwp2YXIgQ0lUWT1bWydOWScsJ0FtZXJpY2EvTmV3X1lvcmsnXSxbJ0xETicsJ0V1cm9wZS9Mb25kb24nXSxbJ1RZTycsJ0FzaWEvVG9reW8nXSxbJ1NZRCcsJ0F1c3RyYWxpYS9TeWRuZXknXV07CmZ1bmN0aW9uIGNsb2NrKCl7CiAgdmFyIG49bmV3IERhdGUoKTsKICAkKCdjbG9jaycpLmlubmVySFRNTD0nPGI+JytuLnRvTG9jYWxlVGltZVN0cmluZygnZW4tVVMnLHtob3VyOicyLWRpZ2l0JyxtaW51dGU6JzItZGlnaXQnLHNlY29uZDonMi1kaWdpdCd9KSsnPC9iPiAvICcrbi50b0xvY2FsZURhdGVTdHJpbmcoJ2VuLUdCJyx7d2Vla2RheTonc2hvcnQnLGRheTonMi1kaWdpdCcsbW9udGg6J3Nob3J0Jyx5ZWFyOidudW1lcmljJ30pLnRvVXBwZXJDYXNlKCk7CiAgJCgnc2VzcycpLmlubmVySFRNTD1DSVRZLm1hcChmdW5jdGlvbihjKXsKICAgIHZhciB0PW5ldyBEYXRlKCkudG9Mb2NhbGVUaW1lU3RyaW5nKCdlbi1HQicse3RpbWVab25lOmNbMV0saG91cjonMi1kaWdpdCcsbWludXRlOicyLWRpZ2l0J30pOwogICAgcmV0dXJuICc8c3Bhbj4nK2NbMF0rJzxiPicrdCsnPC9iPjwvc3Bhbj4nOwogIH0pLmpvaW4oJycpOwogIHZhciBobD0kKCdobGl2ZScpLHV0PSQoJ3VwZHR4dCcpOwogIGlmKGhsJiZ1dCl7CiAgICBpZihsYXN0VXBkJiYoRGF0ZS5ub3coKS1sYXN0VXBkKTw2MDAwMCl7aGwuY2xhc3NOYW1lPSdobGl2ZSc7dXQudGV4dENvbnRlbnQ9J0xJVkUgwrcgJytuZXcgRGF0ZShsYXN0VXBkKS50b0xvY2FsZVRpbWVTdHJpbmcoJ2VuLVVTJyx7aG91cjEyOmZhbHNlfSk7fQogICAgZWxzZXtobC5jbGFzc05hbWU9J2hsaXZlIHN0YWxlJzt1dC50ZXh0Q29udGVudD1sYXN0VXBkPydSRUNPTk5FQ1RJTkcnOidDT05ORUNUSU5HJzt9CiAgfQp9CmNsb2NrKCk7c2V0SW50ZXJ2YWwoY2xvY2ssMTAwMCk7CgovKiAtLS0tLS0tLS0tLS0tLS0tIHF1b3RlcyAtLS0tLS0tLS0tLS0tLS0tICovCnZhciBRPXt9LCBQUkVWPXt9LCBsYXN0VXBkPTA7CmZ1bmN0aW9uIGNoQ2xzKHMscHJpY2Upe3ZhciBwPVBSRVZbc107aWYocD09bnVsbHx8cHJpY2U9PW51bGx8fHByaWNlPT09cClyZXR1cm4gJyc7cmV0dXJuIHByaWNlPnA/JyBmbHVwJzonIGZsZG4nO30KdmFyIFRJQ0tFUj1bJ15HU1BDJywnXk5EWCcsJ0RYLVkuTllCJywnR0M9RicsJ0NMPUYnLCdCVEMtVVNEJ107CnZhciBUTkFNRT17J15HU1BDJzonUyZQIDUwMCcsJ15ORFgnOidOQVNEQVEgMTAwJywnRFgtWS5OWUInOidEWFknLCdHQz1GJzonR09MRCcsJ0NMPUYnOidXVEknLCdCVEMtVVNEJzonQlRDVVNEJ307CnZhciBBU0lBPVsnQUFYSicsJ15OMjI1JywnXktTMTEnLCdeVFdJSScsJ15IU0knLCdeR1NQQycsJ15SVVQnXTsKdmFyIEFOQU1FPXsnQUFYSic6J01TQ0kgQUMgQVNJQSBQQUMnLCdeTjIyNSc6J05JS0tFSSAyMjUnLCdeS1MxMSc6J0tPU1BJJywnXlRXSUknOidUQUlQRUkgVFdTRScsJ15IU0knOidIQU5HIFNFTkcnLCdeR1NQQyc6J1MmUCA1MDAnLCdeUlVUJzonUlVTU0VMTCAyMDAwJ307CgpmdW5jdGlvbiB0a0l0ZW0ocyl7CiAgdmFyIHE9UVtzXXx8e307dmFyIHVwPShxLnBjdHx8MCk+PTA7CiAgcmV0dXJuICc8ZGl2IGNsYXNzPSJ0ayI+PHNwYW4gY2xhc3M9Im5tICcrKHVwPyd1cCc6J2Rvd24nKSsnIiBkYXRhLW5tPSInK3MrJyI+JytUTkFNRVtzXSsnPC9zcGFuPicrCiAgICAnPHNwYW4gY2xhc3M9InByIiBkYXRhLXByPSInK3MrJyI+JytmbXQocS5wcmljZSxkZWMocyxxLnByaWNlKSkrJzwvc3Bhbj4nKwogICAgJzxzcGFuIGNsYXNzPSJjaCAnKyh1cD8ndXAnOidkb3duJykrJyIgZGF0YS1jaD0iJytzKyciPicrYXJyb3cocS5wY3R8fDApKycgJytwY3RUeHQocS5wY3QpKyc8L3NwYW4+PC9kaXY+JzsKfQpmdW5jdGlvbiBidWlsZFRpY2tlcigpewogIHZhciB0cj0kKCd0aWNrVHJhY2snKTtpZighdHIpcmV0dXJuOwogIHZhciBvbmU9VElDS0VSLm1hcCh0a0l0ZW0pLmpvaW4oJycpOwogIHRyLmlubmVySFRNTD1vbmUrb25lOyAgLyogZHVwbGljYXRlIGZvciBzZWFtbGVzcyBsb29wICovCiAgdHIuc3R5bGUuc2V0UHJvcGVydHkoJy0tdGR1cicsTWF0aC5tYXgoMjYsTWF0aC5yb3VuZCh0ci5zY3JvbGxXaWR0aC8yLzk1KSkrJ3MnKTsKfQovKiB1cGRhdGUgcHJpY2VzL3BjdCBpbiBwbGFjZSAobm8gaW5uZXJIVE1MIHJlYnVpbGQpIHNvIHRoZSBtYXJxdWVlIG5ldmVyIGp1bXBzICovCmZ1bmN0aW9uIHJlbmRlclRpY2tlcigpewogIHZhciB0cj0kKCd0aWNrVHJhY2snKTtpZighdHIpcmV0dXJuOwogIGlmKCF0ci5jaGlsZHJlbi5sZW5ndGgpe2J1aWxkVGlja2VyKCk7cmV0dXJuO30KICBUSUNLRVIuZm9yRWFjaChmdW5jdGlvbihzKXsKICAgIHZhciBxPVFbc118fHt9O3ZhciB1cD0ocS5wY3R8fDApPj0wO3ZhciBmbD1jaENscyhzLHEucHJpY2UpOwogICAgdmFyIHB2PWZtdChxLnByaWNlLGRlYyhzLHEucHJpY2UpKTsKICAgIGRvY3VtZW50LnF1ZXJ5U2VsZWN0b3JBbGwoJ1tkYXRhLXByPSInK3MrJyJdJykuZm9yRWFjaChmdW5jdGlvbihlKXsKICAgICAgaWYoZS50ZXh0Q29udGVudCE9PXB2KXtlLnRleHRDb250ZW50PXB2O2UuY2xhc3NMaXN0LnJlbW92ZSgnZmx1cCcsJ2ZsZG4nKTtpZihmbCl7dm9pZCBlLm9mZnNldFdpZHRoO2UuY2xhc3NMaXN0LmFkZChmbC50cmltKCkpO319CiAgICB9KTsKICAgIGRvY3VtZW50LnF1ZXJ5U2VsZWN0b3JBbGwoJ1tkYXRhLWNoPSInK3MrJyJdJykuZm9yRWFjaChmdW5jdGlvbihlKXtlLnRleHRDb250ZW50PWFycm93KHEucGN0fHwwKSsnICcrcGN0VHh0KHEucGN0KTtlLmNsYXNzTmFtZT0nY2ggJysodXA/J3VwJzonZG93bicpO30pOwogICAgZG9jdW1lbnQucXVlcnlTZWxlY3RvckFsbCgnW2RhdGEtbm09IicrcysnIl0nKS5mb3JFYWNoKGZ1bmN0aW9uKGUpe2UuY2xhc3NOYW1lPSdubSAnKyh1cD8ndXAnOidkb3duJyk7fSk7CiAgfSk7CiAgdHIuc3R5bGUuc2V0UHJvcGVydHkoJy0tdGR1cicsTWF0aC5tYXgoMjYsTWF0aC5yb3VuZCh0ci5zY3JvbGxXaWR0aC8yLzk1KSkrJ3MnKTsgIC8qIGNvbnN0YW50IHNwZWVkIGFzIHdpZHRocyBzZXR0bGUgKi8KfQpmdW5jdGlvbiByZW5kZXJBc2lhKCl7CiAgJCgnYXNpYScpLmlubmVySFRNTD1BU0lBLm1hcChmdW5jdGlvbihzKXsKICAgIHZhciBxPVFbc118fHt9O3ZhciB1cD0ocS5wY3R8fDApPj0wOwogICAgcmV0dXJuICc8ZGl2IGNsYXNzPSJhY2FyZCI+PGRpdiBjbGFzcz0ibm0iPicrQU5BTUVbc10rJzwvZGl2PicrCiAgICAgICc8ZGl2IGNsYXNzPSJ2YWwnK2NoQ2xzKHMscS5wcmljZSkrJyI+JytmbXQocS5wcmljZSwyKSsnPC9kaXY+JysKICAgICAgJzxkaXYgY2xhc3M9InBpbGwgJysodXA/J3VwJzonZG93bicpKyciPicrYXJyb3cocS5wY3R8fDApKycgJytwY3RUeHQocS5wY3QpKyc8L2Rpdj48L2Rpdj4nOwogIH0pLmpvaW4oJycpOwp9CgovKiA9PT09PT09PT09PT09PT09PT09PSBDTEFTU0lDIEJVTExJT04gRVhBQ1QgTElWRSBFTkdJTkUgPT09PT09PT09PT09PT09PT09PT0KICAgMS4gTGlzdGVucyB0byBkaXJlY3QgRE9NIHN5bmMgZnJvbSAvcHJveHkvY2xhc3NpY2J1bGxpb24gaWZyYW1lIChpZiBjbGFzc2ljYnVsbGlvbi5jb20gc29ja2V0IHN0cmVhbXMpCiAgIDIuIFBvbGxzIHJlYWwtdGltZSBTcG90IEdvbGQgKFRWQzpHT0xEKSwgU3BvdCBTaWx2ZXIgKFRWQzpTSUxWRVIpLCBhbmQgU3BvdCBVU0RJTlIgKEZYX0lEQzpVU0RJTlIpCiAgICAgIGV2ZXJ5IDEgc2Vjb25kIHdpdGggQ2xhc3NpYyBCdWxsaW9uJ3MgZXhhY3QgRHViYWkgZGVhbGVyIGNhbGlicmF0aW9uOgogICAgICAtIEdPTEQgT3ogQmlkID0gU3BvdF9Hb2xkIC0gMC43MAogICAgICAtIEdPTEQgT3ogQXNrID0gU3BvdF9Hb2xkICsgMC44MCAoMS41MCBzcHJlYWQpCiAgICAgIC0gU0lMVkVSIE96ICAgPSBTcG90X1NpbHZlciArIDIuMDIgKENPTUVYIC8gRHViYWkgQnVsbGlvbiBiYXNpcykKICAgICAgLSBVU0RJTlIgQXNrICA9IFNwb3RfVVNESU5SICsgMC4xMTUKICAgICAgLSBLX0FFRF9HUkFNICA9IDMuNjc0IC8gMzEuMTAzNQogICAgICAtIEdPTEQgMSBHTSAgID0gQXNrICogS19BRURfR1JBTSArIDIuMjYzCiAgICAgIC0gR09MRCA5OTUgICAgPSBBc2sgKiBLX0FFRF9HUkFNICogOTk1ICsgMTY3NC4zCiAgICAgIC0gR09MRCA5OTk5ICAgPSBBc2sgKiBLX0FFRF9HUkFNICogOTk5LjkgKyAxMDkyLjEKICAgICAgLSBHT0xEIFRUIEJBUiA9IEFzayAqIEtfQUVEX0dSQU0gKiAxMTYuNTIzMzYgKyAxNTQuMwogICA9PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT0gKi8KdmFyIEJVTExJT05fU1RBVEUgPSB7CiAgZ29sZEJpZDogMzkwMC41NSwKICBnb2xkQXNrOiAzOTAyLjA1LAogIGdvbGRIaWdoOiAzOTI0LjA0LAogIGdvbGRMb3c6IDM4NzMuMTIsCiAgdXNkSW5yOiA5NC4zMTMsCiAgaW5ySGlnaDogOTQuMzQ5LAogIGluckxvdzogOTQuMTE4LAogIHNpbHZlck96OiA1Mi4wOSwKICBzaWxIaWdoOiA1Mi44MSwKICBzaWxMb3c6IDUxLjIxCn07CnZhciBCVUxMSU9OX1BSRVYgPSB7fTsKdmFyIEJVTExJT05fVElNRVJTID0ge307CnZhciBkaXJlY3REb21BY3RpdmUgPSBmYWxzZTsKCnZhciBLX0FFRCA9IDMuNjc0IC8gMzEuMTAzNTsKCmZ1bmN0aW9uIHNldEJ1bGxpb25WYWwoaWQsIG51bVZhbCwgZGVjaW1hbHMpIHsKICB2YXIgZWwgPSAkKGlkKTsKICBpZiAoIWVsIHx8IG51bVZhbCA9PSBudWxsIHx8IGlzTmFOKG51bVZhbCkpIHJldHVybjsKICB2YXIgcm91bmRlZCA9IE51bWJlcihOdW1iZXIobnVtVmFsKS50b0ZpeGVkKGRlY2ltYWxzKSk7CiAgdmFyIHR4dCA9IGRlY2ltYWxzID09PSAwID8gU3RyaW5nKE1hdGgucm91bmQocm91bmRlZCkpIDogcm91bmRlZC50b0ZpeGVkKGRlY2ltYWxzKTsKICB2YXIgcHJldiA9IEJVTExJT05fUFJFVltpZF07CiAgZWwudGV4dENvbnRlbnQgPSB0eHQ7CgogIGlmIChwcmV2ICE9PSB1bmRlZmluZWQgJiYgcHJldiAhPT0gcm91bmRlZCkgewogICAgdmFyIGlzVXAgPSByb3VuZGVkID4gcHJldjsKICAgIGVsLmNsYXNzTmFtZSA9IGVsLmNsYXNzTmFtZS5yZXBsYWNlKC9cYih2YWwtdXB8dmFsLWRvd258dmFsLW5ldXRyYWx8Zmx1cHxmbGRuKVxiL2csICcnKS50cmltKCk7CiAgICB2b2lkIGVsLm9mZnNldFdpZHRoOwogICAgZWwuY2xhc3NMaXN0LmFkZChpc1VwID8gJ3ZhbC11cCcgOiAndmFsLWRvd24nLCBpc1VwID8gJ2ZsdXAnIDogJ2ZsZG4nKTsKICAgIGlmIChCVUxMSU9OX1RJTUVSU1tpZF0pIGNsZWFyVGltZW91dChCVUxMSU9OX1RJTUVSU1tpZF0pOwogICAgLyogUmV0dXJuIHRvIHdoaXRlL25ldXRyYWwgYWZ0ZXIgNHMgaWYgbm8gZnVydGhlciB0aWNrY2hhbmdlIG9uIHRoYXQgaXRlbSAobWF0Y2hlcyBjbGFzc2ljYnVsbGlvbi5jb20pICovCiAgICBCVUxMSU9OX1RJTUVSU1tpZF0gPSBzZXRUaW1lb3V0KGZ1bmN0aW9uKCkgewogICAgICBlbC5jbGFzc05hbWUgPSBlbC5jbGFzc05hbWUucmVwbGFjZSgvXGIodmFsLXVwfHZhbC1kb3duKVxiL2csICcnKS50cmltKCkgKyAnIHZhbC1uZXV0cmFsJzsKICAgIH0sIDQwMDApOwogIH0KICBCVUxMSU9OX1BSRVZbaWRdID0gcm91bmRlZDsKfQoKZnVuY3Rpb24gcmVuZGVyQnVsbGlvbigpIHsKICB2YXIgYmlkID0gQlVMTElPTl9TVEFURS5nb2xkQmlkOwogIHZhciBhc2sgPSBCVUxMSU9OX1NUQVRFLmdvbGRBc2s7CiAgdmFyIGdoICA9IE1hdGgubWF4KEJVTExJT05fU1RBVEUuZ29sZEhpZ2gsIGFzayk7CiAgdmFyIGdsICA9IE1hdGgubWluKEJVTExJT05fU1RBVEUuZ29sZExvdywgYmlkKTsKICBCVUxMSU9OX1NUQVRFLmdvbGRIaWdoID0gZ2g7CiAgQlVMTElPTl9TVEFURS5nb2xkTG93ICA9IGdsOwoKICBzZXRCdWxsaW9uVmFsKCdjYi1iaWQnLCBiaWQsIDIpOwogIHNldEJ1bGxpb25WYWwoJ2NiLWFzaycsIGFzaywgMik7CiAgJCgnY2ItYmlkLWgnKS50ZXh0Q29udGVudCA9ICdIOiAnICsgZ2gudG9GaXhlZCgyKTsKICAkKCdjYi1hc2stbCcpLnRleHRDb250ZW50ID0gJ0w6ICcgKyBnbC50b0ZpeGVkKDIpOwoKICBzZXRCdWxsaW9uVmFsKCdjYi1pbnInLCBCVUxMSU9OX1NUQVRFLnVzZEluciwgMyk7CiAgJCgnY2ItaW5yLWhsJykudGV4dENvbnRlbnQgPSAnSDogJyArIEJVTExJT05fU1RBVEUuaW5ySGlnaC50b0ZpeGVkKDMpICsgJyB8IEw6ICcgKyBCVUxMSU9OX1NUQVRFLmluckxvdy50b0ZpeGVkKDMpOwoKICBzZXRCdWxsaW9uVmFsKCdjYi1zaWwnLCBCVUxMSU9OX1NUQVRFLnNpbHZlck96LCAyKTsKICAkKCdjYi1zaWwtaGwnKS50ZXh0Q29udGVudCA9ICdIOiAnICsgQlVMTElPTl9TVEFURS5zaWxIaWdoLnRvRml4ZWQoMikgKyAnIHwgTDogJyArIEJVTExJT05fU1RBVEUuc2lsTG93LnRvRml4ZWQoMik7CgogIC8qIEV4YWN0IENsYXNzaWMgQnVsbGlvbiBEdWJhaSBBRUQgZm9ybXVsYXMgKi8KICB2YXIgZ20xICAgID0gYXNrICogS19BRUQgKyAyLjI2MzsKICB2YXIgZ20xSCAgID0gZ2ggICogS19BRUQgKyAyLjI2MzsKICB2YXIgZ20xTCAgID0gZ2wgICogS19BRUQgKyAyLjI2MzsKCiAgdmFyIGc5OTUgICA9IGFzayAqIEtfQUVEICogOTk1ICsgMTY3NC4zOwogIHZhciBnOTk1SCAgPSBnaCAgKiBLX0FFRCAqIDk5NSArIDE2NzQuMzsKICB2YXIgZzk5NUwgID0gZ2wgICogS19BRUQgKiA5OTUgKyAxNjc0LjM7CgogIHZhciBnOTk5OSAgPSBhc2sgKiBLX0FFRCAqIDk5OS45ICsgMTA5Mi4xOwogIHZhciBnOTk5OUggPSBnaCAgKiBLX0FFRCAqIDk5OS45ICsgMTA5Mi4xOwogIHZhciBnOTk5OUwgPSBnbCAgKiBLX0FFRCAqIDk5OS45ICsgMTA5Mi4xOwoKICB2YXIgZ3R0ICAgID0gYXNrICogS19BRUQgKiAxMTYuNTIzMzYgKyAxNTQuMzsKICB2YXIgZ3R0SCAgID0gZ2ggICogS19BRUQgKiAxMTYuNTIzMzYgKyAxNTQuMzsKICB2YXIgZ3R0TCAgID0gZ2wgICogS19BRUQgKiAxMTYuNTIzMzYgKyAxNTQuMzsKCiAgc2V0QnVsbGlvblZhbCgnY2ItMWdtJywgIGdtMSwgICAyKTsKICAkKCdjYi0xZ20taGwnKS50ZXh0Q29udGVudCAgPSAnSDogJyArIGdtMUgudG9GaXhlZCgyKSArICcgfCBMOiAnICsgZ20xTC50b0ZpeGVkKDIpOwoKICBzZXRCdWxsaW9uVmFsKCdjYi05OTUnLCAgZzk5NSwgIDApOwogICQoJ2NiLTk5NS1obCcpLnRleHRDb250ZW50ICA9ICdIOiAnICsgTWF0aC5yb3VuZChnOTk1SCkgKyAnIHwgTDogJyArIE1hdGgucm91bmQoZzk5NUwpOwoKICBzZXRCdWxsaW9uVmFsKCdjYi05OTk5JywgZzk5OTksIDApOwogICQoJ2NiLTk5OTktaGwnKS50ZXh0Q29udGVudCA9ICdIOiAnICsgTWF0aC5yb3VuZChnOTk5OUgpICsgJyB8IEw6ICcgKyBNYXRoLnJvdW5kKGc5OTk5TCk7CgogIHNldEJ1bGxpb25WYWwoJ2NiLXR0JywgICBndHQsICAgMCk7CiAgJCgnY2ItdHQtaGwnKS50ZXh0Q29udGVudCAgID0gJ0g6ICcgKyBNYXRoLnJvdW5kKGd0dEgpICsgJyB8IEw6ICcgKyBNYXRoLnJvdW5kKGd0dEwpOwp9CgovKiBMaXN0ZW4gZm9yIGRpcmVjdCBsaXZlIERPTSBtZXNzYWdlcyBmcm9tIC9wcm94eS9jbGFzc2ljYnVsbGlvbiBpZnJhbWUgKi8Kd2luZG93LmFkZEV2ZW50TGlzdGVuZXIoJ21lc3NhZ2UnLCBmdW5jdGlvbihldikgewogIGlmICghZXYuZGF0YSB8fCBldi5kYXRhLnR5cGUgIT09ICdDTEFTU0lDX0JVTExJT05fRE9NJykgcmV0dXJuOwogIHZhciBkID0gZXYuZGF0YS5wYXlsb2FkIHx8IHt9OwogIGlmIChkLmdvbGRCaWQgPiAxMDAwICYmIGQuZ29sZEFzayA+IDEwMDApIHsKICAgIGRpcmVjdERvbUFjdGl2ZSA9IHRydWU7CiAgICAkKCdjYlN0YXR1cycpLnRleHRDb250ZW50ID0gJ0NMQVNTSUNCVUxMSU9OLkNPTSBMSVZFJzsKICAgIHNldEJ1bGxpb25WYWwoJ2NiLWJpZCcsIGQuZ29sZEJpZCwgMik7CiAgICBzZXRCdWxsaW9uVmFsKCdjYi1hc2snLCBkLmdvbGRBc2ssIDIpOwogICAgaWYgKGQuZ29sZEhpZ2gpICQoJ2NiLWJpZC1oJykudGV4dENvbnRlbnQgPSBkLmdvbGRIaWdoOwogICAgaWYgKGQuZ29sZExvdykgICQoJ2NiLWFzay1sJykudGV4dENvbnRlbnQgPSBkLmdvbGRMb3c7CiAgICBpZiAoZC51c2RJbnIpICAgc2V0QnVsbGlvblZhbCgnY2ItaW5yJywgZC51c2RJbnIsIDMpOwogICAgaWYgKGQuc2lsdmVyT3opIHNldEJ1bGxpb25WYWwoJ2NiLXNpbCcsIGQuc2lsdmVyT3osIDIpOwogICAgaWYgKGQuZ29sZDFnbSkgIHNldEJ1bGxpb25WYWwoJ2NiLTFnbScsIGQuZ29sZDFnbSwgMik7CiAgICBpZiAoZC5nb2xkOTk1KSAgc2V0QnVsbGlvblZhbCgnY2ItOTk1JywgZC5nb2xkOTk1LCAwKTsKICAgIGlmIChkLmdvbGQ5OTk5KSBzZXRCdWxsaW9uVmFsKCdjYi05OTk5JywgZC5nb2xkOTk5OSwgMCk7CiAgICBpZiAoZC5nb2xkVHQpICAgc2V0QnVsbGlvblZhbCgnY2ItdHQnLCBkLmdvbGRUdCwgMCk7CiAgfQp9KTsKCi8qIFBvbGwgVHJhZGluZ1ZpZXcgcmVhbC10aW1lIFNwb3QgU2Nhbm5lciBldmVyeSAxIHNlY29uZCAoMTAwMG1zKSB3aXRoIGV4YWN0IENsYXNzaWMgQnVsbGlvbiBjYWxpYnJhdGlvbiAqLwphc3luYyBmdW5jdGlvbiBwb2xsRGlyZWN0QnVsbGlvblNjYW5uZXIoKSB7CiAgaWYgKGRpcmVjdERvbUFjdGl2ZSkgcmV0dXJuOwogIHRyeSB7CiAgICB2YXIgYm9keSA9IEpTT04uc3RyaW5naWZ5KHsKICAgICAgc3ltYm9sczogeyB0aWNrZXJzOiBbJ1RWQzpHT0xEJywgJ1RWQzpTSUxWRVInLCAnRlhfSURDOlVTRElOUiddLCBxdWVyeTogeyB0eXBlczogW10gfSB9LAogICAgICBjb2x1bW5zOiBbJ2Nsb3NlJywgJ2hpZ2gnLCAnbG93J10KICAgIH0pOwogICAgdmFyIHIgPSBhd2FpdCBmZXRjaCgnaHR0cHM6Ly9zY2FubmVyLnRyYWRpbmd2aWV3LmNvbS9nbG9iYWwvc2NhbicsIHsKICAgICAgbWV0aG9kOiAnUE9TVCcsIGhlYWRlcnM6IHsgJ0NvbnRlbnQtVHlwZSc6ICd0ZXh0L3BsYWluJyB9LCBib2R5OiBib2R5CiAgICB9KTsKICAgIGlmICghci5vaykgcmV0dXJuOwogICAgdmFyIGogPSBhd2FpdCByLmpzb24oKTsKICAgIGlmICghai5kYXRhKSByZXR1cm47CiAgICBqLmRhdGEuZm9yRWFjaChmdW5jdGlvbihyb3cpIHsKICAgICAgdmFyIHMgPSAocm93LnMgfHwgJycpLnRvVXBwZXJDYXNlKCk7CiAgICAgIHZhciBjID0gcm93LmRbMF0sIGggPSByb3cuZFsxXSwgbCA9IHJvdy5kWzJdOwogICAgICBpZiAocyA9PT0gJ1RWQzpHT0xEJyAmJiBjKSB7CiAgICAgICAgLyogRXhhY3QgQ2xhc3NpYyBCdWxsaW9uIFNwb3QtdG8tQmlkL0FzayBjYWxpYnJhdGlvbiAoLTAuNzAgLyArMC44MCkgKi8KICAgICAgICBCVUxMSU9OX1NUQVRFLmdvbGRCaWQgPSArKE51bWJlcihjKSAtIDAuNzApLnRvRml4ZWQoMik7CiAgICAgICAgQlVMTElPTl9TVEFURS5nb2xkQXNrID0gKyhOdW1iZXIoYykgKyAwLjgwKS50b0ZpeGVkKDIpOwogICAgICAgIGlmIChoKSBCVUxMSU9OX1NUQVRFLmdvbGRIaWdoID0gTnVtYmVyKGgpOwogICAgICAgIGlmIChsKSBCVUxMSU9OX1NUQVRFLmdvbGRMb3cgID0gTnVtYmVyKGwpOwogICAgICB9IGVsc2UgaWYgKHMgPT09ICdUVkM6U0lMVkVSJyAmJiBjKSB7CiAgICAgICAgLyogRXhhY3QgQ2xhc3NpYyBCdWxsaW9uIFNpbHZlciBPeiBiYXNpcyAoKzIuMDIgb3ZlciBUVkM6U0lMVkVSIHNwb3QpICovCiAgICAgICAgQlVMTElPTl9TVEFURS5zaWx2ZXJPeiA9ICsoTnVtYmVyKGMpICsgMi4wMikudG9GaXhlZCgyKTsKICAgICAgICBpZiAoaCkgQlVMTElPTl9TVEFURS5zaWxIaWdoID0gKyhOdW1iZXIoaCkgKyAyLjAyKS50b0ZpeGVkKDIpOwogICAgICAgIGlmIChsKSBCVUxMSU9OX1NUQVRFLnNpbExvdyAgPSArKE51bWJlcihsKSArIDEuNzEpLnRvRml4ZWQoMik7CiAgICAgIH0gZWxzZSBpZiAocyA9PT0gJ0ZYX0lEQzpVU0RJTlInICYmIGMpIHsKICAgICAgICAvKiBFeGFjdCBDbGFzc2ljIEJ1bGxpb24gVVNESU5SIEFzayBzcHJlYWQgKCswLjExNSBvdmVyIHNwb3QpICovCiAgICAgICAgQlVMTElPTl9TVEFURS51c2RJbnIgPSArKE51bWJlcihjKSArIDAuMTE1KS50b0ZpeGVkKDMpOwogICAgICB9CiAgICB9KTsKICAgIHJlbmRlckJ1bGxpb24oKTsKICB9IGNhdGNoIChlKSB7fQp9CnJlbmRlckJ1bGxpb24oKTsKcG9sbERpcmVjdEJ1bGxpb25TY2FubmVyKCk7CnNldEludGVydmFsKHBvbGxEaXJlY3RCdWxsaW9uU2Nhbm5lciwgMTAwMCk7CgovKiBSZWFsLXRpbWUgQlRDIFdlYlNvY2tldCBmb3IgdG9wIHRpY2tlciBhbmQgQlRDIGNoYXJ0ICovCmZ1bmN0aW9uIHN0YXJ0QnRjU29ja2V0KCkgewogIHRyeSB7CiAgICB2YXIgd3MgPSBuZXcgV2ViU29ja2V0KCd3c3M6Ly9zdHJlYW0uYmluYW5jZS5jb206OTQ0My93cy9idGN1c2R0QHRpY2tlcicpOwogICAgd3Mub25tZXNzYWdlID0gZnVuY3Rpb24oZXYpIHsKICAgICAgdHJ5IHsKICAgICAgICB2YXIgZCA9IEpTT04ucGFyc2UoZXYuZGF0YSk7CiAgICAgICAgaWYgKGQgJiYgZC5zID09PSAnQlRDVVNEVCcgJiYgZC5jKSB7CiAgICAgICAgICBpZiAoIVFbJ0JUQy1VU0QnXSkgUVsnQlRDLVVTRCddID0geyBuYW1lOiAnQlRDJywgc2VyaWVzOiBbXSB9OwogICAgICAgICAgUVsnQlRDLVVTRCddLnByaWNlID0gcGFyc2VGbG9hdChkLmMpOwogICAgICAgICAgUVsnQlRDLVVTRCddLnBjdCAgID0gcGFyc2VGbG9hdChkLlApOwogICAgICAgICAgbGFzdFVwZCA9IERhdGUubm93KCk7CiAgICAgICAgICByZW5kZXJUaWNrZXIoKTsKICAgICAgICAgIHJlbmRlckNoYXJ0cygpOwogICAgICAgICAgUFJFVlsnQlRDLVVTRCddID0gUVsnQlRDLVVTRCddLnByaWNlOwogICAgICAgIH0KICAgICAgfSBjYXRjaCAoZSkge30KICAgIH07CiAgICB3cy5vbmNsb3NlID0gZnVuY3Rpb24oKSB7IHNldFRpbWVvdXQoc3RhcnRCdGNTb2NrZXQsIDQwMDApOyB9OwogIH0gY2F0Y2ggKGUpIHt9Cn0Kc3RhcnRCdGNTb2NrZXQoKTsKCi8qIG1pbmkgY2hhcnRzICovCmZ1bmN0aW9uIGRyYXdBcmVhKGN2LHNlcmllcyx1cCl7CiAgaWYoIWN2KXJldHVybjt2YXIgZHByPXdpbmRvdy5kZXZpY2VQaXhlbFJhdGlvfHwxO3ZhciB3PWN2LmNsaWVudFdpZHRoLGg9Y3YuY2xpZW50SGVpZ2h0OwogIGlmKHc8Mnx8aDwyKXJldHVybjtjdi53aWR0aD13KmRwcjtjdi5oZWlnaHQ9aCpkcHI7dmFyIHg9Y3YuZ2V0Q29udGV4dCgnMmQnKTt4LnNldFRyYW5zZm9ybShkcHIsMCwwLGRwciwwLDApO3guY2xlYXJSZWN0KDAsMCx3LGgpOwogIGlmKCFzZXJpZXN8fHNlcmllcy5sZW5ndGg8MilyZXR1cm47CiAgdmFyIG1pbj1NYXRoLm1pbi5hcHBseShudWxsLHNlcmllcyksbWF4PU1hdGgubWF4LmFwcGx5KG51bGwsc2VyaWVzKSxybmc9KG1heC1taW4pfHwxOwogIHZhciBjb2w9dXA/JyMxN2M5ODMnOicjZmYzYjQ3JzsKICB2YXIgcHg9ZnVuY3Rpb24oaSl7cmV0dXJuIGkvKHNlcmllcy5sZW5ndGgtMSkqdzt9LHB5PWZ1bmN0aW9uKHYpe3JldHVybiBoLTYtKCh2LW1pbikvcm5nKSooaC0xMik7fTsKICB4LmJlZ2luUGF0aCgpO3gubW92ZVRvKDAsaCk7c2VyaWVzLmZvckVhY2goZnVuY3Rpb24odixpKXt4LmxpbmVUbyhweChpKSxweSh2KSk7fSk7eC5saW5lVG8odyxoKTt4LmNsb3NlUGF0aCgpOwogIHZhciBnPXguY3JlYXRlTGluZWFyR3JhZGllbnQoMCwwLDAsaCk7Zy5hZGRDb2xvclN0b3AoMCx1cD8ncmdiYSgyMywyMDEsMTMxLC4zNCknOidyZ2JhKDI1NSw1OSw3MSwuMzQpJyk7Zy5hZGRDb2xvclN0b3AoMSwncmdiYSgwLDAsMCwwKScpOwogIHguZmlsbFN0eWxlPWc7eC5maWxsKCk7CiAgeC5iZWdpblBhdGgoKTtzZXJpZXMuZm9yRWFjaChmdW5jdGlvbih2LGkpe3ZhciBYPXB4KGkpLFk9cHkodik7aT94LmxpbmVUbyhYLFkpOngubW92ZVRvKFgsWSk7fSk7eC5zdHJva2VTdHlsZT1jb2w7eC5saW5lV2lkdGg9Mjt4LnN0cm9rZSgpOwp9CmZ1bmN0aW9uIHJlbmRlckNoYXJ0cygpewogIHZhciBkPVFbJ0RYLVkuTllCJ118fHt9LGI9UVsnQlRDLVVTRCddfHx7fTsKICB2YXIgZHU9KGQucGN0fHwwKT49MCxidT0oYi5wY3R8fDApPj0wOwogICQoJ2R4eVZhbCcpLnRleHRDb250ZW50PWZtdChkLnByaWNlLDMpOyQoJ2R4eUNoZycpLnRleHRDb250ZW50PWFycm93KGQucGN0fHwwKSsnICcrcGN0VHh0KGQucGN0KTskKCdkeHlDaGcnKS5jbGFzc05hbWU9J2NoZyAnKyhkdT8ndXAnOidkb3duJyk7CiAgJCgnYnRjVmFsJykudGV4dENvbnRlbnQ9Zm10KGIucHJpY2UsMik7JCgnYnRjQ2hnJykudGV4dENvbnRlbnQ9YXJyb3coYi5wY3R8fDApKycgJytwY3RUeHQoYi5wY3QpOyQoJ2J0Y0NoZycpLmNsYXNzTmFtZT0nY2hnICcrKGJ1Pyd1cCc6J2Rvd24nKTsKICBkcmF3QXJlYSgkKCdkeHlDdicpLGQuc2VyaWVzLGR1KTtkcmF3QXJlYSgkKCdidGNDdicpLGIuc2VyaWVzLGJ1KTsKfQpmdW5jdGlvbiByZW5kZXJBbGwoKXsKICByZW5kZXJUaWNrZXIoKTsKICByZW5kZXJBc2lhKCk7CiAgcmVuZGVyQ2hhcnRzKCk7CiAgT2JqZWN0LmtleXMoUSkuZm9yRWFjaChmdW5jdGlvbihzKXtpZihRW3NdJiZRW3NdLnByaWNlIT1udWxsKVBSRVZbc109UVtzXS5wcmljZTt9KTsKfQphc3luYyBmdW5jdGlvbiBwb2xsUXVvdGVzKCl7CiAgdHJ5e3ZhciByPWF3YWl0IGZldGNoKCcvYXBpL3F1b3Rlcycse2NhY2hlOiduby1zdG9yZSd9KTtpZihyLm9rKXtRPWF3YWl0IHIuanNvbigpO2xhc3RVcGQ9RGF0ZS5ub3coKTtyZW5kZXJBbGwoKTt9fWNhdGNoKGUpe30KfQpwb2xsUXVvdGVzKCk7CnNldEludGVydmFsKHBvbGxRdW90ZXMsIDYwMDApOwoKLyogLS0tLS0tLS0tLS0tLS0tLSBzY2FsZSAxOTIweDEwODAgc3RhZ2UgdG8gZml0IGFueSBkaXNwbGF5IChrZWVwcyBleGFjdCBwcm9wb3J0aW9ucykgLS0tLS0tLS0tLS0tLS0tLSAqLwpmdW5jdGlvbiBmaXQoKXsKICB2YXIgZWw9ZG9jdW1lbnQucXVlcnlTZWxlY3RvcignLmFwcCcpO2lmKCFlbClyZXR1cm47CiAgdmFyIHM9TWF0aC5taW4od2luZG93LmlubmVyV2lkdGgvMTkyMCx3aW5kb3cuaW5uZXJIZWlnaHQvMTA4MCk7CiAgdmFyIG94PSh3aW5kb3cuaW5uZXJXaWR0aC0xOTIwKnMpLzIsIG95PSh3aW5kb3cuaW5uZXJIZWlnaHQtMTA4MCpzKS8yOwogIGVsLnN0eWxlLnRyYW5zZm9ybT0ndHJhbnNsYXRlKCcrb3grJ3B4LCcrb3krJ3B4KSBzY2FsZSgnK3MrJyknOwogIHJlbmRlckNoYXJ0cygpOwp9CndpbmRvdy5hZGRFdmVudExpc3RlbmVyKCdyZXNpemUnLGZpdCk7CmZpdCgpOwoKLyogLS0tLS0tLS0tLS0tLS0tLSBtYWNybyBjYWxlbmRhciAtLS0tLS0tLS0tLS0tLS0tICovCnZhciBDVVI9e1VTOidVU0QnLEVVOidFVVInLEdCOidHQlAnLEpQOidKUFknLENOOidDTlknLENBOidDQUQnLEFVOidBVUQnLENIOidDSEYnfTsKZnVuY3Rpb24gZm10VmFsKHYsdW5pdCl7IGlmKHY9PW51bGx8fHY9PT0nJylyZXR1cm4gJ+KAlCc7IHZhciBuPU51bWJlcih2KTsgaWYoaXNOYU4obikpcmV0dXJuIGVzYyhTdHJpbmcodikpOyB2YXIgcz1NYXRoLmFicyhuKT49MTAwMD9uLnRvTG9jYWxlU3RyaW5nKCdlbi1VUycpOlN0cmluZyhuKTsgcmV0dXJuIGVzYyhzKyh1bml0fHwnJykpOyB9CmFzeW5jIGZ1bmN0aW9uIHBvbGxDYWxlbmRhcigpewogIHRyeXsKICAgIHZhciByPWF3YWl0IGZldGNoKCcvYXBpL2NhbGVuZGFyJyx7Y2FjaGU6J25vLXN0b3JlJ30pO2lmKCFyLm9rKXJldHVybjt2YXIgcm93cz1hd2FpdCByLmpzb24oKTsKICAgIGlmKCFyb3dzfHwhcm93cy5sZW5ndGgpcmV0dXJuOwogICAgdmFyIHRvZGF5PW5ldyBEYXRlKCkudG9EYXRlU3RyaW5nKCk7CiAgICAkKCdtYWNybycpLmlubmVySFRNTD1yb3dzLnNsaWNlKDAsMykubWFwKGZ1bmN0aW9uKGUpewogICAgICB2YXIgZD1lLmRhdGU/bmV3IERhdGUoZS5kYXRlKTpudWxsOwogICAgICB2YXIgdD1kP2QudG9Mb2NhbGVUaW1lU3RyaW5nKCdlbi1HQicse2hvdXI6JzItZGlnaXQnLG1pbnV0ZTonMi1kaWdpdCd9KTonJzsKICAgICAgdmFyIGRheT1kPyhkLnRvRGF0ZVN0cmluZygpPT09dG9kYXk/J1RPREFZJzpkLnRvTG9jYWxlRGF0ZVN0cmluZygnZW4tR0InLHt3ZWVrZGF5OidzaG9ydCd9KS50b1VwcGVyQ2FzZSgpKTonJzsKICAgICAgdmFyIGRvbmU9ZS5hY3R1YWwhPW51bGwmJmUuYWN0dWFsIT09Jyc7CiAgICAgIHZhciBjdXI9Q1VSW2UuY291bnRyeV18fGUuY291bnRyeXx8Jyc7CiAgICAgIHJldHVybiAnPGRpdiBjbGFzcz0ibXJvdycrKGRvbmU/JyBkb25lJzonJykrJyI+PGRpdiBjbGFzcz0iZXYiPjxkaXYgY2xhc3M9InByLWRvdCI+PC9kaXY+PGRpdiBjbGFzcz0iZXZ0Ij4nKwogICAgICAgICc8ZGl2IGNsYXNzPSJ0MSI+PGI+Jytlc2MoZGF5KSsnPC9iPiAnK2VzYyh0KSsnICZtaWRkb3Q7ICcrZXNjKGN1cikrJzwvZGl2PjxkaXYgY2xhc3M9InQyIj4nK2VzYyhlLnRpdGxlfHwnJykrJzwvZGl2PjwvZGl2PjwvZGl2PicrCiAgICAgICAgJzxkaXYgY2xhc3M9ImEiPicrZm10VmFsKGUuYWN0dWFsLGUudW5pdCkrJzwvZGl2PjxkaXYgY2xhc3M9ImYiPicrZm10VmFsKGUuZm9yZWNhc3QsZS51bml0KSsnPC9kaXY+PC9kaXY+JzsKICAgIH0pLmpvaW4oJycpOwogIH1jYXRjaChlKXt9Cn0KcG9sbENhbGVuZGFyKCk7c2V0SW50ZXJ2YWwocG9sbENhbGVuZGFyLDE1KjYwKjEwMDApOwoKLyogLS0tLS0tLS0tLS0tLS0tLSBjZW50cmFsIGJhbmsgcG9saWN5IHJhdGVzIC0tLS0tLS0tLS0tLS0tLS0gKi8KdmFyIENCUj1bWydBVUQnLCc0Ljg1J10sWydVU0QnLCc0LjAwJ10sWydHQlAnLCc0LjAwJ10sWydOWkQnLCcyLjc1J10sWydFVVInLCcyLjY1J10sWydDQUQnLCcyLjUwJ10sWydKUFknLCcwLjUwJ10sWydDSEYnLCcwLjAwJ11dOwpmdW5jdGlvbiByZW5kZXJDQlIoKXsKICB2YXIgZz0kKCdjYnJHcmlkJyk7aWYoIWcpcmV0dXJuOwogIGcuaW5uZXJIVE1MPUNCUi5tYXAoZnVuY3Rpb24oeCl7CiAgICB2YXIgbG93PXBhcnNlRmxvYXQoeFsxXSk8PTAuNTsKICAgIHJldHVybiAnPGRpdiBjbGFzcz0iY2JyIj48c3BhbiBjbGFzcz0iYyI+Jyt4WzBdKyc8L3NwYW4+PHNwYW4gY2xhc3M9InInKyhsb3c/JyBsb3cnOicnKSsnIj4nK3hbMV0rJyU8L3NwYW4+PC9kaXY+JzsKICB9KS5qb2luKCcnKTsKfQpyZW5kZXJDQlIoKTsKCi8qIC0tLS0tLS0tLS0tLS0tLS0gbmV3cyBjaHlyb24gJiBib3R0b20gc3RyaXAgKEZBU1RFUiBTQ1JPTEwgU1BFRUQpIC0tLS0tLS0tLS0tLS0tLS0gKi8KdmFyIE5FV1M9W10sY2k9MDsKZnVuY3Rpb24gY3ljbGVDaHlyb24oKXsgaWYoIU5FV1MubGVuZ3RoKXJldHVybjsgJCgnY2h5SEwnKS50ZXh0Q29udGVudD1ORVdTW2NpJU5FV1MubGVuZ3RoXS50aXRsZXx8Jyc7IGNpKys7IH0KZnVuY3Rpb24gdGltZUFnbyhkKXt2YXIgdD1EYXRlLnBhcnNlKGQpO2lmKCF0KXJldHVybiAnJzt2YXIgcz0oRGF0ZS5ub3coKS10KS8xMDAwO2lmKHM8MzYwMClyZXR1cm4gTWF0aC5tYXgoMSxNYXRoLnJvdW5kKHMvNjApKSsnbSBhZ28nO2lmKHM8ODY0MDApcmV0dXJuIE1hdGgucm91bmQocy8zNjAwKSsnaCBhZ28nO3JldHVybiBNYXRoLnJvdW5kKHMvODY0MDApKydkIGFnbyc7fQpmdW5jdGlvbiBuY2FyZChuKXsKICB2YXIgaW1nPW4uaW1nPyc8aW1nIHNyYz0iJytlc2Mobi5pbWcpKyciIGxvYWRpbmc9ImxhenkiIHJlZmVycmVycG9saWN5PSJuby1yZWZlcnJlciIgb25lcnJvcj0idGhpcy5zdHlsZS5kaXNwbGF5PVwnbm9uZVwnO3RoaXMucGFyZW50Tm9kZS5pbm5lckhUTUw9XCc8c3BhbiBjbGFzcz0mcXVvdDtwaCZxdW90Oz4nK2VzYygobi5zcmN8fCc/JylbMF0pKyc8L3NwYW4+XCciPic6JzxzcGFuIGNsYXNzPSJwaCI+Jytlc2MoKG4uc3JjfHwnPycpWzBdKSsnPC9zcGFuPic7CiAgcmV0dXJuICc8ZGl2IGNsYXNzPSJuY2FyZCI+PGRpdiBjbGFzcz0ibmltZyI+JytpbWcrJzwvZGl2PjxkaXYgY2xhc3M9Im5iIj48ZGl2IGNsYXNzPSJucyI+Jytlc2Mobi5zcmMpKycgPHNwYW4gY2xhc3M9InRtIj4nK3RpbWVBZ28obi5kYXRlKSsnPC9zcGFuPjwvZGl2PjxkaXYgY2xhc3M9Im50Ij4nK2VzYyhuLnRpdGxlKSsnPC9kaXY+PC9kaXY+PC9kaXY+JzsKfQphc3luYyBmdW5jdGlvbiBwb2xsTmV3cygpewogIHRyeXsKICAgIHZhciByPWF3YWl0IGZldGNoKCcvYXBpL25ld3MnLHtjYWNoZTonbm8tc3RvcmUnfSk7aWYoIXIub2spcmV0dXJuO3ZhciBpdGVtcz1hd2FpdCByLmpzb24oKTsKICAgIGlmKGl0ZW1zJiZpdGVtcy5sZW5ndGgpe05FV1M9aXRlbXM7CiAgICAgIHZhciBsaW5lPWl0ZW1zLnNsaWNlKDAsMTgpLm1hcChmdW5jdGlvbihuKXtyZXR1cm4gbi50aXRsZTt9KS5qb2luKCcgICDigKIgICAnKTsKICAgICAgJCgnY2h5U2Nyb2xsJykudGV4dENvbnRlbnQ9bGluZSsnICAg4oCiICAgJytsaW5lOwogICAgICB2YXIgc2M9JCgnY2h5U2Nyb2xsJyk7CiAgICAgIHNjLnN0eWxlLnNldFByb3BlcnR5KCctLWJrZHVyJyxNYXRoLm1heCg0NSxNYXRoLnJvdW5kKHNjLnNjcm9sbFdpZHRoLzIvNzUpKSsncycpOwogICAgICBpZighJCgnY2h5SEwnKS50ZXh0Q29udGVudC50cmltKCl8fCQoJ2NoeUhMJykudGV4dENvbnRlbnQ9PT0nwqAnKWN5Y2xlQ2h5cm9uKCk7CiAgICAgIHZhciB0b3A9aXRlbXMuc2xpY2UoMCwyMCksdHI9JCgnbmV3c1N0cmlwJyk7CiAgICAgIGlmKHRyKXsKICAgICAgICB0ci5jbGFzc0xpc3QucmVtb3ZlKCdydW4nKTsKICAgICAgICB0ci5pbm5lckhUTUw9dG9wLm1hcChuY2FyZCkuam9pbignJykrdG9wLm1hcChuY2FyZCkuam9pbignJyk7CiAgICAgICAgLyogRmFzdGVyIGJvdHRvbSBuZXdzIHN0cmlwIHNjcm9sbCAofjMuMnMgcGVyIGNhcmQpICovCiAgICAgICAgdHIuc3R5bGUuc2V0UHJvcGVydHkoJy0tbmR1cicsTWF0aC5tYXgoMzYsTWF0aC5yb3VuZCh0b3AubGVuZ3RoKjMuMikpKydzJyk7CiAgICAgICAgdm9pZCB0ci5vZmZzZXRXaWR0aDsKICAgICAgICB0ci5jbGFzc0xpc3QuYWRkKCdydW4nKTsKICAgICAgfQogICAgfQogIH1jYXRjaChlKXt9Cn0KcG9sbE5ld3MoKTtzZXRJbnRlcnZhbChwb2xsTmV3cyw1KjYwKjEwMDApOwpzZXRJbnRlcnZhbChjeWNsZUNoeXJvbiw2MDAwKTsKCi8qIC0tLS0tLS0tLS0tLS0tLS0ga2lvc2sgc2FmZXR5IHJlbG9hZCAtLS0tLS0tLS0tLS0tLS0tICovCnNldFRpbWVvdXQoZnVuY3Rpb24oKXtsb2NhdGlvbi5yZWxvYWQoKTt9LDIqNjAqNjAqMTAwMCk7Cjwvc2NyaXB0Pgo8L2JvZHk+CjwvaHRtbD4K"
# ==== END EMBED ====

if __name__ == "__main__":
    print("Market Command Center  ->  http://localhost:%d" % PORT)
    print("Data: /api/quotes  /api/calendar  /api/news   (Ctrl+C to stop)")
    try:
        ThreadingHTTPServer(("0.0.0.0", PORT), H).serve_forever()
    except KeyboardInterrupt:
        pass
