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
EMBED_HTML_B64 = "PCFkb2N0eXBlIGh0bWw+CjxodG1sIGxhbmc9ImVuIj4KPGhlYWQ+CjxtZXRhIGNoYXJzZXQ9InV0Zi04Ij4KPG1ldGEgbmFtZT0idmlld3BvcnQiIGNvbnRlbnQ9IndpZHRoPWRldmljZS13aWR0aCxpbml0aWFsLXNjYWxlPTEiPgo8dGl0bGU+TWFya2V0IENvbW1hbmQgQ2VudGVyPC90aXRsZT4KPGxpbmsgcmVsPSJwcmVjb25uZWN0IiBocmVmPSJodHRwczovL2ZvbnRzLmdvb2dsZWFwaXMuY29tIj48bGluayByZWw9InByZWNvbm5lY3QiIGhyZWY9Imh0dHBzOi8vZm9udHMuZ3N0YXRpYy5jb20iIGNyb3Nzb3JpZ2luPgo8bGluayByZWw9InN0eWxlc2hlZXQiIGhyZWY9Imh0dHBzOi8vZm9udHMuZ29vZ2xlYXBpcy5jb20vY3NzMj9mYW1pbHk9SW50ZXI6d2dodEA0MDA7NTAwOzYwMDs3MDA7ODAwOzkwMCZkaXNwbGF5PXN3YXAiPgo8c3R5bGU+CiAgOnJvb3R7CiAgICAtLWJnOiMwMDA7IC0tcGFuZWw6IzBiMGQxMjsgLS1wYW5lbC0yOiMwZTExMTg7IC0tY2FyZDojMGQxMDE3OyAtLWxpbmU6IzFhMWYyOTsgLS1saW5lLTI6IzE0MTkyMjsKICAgIC0tZmc6I2YyZjVmYTsgLS1tdXRlZDojOGE5NGE2OyAtLWZhaW50OiM1YTY0NzQ7CiAgICAtLXVwOiMxN2M5ODM7IC0tdXAtYmc6cmdiYSgyMywyMDEsMTMxLC4xNCk7IC0tZG93bjojZmYzYjQ3OyAtLWRvd24tYmc6cmdiYSgyNTUsNTksNzEsLjE0KTsKICAgIC0tZ29sZDojZjBiNDI5OyAtLW9yYW5nZTojZmY3YTFhOyAtLWxpdmU6I2ZmM2I0NzsgLS1hY2NlbnQ6I2YwYjQyOTsKICAgIC0tc2FuczoiSW50ZXIiLHN5c3RlbS11aSwtYXBwbGUtc3lzdGVtLCJTZWdvZSBVSSIsc2Fucy1zZXJpZjsKICB9CiAgKntib3gtc2l6aW5nOmJvcmRlci1ib3g7bWFyZ2luOjA7cGFkZGluZzowfQogIGh0bWwsYm9keXtoZWlnaHQ6MTAwJTt3aWR0aDoxMDAlfQogIGJvZHl7YmFja2dyb3VuZDojMDAwO2NvbG9yOnZhcigtLWZnKTtmb250LWZhbWlseTp2YXIoLS1zYW5zKTtvdmVyZmxvdzpoaWRkZW47LXdlYmtpdC1mb250LXNtb290aGluZzphbnRpYWxpYXNlZDtmb250LXZhcmlhbnQtbnVtZXJpYzp0YWJ1bGFyLW51bXN9CiAgLyogZml4ZWQgMTkyMHgxMDgwIHN0YWdlLCBzY2FsZWQgdG8gZml0IGFueSBkaXNwbGF5IC0+IGlkZW50aWNhbCBwcm9wb3J0aW9ucyBldmVyeXdoZXJlICovCiAgLmFwcHt3aWR0aDoxOTIwcHg7aGVpZ2h0OjEwODBweDt0cmFuc2Zvcm0tb3JpZ2luOnRvcCBsZWZ0O3Bvc2l0aW9uOmFic29sdXRlO3RvcDowO2xlZnQ6MDtkaXNwbGF5OmdyaWQ7Z3JpZC10ZW1wbGF0ZS1yb3dzOjYwcHggODBweCAxZnIgMTEwcHg7Z2FwOjEycHg7cGFkZGluZzoxM3B4O2JhY2tncm91bmQ6dmFyKC0tYmcpfQoKICAvKiBoZWFkZXIgKi8KICAuaGVhZHtkaXNwbGF5OmZsZXg7YWxpZ24taXRlbXM6Y2VudGVyO2p1c3RpZnktY29udGVudDpzcGFjZS1iZXR3ZWVuO3BhZGRpbmc6MCA4cHh9CiAgLmh0aXRsZXtmb250LXdlaWdodDo4MDA7Zm9udC1zaXplOjI4cHg7bGV0dGVyLXNwYWNpbmc6LjA5ZW19CiAgLmhzZXNze2Rpc3BsYXk6ZmxleDtnYXA6MjhweDtmb250LXdlaWdodDo3MDA7Zm9udC1zaXplOjE4cHh9CiAgLmhzZXNzIHNwYW57Y29sb3I6dmFyKC0tbXV0ZWQpfSAuaHNlc3MgYntjb2xvcjp2YXIoLS1mZyk7Zm9udC13ZWlnaHQ6ODAwO21hcmdpbi1sZWZ0OjdweH0KICAuaGNsb2Nre2ZvbnQtd2VpZ2h0OjcwMDtmb250LXNpemU6MTlweDtjb2xvcjp2YXIoLS1tdXRlZCl9IC5oY2xvY2sgYntjb2xvcjp2YXIoLS1mZyk7Zm9udC13ZWlnaHQ6ODAwfQogIC5obGl2ZXtkaXNwbGF5OmlubGluZS1mbGV4O2FsaWduLWl0ZW1zOmNlbnRlcjtnYXA6N3B4O2ZvbnQtd2VpZ2h0OjgwMDtmb250LXNpemU6MTNweDtsZXR0ZXItc3BhY2luZzouMDhlbTtjb2xvcjp2YXIoLS11cCl9CiAgLmhsaXZlIC5kb3R7d2lkdGg6OXB4O2hlaWdodDo5cHg7Ym9yZGVyLXJhZGl1czo1MCU7YmFja2dyb3VuZDp2YXIoLS11cCk7Ym94LXNoYWRvdzowIDAgOXB4IHZhcigtLXVwKTthbmltYXRpb246cHVsc2UgMS40cyBpbmZpbml0ZX0KICAuaGxpdmUuc3RhbGV7Y29sb3I6dmFyKC0tZmFpbnQpfSAuaGxpdmUuc3RhbGUgLmRvdHtiYWNrZ3JvdW5kOnZhcigtLWZhaW50KTtib3gtc2hhZG93Om5vbmU7YW5pbWF0aW9uOm5vbmV9CiAgQGtleWZyYW1lcyBmbHVwezAle2JhY2tncm91bmQ6cmdiYSgyMywyMDEsMTMxLC40NSl9MTAwJXtiYWNrZ3JvdW5kOnRyYW5zcGFyZW50fX0KICBAa2V5ZnJhbWVzIGZsZG57MCV7YmFja2dyb3VuZDpyZ2JhKDI1NSw1OSw3MSwuNDUpfTEwMCV7YmFja2dyb3VuZDp0cmFuc3BhcmVudH19CiAgLmZsdXB7YW5pbWF0aW9uOmZsdXAgLjhzIGVhc2Utb3V0O2JvcmRlci1yYWRpdXM6NXB4fSAuZmxkbnthbmltYXRpb246ZmxkbiAuOHMgZWFzZS1vdXQ7Ym9yZGVyLXJhZGl1czo1cHh9CgogIC8qIHRpY2tlciByb3cgKi8KICAudGlja3tkaXNwbGF5OmZsZXg7YWxpZ24taXRlbXM6Y2VudGVyO2JhY2tncm91bmQ6dmFyKC0tcGFuZWwpO2JvcmRlcjoxcHggc29saWQgdmFyKC0tbGluZSk7Ym9yZGVyLXJhZGl1czoxMnB4O292ZXJmbG93OmhpZGRlbn0KICAudGljay10cmFja3tkaXNwbGF5OmZsZXg7YWxpZ24taXRlbXM6Y2VudGVyO2hlaWdodDoxMDAlO3dpZHRoOm1heC1jb250ZW50O2FuaW1hdGlvbjp0bWFycSB2YXIoLS10ZHVyLDQ2cykgbGluZWFyIGluZmluaXRlO3dpbGwtY2hhbmdlOnRyYW5zZm9ybX0KICBAa2V5ZnJhbWVzIHRtYXJxe2Zyb217dHJhbnNmb3JtOnRyYW5zbGF0ZVgoMCl9dG97dHJhbnNmb3JtOnRyYW5zbGF0ZVgoLTUwJSl9fQogIC50a3tkaXNwbGF5OmZsZXg7YWxpZ24taXRlbXM6Y2VudGVyO2dhcDoxNHB4O3BhZGRpbmc6MCAzMHB4O2hlaWdodDoxMDAlO2JvcmRlci1yaWdodDoxcHggc29saWQgdmFyKC0tbGluZS0yKTt3aGl0ZS1zcGFjZTpub3dyYXB9CiAgLnRrIC5ubXtmb250LXdlaWdodDo4MDA7Zm9udC1zaXplOjI2cHh9CiAgLnRrIC5wcntmb250LXdlaWdodDo3MDA7Zm9udC1zaXplOjI2cHg7Y29sb3I6dmFyKC0tZmcpfQogIC50ayAuY2h7Zm9udC13ZWlnaHQ6ODAwO2ZvbnQtc2l6ZToyMnB4O2Rpc3BsYXk6ZmxleDthbGlnbi1pdGVtczpjZW50ZXI7Z2FwOjZweH0KICAudXB7Y29sb3I6dmFyKC0tdXApfSAuZG93bntjb2xvcjp2YXIoLS1kb3duKX0KCiAgLyogbWFpbiAzIGNvbHMgKi8KICAubWFpbntkaXNwbGF5OmdyaWQ7Z3JpZC10ZW1wbGF0ZS1jb2x1bW5zOjEuMzJmciAuOTJmciAxLjA2ZnI7Z2FwOjlweDttaW4taGVpZ2h0OjB9CiAgLmNvbHtkaXNwbGF5OmZsZXg7ZmxleC1kaXJlY3Rpb246Y29sdW1uO2dhcDo5cHg7bWluLWhlaWdodDowO21pbi13aWR0aDowfQogIC5wYW5lbHtiYWNrZ3JvdW5kOnZhcigtLXBhbmVsKTtib3JkZXI6MXB4IHNvbGlkIHZhcigtLWxpbmUpO2JvcmRlci1yYWRpdXM6MTRweDtkaXNwbGF5OmZsZXg7ZmxleC1kaXJlY3Rpb246Y29sdW1uO21pbi1oZWlnaHQ6MDtvdmVyZmxvdzpoaWRkZW59CiAgLnB0aXRsZXtmb250LXdlaWdodDo4MDA7Zm9udC1zaXplOjE1cHg7bGV0dGVyLXNwYWNpbmc6LjFlbTtwYWRkaW5nOjlweCAxNnB4IDZweDtjb2xvcjp2YXIoLS1mZyk7ZmxleDowIDAgYXV0bztkaXNwbGF5OmZsZXg7YWxpZ24taXRlbXM6Y2VudGVyO2p1c3RpZnktY29udGVudDpzcGFjZS1iZXR3ZWVufQogIC5wdGl0bGUgLnBzdWJ7Zm9udC13ZWlnaHQ6NzAwO2ZvbnQtc2l6ZToxMXB4O2xldHRlci1zcGFjaW5nOi4wOGVtO2NvbG9yOnZhcigtLWdvbGQpO2Rpc3BsYXk6aW5saW5lLWZsZXg7YWxpZ24taXRlbXM6Y2VudGVyO2dhcDo2cHh9CiAgLnB0aXRsZSAucHN1YiAuYmRvdHt3aWR0aDo3cHg7aGVpZ2h0OjdweDtib3JkZXItcmFkaXVzOjUwJTtiYWNrZ3JvdW5kOnZhcigtLXVwKTtib3gtc2hhZG93OjAgMCA4cHggdmFyKC0tdXApO2FuaW1hdGlvbjpwdWxzZSAxLjJzIGluZmluaXRlfQogIC5tb2RlLWJ0bntiYWNrZ3JvdW5kOiMxNTFhMjQ7Ym9yZGVyOjFweCBzb2xpZCAjMjYyZjQwO2NvbG9yOnZhcigtLW11dGVkKTtmb250OjcwMCAxMHB4LzEgdmFyKC0tc2Fucyk7cGFkZGluZzo0cHggOHB4O2JvcmRlci1yYWRpdXM6NXB4O2N1cnNvcjpwb2ludGVyO2xldHRlci1zcGFjaW5nOi4wNmVtfQogIC5tb2RlLWJ0bi5hY3RpdmV7Y29sb3I6dmFyKC0tZ29sZCk7Ym9yZGVyLWNvbG9yOnJnYmEoMjQwLDE4MCw0MSwuNSk7YmFja2dyb3VuZDpyZ2JhKDI0MCwxODAsNDEsLjEyKX0KCiAgLyogdmlkZW8gKi8KICAudmlke2ZsZXg6MTttaW4taGVpZ2h0OjA7cG9zaXRpb246cmVsYXRpdmU7YmFja2dyb3VuZDojMDAwO2JvcmRlcjoxcHggc29saWQgdmFyKC0tbGluZSk7Ym9yZGVyLXJhZGl1czoxNHB4O292ZXJmbG93OmhpZGRlbn0KICAudmlkIGlmcmFtZXtwb3NpdGlvbjphYnNvbHV0ZTtpbnNldDowO3dpZHRoOjEwMCU7aGVpZ2h0OjEwMCU7Ym9yZGVyOjB9CiAgLnZ0bHtwb3NpdGlvbjphYnNvbHV0ZTt0b3A6MTJweDtsZWZ0OjEycHg7ei1pbmRleDozO2JhY2tncm91bmQ6cmdiYSgxMCwxMCwxMiwuNzIpO2JhY2tkcm9wLWZpbHRlcjpibHVyKDRweCk7Ym9yZGVyLXJhZGl1czo4cHg7cGFkZGluZzo3cHggMTJweDtmb250LXdlaWdodDo3MDA7Zm9udC1zaXplOjEzcHg7ZGlzcGxheTpmbGV4O2FsaWduLWl0ZW1zOmNlbnRlcjtnYXA6OHB4fQogIC52dGwgLnl0e2NvbG9yOiNmZjJkMmQ7Zm9udC13ZWlnaHQ6OTAwfQogIC52dHJ7cG9zaXRpb246YWJzb2x1dGU7dG9wOjEycHg7cmlnaHQ6MTJweDt6LWluZGV4OjM7YmFja2dyb3VuZDojZTAxYjI0O2JvcmRlci1yYWRpdXM6N3B4O3BhZGRpbmc6NnB4IDExcHg7Zm9udC13ZWlnaHQ6OTAwO2ZvbnQtc2l6ZToxM3B4O2xldHRlci1zcGFjaW5nOi4wOGVtO2Rpc3BsYXk6ZmxleDthbGlnbi1pdGVtczpjZW50ZXI7Z2FwOjdweH0KICAudnRyIC5ke3dpZHRoOjhweDtoZWlnaHQ6OHB4O2JvcmRlci1yYWRpdXM6NTAlO2JhY2tncm91bmQ6I2ZmZjthbmltYXRpb246cHVsc2UgMS4zcyBpbmZpbml0ZX0KICAudmJye3Bvc2l0aW9uOmFic29sdXRlO2JvdHRvbTo3MnB4O3JpZ2h0OjEycHg7ei1pbmRleDozO2JhY2tncm91bmQ6cmdiYSgxMCwxMCwxMiwuNzgpO2JvcmRlci1yYWRpdXM6NnB4O3BhZGRpbmc6NXB4IDEwcHg7Zm9udC13ZWlnaHQ6ODAwO2ZvbnQtc2l6ZToxMnB4fQogIC52YnIgYntjb2xvcjp2YXIoLS1nb2xkKX0KICAuY2h5cm9ue3Bvc2l0aW9uOmFic29sdXRlO2xlZnQ6MDtyaWdodDowO2JvdHRvbTowO3otaW5kZXg6M30KICAuY2h5cm9uIC5obHtiYWNrZ3JvdW5kOmxpbmVhci1ncmFkaWVudCgwZGVnLHJnYmEoMCwwLDAsLjkyKSxyZ2JhKDAsMCwwLC41NSkpO3BhZGRpbmc6MTRweCAxNnB4IDEwcHg7Zm9udC13ZWlnaHQ6OTAwO2ZvbnQtc2l6ZTpjbGFtcCgxOHB4LDJ2dywzMHB4KTtsaW5lLWhlaWdodDoxLjA0O2xldHRlci1zcGFjaW5nOi4wMWVtO3RleHQtdHJhbnNmb3JtOnVwcGVyY2FzZTtjb2xvcjojZmZmO2Rpc3BsYXk6LXdlYmtpdC1ib3g7LXdlYmtpdC1saW5lLWNsYW1wOjI7LXdlYmtpdC1ib3gtb3JpZW50OnZlcnRpY2FsO292ZXJmbG93OmhpZGRlbjttaW4taGVpZ2h0Ojc2cHh9CiAgLmNoeXJvbiAuYmt7ZGlzcGxheTpmbGV4O2FsaWduLWl0ZW1zOmNlbnRlcjtiYWNrZ3JvdW5kOiMwYTBjMTA7Ym9yZGVyLXRvcDoycHggc29saWQgdmFyKC0tb3JhbmdlKX0KICAuY2h5cm9uIC5iayAudGFne2JhY2tncm91bmQ6dmFyKC0tb3JhbmdlKTtjb2xvcjojMTExO2ZvbnQtd2VpZ2h0OjkwMDtmb250LXNpemU6MTJweDtsZXR0ZXItc3BhY2luZzouMDZlbTtwYWRkaW5nOjhweCAxMnB4O3doaXRlLXNwYWNlOm5vd3JhcH0KICAuY2h5cm9uIC5iayAuc2Nyb2xsd3JhcHtmbGV4OjE7b3ZlcmZsb3c6aGlkZGVuO3Bvc2l0aW9uOnJlbGF0aXZlfQogIC5jaHlyb24gLmJrIC5zY3JvbGx7ZGlzcGxheTppbmxpbmUtZmxleDt3aGl0ZS1zcGFjZTpub3dyYXA7cGFkZGluZzo4cHggMDtmb250LXdlaWdodDo2MDA7Zm9udC1zaXplOjE0cHg7Y29sb3I6I2YyZDljNDthbmltYXRpb246YmttYXJxIHZhcigtLWJrZHVyLDc1cykgbGluZWFyIGluZmluaXRlO3dpbGwtY2hhbmdlOnRyYW5zZm9ybX0KICBAa2V5ZnJhbWVzIGJrbWFycXtmcm9te3RyYW5zZm9ybTp0cmFuc2xhdGVYKDApfXRve3RyYW5zZm9ybTp0cmFuc2xhdGVYKC01MCUpfX0KCiAgLyogbWluaSBjaGFydHMgcm93ICovCiAgLmNoYXJ0c3tkaXNwbGF5OmdyaWQ7Z3JpZC10ZW1wbGF0ZS1jb2x1bW5zOjFmciAxZnI7Z2FwOjlweDtmbGV4OjAgMCAxOTBweH0KICAubWN7YmFja2dyb3VuZDp2YXIoLS1wYW5lbCk7Ym9yZGVyOjFweCBzb2xpZCB2YXIoLS1saW5lKTtib3JkZXItcmFkaXVzOjE0cHg7cGFkZGluZzoxMXB4IDEzcHg7ZGlzcGxheTpmbGV4O2ZsZXgtZGlyZWN0aW9uOmNvbHVtbjttaW4td2lkdGg6MH0KICAubWMgLnRvcHtkaXNwbGF5OmZsZXg7YWxpZ24taXRlbXM6YmFzZWxpbmU7Z2FwOjEwcHg7ZmxleC13cmFwOndyYXB9CiAgLm1jIC5zeW17Zm9udC13ZWlnaHQ6OTAwO2ZvbnQtc2l6ZToxN3B4fQogIC5tYyAudmFse2ZvbnQtd2VpZ2h0OjcwMDtmb250LXNpemU6MTVweDtjb2xvcjp2YXIoLS1tdXRlZCk7bWFyZ2luLWxlZnQ6YXV0b30KICAubWMgLmNoZ3tmb250LXdlaWdodDo4MDA7Zm9udC1zaXplOjE0cHh9CiAgLm1jIGNhbnZhc3tmbGV4OjE7d2lkdGg6MTAwJTttaW4taGVpZ2h0OjA7bWFyZ2luLXRvcDo0cHh9CgogIC8qIGFzaWEgY2FyZHMgKi8KICAuYXNpYXtnYXA6MDtqdXN0aWZ5LWNvbnRlbnQ6c3RyZXRjaH0KICAuYWNhcmR7cGFkZGluZzowIDIwcHg7Ym9yZGVyLXRvcDoxcHggc29saWQgdmFyKC0tbGluZS0yKTtkaXNwbGF5OmZsZXg7ZmxleC1kaXJlY3Rpb246cm93O2FsaWduLWl0ZW1zOmNlbnRlcjtnYXA6MTJweDtmbGV4OjE7bWluLWhlaWdodDowO292ZXJmbG93OmhpZGRlbn0KICAuYWNhcmQ6Zmlyc3Qtb2YtdHlwZXtib3JkZXItdG9wOjB9CiAgLmFjYXJkIC5ubXtmb250LXdlaWdodDo3MDA7Zm9udC1zaXplOjE1cHg7Y29sb3I6dmFyKC0tbXV0ZWQpO2xldHRlci1zcGFjaW5nOi4wMmVtO2ZsZXg6MTttaW4td2lkdGg6MDt3aGl0ZS1zcGFjZTpub3dyYXA7b3ZlcmZsb3c6aGlkZGVuO3RleHQtb3ZlcmZsb3c6ZWxsaXBzaXN9CiAgLmFjYXJkIC52YWx7Zm9udC13ZWlnaHQ6ODAwO2ZvbnQtc2l6ZToyNnB4O2xpbmUtaGVpZ2h0OjF9CiAgLmFjYXJkIC5waWxse2ZvbnQtc2l6ZToxNHB4O3BhZGRpbmc6NXB4IDExcHg7bWluLXdpZHRoOjgycHg7anVzdGlmeS1jb250ZW50OmNlbnRlcn0KICAucGlsbHtkaXNwbGF5OmlubGluZS1mbGV4O2FsaWduLWl0ZW1zOmNlbnRlcjtnYXA6NnB4O2ZvbnQtd2VpZ2h0OjgwMDtmb250LXNpemU6MTVweDtwYWRkaW5nOjZweCAxMnB4O2JvcmRlci1yYWRpdXM6OXB4fQogIC5waWxsLnVwe2JhY2tncm91bmQ6dmFyKC0tdXAtYmcpO2NvbG9yOnZhcigtLXVwKTtib3gtc2hhZG93OjAgMCAwIDFweCByZ2JhKDIzLDIwMSwxMzEsLjMpIGluc2V0fQogIC5waWxsLmRvd257YmFja2dyb3VuZDp2YXIoLS1kb3duLWJnKTtjb2xvcjp2YXIoLS1kb3duKTtib3gtc2hhZG93OjAgMCAwIDFweCByZ2JhKDI1NSw1OSw3MSwuMykgaW5zZXR9CgogIC8qID09PT09PT09PT09PT09PT09PT09IENMQVNTSUMgQlVMTElPTiBMSVZFIFBBTkVMID09PT09PT09PT09PT09PT09PT09ICovCiAgLmJ1bGxpb24tcGFuZWx7ZmxleDowIDAgYXV0bztiYWNrZ3JvdW5kOnZhcigtLXBhbmVsKTtib3JkZXI6MXB4IHNvbGlkIHZhcigtLWxpbmUpO2JvcmRlci1yYWRpdXM6MTRweDtvdmVyZmxvdzpoaWRkZW47ZGlzcGxheTpmbGV4O2ZsZXgtZGlyZWN0aW9uOmNvbHVtbjtwb3NpdGlvbjpyZWxhdGl2ZX0KICAuYi10b3AtcGlsbHsKICAgIG1hcmdpbjoxcHggMTJweCA2cHg7CiAgICBiYWNrZ3JvdW5kOmxpbmVhci1ncmFkaWVudCgxODBkZWcsIzE2MWIyNiAwJSwjMGYxMzFjIDEwMCUpOwogICAgYm9yZGVyOjFweCBzb2xpZCByZ2JhKDI0MCwxODAsNDEsLjM4KTsKICAgIGJvcmRlci1yYWRpdXM6MjJweDsKICAgIHBhZGRpbmc6NHB4IDExcHg7CiAgICBkaXNwbGF5OmdyaWQ7CiAgICBncmlkLXRlbXBsYXRlLWNvbHVtbnM6MWZyIDFmciAxZnI7CiAgICBnYXA6OHB4OwogICAgYm94LXNoYWRvdzowIDRweCAxNnB4IHJnYmEoMCwwLDAsLjQ1KSwgaW5zZXQgMCAxcHggMCByZ2JhKDI1NSwyNTUsMjU1LC4wNik7CiAgfQogIC5iLXNwb3R7CiAgICBkaXNwbGF5OmZsZXg7CiAgICBmbGV4LWRpcmVjdGlvbjpjb2x1bW47CiAgICBhbGlnbi1pdGVtczpjZW50ZXI7CiAgICBqdXN0aWZ5LWNvbnRlbnQ6Y2VudGVyOwogICAgdGV4dC1hbGlnbjpjZW50ZXI7CiAgICBib3JkZXItcmlnaHQ6MXB4IHNvbGlkIHZhcigtLWxpbmUpOwogICAgcGFkZGluZzowIDRweDsKICB9CiAgLmItc3BvdDpsYXN0LWNoaWxke2JvcmRlci1yaWdodDowfQogIC5iLXNwb3QgLmxibHsKICAgIGZvbnQtd2VpZ2h0OjgwMDsKICAgIGZvbnQtc2l6ZToxMi41cHg7CiAgICBjb2xvcjp2YXIoLS1tdXRlZCk7CiAgICBsZXR0ZXItc3BhY2luZzouMDRlbTsKICAgIG1hcmdpbi1ib3R0b206M3B4OwogIH0KICAuYi1zcG90IC5tYWluLXZhbHsKICAgIGZvbnQtd2VpZ2h0OjkwMDsKICAgIGZvbnQtc2l6ZToyMnB4OwogICAgbGluZS1oZWlnaHQ6MS4wNTsKICAgIHBhZGRpbmc6MXB4IDZweDsKICAgIGJvcmRlci1yYWRpdXM6NXB4OwogICAgdHJhbnNpdGlvbjpjb2xvciAuMTVzOwogIH0KICAuYi1zcG90IC5obC1zdWJ7CiAgICBmb250LXdlaWdodDo3MDA7CiAgICBmb250LXNpemU6MTEuNXB4OwogICAgY29sb3I6dmFyKC0tbXV0ZWQpOwogICAgbWFyZ2luLXRvcDozcHg7CiAgICBsZXR0ZXItc3BhY2luZzouMDJlbTsKICB9CgogIC5iLXRibC1oZWFkewogICAgYmFja2dyb3VuZDpsaW5lYXItZ3JhZGllbnQoOTBkZWcsIzllN2IyZiAwJSwjYzc5ZjQyIDUwJSwjOWU3YjJmIDEwMCUpOwogICAgY29sb3I6IzA5MGIxMDsKICAgIGZvbnQtd2VpZ2h0OjkwMDsKICAgIGZvbnQtc2l6ZToxMi41cHg7CiAgICBsZXR0ZXItc3BhY2luZzouMTJlbTsKICAgIHBhZGRpbmc6NXB4IDE2cHg7CiAgICBkaXNwbGF5OmdyaWQ7CiAgICBncmlkLXRlbXBsYXRlLWNvbHVtbnM6MS4xZnIgMWZyOwogICAgdGV4dC10cmFuc2Zvcm06dXBwZXJjYXNlOwogIH0KICAuYi10YmwtaGVhZCBzcGFuOmxhc3QtY2hpbGR7dGV4dC1hbGlnbjpjZW50ZXJ9CgogIC5iLXJvd3N7ZGlzcGxheTpmbGV4O2ZsZXgtZGlyZWN0aW9uOmNvbHVtbn0KICAuYi1yb3d7CiAgICBkaXNwbGF5OmdyaWQ7CiAgICBncmlkLXRlbXBsYXRlLWNvbHVtbnM6MS4xZnIgMWZyOwogICAgYWxpZ24taXRlbXM6Y2VudGVyOwogICAgcGFkZGluZzoycHggMTZweDsKICAgIGJvcmRlci10b3A6MXB4IHNvbGlkIHZhcigtLWxpbmUtMik7CiAgICBiYWNrZ3JvdW5kOnJnYmEoMTEsMTMsMTgsLjkpOwogIH0KICAuYi1yb3c6bnRoLWNoaWxkKGV2ZW4pe2JhY2tncm91bmQ6cmdiYSgxNiwyMCwyOCwuNzUpfQogIC5iLXJvdyAuYi1uYW1lewogICAgZm9udC13ZWlnaHQ6ODAwOwogICAgZm9udC1zaXplOjE2LjVweDsKICAgIGNvbG9yOnZhcigtLWZnKTsKICAgIGxldHRlci1zcGFjaW5nOi4wM2VtOwogICAgYm9yZGVyLXJpZ2h0OjFweCBzb2xpZCB2YXIoLS1saW5lLTIpOwogICAgcGFkZGluZy1yaWdodDoxMHB4OwogIH0KICAuYi1yb3cgLmItY2VsbHsKICAgIGRpc3BsYXk6ZmxleDsKICAgIGZsZXgtZGlyZWN0aW9uOmNvbHVtbjsKICAgIGFsaWduLWl0ZW1zOmNlbnRlcjsKICAgIGp1c3RpZnktY29udGVudDpjZW50ZXI7CiAgICB0ZXh0LWFsaWduOmNlbnRlcjsKICAgIHBhZGRpbmctbGVmdDoxMHB4OwogIH0KICAuYi1yb3cgLmItcHJpY2V7CiAgICBmb250LXdlaWdodDo5MDA7CiAgICBmb250LXNpemU6MjBweDsKICAgIGxpbmUtaGVpZ2h0OjEuMDg7CiAgICBwYWRkaW5nOjFweCA4cHg7CiAgICBib3JkZXItcmFkaXVzOjVweDsKICAgIHRyYW5zaXRpb246Y29sb3IgLjE1czsKICB9CiAgLmItcm93IC5iLWhsewogICAgZm9udC13ZWlnaHQ6NjAwOwogICAgZm9udC1zaXplOjExLjVweDsKICAgIGNvbG9yOnZhcigtLW11dGVkKTsKICAgIG1hcmdpbi10b3A6MnB4OwogIH0KICAudmFsLXVwe2NvbG9yOnZhcigtLXVwKX0KICAudmFsLWRvd257Y29sb3I6dmFyKC0tZG93bil9CiAgLnZhbC1uZXV0cmFse2NvbG9yOiNmZmZ9CgogIC8qIERpcmVjdCBjcm9wcGVkIGNsYXNzaWNidWxsaW9uLmNvbSBpZnJhbWUgbW9kZSAqLwogIC5jYi1zaXRlLXdyYXB7CiAgICBkaXNwbGF5Om5vbmU7CiAgICB3aWR0aDoxMDAlOwogICAgaGVpZ2h0OjM3MnB4OwogICAgcG9zaXRpb246cmVsYXRpdmU7CiAgICBvdmVyZmxvdzpoaWRkZW47CiAgICBiYWNrZ3JvdW5kOiMxMTE7CiAgfQogIC5jYi1zaXRlLXdyYXAgaWZyYW1lewogICAgcG9zaXRpb246YWJzb2x1dGU7CiAgICB3aWR0aDoxMzYwcHg7CiAgICBoZWlnaHQ6OTIwcHg7CiAgICBsZWZ0OjUwJTsKICAgIHRvcDotMjk1cHg7CiAgICB0cmFuc2Zvcm06dHJhbnNsYXRlWCgtNTAlKSBzY2FsZSgwLjk1KTsKICAgIHRyYW5zZm9ybS1vcmlnaW46dG9wIGNlbnRlcjsKICAgIGJvcmRlcjowOwogIH0KCiAgLyogbWFjcm8gKi8KICAubWFjcm97ZmxleDoxO21pbi1oZWlnaHQ6MH0KICAubWFjcm97ZmxleDoxLjM7bWluLWhlaWdodDowfQogIC5taGVhZHtkaXNwbGF5OmdyaWQ7Z3JpZC10ZW1wbGF0ZS1jb2x1bW5zOjFmciA3OHB4IDg4cHg7Z2FwOjhweDtwYWRkaW5nOjhweCAxOHB4O2NvbG9yOnZhcigtLWZhaW50KTtmb250LXdlaWdodDo3MDA7Zm9udC1zaXplOjEzcHg7bGV0dGVyLXNwYWNpbmc6LjA5ZW19CiAgLm1oZWFkIC5ye3RleHQtYWxpZ246cmlnaHR9CiAgLm1ib2R5e292ZXJmbG93OmhpZGRlbjtmbGV4OjE7bWluLWhlaWdodDowO2Rpc3BsYXk6ZmxleDtmbGV4LWRpcmVjdGlvbjpjb2x1bW47anVzdGlmeS1jb250ZW50OnNwYWNlLWFyb3VuZH0KICAubXJvd3tkaXNwbGF5OmdyaWQ7Z3JpZC10ZW1wbGF0ZS1jb2x1bW5zOjFmciA3OHB4IDg4cHg7Z2FwOjhweDthbGlnbi1pdGVtczpjZW50ZXI7cGFkZGluZzo3cHggMThweDtib3JkZXItdG9wOjFweCBzb2xpZCB2YXIoLS1saW5lLTIpfQogIC5tcm93IC5ldntkaXNwbGF5OmZsZXg7YWxpZ24taXRlbXM6ZmxleC1zdGFydDtnYXA6MTFweDttaW4td2lkdGg6MH0KICAubXJvdyAucHItZG90e3dpZHRoOjExcHg7aGVpZ2h0OjExcHg7Ym9yZGVyLXJhZGl1czo1MCU7YmFja2dyb3VuZDp2YXIoLS1kb3duKTttYXJnaW4tdG9wOjZweDtmbGV4OjAgMCBhdXRvfQogIC5tcm93IC5ldnR7bWluLXdpZHRoOjB9CiAgLm1yb3cgLnQxe2ZvbnQtd2VpZ2h0OjcwMDtmb250LXNpemU6MTMuNXB4O2NvbG9yOnZhcigtLW11dGVkKX0KICAubXJvdyAudDEgYntjb2xvcjp2YXIoLS1hY2NlbnQpO2ZvbnQtd2VpZ2h0OjgwMDtsZXR0ZXItc3BhY2luZzouMDRlbX0KICAubXJvdyAudDJ7Zm9udC13ZWlnaHQ6ODAwO2ZvbnQtc2l6ZToxNi41cHg7Y29sb3I6dmFyKC0tZmcpO3doaXRlLXNwYWNlOm5vd3JhcDtvdmVyZmxvdzpoaWRkZW47dGV4dC1vdmVyZmxvdzplbGxpcHNpc30KICAubXJvdyAuYXt0ZXh0LWFsaWduOnJpZ2h0O2ZvbnQtd2VpZ2h0OjgwMDtmb250LXNpemU6MThweDtjb2xvcjp2YXIoLS1tdXRlZCl9CiAgLm1yb3cuZG9uZSAuYXtjb2xvcjp2YXIoLS11cCl9CiAgLm1yb3cgLmZ7dGV4dC1hbGlnbjpyaWdodDtmb250LXdlaWdodDo4MDA7Zm9udC1zaXplOjE4cHg7Y29sb3I6dmFyKC0tbXV0ZWQpfQoKICAvKiBjZW50cmFsIGJhbmsgcmF0ZXMgKi8KICAuY2JyYXRlc3tmbGV4OjE7bWluLWhlaWdodDowfQogIC5jYnItZ3JpZHtkaXNwbGF5OmdyaWQ7Z3JpZC10ZW1wbGF0ZS1jb2x1bW5zOjFmciAxZnI7Z2FwOjAgMTJweDtwYWRkaW5nOjJweCAxNnB4IDhweDtmbGV4OjE7bWluLWhlaWdodDowO2FsaWduLWNvbnRlbnQ6c3BhY2UtYXJvdW5kfQogIC5jYnJ7ZGlzcGxheTpmbGV4O2FsaWduLWl0ZW1zOmNlbnRlcjtqdXN0aWZ5LWNvbnRlbnQ6c3BhY2UtYmV0d2VlbjtwYWRkaW5nOjZweCAycHg7Ym9yZGVyLXRvcDoxcHggc29saWQgdmFyKC0tbGluZS0yKX0KICAuY2JyLWdyaWQgLmNicjpudGgtY2hpbGQoLW4rMil7Ym9yZGVyLXRvcDowfQogIC5jYnIgLmN7Zm9udC13ZWlnaHQ6ODAwO2ZvbnQtc2l6ZToxN3B4O2xldHRlci1zcGFjaW5nOi4wNGVtO2NvbG9yOnZhcigtLWZnKX0KICAuY2JyIC5ye2ZvbnQtd2VpZ2h0OjgwMDtmb250LXNpemU6MTdweDtjb2xvcjp2YXIoLS11cCk7YmFja2dyb3VuZDp2YXIoLS11cC1iZyk7cGFkZGluZzo0cHggMTJweDtib3JkZXItcmFkaXVzOjdweDttaW4td2lkdGg6NzBweDt0ZXh0LWFsaWduOnJpZ2h0fQogIC5jYnIgLnIubG93e2NvbG9yOnZhcigtLW11dGVkKTtiYWNrZ3JvdW5kOiMxNDE5MjJ9CiAgLmZsYWd7d2lkdGg6MjZweDtoZWlnaHQ6MThweDtib3JkZXItcmFkaXVzOjNweDtvYmplY3QtZml0OmNvdmVyO3ZlcnRpY2FsLWFsaWduOm1pZGRsZTttYXJnaW4tcmlnaHQ6OXB4O2JveC1zaGFkb3c6MCAwIDAgMXB4IHJnYmEoMjU1LDI1NSwyNTUsLjEyKX0KICAubXJvdyAudDEgLmZsYWd7d2lkdGg6MjBweDtoZWlnaHQ6MTRweDttYXJnaW4tcmlnaHQ6NnB4O21hcmdpbi1sZWZ0OjJweH0KCiAgQGtleWZyYW1lcyBwdWxzZXswJSwxMDAle29wYWNpdHk6MX01MCV7b3BhY2l0eTouM319CiAgLmRpbXtjb2xvcjp2YXIoLS1mYWludCl9CgogIC8qIGJvdHRvbSBsaXZlLW5ld3Mgc3RyaXAgKGZhc3RlciBzY3JvbGwgKyBpbWFnZXMpICovCiAgLm5ld3NiYXJ7YmFja2dyb3VuZDp2YXIoLS1wYW5lbCk7Ym9yZGVyOjFweCBzb2xpZCB2YXIoLS1saW5lKTtib3JkZXItcmFkaXVzOjE0cHg7cG9zaXRpb246cmVsYXRpdmU7b3ZlcmZsb3c6aGlkZGVufQogIC5uZXdzYmFyOjphZnRlcntjb250ZW50OiIiO3Bvc2l0aW9uOmFic29sdXRlO3RvcDowO2JvdHRvbTowO3JpZ2h0OjA7d2lkdGg6NzBweDt6LWluZGV4OjM7YmFja2dyb3VuZDpsaW5lYXItZ3JhZGllbnQoMjcwZGVnLHZhcigtLXBhbmVsKSx0cmFuc3BhcmVudCk7cG9pbnRlci1ldmVudHM6bm9uZX0KICAubmV3c2JhciAubGJse3Bvc2l0aW9uOmFic29sdXRlO3RvcDowO2JvdHRvbTowO2xlZnQ6MDt6LWluZGV4OjQ7ZGlzcGxheTpmbGV4O2FsaWduLWl0ZW1zOmNlbnRlcjtnYXA6OXB4O3BhZGRpbmc6MCAyMHB4IDAgMThweDtiYWNrZ3JvdW5kOmxpbmVhci1ncmFkaWVudCg5MGRlZyx2YXIoLS1wYW5lbCkgNzglLHRyYW5zcGFyZW50KTtmb250LXdlaWdodDo4MDA7Zm9udC1zaXplOjEzcHg7bGV0dGVyLXNwYWNpbmc6LjEyZW07Y29sb3I6dmFyKC0tbGl2ZSl9CiAgLm5ld3NiYXIgLmxibCAuZHt3aWR0aDo5cHg7aGVpZ2h0OjlweDtib3JkZXItcmFkaXVzOjUwJTtiYWNrZ3JvdW5kOnZhcigtLWxpdmUpO2JveC1zaGFkb3c6MCAwIDlweCB2YXIoLS1saXZlKTthbmltYXRpb246cHVsc2UgMS40cyBpbmZpbml0ZX0KICAubmV3cy10cmFja3tkaXNwbGF5OmZsZXg7Z2FwOjEycHg7YWxpZ24taXRlbXM6Y2VudGVyO2hlaWdodDoxMDAlO3BhZGRpbmc6MCAxOHB4IDAgMTUwcHg7d2lkdGg6bWF4LWNvbnRlbnQ7d2lsbC1jaGFuZ2U6dHJhbnNmb3JtfQogIC5uZXdzLXRyYWNrLnJ1bnthbmltYXRpb246bm1hcnEgdmFyKC0tbmR1ciw3MHMpIGxpbmVhciBpbmZpbml0ZX0KICAubmNhcmR7ZmxleDowIDAgNDMwcHg7ZGlzcGxheTpmbGV4O2dhcDoxMXB4O2JhY2tncm91bmQ6dmFyKC0tcGFuZWwtMik7Ym9yZGVyOjFweCBzb2xpZCB2YXIoLS1saW5lLTIpO2JvcmRlci1yYWRpdXM6MTBweDtwYWRkaW5nOjhweDtoZWlnaHQ6ODZweDthbGlnbi1pdGVtczpjZW50ZXJ9CiAgLm5jYXJkIC5uaW1ne2ZsZXg6MCAwIDEwOHB4O2hlaWdodDoxMDAlO2JvcmRlci1yYWRpdXM6N3B4O292ZXJmbG93OmhpZGRlbjtiYWNrZ3JvdW5kOmxpbmVhci1ncmFkaWVudCgxMzVkZWcsIzFjMjYzNCwjMGUxNDIwKTtkaXNwbGF5OmZsZXg7YWxpZ24taXRlbXM6Y2VudGVyO2p1c3RpZnktY29udGVudDpjZW50ZXJ9CiAgLm5jYXJkIC5uaW1nIGltZ3t3aWR0aDoxMDAlO2hlaWdodDoxMDAlO29iamVjdC1maXQ6Y292ZXJ9CiAgLm5jYXJkIC5uaW1nIC5waHtmb250LXdlaWdodDo4MDA7Zm9udC1zaXplOjIwcHg7Y29sb3I6dmFyKC0tZmFpbnQpfQogIC5uY2FyZCAubmJ7bWluLXdpZHRoOjA7ZGlzcGxheTpmbGV4O2ZsZXgtZGlyZWN0aW9uOmNvbHVtbjtnYXA6NHB4fQogIC5uY2FyZCAubnN7Zm9udC13ZWlnaHQ6ODAwO2ZvbnQtc2l6ZToxMXB4O2NvbG9yOnZhcigtLWFjY2VudCk7dGV4dC10cmFuc2Zvcm06dXBwZXJjYXNlO2xldHRlci1zcGFjaW5nOi4wNmVtfQogIC5uY2FyZCAubnMgLnRte2NvbG9yOnZhcigtLWZhaW50KX0KICAubmNhcmQgLm50e2ZvbnQtd2VpZ2h0OjcwMDtmb250LXNpemU6MTRweDtsaW5lLWhlaWdodDoxLjI4O2Rpc3BsYXk6LXdlYmtpdC1ib3g7LXdlYmtpdC1saW5lLWNsYW1wOjI7LXdlYmtpdC1ib3gtb3JpZW50OnZlcnRpY2FsO292ZXJmbG93OmhpZGRlbn0KICBAa2V5ZnJhbWVzIG5tYXJxe2Zyb217dHJhbnNmb3JtOnRyYW5zbGF0ZVgoMCl9dG97dHJhbnNmb3JtOnRyYW5zbGF0ZVgoLTUwJSl9fQo8L3N0eWxlPgo8L2hlYWQ+Cjxib2R5Pgo8ZGl2IGNsYXNzPSJhcHAiPgoKICA8IS0tIEhFQURFUiAtLT4KICA8aGVhZGVyIGNsYXNzPSJoZWFkIj4KICAgIDxkaXYgY2xhc3M9Imh0aXRsZSI+TUFSS0VUIENPTU1BTkQgQ0VOVEVSPC9kaXY+CiAgICA8ZGl2IGNsYXNzPSJoc2VzcyIgaWQ9InNlc3MiPjwvZGl2PgogICAgPGRpdiBzdHlsZT0iZGlzcGxheTpmbGV4O2FsaWduLWl0ZW1zOmNlbnRlcjtnYXA6MThweCI+CiAgICAgIDxzcGFuIGNsYXNzPSJobGl2ZSBzdGFsZSIgaWQ9ImhsaXZlIj48c3BhbiBjbGFzcz0iZG90Ij48L3NwYW4+PHNwYW4gaWQ9InVwZHR4dCI+Q09OTkVDVElORzwvc3Bhbj48L3NwYW4+CiAgICAgIDxkaXYgY2xhc3M9ImhjbG9jayIgaWQ9ImNsb2NrIj7igJQ8L2Rpdj4KICAgIDwvZGl2PgogIDwvaGVhZGVyPgoKICA8IS0tIFRJQ0tFUiAoc2Nyb2xsaW5nIG1hcnF1ZWUpIC0tPgogIDxkaXYgY2xhc3M9InRpY2siPjxkaXYgY2xhc3M9InRpY2stdHJhY2siIGlkPSJ0aWNrVHJhY2siPjwvZGl2PjwvZGl2PgoKICA8IS0tIE1BSU4gLS0+CiAgPGRpdiBjbGFzcz0ibWFpbiI+CgogICAgPCEtLSBDT0wgMTogdmlkZW8gKyBjaGFydHMgLS0+CiAgICA8ZGl2IGNsYXNzPSJjb2wiPgogICAgICA8ZGl2IGNsYXNzPSJ2aWQiPgogICAgICAgIDxkaXYgY2xhc3M9InZ0bCI+PHNwYW4gY2xhc3M9Inl0Ij4mIzk2NTQ7IFlvdVR1YmUgTElWRTwvc3Bhbj4gJmJ1bGw7IEJsb29tYmVyZyBCdXNpbmVzcyBOZXdzPC9kaXY+CiAgICAgICAgPGRpdiBjbGFzcz0idnRyIj48c3BhbiBjbGFzcz0iZCI+PC9zcGFuPkxJVkU8L2Rpdj4KICAgICAgICA8ZGl2IGNsYXNzPSJ2YnIiPkJMT09NQkVSRyA8Yj5MSVZFPC9iPjwvZGl2PgogICAgICAgIDxpZnJhbWUgaWQ9InR2IiBzcmM9Imh0dHBzOi8vd3d3LnlvdXR1YmUtbm9jb29raWUuY29tL2VtYmVkL2xpdmVfc3RyZWFtP2NoYW5uZWw9VUNJQUxNS3ZPYlpOdEo2QW1kQ0xQN0xnJmF1dG9wbGF5PTEmbXV0ZT0xJnBsYXlzaW5saW5lPTEmcmVsPTAiCiAgICAgICAgICBhbGxvdz0iYXV0b3BsYXk7IGVuY3J5cHRlZC1tZWRpYTsgcGljdHVyZS1pbi1waWN0dXJlIiBhbGxvd2Z1bGxzY3JlZW4gcmVmZXJyZXJwb2xpY3k9Im9yaWdpbi13aGVuLWNyb3NzLW9yaWdpbiI+PC9pZnJhbWU+CiAgICAgICAgPGRpdiBjbGFzcz0iY2h5cm9uIj4KICAgICAgICAgIDxkaXYgY2xhc3M9ImhsIiBpZD0iY2h5SEwiPiZuYnNwOzwvZGl2PgogICAgICAgICAgPGRpdiBjbGFzcz0iYmsiPjxkaXYgY2xhc3M9InRhZyI+QlJFQUtJTkcgTkVXUzwvZGl2PjxkaXYgY2xhc3M9InNjcm9sbHdyYXAiPjxkaXYgY2xhc3M9InNjcm9sbCIgaWQ9ImNoeVNjcm9sbCI+PC9kaXY+PC9kaXY+PC9kaXY+CiAgICAgICAgPC9kaXY+CiAgICAgIDwvZGl2PgogICAgPC9kaXY+CgogICAgPCEtLSBDT0wgMjogbWFjcm8gKyBjZW50cmFsIGJhbmsgcmF0ZXMgLS0+CiAgICA8ZGl2IGNsYXNzPSJjb2wiPgogICAgICA8ZGl2IGNsYXNzPSJwYW5lbCBtYWNybyIgc3R5bGU9ImZsZXg6MS4yNSI+CiAgICAgICAgPGRpdiBjbGFzcz0icHRpdGxlIj5NQUNSTyBFQ09OT01JQyBDQUxFTkRBUjwvZGl2PgogICAgICAgIDxkaXYgY2xhc3M9Im1oZWFkIj48c3Bhbj5QUklPUklUWTwvc3Bhbj48c3BhbiBjbGFzcz0iciI+QUNUVUFMPC9zcGFuPjxzcGFuIGNsYXNzPSJyIj5GT1JFQ0FTVDwvc3Bhbj48L2Rpdj4KICAgICAgICA8ZGl2IGNsYXNzPSJtYm9keSIgaWQ9Im1hY3JvIj48L2Rpdj4KICAgICAgPC9kaXY+CiAgICAgIDxkaXYgY2xhc3M9InBhbmVsIGNicmF0ZXMiIHN0eWxlPSJmbGV4OjEiPgogICAgICAgIDxkaXYgY2xhc3M9InB0aXRsZSI+Q0VOVFJBTCBCQU5LIFJBVEVTPC9kaXY+CiAgICAgICAgPGRpdiBjbGFzcz0iY2JyLWdyaWQiIGlkPSJjYnJHcmlkIj48L2Rpdj4KICAgICAgPC9kaXY+CiAgICA8L2Rpdj4KCiAgICA8IS0tIENPTCAzOiBDTEFTU0lDIEJVTExJT04gKyBBU0lBIFNUT0NLUyAtLT4KICAgIDxkaXYgY2xhc3M9ImNvbCI+CiAgICAgIDxkaXYgY2xhc3M9ImJ1bGxpb24tcGFuZWwiPgogICAgICAgIDxkaXYgY2xhc3M9InB0aXRsZSI+CiAgICAgICAgICA8c3Bhbj5DTEFTU0lDIEJVTExJT04gJm1pZGRvdDsgTElWRSBSQVRFUzwvc3Bhbj4KICAgICAgICAgIDxkaXYgc3R5bGU9ImRpc3BsYXk6ZmxleDthbGlnbi1pdGVtczpjZW50ZXI7Z2FwOjhweCI+CiAgICAgICAgICAgIDxzcGFuIGNsYXNzPSJwc3ViIj48c3BhbiBjbGFzcz0iYmRvdCI+PC9zcGFuPjxzcGFuIGlkPSJjYlN0YXR1cyI+TElWRSBTWU5DPC9zcGFuPjwvc3Bhbj4KICAgICAgICAgICAgPGJ1dHRvbiBjbGFzcz0ibW9kZS1idG4gYWN0aXZlIiBpZD0iYnRuQ2JEYXJrIiBvbmNsaWNrPSJzZXRDYlZpZXcoJ2RhcmsnKSI+REFSSyBVSTwvYnV0dG9uPgogICAgICAgICAgICA8YnV0dG9uIGNsYXNzPSJtb2RlLWJ0biIgaWQ9ImJ0bkNiU2l0ZSIgb25jbGljaz0ic2V0Q2JWaWV3KCdzaXRlJykiPk9SSUdJTkFMIFNJVEU8L2J1dHRvbj4KICAgICAgICAgIDwvZGl2PgogICAgICAgIDwvZGl2PgoKICAgICAgICA8IS0tIE1PREUgMTogREFSSyBVSSBQQU5FTCAoMTAwJSBDQUxJQlJBVEVEICsgRElSRUNUIFBST1hZIEJSSURHRSkgLS0+CiAgICAgICAgPGRpdiBpZD0iY2JEYXJrQm94Ij4KICAgICAgICAgIDxkaXYgY2xhc3M9ImItdG9wLXBpbGwiPgogICAgICAgICAgICA8ZGl2IGNsYXNzPSJiLXNwb3QiPgogICAgICAgICAgICAgIDxkaXYgY2xhc3M9ImxibCI+R09MRCBPeiBCaWQ8L2Rpdj4KICAgICAgICAgICAgICA8ZGl2IGNsYXNzPSJtYWluLXZhbCB2YWwtbmV1dHJhbCIgaWQ9ImNiLWJpZCI+MzkwMC41NTwvZGl2PgogICAgICAgICAgICAgIDxkaXYgY2xhc3M9ImhsLXN1YiIgaWQ9ImNiLWJpZC1oIj5IOiAzOTI0LjA0PC9kaXY+CiAgICAgICAgICAgIDwvZGl2PgogICAgICAgICAgICA8ZGl2IGNsYXNzPSJiLXNwb3QiPgogICAgICAgICAgICAgIDxkaXYgY2xhc3M9ImxibCI+R09MRCBPeiBBc2s8L2Rpdj4KICAgICAgICAgICAgICA8ZGl2IGNsYXNzPSJtYWluLXZhbCB2YWwtbmV1dHJhbCIgaWQ9ImNiLWFzayI+MzkwMi4wNTwvZGl2PgogICAgICAgICAgICAgIDxkaXYgY2xhc3M9ImhsLXN1YiIgaWQ9ImNiLWFzay1sIj5MOiAzODczLjEyPC9kaXY+CiAgICAgICAgICAgIDwvZGl2PgogICAgICAgICAgICA8ZGl2IGNsYXNzPSJiLXNwb3QiPgogICAgICAgICAgICAgIDxkaXYgY2xhc3M9ImxibCI+VVNESU5SIEFzazwvZGl2PgogICAgICAgICAgICAgIDxkaXYgY2xhc3M9Im1haW4tdmFsIHZhbC1uZXV0cmFsIiBpZD0iY2ItaW5yIj45NC4zMTM8L2Rpdj4KICAgICAgICAgICAgICA8ZGl2IGNsYXNzPSJobC1zdWIiIGlkPSJjYi1pbnItaGwiPkg6IDk0LjM0OSB8IEw6IDk0LjExODwvZGl2PgogICAgICAgICAgICA8L2Rpdj4KICAgICAgICAgIDwvZGl2PgoKICAgICAgICAgIDxkaXYgY2xhc3M9ImItdGJsLWhlYWQiPgogICAgICAgICAgICA8c3Bhbj5CVUxMSU9OPC9zcGFuPgogICAgICAgICAgICA8c3Bhbj5QUklDRTwvc3Bhbj4KICAgICAgICAgIDwvZGl2PgoKICAgICAgICAgIDxkaXYgY2xhc3M9ImItcm93cyI+CiAgICAgICAgICAgIDxkaXYgY2xhc3M9ImItcm93Ij4KICAgICAgICAgICAgICA8ZGl2IGNsYXNzPSJiLW5hbWUiPlNJTFZFUiBPejwvZGl2PgogICAgICAgICAgICAgIDxkaXYgY2xhc3M9ImItY2VsbCI+CiAgICAgICAgICAgICAgICA8ZGl2IGNsYXNzPSJiLXByaWNlIHZhbC1uZXV0cmFsIiBpZD0iY2Itc2lsIj41Mi4wOTwvZGl2PgogICAgICAgICAgICAgICAgPGRpdiBjbGFzcz0iYi1obCIgaWQ9ImNiLXNpbC1obCI+SDogNTIuODEgfCBMOiA1MS4yMTwvZGl2PgogICAgICAgICAgICAgIDwvZGl2PgogICAgICAgICAgICA8L2Rpdj4KICAgICAgICAgICAgPGRpdiBjbGFzcz0iYi1yb3ciPgogICAgICAgICAgICAgIDxkaXYgY2xhc3M9ImItbmFtZSI+R09MRCAxIEdNPC9kaXY+CiAgICAgICAgICAgICAgPGRpdiBjbGFzcz0iYi1jZWxsIj4KICAgICAgICAgICAgICAgIDxkaXYgY2xhc3M9ImItcHJpY2UgdmFsLW5ldXRyYWwiIGlkPSJjYi0xZ20iPjQ2My4xODwvZGl2PgogICAgICAgICAgICAgICAgPGRpdiBjbGFzcz0iYi1obCIgaWQ9ImNiLTFnbS1obCI+SDogNDY1Ljc4IHwgTDogNDU5Ljc3PC9kaXY+CiAgICAgICAgICAgICAgPC9kaXY+CiAgICAgICAgICAgIDwvZGl2PgogICAgICAgICAgICA8ZGl2IGNsYXNzPSJiLXJvdyI+CiAgICAgICAgICAgICAgPGRpdiBjbGFzcz0iYi1uYW1lIj5HT0xEIDk5OTk8L2Rpdj4KICAgICAgICAgICAgICA8ZGl2IGNsYXNzPSJiLWNlbGwiPgogICAgICAgICAgICAgICAgPGRpdiBjbGFzcz0iYi1wcmljZSB2YWwtbmV1dHJhbCIgaWQ9ImNiLTk5OTkiPjQ2MTk2MzwvZGl2PgogICAgICAgICAgICAgICAgPGRpdiBjbGFzcz0iYi1obCIgaWQ9ImNiLTk5OTktaGwiPkg6IDQ2NDU2MCB8IEw6IDQ1ODU0NjwvZGl2PgogICAgICAgICAgICAgIDwvZGl2PgogICAgICAgICAgICA8L2Rpdj4KICAgICAgICAgIDwvZGl2PgogICAgICAgIDwvZGl2PgoKICAgICAgICA8IS0tIE1PREUgMjogRElSRUNUIENST1BQRUQgQ0xBU1NJQ0JVTExJT04uQ09NIExJVkUgRU1CRUQgLS0+CiAgICAgICAgPGRpdiBjbGFzcz0iY2Itc2l0ZS13cmFwIiBpZD0iY2JTaXRlQm94Ij4KICAgICAgICAgIDxpZnJhbWUgaWQ9ImNiRGlyZWN0SWZyYW1lIiBzcmM9Ii9wcm94eS9jbGFzc2ljYnVsbGlvbiI+PC9pZnJhbWU+CiAgICAgICAgPC9kaXY+CiAgICAgIDwvZGl2PgoKICAgICAgPGRpdiBjbGFzcz0icGFuZWwgYXNpYSIgc3R5bGU9ImZsZXg6MSI+CiAgICAgICAgPGRpdiBjbGFzcz0icHRpdGxlIj5BU0lBIFNUT0NLUyBJTlRSQURBWTwvZGl2PgogICAgICAgIDxkaXYgaWQ9ImFzaWEiIHN0eWxlPSJkaXNwbGF5OmZsZXg7ZmxleC1kaXJlY3Rpb246Y29sdW1uO2ZsZXg6MTttaW4taGVpZ2h0OjAiPjwvZGl2PgogICAgICA8L2Rpdj4KICAgIDwvZGl2PgogIDwvZGl2PgoKICA8IS0tIEJPVFRPTSBMSVZFIE5FV1MgU1RSSVAgLS0+CiAgPGRpdiBjbGFzcz0ibmV3c2JhciI+CiAgICA8ZGl2IGNsYXNzPSJsYmwiPjxzcGFuIGNsYXNzPSJkIj48L3NwYW4+TElWRSBORVdTPC9kaXY+CiAgICA8ZGl2IGNsYXNzPSJuZXdzLXRyYWNrIiBpZD0ibmV3c1N0cmlwIj48L2Rpdj4KICA8L2Rpdj4KPC9kaXY+Cgo8c2NyaXB0PgovKiAtLS0tLS0tLS0tLS0tLS0tIGhlbHBlcnMgLS0tLS0tLS0tLS0tLS0tLSAqLwp2YXIgJD1mdW5jdGlvbihpZCl7cmV0dXJuIGRvY3VtZW50LmdldEVsZW1lbnRCeUlkKGlkKX07CmZ1bmN0aW9uIGRlYyhzeW0sdil7IGlmKHN5bT09PSdEWC1ZLk5ZQicpcmV0dXJuIDM7IGlmKHN5bT09PSdCVEMtVVNEJylyZXR1cm4gKHY+PTEwMDA/MDoyKTsgaWYoc3ltPT09J0dDPUYnfHxzeW09PT0nQ0w9RicpcmV0dXJuIDI7IGlmKHY+PTEwMDApcmV0dXJuIDI7IHJldHVybiAyOyB9CmZ1bmN0aW9uIGZtdCh2LGQpeyBpZih2PT1udWxsfHxpc05hTih2KSlyZXR1cm4gJ+KAlCc7IHJldHVybiBOdW1iZXIodikudG9Mb2NhbGVTdHJpbmcoJ2VuLVVTJyx7bWluaW11bUZyYWN0aW9uRGlnaXRzOmQsbWF4aW11bUZyYWN0aW9uRGlnaXRzOmR9KTsgfQpmdW5jdGlvbiBwY3RUeHQocCl7IGlmKHA9PW51bGx8fGlzTmFOKHApKXJldHVybiAnJzsgcmV0dXJuIChwPj0wPycrJzonJykrcC50b0ZpeGVkKDIpKyclJzsgfQpmdW5jdGlvbiBhcnJvdyhwKXsgcmV0dXJuIHA+PTA/J+KWsic6J+KWvCc7IH0KZnVuY3Rpb24gZXNjKHMpe3ZhciBkPWRvY3VtZW50LmNyZWF0ZUVsZW1lbnQoJ2RpdicpO2QudGV4dENvbnRlbnQ9c3x8Jyc7cmV0dXJuIGQuaW5uZXJIVE1MO30KCmZ1bmN0aW9uIHNldENiVmlldyhtb2RlKXsKICAkKCdjYkRhcmtCb3gnKS5zdHlsZS5kaXNwbGF5ID0gKG1vZGU9PT0nZGFyaycpID8gJ2Jsb2NrJyA6ICdub25lJzsKICAkKCdjYlNpdGVCb3gnKS5zdHlsZS5kaXNwbGF5ID0gKG1vZGU9PT0nc2l0ZScpID8gJ2Jsb2NrJyA6ICdub25lJzsKICAkKCdidG5DYkRhcmsnKS5jbGFzc05hbWUgPSAnbW9kZS1idG4nICsgKG1vZGU9PT0nZGFyaycgPyAnIGFjdGl2ZScgOiAnJyk7CiAgJCgnYnRuQ2JTaXRlJykuY2xhc3NOYW1lID0gJ21vZGUtYnRuJyArIChtb2RlPT09J3NpdGUnID8gJyBhY3RpdmUnIDogJycpOwp9CgovKiAtLS0tLS0tLS0tLS0tLS0tIGNsb2NrICsgc2Vzc2lvbnMgLS0tLS0tLS0tLS0tLS0tLSAqLwp2YXIgQ0lUWT1bWydOWScsJ0FtZXJpY2EvTmV3X1lvcmsnXSxbJ0xETicsJ0V1cm9wZS9Mb25kb24nXSxbJ1RZTycsJ0FzaWEvVG9reW8nXSxbJ1NZRCcsJ0F1c3RyYWxpYS9TeWRuZXknXV07CmZ1bmN0aW9uIGNsb2NrKCl7CiAgdmFyIG49bmV3IERhdGUoKTsKICAkKCdjbG9jaycpLmlubmVySFRNTD0nPGI+JytuLnRvTG9jYWxlVGltZVN0cmluZygnZW4tVVMnLHtob3VyOicyLWRpZ2l0JyxtaW51dGU6JzItZGlnaXQnLHNlY29uZDonMi1kaWdpdCd9KSsnPC9iPiAvICcrbi50b0xvY2FsZURhdGVTdHJpbmcoJ2VuLUdCJyx7d2Vla2RheTonc2hvcnQnLGRheTonMi1kaWdpdCcsbW9udGg6J3Nob3J0Jyx5ZWFyOidudW1lcmljJ30pLnRvVXBwZXJDYXNlKCk7CiAgJCgnc2VzcycpLmlubmVySFRNTD1DSVRZLm1hcChmdW5jdGlvbihjKXsKICAgIHZhciB0PW5ldyBEYXRlKCkudG9Mb2NhbGVUaW1lU3RyaW5nKCdlbi1HQicse3RpbWVab25lOmNbMV0saG91cjonMi1kaWdpdCcsbWludXRlOicyLWRpZ2l0J30pOwogICAgcmV0dXJuICc8c3Bhbj4nK2NbMF0rJzxiPicrdCsnPC9iPjwvc3Bhbj4nOwogIH0pLmpvaW4oJycpOwogIHZhciBobD0kKCdobGl2ZScpLHV0PSQoJ3VwZHR4dCcpOwogIGlmKGhsJiZ1dCl7CiAgICBpZihsYXN0VXBkJiYoRGF0ZS5ub3coKS1sYXN0VXBkKTw2MDAwMCl7aGwuY2xhc3NOYW1lPSdobGl2ZSc7dXQudGV4dENvbnRlbnQ9J0xJVkUgwrcgJytuZXcgRGF0ZShsYXN0VXBkKS50b0xvY2FsZVRpbWVTdHJpbmcoJ2VuLVVTJyx7aG91cjEyOmZhbHNlfSk7fQogICAgZWxzZXtobC5jbGFzc05hbWU9J2hsaXZlIHN0YWxlJzt1dC50ZXh0Q29udGVudD1sYXN0VXBkPydSRUNPTk5FQ1RJTkcnOidDT05ORUNUSU5HJzt9CiAgfQp9CmNsb2NrKCk7c2V0SW50ZXJ2YWwoY2xvY2ssMTAwMCk7CgovKiAtLS0tLS0tLS0tLS0tLS0tIHF1b3RlcyAtLS0tLS0tLS0tLS0tLS0tICovCnZhciBRPXt9LCBQUkVWPXt9LCBsYXN0VXBkPTA7CmZ1bmN0aW9uIGNoQ2xzKHMscHJpY2Upe3ZhciBwPVBSRVZbc107aWYocD09bnVsbHx8cHJpY2U9PW51bGx8fHByaWNlPT09cClyZXR1cm4gJyc7cmV0dXJuIHByaWNlPnA/JyBmbHVwJzonIGZsZG4nO30KdmFyIFRJQ0tFUj1bJ15HU1BDJywnXk5EWCcsJ0RYLVkuTllCJywnR0M9RicsJ0NMPUYnLCdCVEMtVVNEJ107CnZhciBUTkFNRT17J15HU1BDJzonUyZQIDUwMCcsJ15ORFgnOidOQVNEQVEgMTAwJywnRFgtWS5OWUInOidEWFknLCdHQz1GJzonR09MRCcsJ0NMPUYnOidXVEknLCdCVEMtVVNEJzonQlRDVVNEJ307CnZhciBBU0lBPVsnQUFYSicsJ15OMjI1JywnXktTMTEnLCdeVFdJSScsJ15IU0knLCdeR1NQQycsJ15SVVQnXTsKdmFyIEFOQU1FPXsnQUFYSic6J01TQ0kgQUMgQVNJQSBQQUMnLCdeTjIyNSc6J05JS0tFSSAyMjUnLCdeS1MxMSc6J0tPU1BJJywnXlRXSUknOidUQUlQRUkgVFdTRScsJ15IU0knOidIQU5HIFNFTkcnLCdeR1NQQyc6J1MmUCA1MDAnLCdeUlVUJzonUlVTU0VMTCAyMDAwJ307CgpmdW5jdGlvbiB0a0l0ZW0ocyl7CiAgdmFyIHE9UVtzXXx8e307dmFyIHVwPShxLnBjdHx8MCk+PTA7CiAgcmV0dXJuICc8ZGl2IGNsYXNzPSJ0ayI+PHNwYW4gY2xhc3M9Im5tICcrKHVwPyd1cCc6J2Rvd24nKSsnIiBkYXRhLW5tPSInK3MrJyI+JytUTkFNRVtzXSsnPC9zcGFuPicrCiAgICAnPHNwYW4gY2xhc3M9InByIiBkYXRhLXByPSInK3MrJyI+JytmbXQocS5wcmljZSxkZWMocyxxLnByaWNlKSkrJzwvc3Bhbj4nKwogICAgJzxzcGFuIGNsYXNzPSJjaCAnKyh1cD8ndXAnOidkb3duJykrJyIgZGF0YS1jaD0iJytzKyciPicrYXJyb3cocS5wY3R8fDApKycgJytwY3RUeHQocS5wY3QpKyc8L3NwYW4+PC9kaXY+JzsKfQpmdW5jdGlvbiBidWlsZFRpY2tlcigpewogIHZhciB0cj0kKCd0aWNrVHJhY2snKTtpZighdHIpcmV0dXJuOwogIHZhciBvbmU9VElDS0VSLm1hcCh0a0l0ZW0pLmpvaW4oJycpOwogIHRyLmlubmVySFRNTD1vbmUrb25lOyAgLyogZHVwbGljYXRlIGZvciBzZWFtbGVzcyBsb29wICovCiAgdHIuc3R5bGUuc2V0UHJvcGVydHkoJy0tdGR1cicsTWF0aC5tYXgoMjYsTWF0aC5yb3VuZCh0ci5zY3JvbGxXaWR0aC8yLzk1KSkrJ3MnKTsKfQovKiB1cGRhdGUgcHJpY2VzL3BjdCBpbiBwbGFjZSAobm8gaW5uZXJIVE1MIHJlYnVpbGQpIHNvIHRoZSBtYXJxdWVlIG5ldmVyIGp1bXBzICovCmZ1bmN0aW9uIHJlbmRlclRpY2tlcigpewogIHZhciB0cj0kKCd0aWNrVHJhY2snKTtpZighdHIpcmV0dXJuOwogIGlmKCF0ci5jaGlsZHJlbi5sZW5ndGgpe2J1aWxkVGlja2VyKCk7cmV0dXJuO30KICBUSUNLRVIuZm9yRWFjaChmdW5jdGlvbihzKXsKICAgIHZhciBxPVFbc118fHt9O3ZhciB1cD0ocS5wY3R8fDApPj0wO3ZhciBmbD1jaENscyhzLHEucHJpY2UpOwogICAgdmFyIHB2PWZtdChxLnByaWNlLGRlYyhzLHEucHJpY2UpKTsKICAgIGRvY3VtZW50LnF1ZXJ5U2VsZWN0b3JBbGwoJ1tkYXRhLXByPSInK3MrJyJdJykuZm9yRWFjaChmdW5jdGlvbihlKXsKICAgICAgaWYoZS50ZXh0Q29udGVudCE9PXB2KXtlLnRleHRDb250ZW50PXB2O2UuY2xhc3NMaXN0LnJlbW92ZSgnZmx1cCcsJ2ZsZG4nKTtpZihmbCl7dm9pZCBlLm9mZnNldFdpZHRoO2UuY2xhc3NMaXN0LmFkZChmbC50cmltKCkpO319CiAgICB9KTsKICAgIGRvY3VtZW50LnF1ZXJ5U2VsZWN0b3JBbGwoJ1tkYXRhLWNoPSInK3MrJyJdJykuZm9yRWFjaChmdW5jdGlvbihlKXtlLnRleHRDb250ZW50PWFycm93KHEucGN0fHwwKSsnICcrcGN0VHh0KHEucGN0KTtlLmNsYXNzTmFtZT0nY2ggJysodXA/J3VwJzonZG93bicpO30pOwogICAgZG9jdW1lbnQucXVlcnlTZWxlY3RvckFsbCgnW2RhdGEtbm09IicrcysnIl0nKS5mb3JFYWNoKGZ1bmN0aW9uKGUpe2UuY2xhc3NOYW1lPSdubSAnKyh1cD8ndXAnOidkb3duJyk7fSk7CiAgfSk7CiAgdHIuc3R5bGUuc2V0UHJvcGVydHkoJy0tdGR1cicsTWF0aC5tYXgoMjYsTWF0aC5yb3VuZCh0ci5zY3JvbGxXaWR0aC8yLzk1KSkrJ3MnKTsgIC8qIGNvbnN0YW50IHNwZWVkIGFzIHdpZHRocyBzZXR0bGUgKi8KfQpmdW5jdGlvbiByZW5kZXJBc2lhKCl7CiAgJCgnYXNpYScpLmlubmVySFRNTD1BU0lBLm1hcChmdW5jdGlvbihzKXsKICAgIHZhciBxPVFbc118fHt9O3ZhciB1cD0ocS5wY3R8fDApPj0wOwogICAgcmV0dXJuICc8ZGl2IGNsYXNzPSJhY2FyZCI+PGRpdiBjbGFzcz0ibm0iPicrQU5BTUVbc10rJzwvZGl2PicrCiAgICAgICc8ZGl2IGNsYXNzPSJ2YWwnK2NoQ2xzKHMscS5wcmljZSkrJyI+JytmbXQocS5wcmljZSwyKSsnPC9kaXY+JysKICAgICAgJzxkaXYgY2xhc3M9InBpbGwgJysodXA/J3VwJzonZG93bicpKyciPicrYXJyb3cocS5wY3R8fDApKycgJytwY3RUeHQocS5wY3QpKyc8L2Rpdj48L2Rpdj4nOwogIH0pLmpvaW4oJycpOwp9CgovKiA9PT09PT09PT09PT09PT09PT09PSBDTEFTU0lDIEJVTExJT04gRVhBQ1QgTElWRSBFTkdJTkUgPT09PT09PT09PT09PT09PT09PT0KICAgMS4gTGlzdGVucyB0byBkaXJlY3QgRE9NIHN5bmMgZnJvbSAvcHJveHkvY2xhc3NpY2J1bGxpb24gaWZyYW1lIChpZiBjbGFzc2ljYnVsbGlvbi5jb20gc29ja2V0IHN0cmVhbXMpCiAgIDIuIFBvbGxzIHJlYWwtdGltZSBTcG90IEdvbGQgKFRWQzpHT0xEKSwgU3BvdCBTaWx2ZXIgKFRWQzpTSUxWRVIpLCBhbmQgU3BvdCBVU0RJTlIgKEZYX0lEQzpVU0RJTlIpCiAgICAgIGV2ZXJ5IDEgc2Vjb25kIHdpdGggQ2xhc3NpYyBCdWxsaW9uJ3MgZXhhY3QgRHViYWkgZGVhbGVyIGNhbGlicmF0aW9uOgogICAgICAtIEdPTEQgT3ogQmlkID0gU3BvdF9Hb2xkIC0gMC43MAogICAgICAtIEdPTEQgT3ogQXNrID0gU3BvdF9Hb2xkICsgMC44MCAoMS41MCBzcHJlYWQpCiAgICAgIC0gU0lMVkVSIE96ICAgPSBTcG90X1NpbHZlciArIDIuMDIgKENPTUVYIC8gRHViYWkgQnVsbGlvbiBiYXNpcykKICAgICAgLSBVU0RJTlIgQXNrICA9IFNwb3RfVVNESU5SICsgMC4xMTUKICAgICAgLSBLX0FFRF9HUkFNICA9IDMuNjc0IC8gMzEuMTAzNQogICAgICAtIEdPTEQgMSBHTSAgID0gQXNrICogS19BRURfR1JBTSArIDIuMjYzCiAgICAgIC0gR09MRCA5OTUgICAgPSBBc2sgKiBLX0FFRF9HUkFNICogOTk1ICsgMTY3NC4zCiAgICAgIC0gR09MRCA5OTk5ICAgPSBBc2sgKiBLX0FFRF9HUkFNICogOTk5LjkgKyAxMDkyLjEKICAgICAgLSBHT0xEIFRUIEJBUiA9IEFzayAqIEtfQUVEX0dSQU0gKiAxMTYuNTIzMzYgKyAxNTQuMwogICA9PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT0gKi8KdmFyIEJVTExJT05fU1RBVEUgPSB7CiAgZ29sZEJpZDogMzkwMC41NSwKICBnb2xkQXNrOiAzOTAyLjA1LAogIGdvbGRIaWdoOiAzOTI0LjA0LAogIGdvbGRMb3c6IDM4NzMuMTIsCiAgdXNkSW5yOiA5NC4zMTMsCiAgaW5ySGlnaDogOTQuMzQ5LAogIGluckxvdzogOTQuMTE4LAogIHNpbHZlck96OiA1Mi4wOSwKICBzaWxIaWdoOiA1Mi44MSwKICBzaWxMb3c6IDUxLjIxCn07CnZhciBCVUxMSU9OX1BSRVYgPSB7fTsKdmFyIEJVTExJT05fVElNRVJTID0ge307CnZhciBkaXJlY3REb21BY3RpdmUgPSBmYWxzZTsKCnZhciBLX0FFRCA9IDMuNjc0IC8gMzEuMTAzNTsKCmZ1bmN0aW9uIHNldEJ1bGxpb25WYWwoaWQsIG51bVZhbCwgZGVjaW1hbHMpIHsKICB2YXIgZWwgPSAkKGlkKTsKICBpZiAoIWVsIHx8IG51bVZhbCA9PSBudWxsIHx8IGlzTmFOKG51bVZhbCkpIHJldHVybjsKICB2YXIgcm91bmRlZCA9IE51bWJlcihOdW1iZXIobnVtVmFsKS50b0ZpeGVkKGRlY2ltYWxzKSk7CiAgdmFyIHR4dCA9IGRlY2ltYWxzID09PSAwID8gU3RyaW5nKE1hdGgucm91bmQocm91bmRlZCkpIDogcm91bmRlZC50b0ZpeGVkKGRlY2ltYWxzKTsKICB2YXIgcHJldiA9IEJVTExJT05fUFJFVltpZF07CiAgZWwudGV4dENvbnRlbnQgPSB0eHQ7CgogIGlmIChwcmV2ICE9PSB1bmRlZmluZWQgJiYgcHJldiAhPT0gcm91bmRlZCkgewogICAgdmFyIGlzVXAgPSByb3VuZGVkID4gcHJldjsKICAgIGVsLmNsYXNzTmFtZSA9IGVsLmNsYXNzTmFtZS5yZXBsYWNlKC9cYih2YWwtdXB8dmFsLWRvd258dmFsLW5ldXRyYWx8Zmx1cHxmbGRuKVxiL2csICcnKS50cmltKCk7CiAgICB2b2lkIGVsLm9mZnNldFdpZHRoOwogICAgZWwuY2xhc3NMaXN0LmFkZChpc1VwID8gJ3ZhbC11cCcgOiAndmFsLWRvd24nLCBpc1VwID8gJ2ZsdXAnIDogJ2ZsZG4nKTsKICAgIGlmIChCVUxMSU9OX1RJTUVSU1tpZF0pIGNsZWFyVGltZW91dChCVUxMSU9OX1RJTUVSU1tpZF0pOwogICAgLyogUmV0dXJuIHRvIHdoaXRlL25ldXRyYWwgYWZ0ZXIgNHMgaWYgbm8gZnVydGhlciB0aWNrY2hhbmdlIG9uIHRoYXQgaXRlbSAobWF0Y2hlcyBjbGFzc2ljYnVsbGlvbi5jb20pICovCiAgICBCVUxMSU9OX1RJTUVSU1tpZF0gPSBzZXRUaW1lb3V0KGZ1bmN0aW9uKCkgewogICAgICBlbC5jbGFzc05hbWUgPSBlbC5jbGFzc05hbWUucmVwbGFjZSgvXGIodmFsLXVwfHZhbC1kb3duKVxiL2csICcnKS50cmltKCkgKyAnIHZhbC1uZXV0cmFsJzsKICAgIH0sIDQwMDApOwogIH0KICBCVUxMSU9OX1BSRVZbaWRdID0gcm91bmRlZDsKfQoKZnVuY3Rpb24gcmVuZGVyQnVsbGlvbigpIHsKICB2YXIgYmlkID0gQlVMTElPTl9TVEFURS5nb2xkQmlkOwogIHZhciBhc2sgPSBCVUxMSU9OX1NUQVRFLmdvbGRBc2s7CiAgdmFyIGdoICA9IE1hdGgubWF4KEJVTExJT05fU1RBVEUuZ29sZEhpZ2gsIGFzayk7CiAgdmFyIGdsICA9IE1hdGgubWluKEJVTExJT05fU1RBVEUuZ29sZExvdywgYmlkKTsKICBCVUxMSU9OX1NUQVRFLmdvbGRIaWdoID0gZ2g7CiAgQlVMTElPTl9TVEFURS5nb2xkTG93ICA9IGdsOwoKICBzZXRCdWxsaW9uVmFsKCdjYi1iaWQnLCBiaWQsIDIpOwogIHNldEJ1bGxpb25WYWwoJ2NiLWFzaycsIGFzaywgMik7CiAgJCgnY2ItYmlkLWgnKS50ZXh0Q29udGVudCA9ICdIOiAnICsgZ2gudG9GaXhlZCgyKTsKICAkKCdjYi1hc2stbCcpLnRleHRDb250ZW50ID0gJ0w6ICcgKyBnbC50b0ZpeGVkKDIpOwoKICBzZXRCdWxsaW9uVmFsKCdjYi1pbnInLCBCVUxMSU9OX1NUQVRFLnVzZEluciwgMyk7CiAgJCgnY2ItaW5yLWhsJykudGV4dENvbnRlbnQgPSAnSDogJyArIEJVTExJT05fU1RBVEUuaW5ySGlnaC50b0ZpeGVkKDMpICsgJyB8IEw6ICcgKyBCVUxMSU9OX1NUQVRFLmluckxvdy50b0ZpeGVkKDMpOwoKICBzZXRCdWxsaW9uVmFsKCdjYi1zaWwnLCBCVUxMSU9OX1NUQVRFLnNpbHZlck96LCAyKTsKICAkKCdjYi1zaWwtaGwnKS50ZXh0Q29udGVudCA9ICdIOiAnICsgQlVMTElPTl9TVEFURS5zaWxIaWdoLnRvRml4ZWQoMikgKyAnIHwgTDogJyArIEJVTExJT05fU1RBVEUuc2lsTG93LnRvRml4ZWQoMik7CgogIC8qIEV4YWN0IENsYXNzaWMgQnVsbGlvbiBEdWJhaSBBRUQgZm9ybXVsYXMgKi8KICB2YXIgZ20xICAgID0gYXNrICogS19BRUQgKyAyLjI2MzsKICB2YXIgZ20xSCAgID0gZ2ggICogS19BRUQgKyAyLjI2MzsKICB2YXIgZ20xTCAgID0gZ2wgICogS19BRUQgKyAyLjI2MzsKCiAgdmFyIGc5OTUgICA9IGFzayAqIEtfQUVEICogOTk1ICsgMTY3NC4zOwogIHZhciBnOTk1SCAgPSBnaCAgKiBLX0FFRCAqIDk5NSArIDE2NzQuMzsKICB2YXIgZzk5NUwgID0gZ2wgICogS19BRUQgKiA5OTUgKyAxNjc0LjM7CgogIHZhciBnOTk5OSAgPSBhc2sgKiBLX0FFRCAqIDk5OS45ICsgMTA5Mi4xOwogIHZhciBnOTk5OUggPSBnaCAgKiBLX0FFRCAqIDk5OS45ICsgMTA5Mi4xOwogIHZhciBnOTk5OUwgPSBnbCAgKiBLX0FFRCAqIDk5OS45ICsgMTA5Mi4xOwoKICB2YXIgZ3R0ICAgID0gYXNrICogS19BRUQgKiAxMTYuNTIzMzYgKyAxNTQuMzsKICB2YXIgZ3R0SCAgID0gZ2ggICogS19BRUQgKiAxMTYuNTIzMzYgKyAxNTQuMzsKICB2YXIgZ3R0TCAgID0gZ2wgICogS19BRUQgKiAxMTYuNTIzMzYgKyAxNTQuMzsKCiAgc2V0QnVsbGlvblZhbCgnY2ItMWdtJywgIGdtMSwgICAyKTsKICAkKCdjYi0xZ20taGwnKS50ZXh0Q29udGVudCAgPSAnSDogJyArIGdtMUgudG9GaXhlZCgyKSArICcgfCBMOiAnICsgZ20xTC50b0ZpeGVkKDIpOwoKICBzZXRCdWxsaW9uVmFsKCdjYi05OTk5JywgZzk5OTksIDApOwogICQoJ2NiLTk5OTktaGwnKS50ZXh0Q29udGVudCA9ICdIOiAnICsgTWF0aC5yb3VuZChnOTk5OUgpICsgJyB8IEw6ICcgKyBNYXRoLnJvdW5kKGc5OTk5TCk7Cn0KCi8qIExpc3RlbiBmb3IgZGlyZWN0IGxpdmUgRE9NIG1lc3NhZ2VzIGZyb20gL3Byb3h5L2NsYXNzaWNidWxsaW9uIGlmcmFtZSAqLwp3aW5kb3cuYWRkRXZlbnRMaXN0ZW5lcignbWVzc2FnZScsIGZ1bmN0aW9uKGV2KSB7CiAgaWYgKCFldi5kYXRhIHx8IGV2LmRhdGEudHlwZSAhPT0gJ0NMQVNTSUNfQlVMTElPTl9ET00nKSByZXR1cm47CiAgdmFyIGQgPSBldi5kYXRhLnBheWxvYWQgfHwge307CiAgaWYgKGQuZ29sZEJpZCA+IDEwMDAgJiYgZC5nb2xkQXNrID4gMTAwMCkgewogICAgZGlyZWN0RG9tQWN0aXZlID0gdHJ1ZTsKICAgICQoJ2NiU3RhdHVzJykudGV4dENvbnRlbnQgPSAnQ0xBU1NJQ0JVTExJT04uQ09NIExJVkUnOwogICAgc2V0QnVsbGlvblZhbCgnY2ItYmlkJywgZC5nb2xkQmlkLCAyKTsKICAgIHNldEJ1bGxpb25WYWwoJ2NiLWFzaycsIGQuZ29sZEFzaywgMik7CiAgICBpZiAoZC5nb2xkSGlnaCkgJCgnY2ItYmlkLWgnKS50ZXh0Q29udGVudCA9IGQuZ29sZEhpZ2g7CiAgICBpZiAoZC5nb2xkTG93KSAgJCgnY2ItYXNrLWwnKS50ZXh0Q29udGVudCA9IGQuZ29sZExvdzsKICAgIGlmIChkLnVzZElucikgICBzZXRCdWxsaW9uVmFsKCdjYi1pbnInLCBkLnVzZEluciwgMyk7CiAgICBpZiAoZC5zaWx2ZXJPeikgc2V0QnVsbGlvblZhbCgnY2Itc2lsJywgZC5zaWx2ZXJPeiwgMik7CiAgICBpZiAoZC5nb2xkMWdtKSAgc2V0QnVsbGlvblZhbCgnY2ItMWdtJywgZC5nb2xkMWdtLCAyKTsKICAgIGlmIChkLmdvbGQ5OTUpICBzZXRCdWxsaW9uVmFsKCdjYi05OTUnLCBkLmdvbGQ5OTUsIDApOwogICAgaWYgKGQuZ29sZDk5OTkpIHNldEJ1bGxpb25WYWwoJ2NiLTk5OTknLCBkLmdvbGQ5OTk5LCAwKTsKICAgIGlmIChkLmdvbGRUdCkgICBzZXRCdWxsaW9uVmFsKCdjYi10dCcsIGQuZ29sZFR0LCAwKTsKICB9Cn0pOwoKLyogUG9sbCBUcmFkaW5nVmlldyByZWFsLXRpbWUgU3BvdCBTY2FubmVyIGV2ZXJ5IDEgc2Vjb25kICgxMDAwbXMpIHdpdGggZXhhY3QgQ2xhc3NpYyBCdWxsaW9uIGNhbGlicmF0aW9uICovCmFzeW5jIGZ1bmN0aW9uIHBvbGxEaXJlY3RCdWxsaW9uU2Nhbm5lcigpIHsKICBpZiAoZGlyZWN0RG9tQWN0aXZlKSByZXR1cm47CiAgdHJ5IHsKICAgIHZhciBib2R5ID0gSlNPTi5zdHJpbmdpZnkoewogICAgICBzeW1ib2xzOiB7IHRpY2tlcnM6IFsnVFZDOkdPTEQnLCAnVFZDOlNJTFZFUicsICdGWF9JREM6VVNESU5SJ10sIHF1ZXJ5OiB7IHR5cGVzOiBbXSB9IH0sCiAgICAgIGNvbHVtbnM6IFsnY2xvc2UnLCAnaGlnaCcsICdsb3cnXQogICAgfSk7CiAgICB2YXIgciA9IGF3YWl0IGZldGNoKCdodHRwczovL3NjYW5uZXIudHJhZGluZ3ZpZXcuY29tL2dsb2JhbC9zY2FuJywgewogICAgICBtZXRob2Q6ICdQT1NUJywgaGVhZGVyczogeyAnQ29udGVudC1UeXBlJzogJ3RleHQvcGxhaW4nIH0sIGJvZHk6IGJvZHkKICAgIH0pOwogICAgaWYgKCFyLm9rKSByZXR1cm47CiAgICB2YXIgaiA9IGF3YWl0IHIuanNvbigpOwogICAgaWYgKCFqLmRhdGEpIHJldHVybjsKICAgIGouZGF0YS5mb3JFYWNoKGZ1bmN0aW9uKHJvdykgewogICAgICB2YXIgcyA9IChyb3cucyB8fCAnJykudG9VcHBlckNhc2UoKTsKICAgICAgdmFyIGMgPSByb3cuZFswXSwgaCA9IHJvdy5kWzFdLCBsID0gcm93LmRbMl07CiAgICAgIGlmIChzID09PSAnVFZDOkdPTEQnICYmIGMpIHsKICAgICAgICAvKiBFeGFjdCBDbGFzc2ljIEJ1bGxpb24gU3BvdC10by1CaWQvQXNrIGNhbGlicmF0aW9uICgtMC43MCAvICswLjgwKSAqLwogICAgICAgIEJVTExJT05fU1RBVEUuZ29sZEJpZCA9ICsoTnVtYmVyKGMpIC0gMC43MCkudG9GaXhlZCgyKTsKICAgICAgICBCVUxMSU9OX1NUQVRFLmdvbGRBc2sgPSArKE51bWJlcihjKSArIDAuODApLnRvRml4ZWQoMik7CiAgICAgICAgaWYgKGgpIEJVTExJT05fU1RBVEUuZ29sZEhpZ2ggPSBOdW1iZXIoaCk7CiAgICAgICAgaWYgKGwpIEJVTExJT05fU1RBVEUuZ29sZExvdyAgPSBOdW1iZXIobCk7CiAgICAgIH0gZWxzZSBpZiAocyA9PT0gJ1RWQzpTSUxWRVInICYmIGMpIHsKICAgICAgICAvKiBFeGFjdCBDbGFzc2ljIEJ1bGxpb24gU2lsdmVyIE96IGJhc2lzICgrMi4wMiBvdmVyIFRWQzpTSUxWRVIgc3BvdCkgKi8KICAgICAgICBCVUxMSU9OX1NUQVRFLnNpbHZlck96ID0gKyhOdW1iZXIoYykgKyAyLjAyKS50b0ZpeGVkKDIpOwogICAgICAgIGlmIChoKSBCVUxMSU9OX1NUQVRFLnNpbEhpZ2ggPSArKE51bWJlcihoKSArIDIuMDIpLnRvRml4ZWQoMik7CiAgICAgICAgaWYgKGwpIEJVTExJT05fU1RBVEUuc2lsTG93ICA9ICsoTnVtYmVyKGwpICsgMS43MSkudG9GaXhlZCgyKTsKICAgICAgfSBlbHNlIGlmIChzID09PSAnRlhfSURDOlVTRElOUicgJiYgYykgewogICAgICAgIC8qIEV4YWN0IENsYXNzaWMgQnVsbGlvbiBVU0RJTlIgQXNrIHNwcmVhZCAoKzAuMTE1IG92ZXIgc3BvdCkgKi8KICAgICAgICBCVUxMSU9OX1NUQVRFLnVzZEluciA9ICsoTnVtYmVyKGMpICsgMC4xMTUpLnRvRml4ZWQoMyk7CiAgICAgIH0KICAgIH0pOwogICAgcmVuZGVyQnVsbGlvbigpOwogIH0gY2F0Y2ggKGUpIHt9Cn0KcmVuZGVyQnVsbGlvbigpOwpwb2xsRGlyZWN0QnVsbGlvblNjYW5uZXIoKTsKc2V0SW50ZXJ2YWwocG9sbERpcmVjdEJ1bGxpb25TY2FubmVyLCAxMDAwKTsKCi8qIFJlYWwtdGltZSBCVEMgV2ViU29ja2V0IGZvciB0b3AgdGlja2VyIGFuZCBCVEMgY2hhcnQgKi8KZnVuY3Rpb24gc3RhcnRCdGNTb2NrZXQoKSB7CiAgdHJ5IHsKICAgIHZhciB3cyA9IG5ldyBXZWJTb2NrZXQoJ3dzczovL3N0cmVhbS5iaW5hbmNlLmNvbTo5NDQzL3dzL2J0Y3VzZHRAdGlja2VyJyk7CiAgICB3cy5vbm1lc3NhZ2UgPSBmdW5jdGlvbihldikgewogICAgICB0cnkgewogICAgICAgIHZhciBkID0gSlNPTi5wYXJzZShldi5kYXRhKTsKICAgICAgICBpZiAoZCAmJiBkLnMgPT09ICdCVENVU0RUJyAmJiBkLmMpIHsKICAgICAgICAgIGlmICghUVsnQlRDLVVTRCddKSBRWydCVEMtVVNEJ10gPSB7IG5hbWU6ICdCVEMnLCBzZXJpZXM6IFtdIH07CiAgICAgICAgICBRWydCVEMtVVNEJ10ucHJpY2UgPSBwYXJzZUZsb2F0KGQuYyk7CiAgICAgICAgICBRWydCVEMtVVNEJ10ucGN0ICAgPSBwYXJzZUZsb2F0KGQuUCk7CiAgICAgICAgICBsYXN0VXBkID0gRGF0ZS5ub3coKTsKICAgICAgICAgIHJlbmRlclRpY2tlcigpOwogICAgICAgICAgcmVuZGVyQ2hhcnRzKCk7CiAgICAgICAgICBQUkVWWydCVEMtVVNEJ10gPSBRWydCVEMtVVNEJ10ucHJpY2U7CiAgICAgICAgfQogICAgICB9IGNhdGNoIChlKSB7fQogICAgfTsKICAgIHdzLm9uY2xvc2UgPSBmdW5jdGlvbigpIHsgc2V0VGltZW91dChzdGFydEJ0Y1NvY2tldCwgNDAwMCk7IH07CiAgfSBjYXRjaCAoZSkge30KfQpzdGFydEJ0Y1NvY2tldCgpOwoKLyogbWluaSBjaGFydHMgKi8KZnVuY3Rpb24gZHJhd0FyZWEoY3Ysc2VyaWVzLHVwKXsKICBpZighY3YpcmV0dXJuO3ZhciBkcHI9d2luZG93LmRldmljZVBpeGVsUmF0aW98fDE7dmFyIHc9Y3YuY2xpZW50V2lkdGgsaD1jdi5jbGllbnRIZWlnaHQ7CiAgaWYodzwyfHxoPDIpcmV0dXJuO2N2LndpZHRoPXcqZHByO2N2LmhlaWdodD1oKmRwcjt2YXIgeD1jdi5nZXRDb250ZXh0KCcyZCcpO3guc2V0VHJhbnNmb3JtKGRwciwwLDAsZHByLDAsMCk7eC5jbGVhclJlY3QoMCwwLHcsaCk7CiAgaWYoIXNlcmllc3x8c2VyaWVzLmxlbmd0aDwyKXJldHVybjsKICB2YXIgbWluPU1hdGgubWluLmFwcGx5KG51bGwsc2VyaWVzKSxtYXg9TWF0aC5tYXguYXBwbHkobnVsbCxzZXJpZXMpLHJuZz0obWF4LW1pbil8fDE7CiAgdmFyIGNvbD11cD8nIzE3Yzk4Myc6JyNmZjNiNDcnOwogIHZhciBweD1mdW5jdGlvbihpKXtyZXR1cm4gaS8oc2VyaWVzLmxlbmd0aC0xKSp3O30scHk9ZnVuY3Rpb24odil7cmV0dXJuIGgtNi0oKHYtbWluKS9ybmcpKihoLTEyKTt9OwogIHguYmVnaW5QYXRoKCk7eC5tb3ZlVG8oMCxoKTtzZXJpZXMuZm9yRWFjaChmdW5jdGlvbih2LGkpe3gubGluZVRvKHB4KGkpLHB5KHYpKTt9KTt4LmxpbmVUbyh3LGgpO3guY2xvc2VQYXRoKCk7CiAgdmFyIGc9eC5jcmVhdGVMaW5lYXJHcmFkaWVudCgwLDAsMCxoKTtnLmFkZENvbG9yU3RvcCgwLHVwPydyZ2JhKDIzLDIwMSwxMzEsLjM0KSc6J3JnYmEoMjU1LDU5LDcxLC4zNCknKTtnLmFkZENvbG9yU3RvcCgxLCdyZ2JhKDAsMCwwLDApJyk7CiAgeC5maWxsU3R5bGU9Zzt4LmZpbGwoKTsKICB4LmJlZ2luUGF0aCgpO3Nlcmllcy5mb3JFYWNoKGZ1bmN0aW9uKHYsaSl7dmFyIFg9cHgoaSksWT1weSh2KTtpP3gubGluZVRvKFgsWSk6eC5tb3ZlVG8oWCxZKTt9KTt4LnN0cm9rZVN0eWxlPWNvbDt4LmxpbmVXaWR0aD0yO3guc3Ryb2tlKCk7Cn0KZnVuY3Rpb24gcmVuZGVyQ2hhcnRzKCl7CiAgaWYoISQoJ2R4eUN2JykpcmV0dXJuOyAgIC8qIG1pbmktY2hhcnRzIHJlbW92ZWQ7IG5vdGhpbmcgdG8gZHJhdyAqLwogIHZhciBkPVFbJ0RYLVkuTllCJ118fHt9LGI9UVsnQlRDLVVTRCddfHx7fTsKICB2YXIgZHU9KGQucGN0fHwwKT49MCxidT0oYi5wY3R8fDApPj0wOwogICQoJ2R4eVZhbCcpLnRleHRDb250ZW50PWZtdChkLnByaWNlLDMpOyQoJ2R4eUNoZycpLnRleHRDb250ZW50PWFycm93KGQucGN0fHwwKSsnICcrcGN0VHh0KGQucGN0KTskKCdkeHlDaGcnKS5jbGFzc05hbWU9J2NoZyAnKyhkdT8ndXAnOidkb3duJyk7CiAgJCgnYnRjVmFsJykudGV4dENvbnRlbnQ9Zm10KGIucHJpY2UsMik7JCgnYnRjQ2hnJykudGV4dENvbnRlbnQ9YXJyb3coYi5wY3R8fDApKycgJytwY3RUeHQoYi5wY3QpOyQoJ2J0Y0NoZycpLmNsYXNzTmFtZT0nY2hnICcrKGJ1Pyd1cCc6J2Rvd24nKTsKICBkcmF3QXJlYSgkKCdkeHlDdicpLGQuc2VyaWVzLGR1KTtkcmF3QXJlYSgkKCdidGNDdicpLGIuc2VyaWVzLGJ1KTsKfQpmdW5jdGlvbiByZW5kZXJBbGwoKXsKICByZW5kZXJUaWNrZXIoKTsKICByZW5kZXJBc2lhKCk7CiAgcmVuZGVyQ2hhcnRzKCk7CiAgT2JqZWN0LmtleXMoUSkuZm9yRWFjaChmdW5jdGlvbihzKXtpZihRW3NdJiZRW3NdLnByaWNlIT1udWxsKVBSRVZbc109UVtzXS5wcmljZTt9KTsKfQphc3luYyBmdW5jdGlvbiBwb2xsUXVvdGVzKCl7CiAgdHJ5e3ZhciByPWF3YWl0IGZldGNoKCcvYXBpL3F1b3Rlcycse2NhY2hlOiduby1zdG9yZSd9KTtpZihyLm9rKXtRPWF3YWl0IHIuanNvbigpO2xhc3RVcGQ9RGF0ZS5ub3coKTtyZW5kZXJBbGwoKTt9fWNhdGNoKGUpe30KfQpwb2xsUXVvdGVzKCk7CnNldEludGVydmFsKHBvbGxRdW90ZXMsIDYwMDApOwoKLyogLS0tLS0tLS0tLS0tLS0tLSBzY2FsZSAxOTIweDEwODAgc3RhZ2UgdG8gZml0IGFueSBkaXNwbGF5IChrZWVwcyBleGFjdCBwcm9wb3J0aW9ucykgLS0tLS0tLS0tLS0tLS0tLSAqLwpmdW5jdGlvbiBmaXQoKXsKICB2YXIgZWw9ZG9jdW1lbnQucXVlcnlTZWxlY3RvcignLmFwcCcpO2lmKCFlbClyZXR1cm47CiAgdmFyIHM9TWF0aC5taW4od2luZG93LmlubmVyV2lkdGgvMTkyMCx3aW5kb3cuaW5uZXJIZWlnaHQvMTA4MCk7CiAgdmFyIG94PSh3aW5kb3cuaW5uZXJXaWR0aC0xOTIwKnMpLzIsIG95PSh3aW5kb3cuaW5uZXJIZWlnaHQtMTA4MCpzKS8yOwogIGVsLnN0eWxlLnRyYW5zZm9ybT0ndHJhbnNsYXRlKCcrb3grJ3B4LCcrb3krJ3B4KSBzY2FsZSgnK3MrJyknOwogIHJlbmRlckNoYXJ0cygpOwp9CndpbmRvdy5hZGRFdmVudExpc3RlbmVyKCdyZXNpemUnLGZpdCk7CmZpdCgpOwoKLyogLS0tLS0tLS0tLS0tLS0tLSBtYWNybyBjYWxlbmRhciAtLS0tLS0tLS0tLS0tLS0tICovCnZhciBDVVI9e1VTOidVU0QnLEVVOidFVVInLEdCOidHQlAnLEpQOidKUFknLENOOidDTlknLENBOidDQUQnLEFVOidBVUQnLENIOidDSEYnfTsKZnVuY3Rpb24gZm10VmFsKHYsdW5pdCl7IGlmKHY9PW51bGx8fHY9PT0nJylyZXR1cm4gJ+KAlCc7IHZhciBuPU51bWJlcih2KTsgaWYoaXNOYU4obikpcmV0dXJuIGVzYyhTdHJpbmcodikpOyB2YXIgcz1NYXRoLmFicyhuKT49MTAwMD9uLnRvTG9jYWxlU3RyaW5nKCdlbi1VUycpOlN0cmluZyhuKTsgcmV0dXJuIGVzYyhzKyh1bml0fHwnJykpOyB9CmFzeW5jIGZ1bmN0aW9uIHBvbGxDYWxlbmRhcigpewogIHRyeXsKICAgIHZhciByPWF3YWl0IGZldGNoKCcvYXBpL2NhbGVuZGFyJyx7Y2FjaGU6J25vLXN0b3JlJ30pO2lmKCFyLm9rKXJldHVybjt2YXIgcm93cz1hd2FpdCByLmpzb24oKTsKICAgIGlmKCFyb3dzfHwhcm93cy5sZW5ndGgpcmV0dXJuOwogICAgdmFyIHRvZGF5PW5ldyBEYXRlKCkudG9EYXRlU3RyaW5nKCk7CiAgICAkKCdtYWNybycpLmlubmVySFRNTD1yb3dzLnNsaWNlKDAsNSkubWFwKGZ1bmN0aW9uKGUpewogICAgICB2YXIgZD1lLmRhdGU/bmV3IERhdGUoZS5kYXRlKTpudWxsOwogICAgICB2YXIgdD1kP2QudG9Mb2NhbGVUaW1lU3RyaW5nKCdlbi1HQicse2hvdXI6JzItZGlnaXQnLG1pbnV0ZTonMi1kaWdpdCd9KTonJzsKICAgICAgdmFyIGRheT1kPyhkLnRvRGF0ZVN0cmluZygpPT09dG9kYXk/J1RPREFZJzpkLnRvTG9jYWxlRGF0ZVN0cmluZygnZW4tR0InLHt3ZWVrZGF5OidzaG9ydCd9KS50b1VwcGVyQ2FzZSgpKTonJzsKICAgICAgdmFyIGRvbmU9ZS5hY3R1YWwhPW51bGwmJmUuYWN0dWFsIT09Jyc7CiAgICAgIHZhciBjdXI9Q1VSW2UuY291bnRyeV18fGUuY291bnRyeXx8Jyc7CiAgICAgIHJldHVybiAnPGRpdiBjbGFzcz0ibXJvdycrKGRvbmU/JyBkb25lJzonJykrJyI+PGRpdiBjbGFzcz0iZXYiPjxkaXYgY2xhc3M9InByLWRvdCI+PC9kaXY+PGRpdiBjbGFzcz0iZXZ0Ij4nKwogICAgICAgICc8ZGl2IGNsYXNzPSJ0MSI+PGI+Jytlc2MoZGF5KSsnPC9iPiAnK2VzYyh0KSsnICZtaWRkb3Q7ICcrZmxhZyhlLmNvdW50cnkpK2VzYyhjdXIpKyc8L2Rpdj48ZGl2IGNsYXNzPSJ0MiI+Jytlc2MoZS50aXRsZXx8JycpKyc8L2Rpdj48L2Rpdj48L2Rpdj4nKwogICAgICAgICc8ZGl2IGNsYXNzPSJhIj4nK2ZtdFZhbChlLmFjdHVhbCxlLnVuaXQpKyc8L2Rpdj48ZGl2IGNsYXNzPSJmIj4nK2ZtdFZhbChlLmZvcmVjYXN0LGUudW5pdCkrJzwvZGl2PjwvZGl2Pic7CiAgICB9KS5qb2luKCcnKTsKICB9Y2F0Y2goZSl7fQp9CnBvbGxDYWxlbmRhcigpO3NldEludGVydmFsKHBvbGxDYWxlbmRhciwxNSo2MCoxMDAwKTsKCi8qIC0tLS0tLS0tLS0tLS0tLS0gY291bnRyeSBmbGFncyAoY3VycmVuY3kgY29kZSAtPiBJU08gY291bnRyeSBmb3IgZmxhZ2NkbikgLS0tLS0tLS0tLS0tLS0tLSAqLwp2YXIgQ0MyPXtVU0Q6J3VzJyxFVVI6J2V1JyxHQlA6J2diJyxKUFk6J2pwJyxDTlk6J2NuJyxDQUQ6J2NhJyxBVUQ6J2F1JyxDSEY6J2NoJyxOWkQ6J256J307CmZ1bmN0aW9uIGZsYWcoY3VyKXt2YXIgYz1DQzJbKGN1cnx8JycpLnRvVXBwZXJDYXNlKCldO3JldHVybiBjPyc8aW1nIGNsYXNzPSJmbGFnIiBzcmM9Imh0dHBzOi8vZmxhZ2Nkbi5jb20vdzQwLycrYysnLnBuZyIgcmVmZXJyZXJwb2xpY3k9Im5vLXJlZmVycmVyIiBvbmVycm9yPSJ0aGlzLnN0eWxlLmRpc3BsYXk9XCdub25lXCciPic6Jyc7fQoKLyogLS0tLS0tLS0tLS0tLS0tLSBjZW50cmFsIGJhbmsgcG9saWN5IHJhdGVzIC0tLS0tLS0tLS0tLS0tLS0gKi8KdmFyIENCUj1bWydBVUQnLCc0Ljg1J10sWydVU0QnLCc0LjAwJ10sWydHQlAnLCc0LjAwJ10sWydOWkQnLCcyLjc1J10sWydFVVInLCcyLjY1J10sWydDQUQnLCcyLjUwJ10sWydKUFknLCcwLjUwJ10sWydDSEYnLCcwLjAwJ11dOwpmdW5jdGlvbiByZW5kZXJDQlIoKXsKICB2YXIgZz0kKCdjYnJHcmlkJyk7aWYoIWcpcmV0dXJuOwogIGcuaW5uZXJIVE1MPUNCUi5tYXAoZnVuY3Rpb24oeCl7CiAgICB2YXIgbG93PXBhcnNlRmxvYXQoeFsxXSk8PTAuNTsKICAgIHJldHVybiAnPGRpdiBjbGFzcz0iY2JyIj48c3BhbiBjbGFzcz0iYyI+JytmbGFnKHhbMF0pK3hbMF0rJzwvc3Bhbj48c3BhbiBjbGFzcz0icicrKGxvdz8nIGxvdyc6JycpKyciPicreFsxXSsnJTwvc3Bhbj48L2Rpdj4nOwogIH0pLmpvaW4oJycpOwp9CnJlbmRlckNCUigpOwoKLyogLS0tLS0tLS0tLS0tLS0tLSBuZXdzIGNoeXJvbiAmIGJvdHRvbSBzdHJpcCAoRkFTVEVSIFNDUk9MTCBTUEVFRCkgLS0tLS0tLS0tLS0tLS0tLSAqLwp2YXIgTkVXUz1bXSxjaT0wOwpmdW5jdGlvbiBjeWNsZUNoeXJvbigpeyBpZighTkVXUy5sZW5ndGgpcmV0dXJuOyAkKCdjaHlITCcpLnRleHRDb250ZW50PU5FV1NbY2klTkVXUy5sZW5ndGhdLnRpdGxlfHwnJzsgY2krKzsgfQpmdW5jdGlvbiB0aW1lQWdvKGQpe3ZhciB0PURhdGUucGFyc2UoZCk7aWYoIXQpcmV0dXJuICcnO3ZhciBzPShEYXRlLm5vdygpLXQpLzEwMDA7aWYoczwzNjAwKXJldHVybiBNYXRoLm1heCgxLE1hdGgucm91bmQocy82MCkpKydtIGFnbyc7aWYoczw4NjQwMClyZXR1cm4gTWF0aC5yb3VuZChzLzM2MDApKydoIGFnbyc7cmV0dXJuIE1hdGgucm91bmQocy84NjQwMCkrJ2QgYWdvJzt9CmZ1bmN0aW9uIG5jYXJkKG4pewogIHZhciBpbWc9bi5pbWc/JzxpbWcgc3JjPSInK2VzYyhuLmltZykrJyIgbG9hZGluZz0ibGF6eSIgcmVmZXJyZXJwb2xpY3k9Im5vLXJlZmVycmVyIiBvbmVycm9yPSJ0aGlzLnN0eWxlLmRpc3BsYXk9XCdub25lXCc7dGhpcy5wYXJlbnROb2RlLmlubmVySFRNTD1cJzxzcGFuIGNsYXNzPSZxdW90O3BoJnF1b3Q7PicrZXNjKChuLnNyY3x8Jz8nKVswXSkrJzwvc3Bhbj5cJyI+JzonPHNwYW4gY2xhc3M9InBoIj4nK2VzYygobi5zcmN8fCc/JylbMF0pKyc8L3NwYW4+JzsKICByZXR1cm4gJzxkaXYgY2xhc3M9Im5jYXJkIj48ZGl2IGNsYXNzPSJuaW1nIj4nK2ltZysnPC9kaXY+PGRpdiBjbGFzcz0ibmIiPjxkaXYgY2xhc3M9Im5zIj4nK2VzYyhuLnNyYykrJyA8c3BhbiBjbGFzcz0idG0iPicrdGltZUFnbyhuLmRhdGUpKyc8L3NwYW4+PC9kaXY+PGRpdiBjbGFzcz0ibnQiPicrZXNjKG4udGl0bGUpKyc8L2Rpdj48L2Rpdj48L2Rpdj4nOwp9CmFzeW5jIGZ1bmN0aW9uIHBvbGxOZXdzKCl7CiAgdHJ5ewogICAgdmFyIHI9YXdhaXQgZmV0Y2goJy9hcGkvbmV3cycse2NhY2hlOiduby1zdG9yZSd9KTtpZighci5vaylyZXR1cm47dmFyIGl0ZW1zPWF3YWl0IHIuanNvbigpOwogICAgaWYoaXRlbXMmJml0ZW1zLmxlbmd0aCl7TkVXUz1pdGVtczsKICAgICAgdmFyIGxpbmU9aXRlbXMuc2xpY2UoMCwxOCkubWFwKGZ1bmN0aW9uKG4pe3JldHVybiBuLnRpdGxlO30pLmpvaW4oJyAgIOKAoiAgICcpOwogICAgICAkKCdjaHlTY3JvbGwnKS50ZXh0Q29udGVudD1saW5lKycgICDigKIgICAnK2xpbmU7CiAgICAgIHZhciBzYz0kKCdjaHlTY3JvbGwnKTsKICAgICAgc2Muc3R5bGUuc2V0UHJvcGVydHkoJy0tYmtkdXInLE1hdGgubWF4KDQ1LE1hdGgucm91bmQoc2Muc2Nyb2xsV2lkdGgvMi83NSkpKydzJyk7CiAgICAgIGlmKCEkKCdjaHlITCcpLnRleHRDb250ZW50LnRyaW0oKXx8JCgnY2h5SEwnKS50ZXh0Q29udGVudD09PSfCoCcpY3ljbGVDaHlyb24oKTsKICAgICAgdmFyIHRvcD1pdGVtcy5zbGljZSgwLDIwKSx0cj0kKCduZXdzU3RyaXAnKTsKICAgICAgaWYodHIpewogICAgICAgIHRyLmNsYXNzTGlzdC5yZW1vdmUoJ3J1bicpOwogICAgICAgIHRyLmlubmVySFRNTD10b3AubWFwKG5jYXJkKS5qb2luKCcnKSt0b3AubWFwKG5jYXJkKS5qb2luKCcnKTsKICAgICAgICAvKiBGYXN0ZXIgYm90dG9tIG5ld3Mgc3RyaXAgc2Nyb2xsICh+My4ycyBwZXIgY2FyZCkgKi8KICAgICAgICB0ci5zdHlsZS5zZXRQcm9wZXJ0eSgnLS1uZHVyJyxNYXRoLm1heCgzNixNYXRoLnJvdW5kKHRvcC5sZW5ndGgqMy4yKSkrJ3MnKTsKICAgICAgICB2b2lkIHRyLm9mZnNldFdpZHRoOwogICAgICAgIHRyLmNsYXNzTGlzdC5hZGQoJ3J1bicpOwogICAgICB9CiAgICB9CiAgfWNhdGNoKGUpe30KfQpwb2xsTmV3cygpO3NldEludGVydmFsKHBvbGxOZXdzLDUqNjAqMTAwMCk7CnNldEludGVydmFsKGN5Y2xlQ2h5cm9uLDYwMDApOwoKLyogLS0tLS0tLS0tLS0tLS0tLSBraW9zayBzYWZldHkgcmVsb2FkIC0tLS0tLS0tLS0tLS0tLS0gKi8Kc2V0VGltZW91dChmdW5jdGlvbigpe2xvY2F0aW9uLnJlbG9hZCgpO30sMio2MCo2MCoxMDAwKTsKPC9zY3JpcHQ+CjwvYm9keT4KPC9odG1sPgo="
# ==== END EMBED ====

if __name__ == "__main__":
    print("Market Command Center  ->  http://localhost:%d" % PORT)
    print("Data: /api/quotes  /api/calendar  /api/news   (Ctrl+C to stop)")
    try:
        ThreadingHTTPServer(("0.0.0.0", PORT), H).serve_forever()
    except KeyboardInterrupt:
        pass
