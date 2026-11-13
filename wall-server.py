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
EMBED_HTML_B64 = "PCFkb2N0eXBlIGh0bWw+CjxodG1sIGxhbmc9ImVuIj4KPGhlYWQ+CjxtZXRhIGNoYXJzZXQ9InV0Zi04Ij4KPG1ldGEgbmFtZT0idmlld3BvcnQiIGNvbnRlbnQ9IndpZHRoPWRldmljZS13aWR0aCxpbml0aWFsLXNjYWxlPTEiPgo8dGl0bGU+TWFya2V0IENvbW1hbmQgQ2VudGVyPC90aXRsZT4KPGxpbmsgcmVsPSJwcmVjb25uZWN0IiBocmVmPSJodHRwczovL2ZvbnRzLmdvb2dsZWFwaXMuY29tIj48bGluayByZWw9InByZWNvbm5lY3QiIGhyZWY9Imh0dHBzOi8vZm9udHMuZ3N0YXRpYy5jb20iIGNyb3Nzb3JpZ2luPgo8bGluayByZWw9InN0eWxlc2hlZXQiIGhyZWY9Imh0dHBzOi8vZm9udHMuZ29vZ2xlYXBpcy5jb20vY3NzMj9mYW1pbHk9SW50ZXI6d2dodEA0MDA7NTAwOzYwMDs3MDA7ODAwOzkwMCZkaXNwbGF5PXN3YXAiPgo8c3R5bGU+CiAgOnJvb3R7CiAgICAtLWJnOiMwMDA7IC0tcGFuZWw6IzBiMGQxMjsgLS1wYW5lbC0yOiMwZTExMTg7IC0tY2FyZDojMGQxMDE3OyAtLWxpbmU6IzFhMWYyOTsgLS1saW5lLTI6IzE0MTkyMjsKICAgIC0tZmc6I2YyZjVmYTsgLS1tdXRlZDojOGE5NGE2OyAtLWZhaW50OiM1YTY0NzQ7CiAgICAtLXVwOiMxN2M5ODM7IC0tdXAtYmc6cmdiYSgyMywyMDEsMTMxLC4xNCk7IC0tZG93bjojZmYzYjQ3OyAtLWRvd24tYmc6cmdiYSgyNTUsNTksNzEsLjE0KTsKICAgIC0tZ29sZDojZjBiNDI5OyAtLW9yYW5nZTojZmY3YTFhOyAtLWxpdmU6I2ZmM2I0NzsgLS1hY2NlbnQ6I2YwYjQyOTsKICAgIC0tc2FuczoiSW50ZXIiLHN5c3RlbS11aSwtYXBwbGUtc3lzdGVtLCJTZWdvZSBVSSIsc2Fucy1zZXJpZjsKICB9CiAgKntib3gtc2l6aW5nOmJvcmRlci1ib3g7bWFyZ2luOjA7cGFkZGluZzowfQogIGh0bWwsYm9keXtoZWlnaHQ6MTAwJTt3aWR0aDoxMDAlfQogIGJvZHl7YmFja2dyb3VuZDojMDAwO2NvbG9yOnZhcigtLWZnKTtmb250LWZhbWlseTp2YXIoLS1zYW5zKTtvdmVyZmxvdzpoaWRkZW47LXdlYmtpdC1mb250LXNtb290aGluZzphbnRpYWxpYXNlZDtmb250LXZhcmlhbnQtbnVtZXJpYzp0YWJ1bGFyLW51bXN9CiAgLyogZml4ZWQgMTkyMHgxMDgwIHN0YWdlLCBzY2FsZWQgdG8gZml0IGFueSBkaXNwbGF5IC0+IGlkZW50aWNhbCBwcm9wb3J0aW9ucyBldmVyeXdoZXJlICovCiAgLmFwcHt3aWR0aDoxOTIwcHg7aGVpZ2h0OjEwODBweDt0cmFuc2Zvcm0tb3JpZ2luOnRvcCBsZWZ0O3Bvc2l0aW9uOmFic29sdXRlO3RvcDowO2xlZnQ6MDtkaXNwbGF5OmdyaWQ7Z3JpZC10ZW1wbGF0ZS1yb3dzOjU2cHggODhweCAxZnIgMTI4cHg7Z2FwOjEycHg7cGFkZGluZzoxM3B4O2JhY2tncm91bmQ6dmFyKC0tYmcpfQoKICAvKiBoZWFkZXIgKi8KICAuaGVhZHtkaXNwbGF5OmZsZXg7YWxpZ24taXRlbXM6Y2VudGVyO2p1c3RpZnktY29udGVudDpzcGFjZS1iZXR3ZWVuO3BhZGRpbmc6MCA4cHh9CiAgLmh0aXRsZXtmb250LXdlaWdodDo4MDA7Zm9udC1zaXplOjI4cHg7bGV0dGVyLXNwYWNpbmc6LjA5ZW19CiAgLmhzZXNze2Rpc3BsYXk6ZmxleDtnYXA6MjhweDtmb250LXdlaWdodDo3MDA7Zm9udC1zaXplOjE4cHh9CiAgLmhzZXNzIHNwYW57Y29sb3I6dmFyKC0tbXV0ZWQpfSAuaHNlc3MgYntjb2xvcjp2YXIoLS1mZyk7Zm9udC13ZWlnaHQ6ODAwO21hcmdpbi1sZWZ0OjdweH0KICAuaGNsb2Nre2ZvbnQtd2VpZ2h0OjcwMDtmb250LXNpemU6MTlweDtjb2xvcjp2YXIoLS1tdXRlZCl9IC5oY2xvY2sgYntjb2xvcjp2YXIoLS1mZyk7Zm9udC13ZWlnaHQ6ODAwfQogIC5obGl2ZXtkaXNwbGF5OmlubGluZS1mbGV4O2FsaWduLWl0ZW1zOmNlbnRlcjtnYXA6N3B4O2ZvbnQtd2VpZ2h0OjgwMDtmb250LXNpemU6MTNweDtsZXR0ZXItc3BhY2luZzouMDhlbTtjb2xvcjp2YXIoLS11cCl9CiAgLmhsaXZlIC5kb3R7d2lkdGg6OXB4O2hlaWdodDo5cHg7Ym9yZGVyLXJhZGl1czo1MCU7YmFja2dyb3VuZDp2YXIoLS11cCk7Ym94LXNoYWRvdzowIDAgOXB4IHZhcigtLXVwKTthbmltYXRpb246cHVsc2UgMS40cyBpbmZpbml0ZX0KICAuaGxpdmUuc3RhbGV7Y29sb3I6dmFyKC0tZmFpbnQpfSAuaGxpdmUuc3RhbGUgLmRvdHtiYWNrZ3JvdW5kOnZhcigtLWZhaW50KTtib3gtc2hhZG93Om5vbmU7YW5pbWF0aW9uOm5vbmV9CiAgQGtleWZyYW1lcyBmbHVwezAle2JhY2tncm91bmQ6cmdiYSgyMywyMDEsMTMxLC40NSl9MTAwJXtiYWNrZ3JvdW5kOnRyYW5zcGFyZW50fX0KICBAa2V5ZnJhbWVzIGZsZG57MCV7YmFja2dyb3VuZDpyZ2JhKDI1NSw1OSw3MSwuNDUpfTEwMCV7YmFja2dyb3VuZDp0cmFuc3BhcmVudH19CiAgLmZsdXB7YW5pbWF0aW9uOmZsdXAgLjhzIGVhc2Utb3V0O2JvcmRlci1yYWRpdXM6NXB4fSAuZmxkbnthbmltYXRpb246ZmxkbiAuOHMgZWFzZS1vdXQ7Ym9yZGVyLXJhZGl1czo1cHh9CgogIC8qIHRpY2tlciByb3cgKi8KICAudGlja3tkaXNwbGF5OmZsZXg7YWxpZ24taXRlbXM6Y2VudGVyO2JhY2tncm91bmQ6dmFyKC0tcGFuZWwpO2JvcmRlcjoxcHggc29saWQgdmFyKC0tbGluZSk7Ym9yZGVyLXJhZGl1czoxMnB4O292ZXJmbG93OmhpZGRlbn0KICAudGljay10cmFja3tkaXNwbGF5OmZsZXg7YWxpZ24taXRlbXM6Y2VudGVyO2hlaWdodDoxMDAlO3dpZHRoOm1heC1jb250ZW50O2FuaW1hdGlvbjp0bWFycSB2YXIoLS10ZHVyLDQ2cykgbGluZWFyIGluZmluaXRlO3dpbGwtY2hhbmdlOnRyYW5zZm9ybX0KICBAa2V5ZnJhbWVzIHRtYXJxe2Zyb217dHJhbnNmb3JtOnRyYW5zbGF0ZVgoMCl9dG97dHJhbnNmb3JtOnRyYW5zbGF0ZVgoLTUwJSl9fQogIC50a3tkaXNwbGF5OmZsZXg7YWxpZ24taXRlbXM6Y2VudGVyO2dhcDoxNXB4O3BhZGRpbmc6MCAzNHB4O2hlaWdodDoxMDAlO2JvcmRlci1yaWdodDoxcHggc29saWQgdmFyKC0tbGluZS0yKTt3aGl0ZS1zcGFjZTpub3dyYXB9CiAgLnRrIC5ubXtmb250LXdlaWdodDo4MDA7Zm9udC1zaXplOjMwcHh9CiAgLnRrIC5wcntmb250LXdlaWdodDo3MDA7Zm9udC1zaXplOjMwcHg7Y29sb3I6dmFyKC0tZmcpfQogIC50ayAuY2h7Zm9udC13ZWlnaHQ6ODAwO2ZvbnQtc2l6ZToyNXB4O2Rpc3BsYXk6ZmxleDthbGlnbi1pdGVtczpjZW50ZXI7Z2FwOjdweH0KICAudXB7Y29sb3I6dmFyKC0tdXApfSAuZG93bntjb2xvcjp2YXIoLS1kb3duKX0KCiAgLyogbWFpbiAzIGNvbHMgKi8KICAubWFpbntkaXNwbGF5OmdyaWQ7Z3JpZC10ZW1wbGF0ZS1jb2x1bW5zOjEuNmZyIC44NmZyIDEuMGZyO2dhcDo5cHg7bWluLWhlaWdodDowfQogIC5jb2x7ZGlzcGxheTpmbGV4O2ZsZXgtZGlyZWN0aW9uOmNvbHVtbjtnYXA6OXB4O21pbi1oZWlnaHQ6MDttaW4td2lkdGg6MH0KICAucGFuZWx7YmFja2dyb3VuZDp2YXIoLS1wYW5lbCk7Ym9yZGVyOjFweCBzb2xpZCB2YXIoLS1saW5lKTtib3JkZXItcmFkaXVzOjE0cHg7ZGlzcGxheTpmbGV4O2ZsZXgtZGlyZWN0aW9uOmNvbHVtbjttaW4taGVpZ2h0OjA7b3ZlcmZsb3c6aGlkZGVufQogIC5wdGl0bGV7Zm9udC13ZWlnaHQ6ODAwO2ZvbnQtc2l6ZToxNXB4O2xldHRlci1zcGFjaW5nOi4xZW07cGFkZGluZzo5cHggMTZweCA2cHg7Y29sb3I6dmFyKC0tZmcpO2ZsZXg6MCAwIGF1dG87ZGlzcGxheTpmbGV4O2FsaWduLWl0ZW1zOmNlbnRlcjtqdXN0aWZ5LWNvbnRlbnQ6c3BhY2UtYmV0d2Vlbn0KICAucHRpdGxlIC5wc3Vie2ZvbnQtd2VpZ2h0OjcwMDtmb250LXNpemU6MTFweDtsZXR0ZXItc3BhY2luZzouMDhlbTtjb2xvcjp2YXIoLS1nb2xkKTtkaXNwbGF5OmlubGluZS1mbGV4O2FsaWduLWl0ZW1zOmNlbnRlcjtnYXA6NnB4fQogIC5wdGl0bGUgLnBzdWIgLmJkb3R7d2lkdGg6N3B4O2hlaWdodDo3cHg7Ym9yZGVyLXJhZGl1czo1MCU7YmFja2dyb3VuZDp2YXIoLS11cCk7Ym94LXNoYWRvdzowIDAgOHB4IHZhcigtLXVwKTthbmltYXRpb246cHVsc2UgMS4ycyBpbmZpbml0ZX0KICAubW9kZS1idG57YmFja2dyb3VuZDojMTUxYTI0O2JvcmRlcjoxcHggc29saWQgIzI2MmY0MDtjb2xvcjp2YXIoLS1tdXRlZCk7Zm9udDo3MDAgMTBweC8xIHZhcigtLXNhbnMpO3BhZGRpbmc6NHB4IDhweDtib3JkZXItcmFkaXVzOjVweDtjdXJzb3I6cG9pbnRlcjtsZXR0ZXItc3BhY2luZzouMDZlbX0KICAubW9kZS1idG4uYWN0aXZle2NvbG9yOnZhcigtLWdvbGQpO2JvcmRlci1jb2xvcjpyZ2JhKDI0MCwxODAsNDEsLjUpO2JhY2tncm91bmQ6cmdiYSgyNDAsMTgwLDQxLC4xMil9CgogIC8qIHZpZGVvICovCiAgLnZpZHtmbGV4OjE7bWluLWhlaWdodDowO3Bvc2l0aW9uOnJlbGF0aXZlO2JhY2tncm91bmQ6IzAwMDtib3JkZXI6MXB4IHNvbGlkIHZhcigtLWxpbmUpO2JvcmRlci1yYWRpdXM6MTRweDtvdmVyZmxvdzpoaWRkZW59CiAgLnZpZCBpZnJhbWV7cG9zaXRpb246YWJzb2x1dGU7aW5zZXQ6MDt3aWR0aDoxMDAlO2hlaWdodDoxMDAlO2JvcmRlcjowfQogIC52dGx7cG9zaXRpb246YWJzb2x1dGU7dG9wOjEycHg7bGVmdDoxMnB4O3otaW5kZXg6MztiYWNrZ3JvdW5kOnJnYmEoMTAsMTAsMTIsLjcyKTtiYWNrZHJvcC1maWx0ZXI6Ymx1cig0cHgpO2JvcmRlci1yYWRpdXM6OHB4O3BhZGRpbmc6N3B4IDEycHg7Zm9udC13ZWlnaHQ6NzAwO2ZvbnQtc2l6ZToxM3B4O2Rpc3BsYXk6ZmxleDthbGlnbi1pdGVtczpjZW50ZXI7Z2FwOjhweH0KICAudnRsIC55dHtjb2xvcjojZmYyZDJkO2ZvbnQtd2VpZ2h0OjkwMH0KICAudnRye3Bvc2l0aW9uOmFic29sdXRlO3RvcDoxMnB4O3JpZ2h0OjEycHg7ei1pbmRleDozO2JhY2tncm91bmQ6I2UwMWIyNDtib3JkZXItcmFkaXVzOjdweDtwYWRkaW5nOjZweCAxMXB4O2ZvbnQtd2VpZ2h0OjkwMDtmb250LXNpemU6MTNweDtsZXR0ZXItc3BhY2luZzouMDhlbTtkaXNwbGF5OmZsZXg7YWxpZ24taXRlbXM6Y2VudGVyO2dhcDo3cHh9CiAgLnZ0ciAuZHt3aWR0aDo4cHg7aGVpZ2h0OjhweDtib3JkZXItcmFkaXVzOjUwJTtiYWNrZ3JvdW5kOiNmZmY7YW5pbWF0aW9uOnB1bHNlIDEuM3MgaW5maW5pdGV9CiAgLnZicntwb3NpdGlvbjphYnNvbHV0ZTtib3R0b206NjJweDtyaWdodDoxMnB4O3otaW5kZXg6MztiYWNrZ3JvdW5kOnJnYmEoMTAsMTAsMTIsLjc4KTtib3JkZXItcmFkaXVzOjZweDtwYWRkaW5nOjVweCAxMHB4O2ZvbnQtd2VpZ2h0OjgwMDtmb250LXNpemU6MTJweH0KICAudmJyIGJ7Y29sb3I6dmFyKC0tZ29sZCl9CiAgLmNoeXJvbntwb3NpdGlvbjphYnNvbHV0ZTtsZWZ0OjA7cmlnaHQ6MDtib3R0b206MDt6LWluZGV4OjN9CiAgLmNoeXJvbiAuYmt7ZGlzcGxheTpmbGV4O2FsaWduLWl0ZW1zOmNlbnRlcjtiYWNrZ3JvdW5kOiMwYTBjMTA7Ym9yZGVyLXRvcDozcHggc29saWQgdmFyKC0tb3JhbmdlKX0KICAuY2h5cm9uIC5iayAudGFne2JhY2tncm91bmQ6dmFyKC0tb3JhbmdlKTtjb2xvcjojMTExO2ZvbnQtd2VpZ2h0OjkwMDtmb250LXNpemU6MTdweDtsZXR0ZXItc3BhY2luZzouMDZlbTtwYWRkaW5nOjEycHggMThweDt3aGl0ZS1zcGFjZTpub3dyYXB9CiAgLmNoeXJvbiAuYmsgLnNjcm9sbHdyYXB7ZmxleDoxO292ZXJmbG93OmhpZGRlbjtwb3NpdGlvbjpyZWxhdGl2ZX0KICAuY2h5cm9uIC5iayAuc2Nyb2xse2Rpc3BsYXk6aW5saW5lLWZsZXg7d2hpdGUtc3BhY2U6bm93cmFwO3BhZGRpbmc6MTJweCAwO2ZvbnQtd2VpZ2h0OjcwMDtmb250LXNpemU6MjFweDtjb2xvcjojZjJkOWM0O2FuaW1hdGlvbjpia21hcnEgdmFyKC0tYmtkdXIsNzVzKSBsaW5lYXIgaW5maW5pdGU7d2lsbC1jaGFuZ2U6dHJhbnNmb3JtfQogIEBrZXlmcmFtZXMgYmttYXJxe2Zyb217dHJhbnNmb3JtOnRyYW5zbGF0ZVgoMCl9dG97dHJhbnNmb3JtOnRyYW5zbGF0ZVgoLTUwJSl9fQoKICAvKiBtaW5pIGNoYXJ0cyByb3cgKi8KICAuY2hhcnRze2Rpc3BsYXk6Z3JpZDtncmlkLXRlbXBsYXRlLWNvbHVtbnM6MWZyIDFmcjtnYXA6OXB4O2ZsZXg6MCAwIDE5MHB4fQogIC5tY3tiYWNrZ3JvdW5kOnZhcigtLXBhbmVsKTtib3JkZXI6MXB4IHNvbGlkIHZhcigtLWxpbmUpO2JvcmRlci1yYWRpdXM6MTRweDtwYWRkaW5nOjExcHggMTNweDtkaXNwbGF5OmZsZXg7ZmxleC1kaXJlY3Rpb246Y29sdW1uO21pbi13aWR0aDowfQogIC5tYyAudG9we2Rpc3BsYXk6ZmxleDthbGlnbi1pdGVtczpiYXNlbGluZTtnYXA6MTBweDtmbGV4LXdyYXA6d3JhcH0KICAubWMgLnN5bXtmb250LXdlaWdodDo5MDA7Zm9udC1zaXplOjE3cHh9CiAgLm1jIC52YWx7Zm9udC13ZWlnaHQ6NzAwO2ZvbnQtc2l6ZToxNXB4O2NvbG9yOnZhcigtLW11dGVkKTttYXJnaW4tbGVmdDphdXRvfQogIC5tYyAuY2hne2ZvbnQtd2VpZ2h0OjgwMDtmb250LXNpemU6MTRweH0KICAubWMgY2FudmFze2ZsZXg6MTt3aWR0aDoxMDAlO21pbi1oZWlnaHQ6MDttYXJnaW4tdG9wOjRweH0KCiAgLyogYXNpYSBjYXJkcyAqLwogIC5hc2lhe2dhcDowO2p1c3RpZnktY29udGVudDpzdHJldGNofQogIC5hY2FyZHtwYWRkaW5nOjAgMjBweDtib3JkZXItdG9wOjFweCBzb2xpZCB2YXIoLS1saW5lLTIpO2Rpc3BsYXk6ZmxleDtmbGV4LWRpcmVjdGlvbjpyb3c7YWxpZ24taXRlbXM6Y2VudGVyO2dhcDoxMnB4O2ZsZXg6MTttaW4taGVpZ2h0OjA7b3ZlcmZsb3c6aGlkZGVufQogIC5hY2FyZDpmaXJzdC1vZi10eXBle2JvcmRlci10b3A6MH0KICAuYWNhcmQgLm5te2ZvbnQtd2VpZ2h0OjcwMDtmb250LXNpemU6MTVweDtjb2xvcjp2YXIoLS1tdXRlZCk7bGV0dGVyLXNwYWNpbmc6LjAyZW07ZmxleDoxO21pbi13aWR0aDowO3doaXRlLXNwYWNlOm5vd3JhcDtvdmVyZmxvdzpoaWRkZW47dGV4dC1vdmVyZmxvdzplbGxpcHNpc30KICAuYWNhcmQgLnZhbHtmb250LXdlaWdodDo4MDA7Zm9udC1zaXplOjI2cHg7bGluZS1oZWlnaHQ6MX0KICAuYWNhcmQgLnBpbGx7Zm9udC1zaXplOjE0cHg7cGFkZGluZzo1cHggMTFweDttaW4td2lkdGg6ODJweDtqdXN0aWZ5LWNvbnRlbnQ6Y2VudGVyfQogIC5waWxse2Rpc3BsYXk6aW5saW5lLWZsZXg7YWxpZ24taXRlbXM6Y2VudGVyO2dhcDo2cHg7Zm9udC13ZWlnaHQ6ODAwO2ZvbnQtc2l6ZToxNXB4O3BhZGRpbmc6NnB4IDEycHg7Ym9yZGVyLXJhZGl1czo5cHh9CiAgLnBpbGwudXB7YmFja2dyb3VuZDp2YXIoLS11cC1iZyk7Y29sb3I6dmFyKC0tdXApO2JveC1zaGFkb3c6MCAwIDAgMXB4IHJnYmEoMjMsMjAxLDEzMSwuMykgaW5zZXR9CiAgLnBpbGwuZG93bntiYWNrZ3JvdW5kOnZhcigtLWRvd24tYmcpO2NvbG9yOnZhcigtLWRvd24pO2JveC1zaGFkb3c6MCAwIDAgMXB4IHJnYmEoMjU1LDU5LDcxLC4zKSBpbnNldH0KCiAgLyogPT09PT09PT09PT09PT09PT09PT0gQ0xBU1NJQyBCVUxMSU9OIExJVkUgUEFORUwgPT09PT09PT09PT09PT09PT09PT0gKi8KICAuYnVsbGlvbi1wYW5lbHtmbGV4OjAgMCBhdXRvO2JhY2tncm91bmQ6dmFyKC0tcGFuZWwpO2JvcmRlcjoxcHggc29saWQgdmFyKC0tbGluZSk7Ym9yZGVyLXJhZGl1czoxNHB4O292ZXJmbG93OmhpZGRlbjtkaXNwbGF5OmZsZXg7ZmxleC1kaXJlY3Rpb246Y29sdW1uO3Bvc2l0aW9uOnJlbGF0aXZlfQogIC5iLXRvcC1waWxsewogICAgbWFyZ2luOjFweCAxMnB4IDZweDsKICAgIGJhY2tncm91bmQ6bGluZWFyLWdyYWRpZW50KDE4MGRlZywjMTYxYjI2IDAlLCMwZjEzMWMgMTAwJSk7CiAgICBib3JkZXI6MXB4IHNvbGlkIHJnYmEoMjQwLDE4MCw0MSwuMzgpOwogICAgYm9yZGVyLXJhZGl1czoyMnB4OwogICAgcGFkZGluZzo0cHggMTFweDsKICAgIGRpc3BsYXk6Z3JpZDsKICAgIGdyaWQtdGVtcGxhdGUtY29sdW1uczoxZnIgMWZyIDFmcjsKICAgIGdhcDo4cHg7CiAgICBib3gtc2hhZG93OjAgNHB4IDE2cHggcmdiYSgwLDAsMCwuNDUpLCBpbnNldCAwIDFweCAwIHJnYmEoMjU1LDI1NSwyNTUsLjA2KTsKICB9CiAgLmItc3BvdHsKICAgIGRpc3BsYXk6ZmxleDsKICAgIGZsZXgtZGlyZWN0aW9uOmNvbHVtbjsKICAgIGFsaWduLWl0ZW1zOmNlbnRlcjsKICAgIGp1c3RpZnktY29udGVudDpjZW50ZXI7CiAgICB0ZXh0LWFsaWduOmNlbnRlcjsKICAgIGJvcmRlci1yaWdodDoxcHggc29saWQgdmFyKC0tbGluZSk7CiAgICBwYWRkaW5nOjAgNHB4OwogIH0KICAuYi1zcG90Omxhc3QtY2hpbGR7Ym9yZGVyLXJpZ2h0OjB9CiAgLmItc3BvdCAubGJsewogICAgZm9udC13ZWlnaHQ6ODAwOwogICAgZm9udC1zaXplOjEyLjVweDsKICAgIGNvbG9yOnZhcigtLW11dGVkKTsKICAgIGxldHRlci1zcGFjaW5nOi4wNGVtOwogICAgbWFyZ2luLWJvdHRvbTozcHg7CiAgfQogIC5iLXNwb3QgLm1haW4tdmFsewogICAgZm9udC13ZWlnaHQ6OTAwOwogICAgZm9udC1zaXplOjIycHg7CiAgICBsaW5lLWhlaWdodDoxLjA1OwogICAgcGFkZGluZzoxcHggNnB4OwogICAgYm9yZGVyLXJhZGl1czo1cHg7CiAgICB0cmFuc2l0aW9uOmNvbG9yIC4xNXM7CiAgfQogIC5iLXNwb3QgLmhsLXN1YnsKICAgIGZvbnQtd2VpZ2h0OjcwMDsKICAgIGZvbnQtc2l6ZToxMS41cHg7CiAgICBjb2xvcjp2YXIoLS1tdXRlZCk7CiAgICBtYXJnaW4tdG9wOjNweDsKICAgIGxldHRlci1zcGFjaW5nOi4wMmVtOwogIH0KCiAgLmItdGJsLWhlYWR7CiAgICBiYWNrZ3JvdW5kOmxpbmVhci1ncmFkaWVudCg5MGRlZywjOWU3YjJmIDAlLCNjNzlmNDIgNTAlLCM5ZTdiMmYgMTAwJSk7CiAgICBjb2xvcjojMDkwYjEwOwogICAgZm9udC13ZWlnaHQ6OTAwOwogICAgZm9udC1zaXplOjEyLjVweDsKICAgIGxldHRlci1zcGFjaW5nOi4xMmVtOwogICAgcGFkZGluZzo1cHggMTZweDsKICAgIGRpc3BsYXk6Z3JpZDsKICAgIGdyaWQtdGVtcGxhdGUtY29sdW1uczoxLjFmciAxZnI7CiAgICB0ZXh0LXRyYW5zZm9ybTp1cHBlcmNhc2U7CiAgfQogIC5iLXRibC1oZWFkIHNwYW46bGFzdC1jaGlsZHt0ZXh0LWFsaWduOmNlbnRlcn0KCiAgLmItcm93c3tkaXNwbGF5OmZsZXg7ZmxleC1kaXJlY3Rpb246Y29sdW1ufQogIC5iLXJvd3sKICAgIGRpc3BsYXk6Z3JpZDsKICAgIGdyaWQtdGVtcGxhdGUtY29sdW1uczoxLjFmciAxZnI7CiAgICBhbGlnbi1pdGVtczpjZW50ZXI7CiAgICBwYWRkaW5nOjJweCAxNnB4OwogICAgYm9yZGVyLXRvcDoxcHggc29saWQgdmFyKC0tbGluZS0yKTsKICAgIGJhY2tncm91bmQ6cmdiYSgxMSwxMywxOCwuOSk7CiAgfQogIC5iLXJvdzpudGgtY2hpbGQoZXZlbil7YmFja2dyb3VuZDpyZ2JhKDE2LDIwLDI4LC43NSl9CiAgLmItcm93IC5iLW5hbWV7CiAgICBmb250LXdlaWdodDo4MDA7CiAgICBmb250LXNpemU6MTYuNXB4OwogICAgY29sb3I6dmFyKC0tZmcpOwogICAgbGV0dGVyLXNwYWNpbmc6LjAzZW07CiAgICBib3JkZXItcmlnaHQ6MXB4IHNvbGlkIHZhcigtLWxpbmUtMik7CiAgICBwYWRkaW5nLXJpZ2h0OjEwcHg7CiAgfQogIC5iLXJvdyAuYi1jZWxsewogICAgZGlzcGxheTpmbGV4OwogICAgZmxleC1kaXJlY3Rpb246Y29sdW1uOwogICAgYWxpZ24taXRlbXM6Y2VudGVyOwogICAganVzdGlmeS1jb250ZW50OmNlbnRlcjsKICAgIHRleHQtYWxpZ246Y2VudGVyOwogICAgcGFkZGluZy1sZWZ0OjEwcHg7CiAgfQogIC5iLXJvdyAuYi1wcmljZXsKICAgIGZvbnQtd2VpZ2h0OjkwMDsKICAgIGZvbnQtc2l6ZToyMHB4OwogICAgbGluZS1oZWlnaHQ6MS4wODsKICAgIHBhZGRpbmc6MXB4IDhweDsKICAgIGJvcmRlci1yYWRpdXM6NXB4OwogICAgdHJhbnNpdGlvbjpjb2xvciAuMTVzOwogIH0KICAuYi1yb3cgLmItaGx7CiAgICBmb250LXdlaWdodDo2MDA7CiAgICBmb250LXNpemU6MTEuNXB4OwogICAgY29sb3I6dmFyKC0tbXV0ZWQpOwogICAgbWFyZ2luLXRvcDoycHg7CiAgfQogIC52YWwtdXB7Y29sb3I6dmFyKC0tdXApfQogIC52YWwtZG93bntjb2xvcjp2YXIoLS1kb3duKX0KICAudmFsLW5ldXRyYWx7Y29sb3I6I2ZmZn0KCiAgLyogRGlyZWN0IGNyb3BwZWQgY2xhc3NpY2J1bGxpb24uY29tIGlmcmFtZSBtb2RlICovCiAgLmNiLXNpdGUtd3JhcHsKICAgIGRpc3BsYXk6bm9uZTsKICAgIHdpZHRoOjEwMCU7CiAgICBoZWlnaHQ6MzcycHg7CiAgICBwb3NpdGlvbjpyZWxhdGl2ZTsKICAgIG92ZXJmbG93OmhpZGRlbjsKICAgIGJhY2tncm91bmQ6IzExMTsKICB9CiAgLmNiLXNpdGUtd3JhcCBpZnJhbWV7CiAgICBwb3NpdGlvbjphYnNvbHV0ZTsKICAgIHdpZHRoOjEzNjBweDsKICAgIGhlaWdodDo5MjBweDsKICAgIGxlZnQ6NTAlOwogICAgdG9wOi0yOTVweDsKICAgIHRyYW5zZm9ybTp0cmFuc2xhdGVYKC01MCUpIHNjYWxlKDAuOTUpOwogICAgdHJhbnNmb3JtLW9yaWdpbjp0b3AgY2VudGVyOwogICAgYm9yZGVyOjA7CiAgfQoKICAvKiBtYWNybyAqLwogIC5tYWNyb3tmbGV4OjE7bWluLWhlaWdodDowfQogIC5tYWNyb3tmbGV4OjEuMzttaW4taGVpZ2h0OjB9CiAgLm1oZWFke2Rpc3BsYXk6Z3JpZDtncmlkLXRlbXBsYXRlLWNvbHVtbnM6MWZyIDc4cHggODhweDtnYXA6OHB4O3BhZGRpbmc6OHB4IDE4cHg7Y29sb3I6dmFyKC0tZmFpbnQpO2ZvbnQtd2VpZ2h0OjcwMDtmb250LXNpemU6MTNweDtsZXR0ZXItc3BhY2luZzouMDllbX0KICAubWhlYWQgLnJ7dGV4dC1hbGlnbjpyaWdodH0KICAubWJvZHl7b3ZlcmZsb3c6aGlkZGVuO2ZsZXg6MTttaW4taGVpZ2h0OjA7ZGlzcGxheTpmbGV4O2ZsZXgtZGlyZWN0aW9uOmNvbHVtbjtqdXN0aWZ5LWNvbnRlbnQ6c3BhY2UtYXJvdW5kfQogIC5tcm93e2Rpc3BsYXk6Z3JpZDtncmlkLXRlbXBsYXRlLWNvbHVtbnM6MWZyIDc4cHggODhweDtnYXA6OHB4O2FsaWduLWl0ZW1zOmNlbnRlcjtwYWRkaW5nOjdweCAxOHB4O2JvcmRlci10b3A6MXB4IHNvbGlkIHZhcigtLWxpbmUtMil9CiAgLm1yb3cgLmV2e2Rpc3BsYXk6ZmxleDthbGlnbi1pdGVtczpmbGV4LXN0YXJ0O2dhcDoxMXB4O21pbi13aWR0aDowfQogIC5tcm93IC5wci1kb3R7d2lkdGg6MTFweDtoZWlnaHQ6MTFweDtib3JkZXItcmFkaXVzOjUwJTtiYWNrZ3JvdW5kOnZhcigtLWRvd24pO21hcmdpbi10b3A6NnB4O2ZsZXg6MCAwIGF1dG99CiAgLm1yb3cgLmV2dHttaW4td2lkdGg6MH0KICAubXJvdyAudDF7Zm9udC13ZWlnaHQ6NzAwO2ZvbnQtc2l6ZToxMy41cHg7Y29sb3I6dmFyKC0tbXV0ZWQpfQogIC5tcm93IC50MSBie2NvbG9yOnZhcigtLWFjY2VudCk7Zm9udC13ZWlnaHQ6ODAwO2xldHRlci1zcGFjaW5nOi4wNGVtfQogIC5tcm93IC50Mntmb250LXdlaWdodDo4MDA7Zm9udC1zaXplOjE2LjVweDtjb2xvcjp2YXIoLS1mZyk7d2hpdGUtc3BhY2U6bm93cmFwO292ZXJmbG93OmhpZGRlbjt0ZXh0LW92ZXJmbG93OmVsbGlwc2lzfQogIC5tcm93IC5he3RleHQtYWxpZ246cmlnaHQ7Zm9udC13ZWlnaHQ6ODAwO2ZvbnQtc2l6ZToxOHB4O2NvbG9yOnZhcigtLW11dGVkKX0KICAubXJvdy5kb25lIC5he2NvbG9yOnZhcigtLXVwKX0KICAubXJvdyAuZnt0ZXh0LWFsaWduOnJpZ2h0O2ZvbnQtd2VpZ2h0OjgwMDtmb250LXNpemU6MThweDtjb2xvcjp2YXIoLS1tdXRlZCl9CgogIC8qIGNlbnRyYWwgYmFuayByYXRlcyAqLwogIC5jYnJhdGVze2ZsZXg6MTttaW4taGVpZ2h0OjB9CiAgLmNici1ncmlke2Rpc3BsYXk6Z3JpZDtncmlkLXRlbXBsYXRlLWNvbHVtbnM6MWZyIDFmcjtnYXA6MCAxMnB4O3BhZGRpbmc6MnB4IDE2cHggOHB4O2ZsZXg6MTttaW4taGVpZ2h0OjA7YWxpZ24tY29udGVudDpzcGFjZS1hcm91bmR9CiAgLmNicntkaXNwbGF5OmZsZXg7YWxpZ24taXRlbXM6Y2VudGVyO2p1c3RpZnktY29udGVudDpzcGFjZS1iZXR3ZWVuO3BhZGRpbmc6NnB4IDJweDtib3JkZXItdG9wOjFweCBzb2xpZCB2YXIoLS1saW5lLTIpfQogIC5jYnItZ3JpZCAuY2JyOm50aC1jaGlsZCgtbisyKXtib3JkZXItdG9wOjB9CiAgLmNiciAuY3tmb250LXdlaWdodDo4MDA7Zm9udC1zaXplOjE3cHg7bGV0dGVyLXNwYWNpbmc6LjA0ZW07Y29sb3I6dmFyKC0tZmcpfQogIC5jYnIgLnJ7Zm9udC13ZWlnaHQ6ODAwO2ZvbnQtc2l6ZToxN3B4O2NvbG9yOnZhcigtLXVwKTtiYWNrZ3JvdW5kOnZhcigtLXVwLWJnKTtwYWRkaW5nOjRweCAxMnB4O2JvcmRlci1yYWRpdXM6N3B4O21pbi13aWR0aDo3MHB4O3RleHQtYWxpZ246cmlnaHR9CiAgLmNiciAuci5sb3d7Y29sb3I6dmFyKC0tbXV0ZWQpO2JhY2tncm91bmQ6IzE0MTkyMn0KICAuZmxhZ3t3aWR0aDoyNnB4O2hlaWdodDoxOHB4O2JvcmRlci1yYWRpdXM6M3B4O29iamVjdC1maXQ6Y292ZXI7dmVydGljYWwtYWxpZ246bWlkZGxlO21hcmdpbi1yaWdodDo5cHg7Ym94LXNoYWRvdzowIDAgMCAxcHggcmdiYSgyNTUsMjU1LDI1NSwuMTIpfQogIC5tcm93IC50MSAuZmxhZ3t3aWR0aDoyMHB4O2hlaWdodDoxNHB4O21hcmdpbi1yaWdodDo2cHg7bWFyZ2luLWxlZnQ6MnB4fQoKICBAa2V5ZnJhbWVzIHB1bHNlezAlLDEwMCV7b3BhY2l0eToxfTUwJXtvcGFjaXR5Oi4zfX0KICAuZGlte2NvbG9yOnZhcigtLWZhaW50KX0KCiAgLyogYm90dG9tIGxpdmUtbmV3cyBzdHJpcCAoZmFzdGVyIHNjcm9sbCArIGltYWdlcykgKi8KICAubmV3c2JhcntiYWNrZ3JvdW5kOnZhcigtLXBhbmVsKTtib3JkZXI6MXB4IHNvbGlkIHZhcigtLWxpbmUpO2JvcmRlci1yYWRpdXM6MTRweDtwb3NpdGlvbjpyZWxhdGl2ZTtvdmVyZmxvdzpoaWRkZW59CiAgLm5ld3NiYXI6OmFmdGVye2NvbnRlbnQ6IiI7cG9zaXRpb246YWJzb2x1dGU7dG9wOjA7Ym90dG9tOjA7cmlnaHQ6MDt3aWR0aDo3MHB4O3otaW5kZXg6MztiYWNrZ3JvdW5kOmxpbmVhci1ncmFkaWVudCgyNzBkZWcsdmFyKC0tcGFuZWwpLHRyYW5zcGFyZW50KTtwb2ludGVyLWV2ZW50czpub25lfQogIC5uZXdzYmFyIC5sYmx7cG9zaXRpb246YWJzb2x1dGU7dG9wOjA7Ym90dG9tOjA7bGVmdDowO3otaW5kZXg6NDtkaXNwbGF5OmZsZXg7YWxpZ24taXRlbXM6Y2VudGVyO2dhcDo5cHg7cGFkZGluZzowIDIwcHggMCAxOHB4O2JhY2tncm91bmQ6bGluZWFyLWdyYWRpZW50KDkwZGVnLHZhcigtLXBhbmVsKSA3OCUsdHJhbnNwYXJlbnQpO2ZvbnQtd2VpZ2h0OjgwMDtmb250LXNpemU6MTNweDtsZXR0ZXItc3BhY2luZzouMTJlbTtjb2xvcjp2YXIoLS1saXZlKX0KICAubmV3c2JhciAubGJsIC5ke3dpZHRoOjlweDtoZWlnaHQ6OXB4O2JvcmRlci1yYWRpdXM6NTAlO2JhY2tncm91bmQ6dmFyKC0tbGl2ZSk7Ym94LXNoYWRvdzowIDAgOXB4IHZhcigtLWxpdmUpO2FuaW1hdGlvbjpwdWxzZSAxLjRzIGluZmluaXRlfQogIC5uZXdzLXRyYWNre2Rpc3BsYXk6ZmxleDtnYXA6MTJweDthbGlnbi1pdGVtczpjZW50ZXI7aGVpZ2h0OjEwMCU7cGFkZGluZzowIDE4cHggMCAxNTBweDt3aWR0aDptYXgtY29udGVudDt3aWxsLWNoYW5nZTp0cmFuc2Zvcm19CiAgLm5ld3MtdHJhY2sucnVue2FuaW1hdGlvbjpubWFycSB2YXIoLS1uZHVyLDcwcykgbGluZWFyIGluZmluaXRlfQogIC5uY2FyZHtmbGV4OjAgMCA0ODBweDtkaXNwbGF5OmZsZXg7Z2FwOjEzcHg7YmFja2dyb3VuZDp2YXIoLS1wYW5lbC0yKTtib3JkZXI6MXB4IHNvbGlkIHZhcigtLWxpbmUtMik7Ym9yZGVyLXJhZGl1czoxMXB4O3BhZGRpbmc6OXB4O2hlaWdodDoxMDRweDthbGlnbi1pdGVtczpjZW50ZXJ9CiAgLm5jYXJkIC5uaW1ne2ZsZXg6MCAwIDEzNHB4O2hlaWdodDoxMDAlO2JvcmRlci1yYWRpdXM6OHB4O292ZXJmbG93OmhpZGRlbjtiYWNrZ3JvdW5kOmxpbmVhci1ncmFkaWVudCgxMzVkZWcsIzFjMjYzNCwjMGUxNDIwKTtkaXNwbGF5OmZsZXg7YWxpZ24taXRlbXM6Y2VudGVyO2p1c3RpZnktY29udGVudDpjZW50ZXJ9CiAgLm5jYXJkIC5uaW1nIGltZ3t3aWR0aDoxMDAlO2hlaWdodDoxMDAlO29iamVjdC1maXQ6Y292ZXJ9CiAgLm5jYXJkIC5uaW1nIC5waHtmb250LXdlaWdodDo4MDA7Zm9udC1zaXplOjIycHg7Y29sb3I6dmFyKC0tZmFpbnQpfQogIC5uY2FyZCAubmJ7bWluLXdpZHRoOjA7ZGlzcGxheTpmbGV4O2ZsZXgtZGlyZWN0aW9uOmNvbHVtbjtnYXA6NXB4fQogIC5uY2FyZCAubnN7Zm9udC13ZWlnaHQ6ODAwO2ZvbnQtc2l6ZToxMnB4O2NvbG9yOnZhcigtLWFjY2VudCk7dGV4dC10cmFuc2Zvcm06dXBwZXJjYXNlO2xldHRlci1zcGFjaW5nOi4wNmVtfQogIC5uY2FyZCAubnMgLnRte2NvbG9yOnZhcigtLWZhaW50KX0KICAubmNhcmQgLm50e2ZvbnQtd2VpZ2h0OjcwMDtmb250LXNpemU6MTZweDtsaW5lLWhlaWdodDoxLjM7ZGlzcGxheTotd2Via2l0LWJveDstd2Via2l0LWxpbmUtY2xhbXA6Mjstd2Via2l0LWJveC1vcmllbnQ6dmVydGljYWw7b3ZlcmZsb3c6aGlkZGVufQogIEBrZXlmcmFtZXMgbm1hcnF7ZnJvbXt0cmFuc2Zvcm06dHJhbnNsYXRlWCgwKX10b3t0cmFuc2Zvcm06dHJhbnNsYXRlWCgtNTAlKX19Cjwvc3R5bGU+CjwvaGVhZD4KPGJvZHk+CjxkaXYgY2xhc3M9ImFwcCI+CgogIDwhLS0gSEVBREVSIC0tPgogIDxoZWFkZXIgY2xhc3M9ImhlYWQiPgogICAgPGRpdiBjbGFzcz0iaHNlc3MiIGlkPSJzZXNzIj48L2Rpdj4KICAgIDxkaXYgc3R5bGU9ImRpc3BsYXk6ZmxleDthbGlnbi1pdGVtczpjZW50ZXI7Z2FwOjE4cHgiPgogICAgICA8c3BhbiBjbGFzcz0iaGxpdmUgc3RhbGUiIGlkPSJobGl2ZSI+PHNwYW4gY2xhc3M9ImRvdCI+PC9zcGFuPjxzcGFuIGlkPSJ1cGR0eHQiPkNPTk5FQ1RJTkc8L3NwYW4+PC9zcGFuPgogICAgICA8ZGl2IGNsYXNzPSJoY2xvY2siIGlkPSJjbG9jayI+4oCUPC9kaXY+CiAgICA8L2Rpdj4KICA8L2hlYWRlcj4KCiAgPCEtLSBUSUNLRVIgKHNjcm9sbGluZyBtYXJxdWVlKSAtLT4KICA8ZGl2IGNsYXNzPSJ0aWNrIj48ZGl2IGNsYXNzPSJ0aWNrLXRyYWNrIiBpZD0idGlja1RyYWNrIj48L2Rpdj48L2Rpdj4KCiAgPCEtLSBNQUlOIC0tPgogIDxkaXYgY2xhc3M9Im1haW4iPgoKICAgIDwhLS0gQ09MIDE6IHZpZGVvICsgY2hhcnRzIC0tPgogICAgPGRpdiBjbGFzcz0iY29sIj4KICAgICAgPGRpdiBjbGFzcz0idmlkIj4KICAgICAgICA8ZGl2IGNsYXNzPSJ2dGwiPjxzcGFuIGNsYXNzPSJ5dCI+JiM5NjU0OyBZb3VUdWJlIExJVkU8L3NwYW4+ICZidWxsOyBCbG9vbWJlcmcgQnVzaW5lc3MgTmV3czwvZGl2PgogICAgICAgIDxkaXYgY2xhc3M9InZ0ciI+PHNwYW4gY2xhc3M9ImQiPjwvc3Bhbj5MSVZFPC9kaXY+CiAgICAgICAgPGRpdiBjbGFzcz0idmJyIj5CTE9PTUJFUkcgPGI+TElWRTwvYj48L2Rpdj4KICAgICAgICA8aWZyYW1lIGlkPSJ0diIgc3JjPSJodHRwczovL3d3dy55b3V0dWJlLW5vY29va2llLmNvbS9lbWJlZC9saXZlX3N0cmVhbT9jaGFubmVsPVVDSUFMTUt2T2JaTnRKNkFtZENMUDdMZyZhdXRvcGxheT0xJm11dGU9MSZwbGF5c2lubGluZT0xJnJlbD0wIgogICAgICAgICAgYWxsb3c9ImF1dG9wbGF5OyBlbmNyeXB0ZWQtbWVkaWE7IHBpY3R1cmUtaW4tcGljdHVyZSIgYWxsb3dmdWxsc2NyZWVuIHJlZmVycmVycG9saWN5PSJvcmlnaW4td2hlbi1jcm9zcy1vcmlnaW4iPjwvaWZyYW1lPgogICAgICAgIDxkaXYgY2xhc3M9ImNoeXJvbiI+CiAgICAgICAgICA8ZGl2IGNsYXNzPSJiayI+PGRpdiBjbGFzcz0idGFnIj5CUkVBS0lORyBORVdTPC9kaXY+PGRpdiBjbGFzcz0ic2Nyb2xsd3JhcCI+PGRpdiBjbGFzcz0ic2Nyb2xsIiBpZD0iY2h5U2Nyb2xsIj48L2Rpdj48L2Rpdj48L2Rpdj4KICAgICAgICA8L2Rpdj4KICAgICAgPC9kaXY+CiAgICA8L2Rpdj4KCiAgICA8IS0tIENPTCAyOiBtYWNybyArIGNlbnRyYWwgYmFuayByYXRlcyAtLT4KICAgIDxkaXYgY2xhc3M9ImNvbCI+CiAgICAgIDxkaXYgY2xhc3M9InBhbmVsIG1hY3JvIiBzdHlsZT0iZmxleDoxLjI1Ij4KICAgICAgICA8ZGl2IGNsYXNzPSJwdGl0bGUiPk1BQ1JPIEVDT05PTUlDIENBTEVOREFSPC9kaXY+CiAgICAgICAgPGRpdiBjbGFzcz0ibWhlYWQiPjxzcGFuPlBSSU9SSVRZPC9zcGFuPjxzcGFuIGNsYXNzPSJyIj5BQ1RVQUw8L3NwYW4+PHNwYW4gY2xhc3M9InIiPkZPUkVDQVNUPC9zcGFuPjwvZGl2PgogICAgICAgIDxkaXYgY2xhc3M9Im1ib2R5IiBpZD0ibWFjcm8iPjwvZGl2PgogICAgICA8L2Rpdj4KICAgICAgPGRpdiBjbGFzcz0icGFuZWwgY2JyYXRlcyIgc3R5bGU9ImZsZXg6MSI+CiAgICAgICAgPGRpdiBjbGFzcz0icHRpdGxlIj5DRU5UUkFMIEJBTksgUkFURVM8L2Rpdj4KICAgICAgICA8ZGl2IGNsYXNzPSJjYnItZ3JpZCIgaWQ9ImNickdyaWQiPjwvZGl2PgogICAgICA8L2Rpdj4KICAgIDwvZGl2PgoKICAgIDwhLS0gQ09MIDM6IENMQVNTSUMgQlVMTElPTiArIEFTSUEgU1RPQ0tTIC0tPgogICAgPGRpdiBjbGFzcz0iY29sIj4KICAgICAgPGRpdiBjbGFzcz0iYnVsbGlvbi1wYW5lbCI+CiAgICAgICAgPGRpdiBjbGFzcz0icHRpdGxlIj4KICAgICAgICAgIDxzcGFuPkNMQVNTSUMgQlVMTElPTiAmbWlkZG90OyBMSVZFIFJBVEVTPC9zcGFuPgogICAgICAgICAgPGRpdiBzdHlsZT0iZGlzcGxheTpmbGV4O2FsaWduLWl0ZW1zOmNlbnRlcjtnYXA6OHB4Ij4KICAgICAgICAgICAgPHNwYW4gY2xhc3M9InBzdWIiPjxzcGFuIGNsYXNzPSJiZG90Ij48L3NwYW4+PHNwYW4gaWQ9ImNiU3RhdHVzIj5MSVZFIFNZTkM8L3NwYW4+PC9zcGFuPgogICAgICAgICAgICA8YnV0dG9uIGNsYXNzPSJtb2RlLWJ0biBhY3RpdmUiIGlkPSJidG5DYkRhcmsiIG9uY2xpY2s9InNldENiVmlldygnZGFyaycpIj5EQVJLIFVJPC9idXR0b24+CiAgICAgICAgICAgIDxidXR0b24gY2xhc3M9Im1vZGUtYnRuIiBpZD0iYnRuQ2JTaXRlIiBvbmNsaWNrPSJzZXRDYlZpZXcoJ3NpdGUnKSI+T1JJR0lOQUwgU0lURTwvYnV0dG9uPgogICAgICAgICAgPC9kaXY+CiAgICAgICAgPC9kaXY+CgogICAgICAgIDwhLS0gTU9ERSAxOiBEQVJLIFVJIFBBTkVMICgxMDAlIENBTElCUkFURUQgKyBESVJFQ1QgUFJPWFkgQlJJREdFKSAtLT4KICAgICAgICA8ZGl2IGlkPSJjYkRhcmtCb3giPgogICAgICAgICAgPGRpdiBjbGFzcz0iYi10b3AtcGlsbCI+CiAgICAgICAgICAgIDxkaXYgY2xhc3M9ImItc3BvdCI+CiAgICAgICAgICAgICAgPGRpdiBjbGFzcz0ibGJsIj5HT0xEIE96IEJpZDwvZGl2PgogICAgICAgICAgICAgIDxkaXYgY2xhc3M9Im1haW4tdmFsIHZhbC1uZXV0cmFsIiBpZD0iY2ItYmlkIj4zOTAwLjU1PC9kaXY+CiAgICAgICAgICAgICAgPGRpdiBjbGFzcz0iaGwtc3ViIiBpZD0iY2ItYmlkLWgiPkg6IDM5MjQuMDQ8L2Rpdj4KICAgICAgICAgICAgPC9kaXY+CiAgICAgICAgICAgIDxkaXYgY2xhc3M9ImItc3BvdCI+CiAgICAgICAgICAgICAgPGRpdiBjbGFzcz0ibGJsIj5HT0xEIE96IEFzazwvZGl2PgogICAgICAgICAgICAgIDxkaXYgY2xhc3M9Im1haW4tdmFsIHZhbC1uZXV0cmFsIiBpZD0iY2ItYXNrIj4zOTAyLjA1PC9kaXY+CiAgICAgICAgICAgICAgPGRpdiBjbGFzcz0iaGwtc3ViIiBpZD0iY2ItYXNrLWwiPkw6IDM4NzMuMTI8L2Rpdj4KICAgICAgICAgICAgPC9kaXY+CiAgICAgICAgICAgIDxkaXYgY2xhc3M9ImItc3BvdCI+CiAgICAgICAgICAgICAgPGRpdiBjbGFzcz0ibGJsIj5VU0RJTlIgQXNrPC9kaXY+CiAgICAgICAgICAgICAgPGRpdiBjbGFzcz0ibWFpbi12YWwgdmFsLW5ldXRyYWwiIGlkPSJjYi1pbnIiPjk0LjMxMzwvZGl2PgogICAgICAgICAgICAgIDxkaXYgY2xhc3M9ImhsLXN1YiIgaWQ9ImNiLWluci1obCI+SDogOTQuMzQ5IHwgTDogOTQuMTE4PC9kaXY+CiAgICAgICAgICAgIDwvZGl2PgogICAgICAgICAgPC9kaXY+CgogICAgICAgICAgPGRpdiBjbGFzcz0iYi10YmwtaGVhZCI+CiAgICAgICAgICAgIDxzcGFuPkJVTExJT048L3NwYW4+CiAgICAgICAgICAgIDxzcGFuPlBSSUNFPC9zcGFuPgogICAgICAgICAgPC9kaXY+CgogICAgICAgICAgPGRpdiBjbGFzcz0iYi1yb3dzIj4KICAgICAgICAgICAgPGRpdiBjbGFzcz0iYi1yb3ciPgogICAgICAgICAgICAgIDxkaXYgY2xhc3M9ImItbmFtZSI+U0lMVkVSIE96PC9kaXY+CiAgICAgICAgICAgICAgPGRpdiBjbGFzcz0iYi1jZWxsIj4KICAgICAgICAgICAgICAgIDxkaXYgY2xhc3M9ImItcHJpY2UgdmFsLW5ldXRyYWwiIGlkPSJjYi1zaWwiPjUyLjA5PC9kaXY+CiAgICAgICAgICAgICAgICA8ZGl2IGNsYXNzPSJiLWhsIiBpZD0iY2Itc2lsLWhsIj5IOiA1Mi44MSB8IEw6IDUxLjIxPC9kaXY+CiAgICAgICAgICAgICAgPC9kaXY+CiAgICAgICAgICAgIDwvZGl2PgogICAgICAgICAgICA8ZGl2IGNsYXNzPSJiLXJvdyI+CiAgICAgICAgICAgICAgPGRpdiBjbGFzcz0iYi1uYW1lIj5HT0xEIDEgR008L2Rpdj4KICAgICAgICAgICAgICA8ZGl2IGNsYXNzPSJiLWNlbGwiPgogICAgICAgICAgICAgICAgPGRpdiBjbGFzcz0iYi1wcmljZSB2YWwtbmV1dHJhbCIgaWQ9ImNiLTFnbSI+NDYzLjE4PC9kaXY+CiAgICAgICAgICAgICAgICA8ZGl2IGNsYXNzPSJiLWhsIiBpZD0iY2ItMWdtLWhsIj5IOiA0NjUuNzggfCBMOiA0NTkuNzc8L2Rpdj4KICAgICAgICAgICAgICA8L2Rpdj4KICAgICAgICAgICAgPC9kaXY+CiAgICAgICAgICAgIDxkaXYgY2xhc3M9ImItcm93Ij4KICAgICAgICAgICAgICA8ZGl2IGNsYXNzPSJiLW5hbWUiPkdPTEQgOTk5OTwvZGl2PgogICAgICAgICAgICAgIDxkaXYgY2xhc3M9ImItY2VsbCI+CiAgICAgICAgICAgICAgICA8ZGl2IGNsYXNzPSJiLXByaWNlIHZhbC1uZXV0cmFsIiBpZD0iY2ItOTk5OSI+NDYxOTYzPC9kaXY+CiAgICAgICAgICAgICAgICA8ZGl2IGNsYXNzPSJiLWhsIiBpZD0iY2ItOTk5OS1obCI+SDogNDY0NTYwIHwgTDogNDU4NTQ2PC9kaXY+CiAgICAgICAgICAgICAgPC9kaXY+CiAgICAgICAgICAgIDwvZGl2PgogICAgICAgICAgPC9kaXY+CiAgICAgICAgPC9kaXY+CgogICAgICAgIDwhLS0gTU9ERSAyOiBESVJFQ1QgQ1JPUFBFRCBDTEFTU0lDQlVMTElPTi5DT00gTElWRSBFTUJFRCAtLT4KICAgICAgICA8ZGl2IGNsYXNzPSJjYi1zaXRlLXdyYXAiIGlkPSJjYlNpdGVCb3giPgogICAgICAgICAgPGlmcmFtZSBpZD0iY2JEaXJlY3RJZnJhbWUiIHNyYz0iL3Byb3h5L2NsYXNzaWNidWxsaW9uIj48L2lmcmFtZT4KICAgICAgICA8L2Rpdj4KICAgICAgPC9kaXY+CgogICAgICA8ZGl2IGNsYXNzPSJwYW5lbCBhc2lhIiBzdHlsZT0iZmxleDoxIj4KICAgICAgICA8ZGl2IGNsYXNzPSJwdGl0bGUiPkFTSUEgU1RPQ0tTIElOVFJBREFZPC9kaXY+CiAgICAgICAgPGRpdiBpZD0iYXNpYSIgc3R5bGU9ImRpc3BsYXk6ZmxleDtmbGV4LWRpcmVjdGlvbjpjb2x1bW47ZmxleDoxO21pbi1oZWlnaHQ6MCI+PC9kaXY+CiAgICAgIDwvZGl2PgogICAgPC9kaXY+CiAgPC9kaXY+CgogIDwhLS0gQk9UVE9NIExJVkUgTkVXUyBTVFJJUCAtLT4KICA8ZGl2IGNsYXNzPSJuZXdzYmFyIj4KICAgIDxkaXYgY2xhc3M9ImxibCI+PHNwYW4gY2xhc3M9ImQiPjwvc3Bhbj5MSVZFIE5FV1M8L2Rpdj4KICAgIDxkaXYgY2xhc3M9Im5ld3MtdHJhY2siIGlkPSJuZXdzU3RyaXAiPjwvZGl2PgogIDwvZGl2Pgo8L2Rpdj4KCjxzY3JpcHQ+Ci8qIC0tLS0tLS0tLS0tLS0tLS0gaGVscGVycyAtLS0tLS0tLS0tLS0tLS0tICovCnZhciAkPWZ1bmN0aW9uKGlkKXtyZXR1cm4gZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoaWQpfTsKZnVuY3Rpb24gZGVjKHN5bSx2KXsgaWYoc3ltPT09J0RYLVkuTllCJylyZXR1cm4gMzsgaWYoc3ltPT09J0JUQy1VU0QnKXJldHVybiAodj49MTAwMD8wOjIpOyBpZihzeW09PT0nR0M9Rid8fHN5bT09PSdDTD1GJylyZXR1cm4gMjsgaWYodj49MTAwMClyZXR1cm4gMjsgcmV0dXJuIDI7IH0KZnVuY3Rpb24gZm10KHYsZCl7IGlmKHY9PW51bGx8fGlzTmFOKHYpKXJldHVybiAn4oCUJzsgcmV0dXJuIE51bWJlcih2KS50b0xvY2FsZVN0cmluZygnZW4tVVMnLHttaW5pbXVtRnJhY3Rpb25EaWdpdHM6ZCxtYXhpbXVtRnJhY3Rpb25EaWdpdHM6ZH0pOyB9CmZ1bmN0aW9uIHBjdFR4dChwKXsgaWYocD09bnVsbHx8aXNOYU4ocCkpcmV0dXJuICcnOyByZXR1cm4gKHA+PTA/JysnOicnKStwLnRvRml4ZWQoMikrJyUnOyB9CmZ1bmN0aW9uIGFycm93KHApeyByZXR1cm4gcD49MD8n4payJzon4pa8JzsgfQpmdW5jdGlvbiBlc2Mocyl7dmFyIGQ9ZG9jdW1lbnQuY3JlYXRlRWxlbWVudCgnZGl2Jyk7ZC50ZXh0Q29udGVudD1zfHwnJztyZXR1cm4gZC5pbm5lckhUTUw7fQoKZnVuY3Rpb24gc2V0Q2JWaWV3KG1vZGUpewogICQoJ2NiRGFya0JveCcpLnN0eWxlLmRpc3BsYXkgPSAobW9kZT09PSdkYXJrJykgPyAnYmxvY2snIDogJ25vbmUnOwogICQoJ2NiU2l0ZUJveCcpLnN0eWxlLmRpc3BsYXkgPSAobW9kZT09PSdzaXRlJykgPyAnYmxvY2snIDogJ25vbmUnOwogICQoJ2J0bkNiRGFyaycpLmNsYXNzTmFtZSA9ICdtb2RlLWJ0bicgKyAobW9kZT09PSdkYXJrJyA/ICcgYWN0aXZlJyA6ICcnKTsKICAkKCdidG5DYlNpdGUnKS5jbGFzc05hbWUgPSAnbW9kZS1idG4nICsgKG1vZGU9PT0nc2l0ZScgPyAnIGFjdGl2ZScgOiAnJyk7Cn0KCi8qIC0tLS0tLS0tLS0tLS0tLS0gY2xvY2sgKyBzZXNzaW9ucyAtLS0tLS0tLS0tLS0tLS0tICovCnZhciBDSVRZPVtbJ05ZJywnQW1lcmljYS9OZXdfWW9yayddLFsnTEROJywnRXVyb3BlL0xvbmRvbiddLFsnVFlPJywnQXNpYS9Ub2t5byddLFsnU1lEJywnQXVzdHJhbGlhL1N5ZG5leSddXTsKZnVuY3Rpb24gY2xvY2soKXsKICB2YXIgbj1uZXcgRGF0ZSgpOwogICQoJ2Nsb2NrJykuaW5uZXJIVE1MPSc8Yj4nK24udG9Mb2NhbGVUaW1lU3RyaW5nKCdlbi1VUycse2hvdXI6JzItZGlnaXQnLG1pbnV0ZTonMi1kaWdpdCcsc2Vjb25kOicyLWRpZ2l0J30pKyc8L2I+IC8gJytuLnRvTG9jYWxlRGF0ZVN0cmluZygnZW4tR0InLHt3ZWVrZGF5OidzaG9ydCcsZGF5OicyLWRpZ2l0Jyxtb250aDonc2hvcnQnLHllYXI6J251bWVyaWMnfSkudG9VcHBlckNhc2UoKTsKICAkKCdzZXNzJykuaW5uZXJIVE1MPUNJVFkubWFwKGZ1bmN0aW9uKGMpewogICAgdmFyIHQ9bmV3IERhdGUoKS50b0xvY2FsZVRpbWVTdHJpbmcoJ2VuLUdCJyx7dGltZVpvbmU6Y1sxXSxob3VyOicyLWRpZ2l0JyxtaW51dGU6JzItZGlnaXQnfSk7CiAgICByZXR1cm4gJzxzcGFuPicrY1swXSsnPGI+Jyt0Kyc8L2I+PC9zcGFuPic7CiAgfSkuam9pbignJyk7CiAgdmFyIGhsPSQoJ2hsaXZlJyksdXQ9JCgndXBkdHh0Jyk7CiAgaWYoaGwmJnV0KXsKICAgIGlmKGxhc3RVcGQmJihEYXRlLm5vdygpLWxhc3RVcGQpPDYwMDAwKXtobC5jbGFzc05hbWU9J2hsaXZlJzt1dC50ZXh0Q29udGVudD0nTElWRSDCtyAnK25ldyBEYXRlKGxhc3RVcGQpLnRvTG9jYWxlVGltZVN0cmluZygnZW4tVVMnLHtob3VyMTI6ZmFsc2V9KTt9CiAgICBlbHNle2hsLmNsYXNzTmFtZT0naGxpdmUgc3RhbGUnO3V0LnRleHRDb250ZW50PWxhc3RVcGQ/J1JFQ09OTkVDVElORyc6J0NPTk5FQ1RJTkcnO30KICB9Cn0KY2xvY2soKTtzZXRJbnRlcnZhbChjbG9jaywxMDAwKTsKCi8qIC0tLS0tLS0tLS0tLS0tLS0gcXVvdGVzIC0tLS0tLS0tLS0tLS0tLS0gKi8KdmFyIFE9e30sIFBSRVY9e30sIGxhc3RVcGQ9MDsKZnVuY3Rpb24gY2hDbHMocyxwcmljZSl7dmFyIHA9UFJFVltzXTtpZihwPT1udWxsfHxwcmljZT09bnVsbHx8cHJpY2U9PT1wKXJldHVybiAnJztyZXR1cm4gcHJpY2U+cD8nIGZsdXAnOicgZmxkbic7fQp2YXIgVElDS0VSPVsnXkdTUEMnLCdeTkRYJywnRFgtWS5OWUInLCdHQz1GJywnQ0w9RicsJ0JUQy1VU0QnXTsKdmFyIFROQU1FPXsnXkdTUEMnOidTJlAgNTAwJywnXk5EWCc6J05BU0RBUSAxMDAnLCdEWC1ZLk5ZQic6J0RYWScsJ0dDPUYnOidHT0xEJywnQ0w9Ric6J1dUSScsJ0JUQy1VU0QnOidCVENVU0QnfTsKdmFyIEFTSUE9WydBQVhKJywnXk4yMjUnLCdeS1MxMScsJ15UV0lJJywnXkhTSScsJ15HU1BDJywnXlJVVCddOwp2YXIgQU5BTUU9eydBQVhKJzonTVNDSSBBQyBBU0lBIFBBQycsJ15OMjI1JzonTklLS0VJIDIyNScsJ15LUzExJzonS09TUEknLCdeVFdJSSc6J1RBSVBFSSBUV1NFJywnXkhTSSc6J0hBTkcgU0VORycsJ15HU1BDJzonUyZQIDUwMCcsJ15SVVQnOidSVVNTRUxMIDIwMDAnfTsKCmZ1bmN0aW9uIHRrSXRlbShzKXsKICB2YXIgcT1RW3NdfHx7fTt2YXIgdXA9KHEucGN0fHwwKT49MDsKICByZXR1cm4gJzxkaXYgY2xhc3M9InRrIj48c3BhbiBjbGFzcz0ibm0gJysodXA/J3VwJzonZG93bicpKyciIGRhdGEtbm09IicrcysnIj4nK1ROQU1FW3NdKyc8L3NwYW4+JysKICAgICc8c3BhbiBjbGFzcz0icHIiIGRhdGEtcHI9IicrcysnIj4nK2ZtdChxLnByaWNlLGRlYyhzLHEucHJpY2UpKSsnPC9zcGFuPicrCiAgICAnPHNwYW4gY2xhc3M9ImNoICcrKHVwPyd1cCc6J2Rvd24nKSsnIiBkYXRhLWNoPSInK3MrJyI+JythcnJvdyhxLnBjdHx8MCkrJyAnK3BjdFR4dChxLnBjdCkrJzwvc3Bhbj48L2Rpdj4nOwp9CmZ1bmN0aW9uIGJ1aWxkVGlja2VyKCl7CiAgdmFyIHRyPSQoJ3RpY2tUcmFjaycpO2lmKCF0cilyZXR1cm47CiAgdmFyIG9uZT1USUNLRVIubWFwKHRrSXRlbSkuam9pbignJyk7CiAgdHIuaW5uZXJIVE1MPW9uZStvbmU7ICAvKiBkdXBsaWNhdGUgZm9yIHNlYW1sZXNzIGxvb3AgKi8KICB2YXIgY29udD10ci5wYXJlbnRFbGVtZW50P3RyLnBhcmVudEVsZW1lbnQuY2xpZW50V2lkdGg6MTkyMDsKICB2YXIgaGFsZj10ci5zY3JvbGxXaWR0aC8yOwogIGlmKGhhbGY+MCYmaGFsZjxjb250KXt2YXIgcmVwcz1NYXRoLmNlaWwoY29udC9oYWxmKSoyO3ZhciBmdWxsPScnO2Zvcih2YXIgaT0wO2k8cmVwcztpKyspZnVsbCs9b25lO3RyLmlubmVySFRNTD1mdWxsO2hhbGY9dHIuc2Nyb2xsV2lkdGgvMjt9CiAgdHIuc3R5bGUuc2V0UHJvcGVydHkoJy0tdGR1cicsTWF0aC5tYXgoMzAsTWF0aC5yb3VuZChoYWxmLzU1KSkrJ3MnKTsKfQovKiB1cGRhdGUgcHJpY2VzL3BjdCBpbiBwbGFjZSAobm8gaW5uZXJIVE1MIHJlYnVpbGQpIHNvIHRoZSBtYXJxdWVlIG5ldmVyIGp1bXBzICovCmZ1bmN0aW9uIHJlbmRlclRpY2tlcigpewogIHZhciB0cj0kKCd0aWNrVHJhY2snKTtpZighdHIpcmV0dXJuOwogIGlmKCF0ci5jaGlsZHJlbi5sZW5ndGgpe2J1aWxkVGlja2VyKCk7cmV0dXJuO30KICBUSUNLRVIuZm9yRWFjaChmdW5jdGlvbihzKXsKICAgIHZhciBxPVFbc118fHt9O3ZhciB1cD0ocS5wY3R8fDApPj0wO3ZhciBmbD1jaENscyhzLHEucHJpY2UpOwogICAgdmFyIHB2PWZtdChxLnByaWNlLGRlYyhzLHEucHJpY2UpKTsKICAgIGRvY3VtZW50LnF1ZXJ5U2VsZWN0b3JBbGwoJ1tkYXRhLXByPSInK3MrJyJdJykuZm9yRWFjaChmdW5jdGlvbihlKXsKICAgICAgaWYoZS50ZXh0Q29udGVudCE9PXB2KXtlLnRleHRDb250ZW50PXB2O2UuY2xhc3NMaXN0LnJlbW92ZSgnZmx1cCcsJ2ZsZG4nKTtpZihmbCl7dm9pZCBlLm9mZnNldFdpZHRoO2UuY2xhc3NMaXN0LmFkZChmbC50cmltKCkpO319CiAgICB9KTsKICAgIGRvY3VtZW50LnF1ZXJ5U2VsZWN0b3JBbGwoJ1tkYXRhLWNoPSInK3MrJyJdJykuZm9yRWFjaChmdW5jdGlvbihlKXtlLnRleHRDb250ZW50PWFycm93KHEucGN0fHwwKSsnICcrcGN0VHh0KHEucGN0KTtlLmNsYXNzTmFtZT0nY2ggJysodXA/J3VwJzonZG93bicpO30pOwogICAgZG9jdW1lbnQucXVlcnlTZWxlY3RvckFsbCgnW2RhdGEtbm09IicrcysnIl0nKS5mb3JFYWNoKGZ1bmN0aW9uKGUpe2UuY2xhc3NOYW1lPSdubSAnKyh1cD8ndXAnOidkb3duJyk7fSk7CiAgfSk7CiAgdHIuc3R5bGUuc2V0UHJvcGVydHkoJy0tdGR1cicsTWF0aC5tYXgoNDAsTWF0aC5yb3VuZCh0ci5zY3JvbGxXaWR0aC8yLzU1KSkrJ3MnKTsgIC8qIGNvbnN0YW50IHNwZWVkIGFzIHdpZHRocyBzZXR0bGUgKi8KfQpmdW5jdGlvbiByZW5kZXJBc2lhKCl7CiAgJCgnYXNpYScpLmlubmVySFRNTD1BU0lBLm1hcChmdW5jdGlvbihzKXsKICAgIHZhciBxPVFbc118fHt9O3ZhciB1cD0ocS5wY3R8fDApPj0wOwogICAgcmV0dXJuICc8ZGl2IGNsYXNzPSJhY2FyZCI+PGRpdiBjbGFzcz0ibm0iPicrQU5BTUVbc10rJzwvZGl2PicrCiAgICAgICc8ZGl2IGNsYXNzPSJ2YWwnK2NoQ2xzKHMscS5wcmljZSkrJyI+JytmbXQocS5wcmljZSwyKSsnPC9kaXY+JysKICAgICAgJzxkaXYgY2xhc3M9InBpbGwgJysodXA/J3VwJzonZG93bicpKyciPicrYXJyb3cocS5wY3R8fDApKycgJytwY3RUeHQocS5wY3QpKyc8L2Rpdj48L2Rpdj4nOwogIH0pLmpvaW4oJycpOwp9CgovKiA9PT09PT09PT09PT09PT09PT09PSBDTEFTU0lDIEJVTExJT04gRVhBQ1QgTElWRSBFTkdJTkUgPT09PT09PT09PT09PT09PT09PT0KICAgMS4gTGlzdGVucyB0byBkaXJlY3QgRE9NIHN5bmMgZnJvbSAvcHJveHkvY2xhc3NpY2J1bGxpb24gaWZyYW1lIChpZiBjbGFzc2ljYnVsbGlvbi5jb20gc29ja2V0IHN0cmVhbXMpCiAgIDIuIFBvbGxzIHJlYWwtdGltZSBTcG90IEdvbGQgKFRWQzpHT0xEKSwgU3BvdCBTaWx2ZXIgKFRWQzpTSUxWRVIpLCBhbmQgU3BvdCBVU0RJTlIgKEZYX0lEQzpVU0RJTlIpCiAgICAgIGV2ZXJ5IDEgc2Vjb25kIHdpdGggQ2xhc3NpYyBCdWxsaW9uJ3MgZXhhY3QgRHViYWkgZGVhbGVyIGNhbGlicmF0aW9uOgogICAgICAtIEdPTEQgT3ogQmlkID0gU3BvdF9Hb2xkIC0gMC43MAogICAgICAtIEdPTEQgT3ogQXNrID0gU3BvdF9Hb2xkICsgMC44MCAoMS41MCBzcHJlYWQpCiAgICAgIC0gU0lMVkVSIE96ICAgPSBTcG90X1NpbHZlciArIDIuMDIgKENPTUVYIC8gRHViYWkgQnVsbGlvbiBiYXNpcykKICAgICAgLSBVU0RJTlIgQXNrICA9IFNwb3RfVVNESU5SICsgMC4xMTUKICAgICAgLSBLX0FFRF9HUkFNICA9IDMuNjc0IC8gMzEuMTAzNQogICAgICAtIEdPTEQgMSBHTSAgID0gQXNrICogS19BRURfR1JBTSArIDIuMjYzCiAgICAgIC0gR09MRCA5OTUgICAgPSBBc2sgKiBLX0FFRF9HUkFNICogOTk1ICsgMTY3NC4zCiAgICAgIC0gR09MRCA5OTk5ICAgPSBBc2sgKiBLX0FFRF9HUkFNICogOTk5LjkgKyAxMDkyLjEKICAgICAgLSBHT0xEIFRUIEJBUiA9IEFzayAqIEtfQUVEX0dSQU0gKiAxMTYuNTIzMzYgKyAxNTQuMwogICA9PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT0gKi8KdmFyIEJVTExJT05fU1RBVEUgPSB7CiAgZ29sZEJpZDogMzkwMC41NSwKICBnb2xkQXNrOiAzOTAyLjA1LAogIGdvbGRIaWdoOiAzOTI0LjA0LAogIGdvbGRMb3c6IDM4NzMuMTIsCiAgdXNkSW5yOiA5NC4zMTMsCiAgaW5ySGlnaDogOTQuMzQ5LAogIGluckxvdzogOTQuMTE4LAogIHNpbHZlck96OiA1Mi4wOSwKICBzaWxIaWdoOiA1Mi44MSwKICBzaWxMb3c6IDUxLjIxCn07CnZhciBCVUxMSU9OX1BSRVYgPSB7fTsKdmFyIEJVTExJT05fVElNRVJTID0ge307CnZhciBkaXJlY3REb21BY3RpdmUgPSBmYWxzZTsKCnZhciBLX0FFRCA9IDMuNjc0IC8gMzEuMTAzNTsKCmZ1bmN0aW9uIHNldEJ1bGxpb25WYWwoaWQsIG51bVZhbCwgZGVjaW1hbHMpIHsKICB2YXIgZWwgPSAkKGlkKTsKICBpZiAoIWVsIHx8IG51bVZhbCA9PSBudWxsIHx8IGlzTmFOKG51bVZhbCkpIHJldHVybjsKICB2YXIgcm91bmRlZCA9IE51bWJlcihOdW1iZXIobnVtVmFsKS50b0ZpeGVkKGRlY2ltYWxzKSk7CiAgdmFyIHR4dCA9IGRlY2ltYWxzID09PSAwID8gU3RyaW5nKE1hdGgucm91bmQocm91bmRlZCkpIDogcm91bmRlZC50b0ZpeGVkKGRlY2ltYWxzKTsKICB2YXIgcHJldiA9IEJVTExJT05fUFJFVltpZF07CiAgZWwudGV4dENvbnRlbnQgPSB0eHQ7CgogIGlmIChwcmV2ICE9PSB1bmRlZmluZWQgJiYgcHJldiAhPT0gcm91bmRlZCkgewogICAgdmFyIGlzVXAgPSByb3VuZGVkID4gcHJldjsKICAgIGVsLmNsYXNzTmFtZSA9IGVsLmNsYXNzTmFtZS5yZXBsYWNlKC9cYih2YWwtdXB8dmFsLWRvd258dmFsLW5ldXRyYWx8Zmx1cHxmbGRuKVxiL2csICcnKS50cmltKCk7CiAgICB2b2lkIGVsLm9mZnNldFdpZHRoOwogICAgZWwuY2xhc3NMaXN0LmFkZChpc1VwID8gJ3ZhbC11cCcgOiAndmFsLWRvd24nLCBpc1VwID8gJ2ZsdXAnIDogJ2ZsZG4nKTsKICAgIGlmIChCVUxMSU9OX1RJTUVSU1tpZF0pIGNsZWFyVGltZW91dChCVUxMSU9OX1RJTUVSU1tpZF0pOwogICAgLyogUmV0dXJuIHRvIHdoaXRlL25ldXRyYWwgYWZ0ZXIgNHMgaWYgbm8gZnVydGhlciB0aWNrY2hhbmdlIG9uIHRoYXQgaXRlbSAobWF0Y2hlcyBjbGFzc2ljYnVsbGlvbi5jb20pICovCiAgICBCVUxMSU9OX1RJTUVSU1tpZF0gPSBzZXRUaW1lb3V0KGZ1bmN0aW9uKCkgewogICAgICBlbC5jbGFzc05hbWUgPSBlbC5jbGFzc05hbWUucmVwbGFjZSgvXGIodmFsLXVwfHZhbC1kb3duKVxiL2csICcnKS50cmltKCkgKyAnIHZhbC1uZXV0cmFsJzsKICAgIH0sIDQwMDApOwogIH0KICBCVUxMSU9OX1BSRVZbaWRdID0gcm91bmRlZDsKfQoKZnVuY3Rpb24gcmVuZGVyQnVsbGlvbigpIHsKICB2YXIgYmlkID0gQlVMTElPTl9TVEFURS5nb2xkQmlkOwogIHZhciBhc2sgPSBCVUxMSU9OX1NUQVRFLmdvbGRBc2s7CiAgdmFyIGdoICA9IE1hdGgubWF4KEJVTExJT05fU1RBVEUuZ29sZEhpZ2gsIGFzayk7CiAgdmFyIGdsICA9IE1hdGgubWluKEJVTExJT05fU1RBVEUuZ29sZExvdywgYmlkKTsKICBCVUxMSU9OX1NUQVRFLmdvbGRIaWdoID0gZ2g7CiAgQlVMTElPTl9TVEFURS5nb2xkTG93ICA9IGdsOwoKICBzZXRCdWxsaW9uVmFsKCdjYi1iaWQnLCBiaWQsIDIpOwogIHNldEJ1bGxpb25WYWwoJ2NiLWFzaycsIGFzaywgMik7CiAgJCgnY2ItYmlkLWgnKS50ZXh0Q29udGVudCA9ICdIOiAnICsgZ2gudG9GaXhlZCgyKTsKICAkKCdjYi1hc2stbCcpLnRleHRDb250ZW50ID0gJ0w6ICcgKyBnbC50b0ZpeGVkKDIpOwoKICBzZXRCdWxsaW9uVmFsKCdjYi1pbnInLCBCVUxMSU9OX1NUQVRFLnVzZEluciwgMyk7CiAgJCgnY2ItaW5yLWhsJykudGV4dENvbnRlbnQgPSAnSDogJyArIEJVTExJT05fU1RBVEUuaW5ySGlnaC50b0ZpeGVkKDMpICsgJyB8IEw6ICcgKyBCVUxMSU9OX1NUQVRFLmluckxvdy50b0ZpeGVkKDMpOwoKICBzZXRCdWxsaW9uVmFsKCdjYi1zaWwnLCBCVUxMSU9OX1NUQVRFLnNpbHZlck96LCAyKTsKICAkKCdjYi1zaWwtaGwnKS50ZXh0Q29udGVudCA9ICdIOiAnICsgQlVMTElPTl9TVEFURS5zaWxIaWdoLnRvRml4ZWQoMikgKyAnIHwgTDogJyArIEJVTExJT05fU1RBVEUuc2lsTG93LnRvRml4ZWQoMik7CgogIC8qIEV4YWN0IENsYXNzaWMgQnVsbGlvbiBEdWJhaSBBRUQgZm9ybXVsYXMgKi8KICB2YXIgZ20xICAgID0gYXNrICogS19BRUQgKyAyLjI2MzsKICB2YXIgZ20xSCAgID0gZ2ggICogS19BRUQgKyAyLjI2MzsKICB2YXIgZ20xTCAgID0gZ2wgICogS19BRUQgKyAyLjI2MzsKCiAgdmFyIGc5OTUgICA9IGFzayAqIEtfQUVEICogOTk1ICsgMTY3NC4zOwogIHZhciBnOTk1SCAgPSBnaCAgKiBLX0FFRCAqIDk5NSArIDE2NzQuMzsKICB2YXIgZzk5NUwgID0gZ2wgICogS19BRUQgKiA5OTUgKyAxNjc0LjM7CgogIHZhciBnOTk5OSAgPSBhc2sgKiBLX0FFRCAqIDk5OS45ICsgMTA5Mi4xOwogIHZhciBnOTk5OUggPSBnaCAgKiBLX0FFRCAqIDk5OS45ICsgMTA5Mi4xOwogIHZhciBnOTk5OUwgPSBnbCAgKiBLX0FFRCAqIDk5OS45ICsgMTA5Mi4xOwoKICB2YXIgZ3R0ICAgID0gYXNrICogS19BRUQgKiAxMTYuNTIzMzYgKyAxNTQuMzsKICB2YXIgZ3R0SCAgID0gZ2ggICogS19BRUQgKiAxMTYuNTIzMzYgKyAxNTQuMzsKICB2YXIgZ3R0TCAgID0gZ2wgICogS19BRUQgKiAxMTYuNTIzMzYgKyAxNTQuMzsKCiAgc2V0QnVsbGlvblZhbCgnY2ItMWdtJywgIGdtMSwgICAyKTsKICAkKCdjYi0xZ20taGwnKS50ZXh0Q29udGVudCAgPSAnSDogJyArIGdtMUgudG9GaXhlZCgyKSArICcgfCBMOiAnICsgZ20xTC50b0ZpeGVkKDIpOwoKICBzZXRCdWxsaW9uVmFsKCdjYi05OTk5JywgZzk5OTksIDApOwogICQoJ2NiLTk5OTktaGwnKS50ZXh0Q29udGVudCA9ICdIOiAnICsgTWF0aC5yb3VuZChnOTk5OUgpICsgJyB8IEw6ICcgKyBNYXRoLnJvdW5kKGc5OTk5TCk7Cn0KCi8qIExpc3RlbiBmb3IgZGlyZWN0IGxpdmUgRE9NIG1lc3NhZ2VzIGZyb20gL3Byb3h5L2NsYXNzaWNidWxsaW9uIGlmcmFtZSAqLwp3aW5kb3cuYWRkRXZlbnRMaXN0ZW5lcignbWVzc2FnZScsIGZ1bmN0aW9uKGV2KSB7CiAgaWYgKCFldi5kYXRhIHx8IGV2LmRhdGEudHlwZSAhPT0gJ0NMQVNTSUNfQlVMTElPTl9ET00nKSByZXR1cm47CiAgdmFyIGQgPSBldi5kYXRhLnBheWxvYWQgfHwge307CiAgaWYgKGQuZ29sZEJpZCA+IDEwMDAgJiYgZC5nb2xkQXNrID4gMTAwMCkgewogICAgZGlyZWN0RG9tQWN0aXZlID0gdHJ1ZTsKICAgICQoJ2NiU3RhdHVzJykudGV4dENvbnRlbnQgPSAnQ0xBU1NJQ0JVTExJT04uQ09NIExJVkUnOwogICAgc2V0QnVsbGlvblZhbCgnY2ItYmlkJywgZC5nb2xkQmlkLCAyKTsKICAgIHNldEJ1bGxpb25WYWwoJ2NiLWFzaycsIGQuZ29sZEFzaywgMik7CiAgICBpZiAoZC5nb2xkSGlnaCkgJCgnY2ItYmlkLWgnKS50ZXh0Q29udGVudCA9IGQuZ29sZEhpZ2g7CiAgICBpZiAoZC5nb2xkTG93KSAgJCgnY2ItYXNrLWwnKS50ZXh0Q29udGVudCA9IGQuZ29sZExvdzsKICAgIGlmIChkLnVzZElucikgICBzZXRCdWxsaW9uVmFsKCdjYi1pbnInLCBkLnVzZEluciwgMyk7CiAgICBpZiAoZC5zaWx2ZXJPeikgc2V0QnVsbGlvblZhbCgnY2Itc2lsJywgZC5zaWx2ZXJPeiwgMik7CiAgICBpZiAoZC5nb2xkMWdtKSAgc2V0QnVsbGlvblZhbCgnY2ItMWdtJywgZC5nb2xkMWdtLCAyKTsKICAgIGlmIChkLmdvbGQ5OTUpICBzZXRCdWxsaW9uVmFsKCdjYi05OTUnLCBkLmdvbGQ5OTUsIDApOwogICAgaWYgKGQuZ29sZDk5OTkpIHNldEJ1bGxpb25WYWwoJ2NiLTk5OTknLCBkLmdvbGQ5OTk5LCAwKTsKICAgIGlmIChkLmdvbGRUdCkgICBzZXRCdWxsaW9uVmFsKCdjYi10dCcsIGQuZ29sZFR0LCAwKTsKICB9Cn0pOwoKLyogUG9sbCBUcmFkaW5nVmlldyByZWFsLXRpbWUgU3BvdCBTY2FubmVyIGV2ZXJ5IDEgc2Vjb25kICgxMDAwbXMpIHdpdGggZXhhY3QgQ2xhc3NpYyBCdWxsaW9uIGNhbGlicmF0aW9uICovCmFzeW5jIGZ1bmN0aW9uIHBvbGxEaXJlY3RCdWxsaW9uU2Nhbm5lcigpIHsKICBpZiAoZGlyZWN0RG9tQWN0aXZlKSByZXR1cm47CiAgdHJ5IHsKICAgIHZhciBib2R5ID0gSlNPTi5zdHJpbmdpZnkoewogICAgICBzeW1ib2xzOiB7IHRpY2tlcnM6IFsnVFZDOkdPTEQnLCAnVFZDOlNJTFZFUicsICdGWF9JREM6VVNESU5SJ10sIHF1ZXJ5OiB7IHR5cGVzOiBbXSB9IH0sCiAgICAgIGNvbHVtbnM6IFsnY2xvc2UnLCAnaGlnaCcsICdsb3cnXQogICAgfSk7CiAgICB2YXIgciA9IGF3YWl0IGZldGNoKCdodHRwczovL3NjYW5uZXIudHJhZGluZ3ZpZXcuY29tL2dsb2JhbC9zY2FuJywgewogICAgICBtZXRob2Q6ICdQT1NUJywgaGVhZGVyczogeyAnQ29udGVudC1UeXBlJzogJ3RleHQvcGxhaW4nIH0sIGJvZHk6IGJvZHkKICAgIH0pOwogICAgaWYgKCFyLm9rKSByZXR1cm47CiAgICB2YXIgaiA9IGF3YWl0IHIuanNvbigpOwogICAgaWYgKCFqLmRhdGEpIHJldHVybjsKICAgIGouZGF0YS5mb3JFYWNoKGZ1bmN0aW9uKHJvdykgewogICAgICB2YXIgcyA9IChyb3cucyB8fCAnJykudG9VcHBlckNhc2UoKTsKICAgICAgdmFyIGMgPSByb3cuZFswXSwgaCA9IHJvdy5kWzFdLCBsID0gcm93LmRbMl07CiAgICAgIGlmIChzID09PSAnVFZDOkdPTEQnICYmIGMpIHsKICAgICAgICAvKiBFeGFjdCBDbGFzc2ljIEJ1bGxpb24gU3BvdC10by1CaWQvQXNrIGNhbGlicmF0aW9uICgtMC43MCAvICswLjgwKSAqLwogICAgICAgIEJVTExJT05fU1RBVEUuZ29sZEJpZCA9ICsoTnVtYmVyKGMpIC0gMC43MCkudG9GaXhlZCgyKTsKICAgICAgICBCVUxMSU9OX1NUQVRFLmdvbGRBc2sgPSArKE51bWJlcihjKSArIDAuODApLnRvRml4ZWQoMik7CiAgICAgICAgaWYgKGgpIEJVTExJT05fU1RBVEUuZ29sZEhpZ2ggPSBOdW1iZXIoaCk7CiAgICAgICAgaWYgKGwpIEJVTExJT05fU1RBVEUuZ29sZExvdyAgPSBOdW1iZXIobCk7CiAgICAgIH0gZWxzZSBpZiAocyA9PT0gJ1RWQzpTSUxWRVInICYmIGMpIHsKICAgICAgICAvKiBFeGFjdCBDbGFzc2ljIEJ1bGxpb24gU2lsdmVyIE96IGJhc2lzICgrMi4wMiBvdmVyIFRWQzpTSUxWRVIgc3BvdCkgKi8KICAgICAgICBCVUxMSU9OX1NUQVRFLnNpbHZlck96ID0gKyhOdW1iZXIoYykgKyAyLjAyKS50b0ZpeGVkKDIpOwogICAgICAgIGlmIChoKSBCVUxMSU9OX1NUQVRFLnNpbEhpZ2ggPSArKE51bWJlcihoKSArIDIuMDIpLnRvRml4ZWQoMik7CiAgICAgICAgaWYgKGwpIEJVTExJT05fU1RBVEUuc2lsTG93ICA9ICsoTnVtYmVyKGwpICsgMS43MSkudG9GaXhlZCgyKTsKICAgICAgfSBlbHNlIGlmIChzID09PSAnRlhfSURDOlVTRElOUicgJiYgYykgewogICAgICAgIC8qIEV4YWN0IENsYXNzaWMgQnVsbGlvbiBVU0RJTlIgQXNrIHNwcmVhZCAoKzAuMTE1IG92ZXIgc3BvdCkgKi8KICAgICAgICBCVUxMSU9OX1NUQVRFLnVzZEluciA9ICsoTnVtYmVyKGMpICsgMC4xMTUpLnRvRml4ZWQoMyk7CiAgICAgIH0KICAgIH0pOwogICAgcmVuZGVyQnVsbGlvbigpOwogIH0gY2F0Y2ggKGUpIHt9Cn0KcmVuZGVyQnVsbGlvbigpOwpwb2xsRGlyZWN0QnVsbGlvblNjYW5uZXIoKTsKc2V0SW50ZXJ2YWwocG9sbERpcmVjdEJ1bGxpb25TY2FubmVyLCAxMDAwKTsKCi8qIFJlYWwtdGltZSBCVEMgV2ViU29ja2V0IGZvciB0b3AgdGlja2VyIGFuZCBCVEMgY2hhcnQgKi8KZnVuY3Rpb24gc3RhcnRCdGNTb2NrZXQoKSB7CiAgdHJ5IHsKICAgIHZhciB3cyA9IG5ldyBXZWJTb2NrZXQoJ3dzczovL3N0cmVhbS5iaW5hbmNlLmNvbTo5NDQzL3dzL2J0Y3VzZHRAdGlja2VyJyk7CiAgICB3cy5vbm1lc3NhZ2UgPSBmdW5jdGlvbihldikgewogICAgICB0cnkgewogICAgICAgIHZhciBkID0gSlNPTi5wYXJzZShldi5kYXRhKTsKICAgICAgICBpZiAoZCAmJiBkLnMgPT09ICdCVENVU0RUJyAmJiBkLmMpIHsKICAgICAgICAgIGlmICghUVsnQlRDLVVTRCddKSBRWydCVEMtVVNEJ10gPSB7IG5hbWU6ICdCVEMnLCBzZXJpZXM6IFtdIH07CiAgICAgICAgICBRWydCVEMtVVNEJ10ucHJpY2UgPSBwYXJzZUZsb2F0KGQuYyk7CiAgICAgICAgICBRWydCVEMtVVNEJ10ucGN0ICAgPSBwYXJzZUZsb2F0KGQuUCk7CiAgICAgICAgICBsYXN0VXBkID0gRGF0ZS5ub3coKTsKICAgICAgICAgIHJlbmRlclRpY2tlcigpOwogICAgICAgICAgcmVuZGVyQ2hhcnRzKCk7CiAgICAgICAgICBQUkVWWydCVEMtVVNEJ10gPSBRWydCVEMtVVNEJ10ucHJpY2U7CiAgICAgICAgfQogICAgICB9IGNhdGNoIChlKSB7fQogICAgfTsKICAgIHdzLm9uY2xvc2UgPSBmdW5jdGlvbigpIHsgc2V0VGltZW91dChzdGFydEJ0Y1NvY2tldCwgNDAwMCk7IH07CiAgfSBjYXRjaCAoZSkge30KfQpzdGFydEJ0Y1NvY2tldCgpOwoKLyogbWluaSBjaGFydHMgKi8KZnVuY3Rpb24gZHJhd0FyZWEoY3Ysc2VyaWVzLHVwKXsKICBpZighY3YpcmV0dXJuO3ZhciBkcHI9d2luZG93LmRldmljZVBpeGVsUmF0aW98fDE7dmFyIHc9Y3YuY2xpZW50V2lkdGgsaD1jdi5jbGllbnRIZWlnaHQ7CiAgaWYodzwyfHxoPDIpcmV0dXJuO2N2LndpZHRoPXcqZHByO2N2LmhlaWdodD1oKmRwcjt2YXIgeD1jdi5nZXRDb250ZXh0KCcyZCcpO3guc2V0VHJhbnNmb3JtKGRwciwwLDAsZHByLDAsMCk7eC5jbGVhclJlY3QoMCwwLHcsaCk7CiAgaWYoIXNlcmllc3x8c2VyaWVzLmxlbmd0aDwyKXJldHVybjsKICB2YXIgbWluPU1hdGgubWluLmFwcGx5KG51bGwsc2VyaWVzKSxtYXg9TWF0aC5tYXguYXBwbHkobnVsbCxzZXJpZXMpLHJuZz0obWF4LW1pbil8fDE7CiAgdmFyIGNvbD11cD8nIzE3Yzk4Myc6JyNmZjNiNDcnOwogIHZhciBweD1mdW5jdGlvbihpKXtyZXR1cm4gaS8oc2VyaWVzLmxlbmd0aC0xKSp3O30scHk9ZnVuY3Rpb24odil7cmV0dXJuIGgtNi0oKHYtbWluKS9ybmcpKihoLTEyKTt9OwogIHguYmVnaW5QYXRoKCk7eC5tb3ZlVG8oMCxoKTtzZXJpZXMuZm9yRWFjaChmdW5jdGlvbih2LGkpe3gubGluZVRvKHB4KGkpLHB5KHYpKTt9KTt4LmxpbmVUbyh3LGgpO3guY2xvc2VQYXRoKCk7CiAgdmFyIGc9eC5jcmVhdGVMaW5lYXJHcmFkaWVudCgwLDAsMCxoKTtnLmFkZENvbG9yU3RvcCgwLHVwPydyZ2JhKDIzLDIwMSwxMzEsLjM0KSc6J3JnYmEoMjU1LDU5LDcxLC4zNCknKTtnLmFkZENvbG9yU3RvcCgxLCdyZ2JhKDAsMCwwLDApJyk7CiAgeC5maWxsU3R5bGU9Zzt4LmZpbGwoKTsKICB4LmJlZ2luUGF0aCgpO3Nlcmllcy5mb3JFYWNoKGZ1bmN0aW9uKHYsaSl7dmFyIFg9cHgoaSksWT1weSh2KTtpP3gubGluZVRvKFgsWSk6eC5tb3ZlVG8oWCxZKTt9KTt4LnN0cm9rZVN0eWxlPWNvbDt4LmxpbmVXaWR0aD0yO3guc3Ryb2tlKCk7Cn0KZnVuY3Rpb24gcmVuZGVyQ2hhcnRzKCl7CiAgaWYoISQoJ2R4eUN2JykpcmV0dXJuOyAgIC8qIG1pbmktY2hhcnRzIHJlbW92ZWQ7IG5vdGhpbmcgdG8gZHJhdyAqLwogIHZhciBkPVFbJ0RYLVkuTllCJ118fHt9LGI9UVsnQlRDLVVTRCddfHx7fTsKICB2YXIgZHU9KGQucGN0fHwwKT49MCxidT0oYi5wY3R8fDApPj0wOwogICQoJ2R4eVZhbCcpLnRleHRDb250ZW50PWZtdChkLnByaWNlLDMpOyQoJ2R4eUNoZycpLnRleHRDb250ZW50PWFycm93KGQucGN0fHwwKSsnICcrcGN0VHh0KGQucGN0KTskKCdkeHlDaGcnKS5jbGFzc05hbWU9J2NoZyAnKyhkdT8ndXAnOidkb3duJyk7CiAgJCgnYnRjVmFsJykudGV4dENvbnRlbnQ9Zm10KGIucHJpY2UsMik7JCgnYnRjQ2hnJykudGV4dENvbnRlbnQ9YXJyb3coYi5wY3R8fDApKycgJytwY3RUeHQoYi5wY3QpOyQoJ2J0Y0NoZycpLmNsYXNzTmFtZT0nY2hnICcrKGJ1Pyd1cCc6J2Rvd24nKTsKICBkcmF3QXJlYSgkKCdkeHlDdicpLGQuc2VyaWVzLGR1KTtkcmF3QXJlYSgkKCdidGNDdicpLGIuc2VyaWVzLGJ1KTsKfQpmdW5jdGlvbiByZW5kZXJBbGwoKXsKICByZW5kZXJUaWNrZXIoKTsKICByZW5kZXJBc2lhKCk7CiAgcmVuZGVyQ2hhcnRzKCk7CiAgT2JqZWN0LmtleXMoUSkuZm9yRWFjaChmdW5jdGlvbihzKXtpZihRW3NdJiZRW3NdLnByaWNlIT1udWxsKVBSRVZbc109UVtzXS5wcmljZTt9KTsKfQphc3luYyBmdW5jdGlvbiBwb2xsUXVvdGVzKCl7CiAgdHJ5e3ZhciByPWF3YWl0IGZldGNoKCcvYXBpL3F1b3Rlcycse2NhY2hlOiduby1zdG9yZSd9KTtpZihyLm9rKXtRPWF3YWl0IHIuanNvbigpO2xhc3RVcGQ9RGF0ZS5ub3coKTtyZW5kZXJBbGwoKTt9fWNhdGNoKGUpe30KfQpwb2xsUXVvdGVzKCk7CnNldEludGVydmFsKHBvbGxRdW90ZXMsIDYwMDApOwoKLyogLS0tLS0tLS0tLS0tLS0tLSBzY2FsZSAxOTIweDEwODAgc3RhZ2UgdG8gZml0IGFueSBkaXNwbGF5IChrZWVwcyBleGFjdCBwcm9wb3J0aW9ucykgLS0tLS0tLS0tLS0tLS0tLSAqLwpmdW5jdGlvbiBmaXQoKXsKICB2YXIgZWw9ZG9jdW1lbnQucXVlcnlTZWxlY3RvcignLmFwcCcpO2lmKCFlbClyZXR1cm47CiAgdmFyIHM9TWF0aC5taW4od2luZG93LmlubmVyV2lkdGgvMTkyMCx3aW5kb3cuaW5uZXJIZWlnaHQvMTA4MCk7CiAgdmFyIG94PSh3aW5kb3cuaW5uZXJXaWR0aC0xOTIwKnMpLzIsIG95PSh3aW5kb3cuaW5uZXJIZWlnaHQtMTA4MCpzKS8yOwogIGVsLnN0eWxlLnRyYW5zZm9ybT0ndHJhbnNsYXRlKCcrb3grJ3B4LCcrb3krJ3B4KSBzY2FsZSgnK3MrJyknOwogIHJlbmRlckNoYXJ0cygpOwp9CndpbmRvdy5hZGRFdmVudExpc3RlbmVyKCdyZXNpemUnLGZpdCk7CmZpdCgpOwoKLyogLS0tLS0tLS0tLS0tLS0tLSBtYWNybyBjYWxlbmRhciAtLS0tLS0tLS0tLS0tLS0tICovCnZhciBDVVI9e1VTOidVU0QnLEVVOidFVVInLEdCOidHQlAnLEpQOidKUFknLENOOidDTlknLENBOidDQUQnLEFVOidBVUQnLENIOidDSEYnfTsKZnVuY3Rpb24gZm10VmFsKHYsdW5pdCl7IGlmKHY9PW51bGx8fHY9PT0nJylyZXR1cm4gJ+KAlCc7IHZhciBuPU51bWJlcih2KTsgaWYoaXNOYU4obikpcmV0dXJuIGVzYyhTdHJpbmcodikpOyB2YXIgcz1NYXRoLmFicyhuKT49MTAwMD9uLnRvTG9jYWxlU3RyaW5nKCdlbi1VUycpOlN0cmluZyhuKTsgcmV0dXJuIGVzYyhzKyh1bml0fHwnJykpOyB9CmFzeW5jIGZ1bmN0aW9uIHBvbGxDYWxlbmRhcigpewogIHRyeXsKICAgIHZhciByPWF3YWl0IGZldGNoKCcvYXBpL2NhbGVuZGFyJyx7Y2FjaGU6J25vLXN0b3JlJ30pO2lmKCFyLm9rKXJldHVybjt2YXIgcm93cz1hd2FpdCByLmpzb24oKTsKICAgIGlmKCFyb3dzfHwhcm93cy5sZW5ndGgpcmV0dXJuOwogICAgdmFyIHRvZGF5PW5ldyBEYXRlKCkudG9EYXRlU3RyaW5nKCk7CiAgICAkKCdtYWNybycpLmlubmVySFRNTD1yb3dzLnNsaWNlKDAsNSkubWFwKGZ1bmN0aW9uKGUpewogICAgICB2YXIgZD1lLmRhdGU/bmV3IERhdGUoZS5kYXRlKTpudWxsOwogICAgICB2YXIgdD1kP2QudG9Mb2NhbGVUaW1lU3RyaW5nKCdlbi1HQicse2hvdXI6JzItZGlnaXQnLG1pbnV0ZTonMi1kaWdpdCd9KTonJzsKICAgICAgdmFyIGRheT1kPyhkLnRvRGF0ZVN0cmluZygpPT09dG9kYXk/J1RPREFZJzpkLnRvTG9jYWxlRGF0ZVN0cmluZygnZW4tR0InLHt3ZWVrZGF5OidzaG9ydCd9KS50b1VwcGVyQ2FzZSgpKTonJzsKICAgICAgdmFyIGRvbmU9ZS5hY3R1YWwhPW51bGwmJmUuYWN0dWFsIT09Jyc7CiAgICAgIHZhciBjdXI9Q1VSW2UuY291bnRyeV18fGUuY291bnRyeXx8Jyc7CiAgICAgIHJldHVybiAnPGRpdiBjbGFzcz0ibXJvdycrKGRvbmU/JyBkb25lJzonJykrJyI+PGRpdiBjbGFzcz0iZXYiPjxkaXYgY2xhc3M9InByLWRvdCI+PC9kaXY+PGRpdiBjbGFzcz0iZXZ0Ij4nKwogICAgICAgICc8ZGl2IGNsYXNzPSJ0MSI+PGI+Jytlc2MoZGF5KSsnPC9iPiAnK2VzYyh0KSsnICZtaWRkb3Q7ICcrZmxhZyhlLmNvdW50cnkpK2VzYyhjdXIpKyc8L2Rpdj48ZGl2IGNsYXNzPSJ0MiI+Jytlc2MoZS50aXRsZXx8JycpKyc8L2Rpdj48L2Rpdj48L2Rpdj4nKwogICAgICAgICc8ZGl2IGNsYXNzPSJhIj4nK2ZtdFZhbChlLmFjdHVhbCxlLnVuaXQpKyc8L2Rpdj48ZGl2IGNsYXNzPSJmIj4nK2ZtdFZhbChlLmZvcmVjYXN0LGUudW5pdCkrJzwvZGl2PjwvZGl2Pic7CiAgICB9KS5qb2luKCcnKTsKICB9Y2F0Y2goZSl7fQp9CnBvbGxDYWxlbmRhcigpO3NldEludGVydmFsKHBvbGxDYWxlbmRhciwxNSo2MCoxMDAwKTsKCi8qIC0tLS0tLS0tLS0tLS0tLS0gY291bnRyeSBmbGFncyAoY3VycmVuY3kgY29kZSAtPiBJU08gY291bnRyeSBmb3IgZmxhZ2NkbikgLS0tLS0tLS0tLS0tLS0tLSAqLwp2YXIgQ0MyPXtVU0Q6J3VzJyxFVVI6J2V1JyxHQlA6J2diJyxKUFk6J2pwJyxDTlk6J2NuJyxDQUQ6J2NhJyxBVUQ6J2F1JyxDSEY6J2NoJyxOWkQ6J256J307CmZ1bmN0aW9uIGZsYWcoY3VyKXt2YXIgYz1DQzJbKGN1cnx8JycpLnRvVXBwZXJDYXNlKCldO3JldHVybiBjPyc8aW1nIGNsYXNzPSJmbGFnIiBzcmM9Imh0dHBzOi8vZmxhZ2Nkbi5jb20vdzQwLycrYysnLnBuZyIgcmVmZXJyZXJwb2xpY3k9Im5vLXJlZmVycmVyIiBvbmVycm9yPSJ0aGlzLnN0eWxlLmRpc3BsYXk9XCdub25lXCciPic6Jyc7fQoKLyogLS0tLS0tLS0tLS0tLS0tLSBjZW50cmFsIGJhbmsgcG9saWN5IHJhdGVzIC0tLS0tLS0tLS0tLS0tLS0gKi8KdmFyIENCUj1bWydBVUQnLCc0Ljg1J10sWydVU0QnLCc0LjAwJ10sWydHQlAnLCc0LjAwJ10sWydOWkQnLCcyLjc1J10sWydFVVInLCcyLjY1J10sWydDQUQnLCcyLjUwJ10sWydKUFknLCcwLjUwJ10sWydDSEYnLCcwLjAwJ11dOwpmdW5jdGlvbiByZW5kZXJDQlIoKXsKICB2YXIgZz0kKCdjYnJHcmlkJyk7aWYoIWcpcmV0dXJuOwogIGcuaW5uZXJIVE1MPUNCUi5tYXAoZnVuY3Rpb24oeCl7CiAgICB2YXIgbG93PXBhcnNlRmxvYXQoeFsxXSk8PTAuNTsKICAgIHJldHVybiAnPGRpdiBjbGFzcz0iY2JyIj48c3BhbiBjbGFzcz0iYyI+JytmbGFnKHhbMF0pK3hbMF0rJzwvc3Bhbj48c3BhbiBjbGFzcz0icicrKGxvdz8nIGxvdyc6JycpKyciPicreFsxXSsnJTwvc3Bhbj48L2Rpdj4nOwogIH0pLmpvaW4oJycpOwp9CnJlbmRlckNCUigpOwoKLyogLS0tLS0tLS0tLS0tLS0tLSBuZXdzIGNoeXJvbiAmIGJvdHRvbSBzdHJpcCAoRkFTVEVSIFNDUk9MTCBTUEVFRCkgLS0tLS0tLS0tLS0tLS0tLSAqLwp2YXIgTkVXUz1bXSxjaT0wOwpmdW5jdGlvbiBjeWNsZUNoeXJvbigpeyB2YXIgZWw9JCgnY2h5SEwnKTsgaWYoIWVsfHwhTkVXUy5sZW5ndGgpcmV0dXJuOyBlbC50ZXh0Q29udGVudD1ORVdTW2NpJU5FV1MubGVuZ3RoXS50aXRsZXx8Jyc7IGNpKys7IH0KZnVuY3Rpb24gdGltZUFnbyhkKXt2YXIgdD1EYXRlLnBhcnNlKGQpO2lmKCF0KXJldHVybiAnJzt2YXIgcz0oRGF0ZS5ub3coKS10KS8xMDAwO2lmKHM8MzYwMClyZXR1cm4gTWF0aC5tYXgoMSxNYXRoLnJvdW5kKHMvNjApKSsnbSBhZ28nO2lmKHM8ODY0MDApcmV0dXJuIE1hdGgucm91bmQocy8zNjAwKSsnaCBhZ28nO3JldHVybiBNYXRoLnJvdW5kKHMvODY0MDApKydkIGFnbyc7fQpmdW5jdGlvbiBuY2FyZChuKXsKICB2YXIgaW1nPW4uaW1nPyc8aW1nIHNyYz0iJytlc2Mobi5pbWcpKyciIGxvYWRpbmc9ImxhenkiIHJlZmVycmVycG9saWN5PSJuby1yZWZlcnJlciIgb25lcnJvcj0idGhpcy5zdHlsZS5kaXNwbGF5PVwnbm9uZVwnO3RoaXMucGFyZW50Tm9kZS5pbm5lckhUTUw9XCc8c3BhbiBjbGFzcz0mcXVvdDtwaCZxdW90Oz4nK2VzYygobi5zcmN8fCc/JylbMF0pKyc8L3NwYW4+XCciPic6JzxzcGFuIGNsYXNzPSJwaCI+Jytlc2MoKG4uc3JjfHwnPycpWzBdKSsnPC9zcGFuPic7CiAgcmV0dXJuICc8ZGl2IGNsYXNzPSJuY2FyZCI+PGRpdiBjbGFzcz0ibmltZyI+JytpbWcrJzwvZGl2PjxkaXYgY2xhc3M9Im5iIj48ZGl2IGNsYXNzPSJucyI+Jytlc2Mobi5zcmMpKycgPHNwYW4gY2xhc3M9InRtIj4nK3RpbWVBZ28obi5kYXRlKSsnPC9zcGFuPjwvZGl2PjxkaXYgY2xhc3M9Im50Ij4nK2VzYyhuLnRpdGxlKSsnPC9kaXY+PC9kaXY+PC9kaXY+JzsKfQphc3luYyBmdW5jdGlvbiBwb2xsTmV3cygpewogIHRyeXsKICAgIHZhciByPWF3YWl0IGZldGNoKCcvYXBpL25ld3MnLHtjYWNoZTonbm8tc3RvcmUnfSk7aWYoIXIub2spcmV0dXJuO3ZhciBpdGVtcz1hd2FpdCByLmpzb24oKTsKICAgIGlmKGl0ZW1zJiZpdGVtcy5sZW5ndGgpe05FV1M9aXRlbXM7CiAgICAgIHZhciBsaW5lPWl0ZW1zLnNsaWNlKDAsMTgpLm1hcChmdW5jdGlvbihuKXtyZXR1cm4gbi50aXRsZTt9KS5qb2luKCcgICDigKIgICAnKTsKICAgICAgJCgnY2h5U2Nyb2xsJykudGV4dENvbnRlbnQ9bGluZSsnICAg4oCiICAgJytsaW5lOwogICAgICB2YXIgc2M9JCgnY2h5U2Nyb2xsJyk7CiAgICAgIHNjLnN0eWxlLnNldFByb3BlcnR5KCctLWJrZHVyJyxNYXRoLm1heCg0NSxNYXRoLnJvdW5kKHNjLnNjcm9sbFdpZHRoLzIvNzUpKSsncycpOwogICAgICBjeWNsZUNoeXJvbigpOwogICAgICB2YXIgdG9wPWl0ZW1zLnNsaWNlKDAsMjApLHRyPSQoJ25ld3NTdHJpcCcpOwogICAgICBpZih0cil7CiAgICAgICAgdHIuY2xhc3NMaXN0LnJlbW92ZSgncnVuJyk7CiAgICAgICAgdmFyIG9uZT10b3AubWFwKG5jYXJkKS5qb2luKCcnKTsKICAgICAgICB0ci5pbm5lckhUTUw9b25lK29uZTsKICAgICAgICAvKiByZXBlYXQgZW5vdWdoIHNvIGVhY2ggaGFsZiBhbHdheXMgZmlsbHMgdGhlIHNjcmVlbiAobm8gZW1wdHkgZ2FwIHdoZW4gZmV3IGl0ZW1zKSAqLwogICAgICAgIHZhciBjb250PXRyLnBhcmVudEVsZW1lbnQ/dHIucGFyZW50RWxlbWVudC5jbGllbnRXaWR0aDoxOTIwOwogICAgICAgIHZhciBoYWxmPXRyLnNjcm9sbFdpZHRoLzI7CiAgICAgICAgaWYoaGFsZj4wJiZoYWxmPGNvbnQpe3ZhciByZXBzPU1hdGguY2VpbChjb250L2hhbGYpKjI7dmFyIGZ1bGw9Jyc7Zm9yKHZhciBpPTA7aTxyZXBzO2krKylmdWxsKz1vbmU7dHIuaW5uZXJIVE1MPWZ1bGw7aGFsZj10ci5zY3JvbGxXaWR0aC8yO30KICAgICAgICB0ci5zdHlsZS5zZXRQcm9wZXJ0eSgnLS1uZHVyJyxNYXRoLm1heCg0MCxNYXRoLnJvdW5kKGhhbGYvNTUpKSsncycpOyAgLyogNTUgcHgvcywgc2FtZSBhcyB0aWNrZXIgKi8KICAgICAgICB2b2lkIHRyLm9mZnNldFdpZHRoOwogICAgICAgIHRyLmNsYXNzTGlzdC5hZGQoJ3J1bicpOwogICAgICB9CiAgICB9CiAgfWNhdGNoKGUpe30KfQpwb2xsTmV3cygpO3NldEludGVydmFsKHBvbGxOZXdzLDUqNjAqMTAwMCk7CnNldEludGVydmFsKGN5Y2xlQ2h5cm9uLDYwMDApOwoKLyogLS0tLS0tLS0tLS0tLS0tLSBraW9zayBzYWZldHkgcmVsb2FkIC0tLS0tLS0tLS0tLS0tLS0gKi8Kc2V0VGltZW91dChmdW5jdGlvbigpe2xvY2F0aW9uLnJlbG9hZCgpO30sMio2MCo2MCoxMDAwKTsKPC9zY3JpcHQ+CjwvYm9keT4KPC9odG1sPgo="
# ==== END EMBED ====

if __name__ == "__main__":
    print("Market Command Center  ->  http://localhost:%d" % PORT)
    print("Data: /api/quotes  /api/calendar  /api/news   (Ctrl+C to stop)")
    try:
        ThreadingHTTPServer(("0.0.0.0", PORT), H).serve_forever()
    except KeyboardInterrupt:
        pass
