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
EMBED_HTML_B64 = "PCFkb2N0eXBlIGh0bWw+CjxodG1sIGxhbmc9ImVuIj4KPGhlYWQ+CjxtZXRhIGNoYXJzZXQ9InV0Zi04Ij4KPG1ldGEgbmFtZT0idmlld3BvcnQiIGNvbnRlbnQ9IndpZHRoPWRldmljZS13aWR0aCxpbml0aWFsLXNjYWxlPTEiPgo8dGl0bGU+TWFya2V0IENvbW1hbmQgQ2VudGVyPC90aXRsZT4KPGxpbmsgcmVsPSJwcmVjb25uZWN0IiBocmVmPSJodHRwczovL2ZvbnRzLmdvb2dsZWFwaXMuY29tIj48bGluayByZWw9InByZWNvbm5lY3QiIGhyZWY9Imh0dHBzOi8vZm9udHMuZ3N0YXRpYy5jb20iIGNyb3Nzb3JpZ2luPgo8bGluayByZWw9InN0eWxlc2hlZXQiIGhyZWY9Imh0dHBzOi8vZm9udHMuZ29vZ2xlYXBpcy5jb20vY3NzMj9mYW1pbHk9SW50ZXI6d2dodEA0MDA7NTAwOzYwMDs3MDA7ODAwOzkwMCZkaXNwbGF5PXN3YXAiPgo8c3R5bGU+CiAgOnJvb3R7CiAgICAtLWJnOiMwMDA7IC0tcGFuZWw6IzBiMGQxMjsgLS1wYW5lbC0yOiMwZTExMTg7IC0tY2FyZDojMGQxMDE3OyAtLWxpbmU6IzFhMWYyOTsgLS1saW5lLTI6IzE0MTkyMjsKICAgIC0tZmc6I2YyZjVmYTsgLS1tdXRlZDojOGE5NGE2OyAtLWZhaW50OiM1YTY0NzQ7CiAgICAtLXVwOiMxN2M5ODM7IC0tdXAtYmc6cmdiYSgyMywyMDEsMTMxLC4xNCk7IC0tZG93bjojZmYzYjQ3OyAtLWRvd24tYmc6cmdiYSgyNTUsNTksNzEsLjE0KTsKICAgIC0tZ29sZDojZjBiNDI5OyAtLW9yYW5nZTojZmY3YTFhOyAtLWxpdmU6I2ZmM2I0NzsgLS1hY2NlbnQ6I2YwYjQyOTsKICAgIC0tc2FuczoiSW50ZXIiLHN5c3RlbS11aSwtYXBwbGUtc3lzdGVtLCJTZWdvZSBVSSIsc2Fucy1zZXJpZjsKICB9CiAgKntib3gtc2l6aW5nOmJvcmRlci1ib3g7bWFyZ2luOjA7cGFkZGluZzowfQogIGh0bWwsYm9keXtoZWlnaHQ6MTAwJTt3aWR0aDoxMDAlfQogIGJvZHl7YmFja2dyb3VuZDojMDAwO2NvbG9yOnZhcigtLWZnKTtmb250LWZhbWlseTp2YXIoLS1zYW5zKTtvdmVyZmxvdzpoaWRkZW47LXdlYmtpdC1mb250LXNtb290aGluZzphbnRpYWxpYXNlZDtmb250LXZhcmlhbnQtbnVtZXJpYzp0YWJ1bGFyLW51bXN9CiAgLyogZml4ZWQgMTkyMHgxMDgwIHN0YWdlLCBzY2FsZWQgdG8gZml0IGFueSBkaXNwbGF5IC0+IGlkZW50aWNhbCBwcm9wb3J0aW9ucyBldmVyeXdoZXJlICovCiAgLmFwcHt3aWR0aDoxOTIwcHg7aGVpZ2h0OjEwODBweDt0cmFuc2Zvcm0tb3JpZ2luOnRvcCBsZWZ0O3Bvc2l0aW9uOmFic29sdXRlO3RvcDowO2xlZnQ6MDtkaXNwbGF5OmdyaWQ7Z3JpZC10ZW1wbGF0ZS1yb3dzOjYwcHggNzJweCAxZnIgMTU4cHg7Z2FwOjEycHg7cGFkZGluZzoxM3B4O2JhY2tncm91bmQ6dmFyKC0tYmcpfQoKICAvKiBoZWFkZXIgKi8KICAuaGVhZHtkaXNwbGF5OmZsZXg7YWxpZ24taXRlbXM6Y2VudGVyO2p1c3RpZnktY29udGVudDpzcGFjZS1iZXR3ZWVuO3BhZGRpbmc6MCA4cHh9CiAgLmh0aXRsZXtmb250LXdlaWdodDo4MDA7Zm9udC1zaXplOjI4cHg7bGV0dGVyLXNwYWNpbmc6LjA5ZW19CiAgLmhzZXNze2Rpc3BsYXk6ZmxleDtnYXA6MjhweDtmb250LXdlaWdodDo3MDA7Zm9udC1zaXplOjE4cHh9CiAgLmhzZXNzIHNwYW57Y29sb3I6dmFyKC0tbXV0ZWQpfSAuaHNlc3MgYntjb2xvcjp2YXIoLS1mZyk7Zm9udC13ZWlnaHQ6ODAwO21hcmdpbi1sZWZ0OjdweH0KICAuaGNsb2Nre2ZvbnQtd2VpZ2h0OjcwMDtmb250LXNpemU6MTlweDtjb2xvcjp2YXIoLS1tdXRlZCl9IC5oY2xvY2sgYntjb2xvcjp2YXIoLS1mZyk7Zm9udC13ZWlnaHQ6ODAwfQogIC5obGl2ZXtkaXNwbGF5OmlubGluZS1mbGV4O2FsaWduLWl0ZW1zOmNlbnRlcjtnYXA6N3B4O2ZvbnQtd2VpZ2h0OjgwMDtmb250LXNpemU6MTNweDtsZXR0ZXItc3BhY2luZzouMDhlbTtjb2xvcjp2YXIoLS11cCl9CiAgLmhsaXZlIC5kb3R7d2lkdGg6OXB4O2hlaWdodDo5cHg7Ym9yZGVyLXJhZGl1czo1MCU7YmFja2dyb3VuZDp2YXIoLS11cCk7Ym94LXNoYWRvdzowIDAgOXB4IHZhcigtLXVwKTthbmltYXRpb246cHVsc2UgMS40cyBpbmZpbml0ZX0KICAuaGxpdmUuc3RhbGV7Y29sb3I6dmFyKC0tZmFpbnQpfSAuaGxpdmUuc3RhbGUgLmRvdHtiYWNrZ3JvdW5kOnZhcigtLWZhaW50KTtib3gtc2hhZG93Om5vbmU7YW5pbWF0aW9uOm5vbmV9CiAgQGtleWZyYW1lcyBmbHVwezAle2JhY2tncm91bmQ6cmdiYSgyMywyMDEsMTMxLC40NSl9MTAwJXtiYWNrZ3JvdW5kOnRyYW5zcGFyZW50fX0KICBAa2V5ZnJhbWVzIGZsZG57MCV7YmFja2dyb3VuZDpyZ2JhKDI1NSw1OSw3MSwuNDUpfTEwMCV7YmFja2dyb3VuZDp0cmFuc3BhcmVudH19CiAgLmZsdXB7YW5pbWF0aW9uOmZsdXAgLjhzIGVhc2Utb3V0O2JvcmRlci1yYWRpdXM6NXB4fSAuZmxkbnthbmltYXRpb246ZmxkbiAuOHMgZWFzZS1vdXQ7Ym9yZGVyLXJhZGl1czo1cHh9CgogIC8qIHRpY2tlciByb3cgKi8KICAudGlja3tkaXNwbGF5OmZsZXg7YWxpZ24taXRlbXM6Y2VudGVyO2JhY2tncm91bmQ6dmFyKC0tcGFuZWwpO2JvcmRlcjoxcHggc29saWQgdmFyKC0tbGluZSk7Ym9yZGVyLXJhZGl1czoxMnB4O292ZXJmbG93OmhpZGRlbn0KICAudGljay10cmFja3tkaXNwbGF5OmZsZXg7YWxpZ24taXRlbXM6Y2VudGVyO2hlaWdodDoxMDAlO3dpZHRoOm1heC1jb250ZW50O2FuaW1hdGlvbjp0bWFycSB2YXIoLS10ZHVyLDQ2cykgbGluZWFyIGluZmluaXRlO3dpbGwtY2hhbmdlOnRyYW5zZm9ybX0KICBAa2V5ZnJhbWVzIHRtYXJxe2Zyb217dHJhbnNmb3JtOnRyYW5zbGF0ZVgoMCl9dG97dHJhbnNmb3JtOnRyYW5zbGF0ZVgoLTUwJSl9fQogIC50a3tkaXNwbGF5OmZsZXg7YWxpZ24taXRlbXM6Y2VudGVyO2dhcDoxNHB4O3BhZGRpbmc6MCAzMHB4O2hlaWdodDoxMDAlO2JvcmRlci1yaWdodDoxcHggc29saWQgdmFyKC0tbGluZS0yKTt3aGl0ZS1zcGFjZTpub3dyYXB9CiAgLnRrIC5ubXtmb250LXdlaWdodDo4MDA7Zm9udC1zaXplOjIzcHh9CiAgLnRrIC5wcntmb250LXdlaWdodDo3MDA7Zm9udC1zaXplOjIzcHg7Y29sb3I6dmFyKC0tZmcpfQogIC50ayAuY2h7Zm9udC13ZWlnaHQ6ODAwO2ZvbnQtc2l6ZToyMHB4O2Rpc3BsYXk6ZmxleDthbGlnbi1pdGVtczpjZW50ZXI7Z2FwOjVweH0KICAudXB7Y29sb3I6dmFyKC0tdXApfSAuZG93bntjb2xvcjp2YXIoLS1kb3duKX0KCiAgLyogbWFpbiAzIGNvbHMgKi8KICAubWFpbntkaXNwbGF5OmdyaWQ7Z3JpZC10ZW1wbGF0ZS1jb2x1bW5zOjEuMzJmciAuOTJmciAxLjA2ZnI7Z2FwOjlweDttaW4taGVpZ2h0OjB9CiAgLmNvbHtkaXNwbGF5OmZsZXg7ZmxleC1kaXJlY3Rpb246Y29sdW1uO2dhcDo5cHg7bWluLWhlaWdodDowO21pbi13aWR0aDowfQogIC5wYW5lbHtiYWNrZ3JvdW5kOnZhcigtLXBhbmVsKTtib3JkZXI6MXB4IHNvbGlkIHZhcigtLWxpbmUpO2JvcmRlci1yYWRpdXM6MTRweDtkaXNwbGF5OmZsZXg7ZmxleC1kaXJlY3Rpb246Y29sdW1uO21pbi1oZWlnaHQ6MDtvdmVyZmxvdzpoaWRkZW59CiAgLnB0aXRsZXtmb250LXdlaWdodDo4MDA7Zm9udC1zaXplOjE1cHg7bGV0dGVyLXNwYWNpbmc6LjFlbTtwYWRkaW5nOjlweCAxNnB4IDZweDtjb2xvcjp2YXIoLS1mZyk7ZmxleDowIDAgYXV0bztkaXNwbGF5OmZsZXg7YWxpZ24taXRlbXM6Y2VudGVyO2p1c3RpZnktY29udGVudDpzcGFjZS1iZXR3ZWVufQogIC5wdGl0bGUgLnBzdWJ7Zm9udC13ZWlnaHQ6NzAwO2ZvbnQtc2l6ZToxMXB4O2xldHRlci1zcGFjaW5nOi4wOGVtO2NvbG9yOnZhcigtLWdvbGQpO2Rpc3BsYXk6aW5saW5lLWZsZXg7YWxpZ24taXRlbXM6Y2VudGVyO2dhcDo2cHh9CiAgLnB0aXRsZSAucHN1YiAuYmRvdHt3aWR0aDo3cHg7aGVpZ2h0OjdweDtib3JkZXItcmFkaXVzOjUwJTtiYWNrZ3JvdW5kOnZhcigtLXVwKTtib3gtc2hhZG93OjAgMCA4cHggdmFyKC0tdXApO2FuaW1hdGlvbjpwdWxzZSAxLjJzIGluZmluaXRlfQogIC5tb2RlLWJ0bntiYWNrZ3JvdW5kOiMxNTFhMjQ7Ym9yZGVyOjFweCBzb2xpZCAjMjYyZjQwO2NvbG9yOnZhcigtLW11dGVkKTtmb250OjcwMCAxMHB4LzEgdmFyKC0tc2Fucyk7cGFkZGluZzo0cHggOHB4O2JvcmRlci1yYWRpdXM6NXB4O2N1cnNvcjpwb2ludGVyO2xldHRlci1zcGFjaW5nOi4wNmVtfQogIC5tb2RlLWJ0bi5hY3RpdmV7Y29sb3I6dmFyKC0tZ29sZCk7Ym9yZGVyLWNvbG9yOnJnYmEoMjQwLDE4MCw0MSwuNSk7YmFja2dyb3VuZDpyZ2JhKDI0MCwxODAsNDEsLjEyKX0KCiAgLyogdmlkZW8gKi8KICAudmlke2ZsZXg6MTttaW4taGVpZ2h0OjA7cG9zaXRpb246cmVsYXRpdmU7YmFja2dyb3VuZDojMDAwO2JvcmRlcjoxcHggc29saWQgdmFyKC0tbGluZSk7Ym9yZGVyLXJhZGl1czoxNHB4O292ZXJmbG93OmhpZGRlbn0KICAudmlkIGlmcmFtZXtwb3NpdGlvbjphYnNvbHV0ZTtpbnNldDowO3dpZHRoOjEwMCU7aGVpZ2h0OjEwMCU7Ym9yZGVyOjB9CiAgLnZ0bHtwb3NpdGlvbjphYnNvbHV0ZTt0b3A6MTJweDtsZWZ0OjEycHg7ei1pbmRleDozO2JhY2tncm91bmQ6cmdiYSgxMCwxMCwxMiwuNzIpO2JhY2tkcm9wLWZpbHRlcjpibHVyKDRweCk7Ym9yZGVyLXJhZGl1czo4cHg7cGFkZGluZzo3cHggMTJweDtmb250LXdlaWdodDo3MDA7Zm9udC1zaXplOjEzcHg7ZGlzcGxheTpmbGV4O2FsaWduLWl0ZW1zOmNlbnRlcjtnYXA6OHB4fQogIC52dGwgLnl0e2NvbG9yOiNmZjJkMmQ7Zm9udC13ZWlnaHQ6OTAwfQogIC52dHJ7cG9zaXRpb246YWJzb2x1dGU7dG9wOjEycHg7cmlnaHQ6MTJweDt6LWluZGV4OjM7YmFja2dyb3VuZDojZTAxYjI0O2JvcmRlci1yYWRpdXM6N3B4O3BhZGRpbmc6NnB4IDExcHg7Zm9udC13ZWlnaHQ6OTAwO2ZvbnQtc2l6ZToxM3B4O2xldHRlci1zcGFjaW5nOi4wOGVtO2Rpc3BsYXk6ZmxleDthbGlnbi1pdGVtczpjZW50ZXI7Z2FwOjdweH0KICAudnRyIC5ke3dpZHRoOjhweDtoZWlnaHQ6OHB4O2JvcmRlci1yYWRpdXM6NTAlO2JhY2tncm91bmQ6I2ZmZjthbmltYXRpb246cHVsc2UgMS4zcyBpbmZpbml0ZX0KICAudmJye3Bvc2l0aW9uOmFic29sdXRlO2JvdHRvbTo3MnB4O3JpZ2h0OjEycHg7ei1pbmRleDozO2JhY2tncm91bmQ6cmdiYSgxMCwxMCwxMiwuNzgpO2JvcmRlci1yYWRpdXM6NnB4O3BhZGRpbmc6NXB4IDEwcHg7Zm9udC13ZWlnaHQ6ODAwO2ZvbnQtc2l6ZToxMnB4fQogIC52YnIgYntjb2xvcjp2YXIoLS1nb2xkKX0KICAuY2h5cm9ue3Bvc2l0aW9uOmFic29sdXRlO2xlZnQ6MDtyaWdodDowO2JvdHRvbTowO3otaW5kZXg6M30KICAuY2h5cm9uIC5obHtiYWNrZ3JvdW5kOmxpbmVhci1ncmFkaWVudCgwZGVnLHJnYmEoMCwwLDAsLjkyKSxyZ2JhKDAsMCwwLC41NSkpO3BhZGRpbmc6MTRweCAxNnB4IDEwcHg7Zm9udC13ZWlnaHQ6OTAwO2ZvbnQtc2l6ZTpjbGFtcCgxOHB4LDJ2dywzMHB4KTtsaW5lLWhlaWdodDoxLjA0O2xldHRlci1zcGFjaW5nOi4wMWVtO3RleHQtdHJhbnNmb3JtOnVwcGVyY2FzZTtjb2xvcjojZmZmO2Rpc3BsYXk6LXdlYmtpdC1ib3g7LXdlYmtpdC1saW5lLWNsYW1wOjI7LXdlYmtpdC1ib3gtb3JpZW50OnZlcnRpY2FsO292ZXJmbG93OmhpZGRlbjttaW4taGVpZ2h0Ojc2cHh9CiAgLmNoeXJvbiAuYmt7ZGlzcGxheTpmbGV4O2FsaWduLWl0ZW1zOmNlbnRlcjtiYWNrZ3JvdW5kOiMwYTBjMTA7Ym9yZGVyLXRvcDoycHggc29saWQgdmFyKC0tb3JhbmdlKX0KICAuY2h5cm9uIC5iayAudGFne2JhY2tncm91bmQ6dmFyKC0tb3JhbmdlKTtjb2xvcjojMTExO2ZvbnQtd2VpZ2h0OjkwMDtmb250LXNpemU6MTJweDtsZXR0ZXItc3BhY2luZzouMDZlbTtwYWRkaW5nOjhweCAxMnB4O3doaXRlLXNwYWNlOm5vd3JhcH0KICAuY2h5cm9uIC5iayAuc2Nyb2xsd3JhcHtmbGV4OjE7b3ZlcmZsb3c6aGlkZGVuO3Bvc2l0aW9uOnJlbGF0aXZlfQogIC5jaHlyb24gLmJrIC5zY3JvbGx7ZGlzcGxheTppbmxpbmUtZmxleDt3aGl0ZS1zcGFjZTpub3dyYXA7cGFkZGluZzo4cHggMDtmb250LXdlaWdodDo2MDA7Zm9udC1zaXplOjE0cHg7Y29sb3I6I2YyZDljNDthbmltYXRpb246YmttYXJxIHZhcigtLWJrZHVyLDc1cykgbGluZWFyIGluZmluaXRlO3dpbGwtY2hhbmdlOnRyYW5zZm9ybX0KICBAa2V5ZnJhbWVzIGJrbWFycXtmcm9te3RyYW5zZm9ybTp0cmFuc2xhdGVYKDApfXRve3RyYW5zZm9ybTp0cmFuc2xhdGVYKC01MCUpfX0KCiAgLyogbWluaSBjaGFydHMgcm93ICovCiAgLmNoYXJ0c3tkaXNwbGF5OmdyaWQ7Z3JpZC10ZW1wbGF0ZS1jb2x1bW5zOjFmciAxZnI7Z2FwOjlweDtmbGV4OjAgMCAxOTBweH0KICAubWN7YmFja2dyb3VuZDp2YXIoLS1wYW5lbCk7Ym9yZGVyOjFweCBzb2xpZCB2YXIoLS1saW5lKTtib3JkZXItcmFkaXVzOjE0cHg7cGFkZGluZzoxMXB4IDEzcHg7ZGlzcGxheTpmbGV4O2ZsZXgtZGlyZWN0aW9uOmNvbHVtbjttaW4td2lkdGg6MH0KICAubWMgLnRvcHtkaXNwbGF5OmZsZXg7YWxpZ24taXRlbXM6YmFzZWxpbmU7Z2FwOjEwcHg7ZmxleC13cmFwOndyYXB9CiAgLm1jIC5zeW17Zm9udC13ZWlnaHQ6OTAwO2ZvbnQtc2l6ZToxN3B4fQogIC5tYyAudmFse2ZvbnQtd2VpZ2h0OjcwMDtmb250LXNpemU6MTVweDtjb2xvcjp2YXIoLS1tdXRlZCk7bWFyZ2luLWxlZnQ6YXV0b30KICAubWMgLmNoZ3tmb250LXdlaWdodDo4MDA7Zm9udC1zaXplOjE0cHh9CiAgLm1jIGNhbnZhc3tmbGV4OjE7d2lkdGg6MTAwJTttaW4taGVpZ2h0OjA7bWFyZ2luLXRvcDo0cHh9CgogIC8qIGFzaWEgY2FyZHMgKi8KICAuYXNpYXtnYXA6MDtqdXN0aWZ5LWNvbnRlbnQ6c3RyZXRjaH0KICAuYWNhcmR7cGFkZGluZzo4cHggMjBweDtib3JkZXItdG9wOjFweCBzb2xpZCB2YXIoLS1saW5lLTIpO2Rpc3BsYXk6ZmxleDtmbGV4LWRpcmVjdGlvbjpjb2x1bW47anVzdGlmeS1jb250ZW50OmNlbnRlcjtnYXA6NXB4O2ZsZXg6MTttaW4taGVpZ2h0OjA7b3ZlcmZsb3c6aGlkZGVufQogIC5hY2FyZDpmaXJzdC1vZi10eXBle2JvcmRlci10b3A6MH0KICAuYWNhcmQgLm5te2ZvbnQtd2VpZ2h0OjcwMDtmb250LXNpemU6MTVweDtjb2xvcjp2YXIoLS1tdXRlZCk7bGV0dGVyLXNwYWNpbmc6LjAzZW19CiAgLmFjYXJkIC52YWx7Zm9udC13ZWlnaHQ6ODAwO2ZvbnQtc2l6ZTozNHB4O2xpbmUtaGVpZ2h0OjF9CiAgLmFjYXJkIC5waWxse2FsaWduLXNlbGY6ZmxleC1zdGFydDtmb250LXNpemU6MTVweDtwYWRkaW5nOjVweCAxMnB4fQogIC5waWxse2Rpc3BsYXk6aW5saW5lLWZsZXg7YWxpZ24taXRlbXM6Y2VudGVyO2dhcDo2cHg7Zm9udC13ZWlnaHQ6ODAwO2ZvbnQtc2l6ZToxNXB4O3BhZGRpbmc6NnB4IDEycHg7Ym9yZGVyLXJhZGl1czo5cHh9CiAgLnBpbGwudXB7YmFja2dyb3VuZDp2YXIoLS11cC1iZyk7Y29sb3I6dmFyKC0tdXApO2JveC1zaGFkb3c6MCAwIDAgMXB4IHJnYmEoMjMsMjAxLDEzMSwuMykgaW5zZXR9CiAgLnBpbGwuZG93bntiYWNrZ3JvdW5kOnZhcigtLWRvd24tYmcpO2NvbG9yOnZhcigtLWRvd24pO2JveC1zaGFkb3c6MCAwIDAgMXB4IHJnYmEoMjU1LDU5LDcxLC4zKSBpbnNldH0KCiAgLyogPT09PT09PT09PT09PT09PT09PT0gQ0xBU1NJQyBCVUxMSU9OIExJVkUgUEFORUwgPT09PT09PT09PT09PT09PT09PT0gKi8KICAuYnVsbGlvbi1wYW5lbHtmbGV4OjAgMCBhdXRvO2JhY2tncm91bmQ6dmFyKC0tcGFuZWwpO2JvcmRlcjoxcHggc29saWQgdmFyKC0tbGluZSk7Ym9yZGVyLXJhZGl1czoxNHB4O292ZXJmbG93OmhpZGRlbjtkaXNwbGF5OmZsZXg7ZmxleC1kaXJlY3Rpb246Y29sdW1uO3Bvc2l0aW9uOnJlbGF0aXZlfQogIC5iLXRvcC1waWxsewogICAgbWFyZ2luOjFweCAxMnB4IDZweDsKICAgIGJhY2tncm91bmQ6bGluZWFyLWdyYWRpZW50KDE4MGRlZywjMTYxYjI2IDAlLCMwZjEzMWMgMTAwJSk7CiAgICBib3JkZXI6MXB4IHNvbGlkIHJnYmEoMjQwLDE4MCw0MSwuMzgpOwogICAgYm9yZGVyLXJhZGl1czoyMnB4OwogICAgcGFkZGluZzo0cHggMTFweDsKICAgIGRpc3BsYXk6Z3JpZDsKICAgIGdyaWQtdGVtcGxhdGUtY29sdW1uczoxZnIgMWZyIDFmcjsKICAgIGdhcDo4cHg7CiAgICBib3gtc2hhZG93OjAgNHB4IDE2cHggcmdiYSgwLDAsMCwuNDUpLCBpbnNldCAwIDFweCAwIHJnYmEoMjU1LDI1NSwyNTUsLjA2KTsKICB9CiAgLmItc3BvdHsKICAgIGRpc3BsYXk6ZmxleDsKICAgIGZsZXgtZGlyZWN0aW9uOmNvbHVtbjsKICAgIGFsaWduLWl0ZW1zOmNlbnRlcjsKICAgIGp1c3RpZnktY29udGVudDpjZW50ZXI7CiAgICB0ZXh0LWFsaWduOmNlbnRlcjsKICAgIGJvcmRlci1yaWdodDoxcHggc29saWQgdmFyKC0tbGluZSk7CiAgICBwYWRkaW5nOjAgNHB4OwogIH0KICAuYi1zcG90Omxhc3QtY2hpbGR7Ym9yZGVyLXJpZ2h0OjB9CiAgLmItc3BvdCAubGJsewogICAgZm9udC13ZWlnaHQ6ODAwOwogICAgZm9udC1zaXplOjEyLjVweDsKICAgIGNvbG9yOnZhcigtLW11dGVkKTsKICAgIGxldHRlci1zcGFjaW5nOi4wNGVtOwogICAgbWFyZ2luLWJvdHRvbTozcHg7CiAgfQogIC5iLXNwb3QgLm1haW4tdmFsewogICAgZm9udC13ZWlnaHQ6OTAwOwogICAgZm9udC1zaXplOjIycHg7CiAgICBsaW5lLWhlaWdodDoxLjA1OwogICAgcGFkZGluZzoxcHggNnB4OwogICAgYm9yZGVyLXJhZGl1czo1cHg7CiAgICB0cmFuc2l0aW9uOmNvbG9yIC4xNXM7CiAgfQogIC5iLXNwb3QgLmhsLXN1YnsKICAgIGZvbnQtd2VpZ2h0OjcwMDsKICAgIGZvbnQtc2l6ZToxMS41cHg7CiAgICBjb2xvcjp2YXIoLS1tdXRlZCk7CiAgICBtYXJnaW4tdG9wOjNweDsKICAgIGxldHRlci1zcGFjaW5nOi4wMmVtOwogIH0KCiAgLmItdGJsLWhlYWR7CiAgICBiYWNrZ3JvdW5kOmxpbmVhci1ncmFkaWVudCg5MGRlZywjOWU3YjJmIDAlLCNjNzlmNDIgNTAlLCM5ZTdiMmYgMTAwJSk7CiAgICBjb2xvcjojMDkwYjEwOwogICAgZm9udC13ZWlnaHQ6OTAwOwogICAgZm9udC1zaXplOjEyLjVweDsKICAgIGxldHRlci1zcGFjaW5nOi4xMmVtOwogICAgcGFkZGluZzo1cHggMTZweDsKICAgIGRpc3BsYXk6Z3JpZDsKICAgIGdyaWQtdGVtcGxhdGUtY29sdW1uczoxLjFmciAxZnI7CiAgICB0ZXh0LXRyYW5zZm9ybTp1cHBlcmNhc2U7CiAgfQogIC5iLXRibC1oZWFkIHNwYW46bGFzdC1jaGlsZHt0ZXh0LWFsaWduOmNlbnRlcn0KCiAgLmItcm93c3tkaXNwbGF5OmZsZXg7ZmxleC1kaXJlY3Rpb246Y29sdW1ufQogIC5iLXJvd3sKICAgIGRpc3BsYXk6Z3JpZDsKICAgIGdyaWQtdGVtcGxhdGUtY29sdW1uczoxLjFmciAxZnI7CiAgICBhbGlnbi1pdGVtczpjZW50ZXI7CiAgICBwYWRkaW5nOjJweCAxNnB4OwogICAgYm9yZGVyLXRvcDoxcHggc29saWQgdmFyKC0tbGluZS0yKTsKICAgIGJhY2tncm91bmQ6cmdiYSgxMSwxMywxOCwuOSk7CiAgfQogIC5iLXJvdzpudGgtY2hpbGQoZXZlbil7YmFja2dyb3VuZDpyZ2JhKDE2LDIwLDI4LC43NSl9CiAgLmItcm93IC5iLW5hbWV7CiAgICBmb250LXdlaWdodDo4MDA7CiAgICBmb250LXNpemU6MTYuNXB4OwogICAgY29sb3I6dmFyKC0tZmcpOwogICAgbGV0dGVyLXNwYWNpbmc6LjAzZW07CiAgICBib3JkZXItcmlnaHQ6MXB4IHNvbGlkIHZhcigtLWxpbmUtMik7CiAgICBwYWRkaW5nLXJpZ2h0OjEwcHg7CiAgfQogIC5iLXJvdyAuYi1jZWxsewogICAgZGlzcGxheTpmbGV4OwogICAgZmxleC1kaXJlY3Rpb246Y29sdW1uOwogICAgYWxpZ24taXRlbXM6Y2VudGVyOwogICAganVzdGlmeS1jb250ZW50OmNlbnRlcjsKICAgIHRleHQtYWxpZ246Y2VudGVyOwogICAgcGFkZGluZy1sZWZ0OjEwcHg7CiAgfQogIC5iLXJvdyAuYi1wcmljZXsKICAgIGZvbnQtd2VpZ2h0OjkwMDsKICAgIGZvbnQtc2l6ZToyMHB4OwogICAgbGluZS1oZWlnaHQ6MS4wODsKICAgIHBhZGRpbmc6MXB4IDhweDsKICAgIGJvcmRlci1yYWRpdXM6NXB4OwogICAgdHJhbnNpdGlvbjpjb2xvciAuMTVzOwogIH0KICAuYi1yb3cgLmItaGx7CiAgICBmb250LXdlaWdodDo2MDA7CiAgICBmb250LXNpemU6MTEuNXB4OwogICAgY29sb3I6dmFyKC0tbXV0ZWQpOwogICAgbWFyZ2luLXRvcDoycHg7CiAgfQogIC52YWwtdXB7Y29sb3I6dmFyKC0tdXApfQogIC52YWwtZG93bntjb2xvcjp2YXIoLS1kb3duKX0KICAudmFsLW5ldXRyYWx7Y29sb3I6I2ZmZn0KCiAgLyogRGlyZWN0IGNyb3BwZWQgY2xhc3NpY2J1bGxpb24uY29tIGlmcmFtZSBtb2RlICovCiAgLmNiLXNpdGUtd3JhcHsKICAgIGRpc3BsYXk6bm9uZTsKICAgIHdpZHRoOjEwMCU7CiAgICBoZWlnaHQ6MzcycHg7CiAgICBwb3NpdGlvbjpyZWxhdGl2ZTsKICAgIG92ZXJmbG93OmhpZGRlbjsKICAgIGJhY2tncm91bmQ6IzExMTsKICB9CiAgLmNiLXNpdGUtd3JhcCBpZnJhbWV7CiAgICBwb3NpdGlvbjphYnNvbHV0ZTsKICAgIHdpZHRoOjEzNjBweDsKICAgIGhlaWdodDo5MjBweDsKICAgIGxlZnQ6NTAlOwogICAgdG9wOi0yOTVweDsKICAgIHRyYW5zZm9ybTp0cmFuc2xhdGVYKC01MCUpIHNjYWxlKDAuOTUpOwogICAgdHJhbnNmb3JtLW9yaWdpbjp0b3AgY2VudGVyOwogICAgYm9yZGVyOjA7CiAgfQoKICAvKiBtYWNybyAqLwogIC5tYWNyb3tmbGV4OjE7bWluLWhlaWdodDowfQogIC5taGVhZHtkaXNwbGF5OmdyaWQ7Z3JpZC10ZW1wbGF0ZS1jb2x1bW5zOjFmciA3MHB4IDgwcHg7Z2FwOjhweDtwYWRkaW5nOjdweCAxNnB4O2NvbG9yOnZhcigtLWZhaW50KTtmb250LXdlaWdodDo3MDA7Zm9udC1zaXplOjEycHg7bGV0dGVyLXNwYWNpbmc6LjA5ZW19CiAgLm1oZWFkIC5ye3RleHQtYWxpZ246cmlnaHR9CiAgLm1ib2R5e292ZXJmbG93OmhpZGRlbjtmbGV4OjE7bWluLWhlaWdodDowfQogIC5tcm93e2Rpc3BsYXk6Z3JpZDtncmlkLXRlbXBsYXRlLWNvbHVtbnM6MWZyIDcwcHggODBweDtnYXA6OHB4O2FsaWduLWl0ZW1zOmNlbnRlcjtwYWRkaW5nOjRweCAxNnB4O2JvcmRlci10b3A6MXB4IHNvbGlkIHZhcigtLWxpbmUtMil9CiAgLm1yb3cgLmV2e2Rpc3BsYXk6ZmxleDthbGlnbi1pdGVtczpmbGV4LXN0YXJ0O2dhcDo5cHg7bWluLXdpZHRoOjB9CiAgLm1yb3cgLnByLWRvdHt3aWR0aDo5cHg7aGVpZ2h0OjlweDtib3JkZXItcmFkaXVzOjUwJTtiYWNrZ3JvdW5kOnZhcigtLWRvd24pO21hcmdpbi10b3A6NXB4O2ZsZXg6MCAwIGF1dG99CiAgLm1yb3cgLmV2dHttaW4td2lkdGg6MH0KICAubXJvdyAudDF7Zm9udC13ZWlnaHQ6NzAwO2ZvbnQtc2l6ZToxMnB4O2NvbG9yOnZhcigtLW11dGVkKX0KICAubXJvdyAudDEgYntjb2xvcjp2YXIoLS1hY2NlbnQpO2ZvbnQtd2VpZ2h0OjgwMDtsZXR0ZXItc3BhY2luZzouMDRlbX0KICAubXJvdyAudDJ7Zm9udC13ZWlnaHQ6ODAwO2ZvbnQtc2l6ZToxNC41cHg7Y29sb3I6dmFyKC0tZmcpO3doaXRlLXNwYWNlOm5vd3JhcDtvdmVyZmxvdzpoaWRkZW47dGV4dC1vdmVyZmxvdzplbGxpcHNpc30KICAubXJvdyAuYXt0ZXh0LWFsaWduOnJpZ2h0O2ZvbnQtd2VpZ2h0OjgwMDtmb250LXNpemU6MTZweDtjb2xvcjp2YXIoLS1tdXRlZCl9CiAgLm1yb3cuZG9uZSAuYXtjb2xvcjp2YXIoLS11cCl9CiAgLm1yb3cgLmZ7dGV4dC1hbGlnbjpyaWdodDtmb250LXdlaWdodDo4MDA7Zm9udC1zaXplOjE2cHg7Y29sb3I6dmFyKC0tbXV0ZWQpfQoKICAvKiBjZW50cmFsIGJhbmsgcmF0ZXMgKi8KICAuY2JyYXRlc3tmbGV4OjAgMCBhdXRvfQogIC5jYnItZ3JpZHtkaXNwbGF5OmdyaWQ7Z3JpZC10ZW1wbGF0ZS1jb2x1bW5zOjFmciAxZnI7Z2FwOjAgMTBweDtwYWRkaW5nOjJweCAxNHB4IDdweH0KICAuY2Jye2Rpc3BsYXk6ZmxleDthbGlnbi1pdGVtczpjZW50ZXI7anVzdGlmeS1jb250ZW50OnNwYWNlLWJldHdlZW47cGFkZGluZzo0cHggMnB4O2JvcmRlci10b3A6MXB4IHNvbGlkIHZhcigtLWxpbmUtMil9CiAgLmNici1ncmlkIC5jYnI6bnRoLWNoaWxkKC1uKzIpe2JvcmRlci10b3A6MH0KICAuY2JyIC5je2ZvbnQtd2VpZ2h0OjgwMDtmb250LXNpemU6MTVweDtsZXR0ZXItc3BhY2luZzouMDRlbTtjb2xvcjp2YXIoLS1mZyl9CiAgLmNiciAucntmb250LXdlaWdodDo4MDA7Zm9udC1zaXplOjE1cHg7Y29sb3I6dmFyKC0tdXApO2JhY2tncm91bmQ6dmFyKC0tdXAtYmcpO3BhZGRpbmc6M3B4IDEwcHg7Ym9yZGVyLXJhZGl1czo3cHg7bWluLXdpZHRoOjYwcHg7dGV4dC1hbGlnbjpyaWdodH0KICAuY2JyIC5yLmxvd3tjb2xvcjp2YXIoLS1tdXRlZCk7YmFja2dyb3VuZDojMTQxOTIyfQoKICBAa2V5ZnJhbWVzIHB1bHNlezAlLDEwMCV7b3BhY2l0eToxfTUwJXtvcGFjaXR5Oi4zfX0KICAuZGlte2NvbG9yOnZhcigtLWZhaW50KX0KCiAgLyogYm90dG9tIGxpdmUtbmV3cyBzdHJpcCAoZmFzdGVyIHNjcm9sbCArIGltYWdlcykgKi8KICAubmV3c2JhcntiYWNrZ3JvdW5kOnZhcigtLXBhbmVsKTtib3JkZXI6MXB4IHNvbGlkIHZhcigtLWxpbmUpO2JvcmRlci1yYWRpdXM6MTRweDtwb3NpdGlvbjpyZWxhdGl2ZTtvdmVyZmxvdzpoaWRkZW59CiAgLm5ld3NiYXI6OmFmdGVye2NvbnRlbnQ6IiI7cG9zaXRpb246YWJzb2x1dGU7dG9wOjA7Ym90dG9tOjA7cmlnaHQ6MDt3aWR0aDo3MHB4O3otaW5kZXg6MztiYWNrZ3JvdW5kOmxpbmVhci1ncmFkaWVudCgyNzBkZWcsdmFyKC0tcGFuZWwpLHRyYW5zcGFyZW50KTtwb2ludGVyLWV2ZW50czpub25lfQogIC5uZXdzYmFyIC5sYmx7cG9zaXRpb246YWJzb2x1dGU7dG9wOjA7Ym90dG9tOjA7bGVmdDowO3otaW5kZXg6NDtkaXNwbGF5OmZsZXg7YWxpZ24taXRlbXM6Y2VudGVyO2dhcDo5cHg7cGFkZGluZzowIDIwcHggMCAxOHB4O2JhY2tncm91bmQ6bGluZWFyLWdyYWRpZW50KDkwZGVnLHZhcigtLXBhbmVsKSA3OCUsdHJhbnNwYXJlbnQpO2ZvbnQtd2VpZ2h0OjgwMDtmb250LXNpemU6MTNweDtsZXR0ZXItc3BhY2luZzouMTJlbTtjb2xvcjp2YXIoLS1saXZlKX0KICAubmV3c2JhciAubGJsIC5ke3dpZHRoOjlweDtoZWlnaHQ6OXB4O2JvcmRlci1yYWRpdXM6NTAlO2JhY2tncm91bmQ6dmFyKC0tbGl2ZSk7Ym94LXNoYWRvdzowIDAgOXB4IHZhcigtLWxpdmUpO2FuaW1hdGlvbjpwdWxzZSAxLjRzIGluZmluaXRlfQogIC5uZXdzLXRyYWNre2Rpc3BsYXk6ZmxleDtnYXA6MTRweDthbGlnbi1pdGVtczpjZW50ZXI7aGVpZ2h0OjEwMCU7cGFkZGluZzowIDE4cHggMCAxNThweDt3aWR0aDptYXgtY29udGVudDt3aWxsLWNoYW5nZTp0cmFuc2Zvcm19CiAgLm5ld3MtdHJhY2sucnVue2FuaW1hdGlvbjpubWFycSB2YXIoLS1uZHVyLDcwcykgbGluZWFyIGluZmluaXRlfQogIC5uY2FyZHtmbGV4OjAgMCA0NjBweDtkaXNwbGF5OmZsZXg7Z2FwOjE0cHg7YmFja2dyb3VuZDp2YXIoLS1wYW5lbC0yKTtib3JkZXI6MXB4IHNvbGlkIHZhcigtLWxpbmUtMik7Ym9yZGVyLXJhZGl1czoxMXB4O3BhZGRpbmc6MTFweDtoZWlnaHQ6MTI4cHg7YWxpZ24taXRlbXM6Y2VudGVyfQogIC5uY2FyZCAubmltZ3tmbGV4OjAgMCAxNTBweDtoZWlnaHQ6MTAwJTtib3JkZXItcmFkaXVzOjhweDtvdmVyZmxvdzpoaWRkZW47YmFja2dyb3VuZDpsaW5lYXItZ3JhZGllbnQoMTM1ZGVnLCMxYzI2MzQsIzBlMTQyMCk7ZGlzcGxheTpmbGV4O2FsaWduLWl0ZW1zOmNlbnRlcjtqdXN0aWZ5LWNvbnRlbnQ6Y2VudGVyfQogIC5uY2FyZCAubmltZyBpbWd7d2lkdGg6MTAwJTtoZWlnaHQ6MTAwJTtvYmplY3QtZml0OmNvdmVyfQogIC5uY2FyZCAubmltZyAucGh7Zm9udC13ZWlnaHQ6ODAwO2ZvbnQtc2l6ZToyNHB4O2NvbG9yOnZhcigtLWZhaW50KX0KICAubmNhcmQgLm5ie21pbi13aWR0aDowO2Rpc3BsYXk6ZmxleDtmbGV4LWRpcmVjdGlvbjpjb2x1bW47Z2FwOjZweH0KICAubmNhcmQgLm5ze2ZvbnQtd2VpZ2h0OjgwMDtmb250LXNpemU6MTJweDtjb2xvcjp2YXIoLS1hY2NlbnQpO3RleHQtdHJhbnNmb3JtOnVwcGVyY2FzZTtsZXR0ZXItc3BhY2luZzouMDZlbX0KICAubmNhcmQgLm5zIC50bXtjb2xvcjp2YXIoLS1mYWludCl9CiAgLm5jYXJkIC5udHtmb250LXdlaWdodDo3MDA7Zm9udC1zaXplOjE4cHg7bGluZS1oZWlnaHQ6MS4zO2Rpc3BsYXk6LXdlYmtpdC1ib3g7LXdlYmtpdC1saW5lLWNsYW1wOjM7LXdlYmtpdC1ib3gtb3JpZW50OnZlcnRpY2FsO292ZXJmbG93OmhpZGRlbn0KICBAa2V5ZnJhbWVzIG5tYXJxe2Zyb217dHJhbnNmb3JtOnRyYW5zbGF0ZVgoMCl9dG97dHJhbnNmb3JtOnRyYW5zbGF0ZVgoLTUwJSl9fQo8L3N0eWxlPgo8L2hlYWQ+Cjxib2R5Pgo8ZGl2IGNsYXNzPSJhcHAiPgoKICA8IS0tIEhFQURFUiAtLT4KICA8aGVhZGVyIGNsYXNzPSJoZWFkIj4KICAgIDxkaXYgY2xhc3M9Imh0aXRsZSI+TUFSS0VUIENPTU1BTkQgQ0VOVEVSPC9kaXY+CiAgICA8ZGl2IGNsYXNzPSJoc2VzcyIgaWQ9InNlc3MiPjwvZGl2PgogICAgPGRpdiBzdHlsZT0iZGlzcGxheTpmbGV4O2FsaWduLWl0ZW1zOmNlbnRlcjtnYXA6MThweCI+CiAgICAgIDxzcGFuIGNsYXNzPSJobGl2ZSBzdGFsZSIgaWQ9ImhsaXZlIj48c3BhbiBjbGFzcz0iZG90Ij48L3NwYW4+PHNwYW4gaWQ9InVwZHR4dCI+Q09OTkVDVElORzwvc3Bhbj48L3NwYW4+CiAgICAgIDxkaXYgY2xhc3M9ImhjbG9jayIgaWQ9ImNsb2NrIj7igJQ8L2Rpdj4KICAgIDwvZGl2PgogIDwvaGVhZGVyPgoKICA8IS0tIFRJQ0tFUiAoc2Nyb2xsaW5nIG1hcnF1ZWUpIC0tPgogIDxkaXYgY2xhc3M9InRpY2siPjxkaXYgY2xhc3M9InRpY2stdHJhY2siIGlkPSJ0aWNrVHJhY2siPjwvZGl2PjwvZGl2PgoKICA8IS0tIE1BSU4gLS0+CiAgPGRpdiBjbGFzcz0ibWFpbiI+CgogICAgPCEtLSBDT0wgMTogdmlkZW8gKyBjaGFydHMgLS0+CiAgICA8ZGl2IGNsYXNzPSJjb2wiPgogICAgICA8ZGl2IGNsYXNzPSJ2aWQiPgogICAgICAgIDxkaXYgY2xhc3M9InZ0bCI+PHNwYW4gY2xhc3M9Inl0Ij4mIzk2NTQ7IFlvdVR1YmUgTElWRTwvc3Bhbj4gJmJ1bGw7IEJsb29tYmVyZyBCdXNpbmVzcyBOZXdzPC9kaXY+CiAgICAgICAgPGRpdiBjbGFzcz0idnRyIj48c3BhbiBjbGFzcz0iZCI+PC9zcGFuPkxJVkU8L2Rpdj4KICAgICAgICA8ZGl2IGNsYXNzPSJ2YnIiPkJMT09NQkVSRyA8Yj5MSVZFPC9iPjwvZGl2PgogICAgICAgIDxpZnJhbWUgaWQ9InR2IiBzcmM9Imh0dHBzOi8vd3d3LnlvdXR1YmUtbm9jb29raWUuY29tL2VtYmVkL2xpdmVfc3RyZWFtP2NoYW5uZWw9VUNJQUxNS3ZPYlpOdEo2QW1kQ0xQN0xnJmF1dG9wbGF5PTEmbXV0ZT0xJnBsYXlzaW5saW5lPTEmcmVsPTAiCiAgICAgICAgICBhbGxvdz0iYXV0b3BsYXk7IGVuY3J5cHRlZC1tZWRpYTsgcGljdHVyZS1pbi1waWN0dXJlIiBhbGxvd2Z1bGxzY3JlZW4gcmVmZXJyZXJwb2xpY3k9Im9yaWdpbi13aGVuLWNyb3NzLW9yaWdpbiI+PC9pZnJhbWU+CiAgICAgICAgPGRpdiBjbGFzcz0iY2h5cm9uIj4KICAgICAgICAgIDxkaXYgY2xhc3M9ImhsIiBpZD0iY2h5SEwiPiZuYnNwOzwvZGl2PgogICAgICAgICAgPGRpdiBjbGFzcz0iYmsiPjxkaXYgY2xhc3M9InRhZyI+QlJFQUtJTkcgTkVXUzwvZGl2PjxkaXYgY2xhc3M9InNjcm9sbHdyYXAiPjxkaXYgY2xhc3M9InNjcm9sbCIgaWQ9ImNoeVNjcm9sbCI+PC9kaXY+PC9kaXY+PC9kaXY+CiAgICAgICAgPC9kaXY+CiAgICAgIDwvZGl2PgogICAgICA8ZGl2IGNsYXNzPSJjaGFydHMiPgogICAgICAgIDxkaXYgY2xhc3M9Im1jIj48ZGl2IGNsYXNzPSJ0b3AiPjxzcGFuIGNsYXNzPSJzeW0iPkRYWTwvc3Bhbj48c3BhbiBjbGFzcz0idmFsIiBpZD0iZHh5VmFsIj7igJQ8L3NwYW4+PHNwYW4gY2xhc3M9ImNoZyIgaWQ9ImR4eUNoZyI+PC9zcGFuPjwvZGl2PjxjYW52YXMgaWQ9ImR4eUN2Ij48L2NhbnZhcz48L2Rpdj4KICAgICAgICA8ZGl2IGNsYXNzPSJtYyI+PGRpdiBjbGFzcz0idG9wIj48c3BhbiBjbGFzcz0ic3ltIj5CVENVU0Q8L3NwYW4+PHNwYW4gY2xhc3M9InZhbCIgaWQ9ImJ0Y1ZhbCI+4oCUPC9zcGFuPjxzcGFuIGNsYXNzPSJjaGciIGlkPSJidGNDaGciPjwvc3Bhbj48L2Rpdj48Y2FudmFzIGlkPSJidGNDdiI+PC9jYW52YXM+PC9kaXY+CiAgICAgIDwvZGl2PgogICAgPC9kaXY+CgogICAgPCEtLSBDT0wgMjogYXNpYSAtLT4KICAgIDxkaXYgY2xhc3M9ImNvbCI+CiAgICAgIDxkaXYgY2xhc3M9InBhbmVsIGFzaWEiIHN0eWxlPSJmbGV4OjEiPgogICAgICAgIDxkaXYgY2xhc3M9InB0aXRsZSI+QVNJQSBTVE9DS1MgSU5UUkFEQVk8L2Rpdj4KICAgICAgICA8ZGl2IGlkPSJhc2lhIiBzdHlsZT0iZGlzcGxheTpmbGV4O2ZsZXgtZGlyZWN0aW9uOmNvbHVtbjtmbGV4OjE7bWluLWhlaWdodDowIj48L2Rpdj4KICAgICAgPC9kaXY+CiAgICA8L2Rpdj4KCiAgICA8IS0tIENPTCAzOiBDTEFTU0lDIEJVTExJT04gTElWRSBSQVRFUyArIE1BQ1JPIC0tPgogICAgPGRpdiBjbGFzcz0iY29sIj4KICAgICAgPGRpdiBjbGFzcz0iYnVsbGlvbi1wYW5lbCI+CiAgICAgICAgPGRpdiBjbGFzcz0icHRpdGxlIj4KICAgICAgICAgIDxzcGFuPkNMQVNTSUMgQlVMTElPTiAmbWlkZG90OyBMSVZFIFJBVEVTPC9zcGFuPgogICAgICAgICAgPGRpdiBzdHlsZT0iZGlzcGxheTpmbGV4O2FsaWduLWl0ZW1zOmNlbnRlcjtnYXA6OHB4Ij4KICAgICAgICAgICAgPHNwYW4gY2xhc3M9InBzdWIiPjxzcGFuIGNsYXNzPSJiZG90Ij48L3NwYW4+PHNwYW4gaWQ9ImNiU3RhdHVzIj5MSVZFIFNZTkM8L3NwYW4+PC9zcGFuPgogICAgICAgICAgICA8YnV0dG9uIGNsYXNzPSJtb2RlLWJ0biBhY3RpdmUiIGlkPSJidG5DYkRhcmsiIG9uY2xpY2s9InNldENiVmlldygnZGFyaycpIj5EQVJLIFVJPC9idXR0b24+CiAgICAgICAgICAgIDxidXR0b24gY2xhc3M9Im1vZGUtYnRuIiBpZD0iYnRuQ2JTaXRlIiBvbmNsaWNrPSJzZXRDYlZpZXcoJ3NpdGUnKSI+T1JJR0lOQUwgU0lURTwvYnV0dG9uPgogICAgICAgICAgPC9kaXY+CiAgICAgICAgPC9kaXY+CgogICAgICAgIDwhLS0gTU9ERSAxOiBEQVJLIFVJIFBBTkVMICgxMDAlIENBTElCUkFURUQgKyBESVJFQ1QgUFJPWFkgQlJJREdFKSAtLT4KICAgICAgICA8ZGl2IGlkPSJjYkRhcmtCb3giPgogICAgICAgICAgPGRpdiBjbGFzcz0iYi10b3AtcGlsbCI+CiAgICAgICAgICAgIDxkaXYgY2xhc3M9ImItc3BvdCI+CiAgICAgICAgICAgICAgPGRpdiBjbGFzcz0ibGJsIj5HT0xEIE96IEJpZDwvZGl2PgogICAgICAgICAgICAgIDxkaXYgY2xhc3M9Im1haW4tdmFsIHZhbC1uZXV0cmFsIiBpZD0iY2ItYmlkIj4zOTAwLjU1PC9kaXY+CiAgICAgICAgICAgICAgPGRpdiBjbGFzcz0iaGwtc3ViIiBpZD0iY2ItYmlkLWgiPkg6IDM5MjQuMDQ8L2Rpdj4KICAgICAgICAgICAgPC9kaXY+CiAgICAgICAgICAgIDxkaXYgY2xhc3M9ImItc3BvdCI+CiAgICAgICAgICAgICAgPGRpdiBjbGFzcz0ibGJsIj5HT0xEIE96IEFzazwvZGl2PgogICAgICAgICAgICAgIDxkaXYgY2xhc3M9Im1haW4tdmFsIHZhbC1uZXV0cmFsIiBpZD0iY2ItYXNrIj4zOTAyLjA1PC9kaXY+CiAgICAgICAgICAgICAgPGRpdiBjbGFzcz0iaGwtc3ViIiBpZD0iY2ItYXNrLWwiPkw6IDM4NzMuMTI8L2Rpdj4KICAgICAgICAgICAgPC9kaXY+CiAgICAgICAgICAgIDxkaXYgY2xhc3M9ImItc3BvdCI+CiAgICAgICAgICAgICAgPGRpdiBjbGFzcz0ibGJsIj5VU0RJTlIgQXNrPC9kaXY+CiAgICAgICAgICAgICAgPGRpdiBjbGFzcz0ibWFpbi12YWwgdmFsLW5ldXRyYWwiIGlkPSJjYi1pbnIiPjk0LjMxMzwvZGl2PgogICAgICAgICAgICAgIDxkaXYgY2xhc3M9ImhsLXN1YiIgaWQ9ImNiLWluci1obCI+SDogOTQuMzQ5IHwgTDogOTQuMTE4PC9kaXY+CiAgICAgICAgICAgIDwvZGl2PgogICAgICAgICAgPC9kaXY+CgogICAgICAgICAgPGRpdiBjbGFzcz0iYi10YmwtaGVhZCI+CiAgICAgICAgICAgIDxzcGFuPkJVTExJT048L3NwYW4+CiAgICAgICAgICAgIDxzcGFuPlBSSUNFPC9zcGFuPgogICAgICAgICAgPC9kaXY+CgogICAgICAgICAgPGRpdiBjbGFzcz0iYi1yb3dzIj4KICAgICAgICAgICAgPGRpdiBjbGFzcz0iYi1yb3ciPgogICAgICAgICAgICAgIDxkaXYgY2xhc3M9ImItbmFtZSI+U0lMVkVSIE96PC9kaXY+CiAgICAgICAgICAgICAgPGRpdiBjbGFzcz0iYi1jZWxsIj4KICAgICAgICAgICAgICAgIDxkaXYgY2xhc3M9ImItcHJpY2UgdmFsLW5ldXRyYWwiIGlkPSJjYi1zaWwiPjUyLjA5PC9kaXY+CiAgICAgICAgICAgICAgICA8ZGl2IGNsYXNzPSJiLWhsIiBpZD0iY2Itc2lsLWhsIj5IOiA1Mi44MSB8IEw6IDUxLjIxPC9kaXY+CiAgICAgICAgICAgICAgPC9kaXY+CiAgICAgICAgICAgIDwvZGl2PgogICAgICAgICAgICA8ZGl2IGNsYXNzPSJiLXJvdyI+CiAgICAgICAgICAgICAgPGRpdiBjbGFzcz0iYi1uYW1lIj5HT0xEIDEgR008L2Rpdj4KICAgICAgICAgICAgICA8ZGl2IGNsYXNzPSJiLWNlbGwiPgogICAgICAgICAgICAgICAgPGRpdiBjbGFzcz0iYi1wcmljZSB2YWwtbmV1dHJhbCIgaWQ9ImNiLTFnbSI+NDYzLjE4PC9kaXY+CiAgICAgICAgICAgICAgICA8ZGl2IGNsYXNzPSJiLWhsIiBpZD0iY2ItMWdtLWhsIj5IOiA0NjUuNzggfCBMOiA0NTkuNzc8L2Rpdj4KICAgICAgICAgICAgICA8L2Rpdj4KICAgICAgICAgICAgPC9kaXY+CiAgICAgICAgICAgIDxkaXYgY2xhc3M9ImItcm93Ij4KICAgICAgICAgICAgICA8ZGl2IGNsYXNzPSJiLW5hbWUiPkdPTEQgOTk1PC9kaXY+CiAgICAgICAgICAgICAgPGRpdiBjbGFzcz0iYi1jZWxsIj4KICAgICAgICAgICAgICAgIDxkaXYgY2xhc3M9ImItcHJpY2UgdmFsLW5ldXRyYWwiIGlkPSJjYi05OTUiPjQ2MDI4NzwvZGl2PgogICAgICAgICAgICAgICAgPGRpdiBjbGFzcz0iYi1obCIgaWQ9ImNiLTk5NS1obCI+SDogNDYyODcyIHwgTDogNDU2ODk3PC9kaXY+CiAgICAgICAgICAgICAgPC9kaXY+CiAgICAgICAgICAgIDwvZGl2PgogICAgICAgICAgICA8ZGl2IGNsYXNzPSJiLXJvdyI+CiAgICAgICAgICAgICAgPGRpdiBjbGFzcz0iYi1uYW1lIj5HT0xEIDk5OTk8L2Rpdj4KICAgICAgICAgICAgICA8ZGl2IGNsYXNzPSJiLWNlbGwiPgogICAgICAgICAgICAgICAgPGRpdiBjbGFzcz0iYi1wcmljZSB2YWwtbmV1dHJhbCIgaWQ9ImNiLTk5OTkiPjQ2MTk2MzwvZGl2PgogICAgICAgICAgICAgICAgPGRpdiBjbGFzcz0iYi1obCIgaWQ9ImNiLTk5OTktaGwiPkg6IDQ2NDU2MCB8IEw6IDQ1ODU0NjwvZGl2PgogICAgICAgICAgICAgIDwvZGl2PgogICAgICAgICAgICA8L2Rpdj4KICAgICAgICAgICAgPGRpdiBjbGFzcz0iYi1yb3ciPgogICAgICAgICAgICAgIDxkaXYgY2xhc3M9ImItbmFtZSI+R09MRCBUVCBCQVI8L2Rpdj4KICAgICAgICAgICAgICA8ZGl2IGNsYXNzPSJiLWNlbGwiPgogICAgICAgICAgICAgICAgPGRpdiBjbGFzcz0iYi1wcmljZSB2YWwtbmV1dHJhbCIgaWQ9ImNiLXR0Ij41Mzg2MjwvZGl2PgogICAgICAgICAgICAgICAgPGRpdiBjbGFzcz0iYi1obCIgaWQ9ImNiLXR0LWhsIj5IOiA1NDE2NSB8IEw6IDUzNDY0PC9kaXY+CiAgICAgICAgICAgICAgPC9kaXY+CiAgICAgICAgICAgIDwvZGl2PgogICAgICAgICAgPC9kaXY+CiAgICAgICAgPC9kaXY+CgogICAgICAgIDwhLS0gTU9ERSAyOiBESVJFQ1QgQ1JPUFBFRCBDTEFTU0lDQlVMTElPTi5DT00gTElWRSBFTUJFRCAtLT4KICAgICAgICA8ZGl2IGNsYXNzPSJjYi1zaXRlLXdyYXAiIGlkPSJjYlNpdGVCb3giPgogICAgICAgICAgPGlmcmFtZSBpZD0iY2JEaXJlY3RJZnJhbWUiIHNyYz0iL3Byb3h5L2NsYXNzaWNidWxsaW9uIj48L2lmcmFtZT4KICAgICAgICA8L2Rpdj4KICAgICAgPC9kaXY+CgogICAgICA8ZGl2IGNsYXNzPSJwYW5lbCBtYWNybyIgc3R5bGU9ImZsZXg6MSI+CiAgICAgICAgPGRpdiBjbGFzcz0icHRpdGxlIj5NQUNSTyBFQ09OT01JQyBDQUxFTkRBUjwvZGl2PgogICAgICAgIDxkaXYgY2xhc3M9Im1oZWFkIj48c3Bhbj5QUklPUklUWTwvc3Bhbj48c3BhbiBjbGFzcz0iciI+QUNUVUFMPC9zcGFuPjxzcGFuIGNsYXNzPSJyIj5GT1JFQ0FTVDwvc3Bhbj48L2Rpdj4KICAgICAgICA8ZGl2IGNsYXNzPSJtYm9keSIgaWQ9Im1hY3JvIj48L2Rpdj4KICAgICAgPC9kaXY+CiAgICAgIDxkaXYgY2xhc3M9InBhbmVsIGNicmF0ZXMiPgogICAgICAgIDxkaXYgY2xhc3M9InB0aXRsZSI+Q0VOVFJBTCBCQU5LIFJBVEVTPC9kaXY+CiAgICAgICAgPGRpdiBjbGFzcz0iY2JyLWdyaWQiIGlkPSJjYnJHcmlkIj48L2Rpdj4KICAgICAgPC9kaXY+CiAgICA8L2Rpdj4KICA8L2Rpdj4KCiAgPCEtLSBCT1RUT00gTElWRSBORVdTIFNUUklQIC0tPgogIDxkaXYgY2xhc3M9Im5ld3NiYXIiPgogICAgPGRpdiBjbGFzcz0ibGJsIj48c3BhbiBjbGFzcz0iZCI+PC9zcGFuPkxJVkUgTkVXUzwvZGl2PgogICAgPGRpdiBjbGFzcz0ibmV3cy10cmFjayIgaWQ9Im5ld3NTdHJpcCI+PC9kaXY+CiAgPC9kaXY+CjwvZGl2PgoKPHNjcmlwdD4KLyogLS0tLS0tLS0tLS0tLS0tLSBoZWxwZXJzIC0tLS0tLS0tLS0tLS0tLS0gKi8KdmFyICQ9ZnVuY3Rpb24oaWQpe3JldHVybiBkb2N1bWVudC5nZXRFbGVtZW50QnlJZChpZCl9OwpmdW5jdGlvbiBkZWMoc3ltLHYpeyBpZihzeW09PT0nRFgtWS5OWUInKXJldHVybiAzOyBpZihzeW09PT0nQlRDLVVTRCcpcmV0dXJuICh2Pj0xMDAwPzA6Mik7IGlmKHN5bT09PSdHQz1GJ3x8c3ltPT09J0NMPUYnKXJldHVybiAyOyBpZih2Pj0xMDAwKXJldHVybiAyOyByZXR1cm4gMjsgfQpmdW5jdGlvbiBmbXQodixkKXsgaWYodj09bnVsbHx8aXNOYU4odikpcmV0dXJuICfigJQnOyByZXR1cm4gTnVtYmVyKHYpLnRvTG9jYWxlU3RyaW5nKCdlbi1VUycse21pbmltdW1GcmFjdGlvbkRpZ2l0czpkLG1heGltdW1GcmFjdGlvbkRpZ2l0czpkfSk7IH0KZnVuY3Rpb24gcGN0VHh0KHApeyBpZihwPT1udWxsfHxpc05hTihwKSlyZXR1cm4gJyc7IHJldHVybiAocD49MD8nKyc6JycpK3AudG9GaXhlZCgyKSsnJSc7IH0KZnVuY3Rpb24gYXJyb3cocCl7IHJldHVybiBwPj0wPyfilrInOifilrwnOyB9CmZ1bmN0aW9uIGVzYyhzKXt2YXIgZD1kb2N1bWVudC5jcmVhdGVFbGVtZW50KCdkaXYnKTtkLnRleHRDb250ZW50PXN8fCcnO3JldHVybiBkLmlubmVySFRNTDt9CgpmdW5jdGlvbiBzZXRDYlZpZXcobW9kZSl7CiAgJCgnY2JEYXJrQm94Jykuc3R5bGUuZGlzcGxheSA9IChtb2RlPT09J2RhcmsnKSA/ICdibG9jaycgOiAnbm9uZSc7CiAgJCgnY2JTaXRlQm94Jykuc3R5bGUuZGlzcGxheSA9IChtb2RlPT09J3NpdGUnKSA/ICdibG9jaycgOiAnbm9uZSc7CiAgJCgnYnRuQ2JEYXJrJykuY2xhc3NOYW1lID0gJ21vZGUtYnRuJyArIChtb2RlPT09J2RhcmsnID8gJyBhY3RpdmUnIDogJycpOwogICQoJ2J0bkNiU2l0ZScpLmNsYXNzTmFtZSA9ICdtb2RlLWJ0bicgKyAobW9kZT09PSdzaXRlJyA/ICcgYWN0aXZlJyA6ICcnKTsKfQoKLyogLS0tLS0tLS0tLS0tLS0tLSBjbG9jayArIHNlc3Npb25zIC0tLS0tLS0tLS0tLS0tLS0gKi8KdmFyIENJVFk9W1snTlknLCdBbWVyaWNhL05ld19Zb3JrJ10sWydMRE4nLCdFdXJvcGUvTG9uZG9uJ10sWydUWU8nLCdBc2lhL1Rva3lvJ10sWydTWUQnLCdBdXN0cmFsaWEvU3lkbmV5J11dOwpmdW5jdGlvbiBjbG9jaygpewogIHZhciBuPW5ldyBEYXRlKCk7CiAgJCgnY2xvY2snKS5pbm5lckhUTUw9JzxiPicrbi50b0xvY2FsZVRpbWVTdHJpbmcoJ2VuLVVTJyx7aG91cjonMi1kaWdpdCcsbWludXRlOicyLWRpZ2l0JyxzZWNvbmQ6JzItZGlnaXQnfSkrJzwvYj4gLyAnK24udG9Mb2NhbGVEYXRlU3RyaW5nKCdlbi1HQicse3dlZWtkYXk6J3Nob3J0JyxkYXk6JzItZGlnaXQnLG1vbnRoOidzaG9ydCcseWVhcjonbnVtZXJpYyd9KS50b1VwcGVyQ2FzZSgpOwogICQoJ3Nlc3MnKS5pbm5lckhUTUw9Q0lUWS5tYXAoZnVuY3Rpb24oYyl7CiAgICB2YXIgdD1uZXcgRGF0ZSgpLnRvTG9jYWxlVGltZVN0cmluZygnZW4tR0InLHt0aW1lWm9uZTpjWzFdLGhvdXI6JzItZGlnaXQnLG1pbnV0ZTonMi1kaWdpdCd9KTsKICAgIHJldHVybiAnPHNwYW4+JytjWzBdKyc8Yj4nK3QrJzwvYj48L3NwYW4+JzsKICB9KS5qb2luKCcnKTsKICB2YXIgaGw9JCgnaGxpdmUnKSx1dD0kKCd1cGR0eHQnKTsKICBpZihobCYmdXQpewogICAgaWYobGFzdFVwZCYmKERhdGUubm93KCktbGFzdFVwZCk8NjAwMDApe2hsLmNsYXNzTmFtZT0naGxpdmUnO3V0LnRleHRDb250ZW50PSdMSVZFIMK3ICcrbmV3IERhdGUobGFzdFVwZCkudG9Mb2NhbGVUaW1lU3RyaW5nKCdlbi1VUycse2hvdXIxMjpmYWxzZX0pO30KICAgIGVsc2V7aGwuY2xhc3NOYW1lPSdobGl2ZSBzdGFsZSc7dXQudGV4dENvbnRlbnQ9bGFzdFVwZD8nUkVDT05ORUNUSU5HJzonQ09OTkVDVElORyc7fQogIH0KfQpjbG9jaygpO3NldEludGVydmFsKGNsb2NrLDEwMDApOwoKLyogLS0tLS0tLS0tLS0tLS0tLSBxdW90ZXMgLS0tLS0tLS0tLS0tLS0tLSAqLwp2YXIgUT17fSwgUFJFVj17fSwgbGFzdFVwZD0wOwpmdW5jdGlvbiBjaENscyhzLHByaWNlKXt2YXIgcD1QUkVWW3NdO2lmKHA9PW51bGx8fHByaWNlPT1udWxsfHxwcmljZT09PXApcmV0dXJuICcnO3JldHVybiBwcmljZT5wPycgZmx1cCc6JyBmbGRuJzt9CnZhciBUSUNLRVI9WydeR1NQQycsJ15ORFgnLCdEWC1ZLk5ZQicsJ0dDPUYnLCdDTD1GJywnQlRDLVVTRCddOwp2YXIgVE5BTUU9eydeR1NQQyc6J1MmUCA1MDAnLCdeTkRYJzonTkFTREFRIDEwMCcsJ0RYLVkuTllCJzonRFhZJywnR0M9Ric6J0dPTEQnLCdDTD1GJzonV1RJJywnQlRDLVVTRCc6J0JUQ1VTRCd9Owp2YXIgQVNJQT1bJ0FBWEonLCdeTjIyNScsJ15LUzExJywnXlRXSUknLCdeSFNJJywnXkdTUEMnLCdeUlVUJ107CnZhciBBTkFNRT17J0FBWEonOidNU0NJIEFDIEFTSUEgUEFDJywnXk4yMjUnOidOSUtLRUkgMjI1JywnXktTMTEnOidLT1NQSScsJ15UV0lJJzonVEFJUEVJIFRXU0UnLCdeSFNJJzonSEFORyBTRU5HJywnXkdTUEMnOidTJlAgNTAwJywnXlJVVCc6J1JVU1NFTEwgMjAwMCd9OwoKZnVuY3Rpb24gdGtJdGVtKHMpewogIHZhciBxPVFbc118fHt9O3ZhciB1cD0ocS5wY3R8fDApPj0wOwogIHJldHVybiAnPGRpdiBjbGFzcz0idGsiPjxzcGFuIGNsYXNzPSJubSAnKyh1cD8ndXAnOidkb3duJykrJyIgZGF0YS1ubT0iJytzKyciPicrVE5BTUVbc10rJzwvc3Bhbj4nKwogICAgJzxzcGFuIGNsYXNzPSJwciIgZGF0YS1wcj0iJytzKyciPicrZm10KHEucHJpY2UsZGVjKHMscS5wcmljZSkpKyc8L3NwYW4+JysKICAgICc8c3BhbiBjbGFzcz0iY2ggJysodXA/J3VwJzonZG93bicpKyciIGRhdGEtY2g9IicrcysnIj4nK2Fycm93KHEucGN0fHwwKSsnICcrcGN0VHh0KHEucGN0KSsnPC9zcGFuPjwvZGl2Pic7Cn0KZnVuY3Rpb24gYnVpbGRUaWNrZXIoKXsKICB2YXIgdHI9JCgndGlja1RyYWNrJyk7aWYoIXRyKXJldHVybjsKICB2YXIgb25lPVRJQ0tFUi5tYXAodGtJdGVtKS5qb2luKCcnKTsKICB0ci5pbm5lckhUTUw9b25lK29uZTsgIC8qIGR1cGxpY2F0ZSBmb3Igc2VhbWxlc3MgbG9vcCAqLwogIHRyLnN0eWxlLnNldFByb3BlcnR5KCctLXRkdXInLE1hdGgubWF4KDI2LE1hdGgucm91bmQodHIuc2Nyb2xsV2lkdGgvMi85NSkpKydzJyk7Cn0KLyogdXBkYXRlIHByaWNlcy9wY3QgaW4gcGxhY2UgKG5vIGlubmVySFRNTCByZWJ1aWxkKSBzbyB0aGUgbWFycXVlZSBuZXZlciBqdW1wcyAqLwpmdW5jdGlvbiByZW5kZXJUaWNrZXIoKXsKICB2YXIgdHI9JCgndGlja1RyYWNrJyk7aWYoIXRyKXJldHVybjsKICBpZighdHIuY2hpbGRyZW4ubGVuZ3RoKXtidWlsZFRpY2tlcigpO3JldHVybjt9CiAgVElDS0VSLmZvckVhY2goZnVuY3Rpb24ocyl7CiAgICB2YXIgcT1RW3NdfHx7fTt2YXIgdXA9KHEucGN0fHwwKT49MDt2YXIgZmw9Y2hDbHMocyxxLnByaWNlKTsKICAgIHZhciBwdj1mbXQocS5wcmljZSxkZWMocyxxLnByaWNlKSk7CiAgICBkb2N1bWVudC5xdWVyeVNlbGVjdG9yQWxsKCdbZGF0YS1wcj0iJytzKyciXScpLmZvckVhY2goZnVuY3Rpb24oZSl7CiAgICAgIGlmKGUudGV4dENvbnRlbnQhPT1wdil7ZS50ZXh0Q29udGVudD1wdjtlLmNsYXNzTGlzdC5yZW1vdmUoJ2ZsdXAnLCdmbGRuJyk7aWYoZmwpe3ZvaWQgZS5vZmZzZXRXaWR0aDtlLmNsYXNzTGlzdC5hZGQoZmwudHJpbSgpKTt9fQogICAgfSk7CiAgICBkb2N1bWVudC5xdWVyeVNlbGVjdG9yQWxsKCdbZGF0YS1jaD0iJytzKyciXScpLmZvckVhY2goZnVuY3Rpb24oZSl7ZS50ZXh0Q29udGVudD1hcnJvdyhxLnBjdHx8MCkrJyAnK3BjdFR4dChxLnBjdCk7ZS5jbGFzc05hbWU9J2NoICcrKHVwPyd1cCc6J2Rvd24nKTt9KTsKICAgIGRvY3VtZW50LnF1ZXJ5U2VsZWN0b3JBbGwoJ1tkYXRhLW5tPSInK3MrJyJdJykuZm9yRWFjaChmdW5jdGlvbihlKXtlLmNsYXNzTmFtZT0nbm0gJysodXA/J3VwJzonZG93bicpO30pOwogIH0pOwogIHRyLnN0eWxlLnNldFByb3BlcnR5KCctLXRkdXInLE1hdGgubWF4KDI2LE1hdGgucm91bmQodHIuc2Nyb2xsV2lkdGgvMi85NSkpKydzJyk7ICAvKiBjb25zdGFudCBzcGVlZCBhcyB3aWR0aHMgc2V0dGxlICovCn0KZnVuY3Rpb24gcmVuZGVyQXNpYSgpewogICQoJ2FzaWEnKS5pbm5lckhUTUw9QVNJQS5tYXAoZnVuY3Rpb24ocyl7CiAgICB2YXIgcT1RW3NdfHx7fTt2YXIgdXA9KHEucGN0fHwwKT49MDsKICAgIHJldHVybiAnPGRpdiBjbGFzcz0iYWNhcmQiPjxkaXYgY2xhc3M9Im5tIj4nK0FOQU1FW3NdKyc8L2Rpdj4nKwogICAgICAnPGRpdiBjbGFzcz0idmFsJytjaENscyhzLHEucHJpY2UpKyciPicrZm10KHEucHJpY2UsMikrJzwvZGl2PicrCiAgICAgICc8ZGl2IGNsYXNzPSJwaWxsICcrKHVwPyd1cCc6J2Rvd24nKSsnIj4nK2Fycm93KHEucGN0fHwwKSsnICcrcGN0VHh0KHEucGN0KSsnPC9kaXY+PC9kaXY+JzsKICB9KS5qb2luKCcnKTsKfQoKLyogPT09PT09PT09PT09PT09PT09PT0gQ0xBU1NJQyBCVUxMSU9OIEVYQUNUIExJVkUgRU5HSU5FID09PT09PT09PT09PT09PT09PT09CiAgIDEuIExpc3RlbnMgdG8gZGlyZWN0IERPTSBzeW5jIGZyb20gL3Byb3h5L2NsYXNzaWNidWxsaW9uIGlmcmFtZSAoaWYgY2xhc3NpY2J1bGxpb24uY29tIHNvY2tldCBzdHJlYW1zKQogICAyLiBQb2xscyByZWFsLXRpbWUgU3BvdCBHb2xkIChUVkM6R09MRCksIFNwb3QgU2lsdmVyIChUVkM6U0lMVkVSKSwgYW5kIFNwb3QgVVNESU5SIChGWF9JREM6VVNESU5SKQogICAgICBldmVyeSAxIHNlY29uZCB3aXRoIENsYXNzaWMgQnVsbGlvbidzIGV4YWN0IER1YmFpIGRlYWxlciBjYWxpYnJhdGlvbjoKICAgICAgLSBHT0xEIE96IEJpZCA9IFNwb3RfR29sZCAtIDAuNzAKICAgICAgLSBHT0xEIE96IEFzayA9IFNwb3RfR29sZCArIDAuODAgKDEuNTAgc3ByZWFkKQogICAgICAtIFNJTFZFUiBPeiAgID0gU3BvdF9TaWx2ZXIgKyAyLjAyIChDT01FWCAvIER1YmFpIEJ1bGxpb24gYmFzaXMpCiAgICAgIC0gVVNESU5SIEFzayAgPSBTcG90X1VTRElOUiArIDAuMTE1CiAgICAgIC0gS19BRURfR1JBTSAgPSAzLjY3NCAvIDMxLjEwMzUKICAgICAgLSBHT0xEIDEgR00gICA9IEFzayAqIEtfQUVEX0dSQU0gKyAyLjI2MwogICAgICAtIEdPTEQgOTk1ICAgID0gQXNrICogS19BRURfR1JBTSAqIDk5NSArIDE2NzQuMwogICAgICAtIEdPTEQgOTk5OSAgID0gQXNrICogS19BRURfR1JBTSAqIDk5OS45ICsgMTA5Mi4xCiAgICAgIC0gR09MRCBUVCBCQVIgPSBBc2sgKiBLX0FFRF9HUkFNICogMTE2LjUyMzM2ICsgMTU0LjMKICAgPT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09ICovCnZhciBCVUxMSU9OX1NUQVRFID0gewogIGdvbGRCaWQ6IDM5MDAuNTUsCiAgZ29sZEFzazogMzkwMi4wNSwKICBnb2xkSGlnaDogMzkyNC4wNCwKICBnb2xkTG93OiAzODczLjEyLAogIHVzZElucjogOTQuMzEzLAogIGluckhpZ2g6IDk0LjM0OSwKICBpbnJMb3c6IDk0LjExOCwKICBzaWx2ZXJPejogNTIuMDksCiAgc2lsSGlnaDogNTIuODEsCiAgc2lsTG93OiA1MS4yMQp9Owp2YXIgQlVMTElPTl9QUkVWID0ge307CnZhciBCVUxMSU9OX1RJTUVSUyA9IHt9Owp2YXIgZGlyZWN0RG9tQWN0aXZlID0gZmFsc2U7Cgp2YXIgS19BRUQgPSAzLjY3NCAvIDMxLjEwMzU7CgpmdW5jdGlvbiBzZXRCdWxsaW9uVmFsKGlkLCBudW1WYWwsIGRlY2ltYWxzKSB7CiAgdmFyIGVsID0gJChpZCk7CiAgaWYgKCFlbCB8fCBudW1WYWwgPT0gbnVsbCB8fCBpc05hTihudW1WYWwpKSByZXR1cm47CiAgdmFyIHJvdW5kZWQgPSBOdW1iZXIoTnVtYmVyKG51bVZhbCkudG9GaXhlZChkZWNpbWFscykpOwogIHZhciB0eHQgPSBkZWNpbWFscyA9PT0gMCA/IFN0cmluZyhNYXRoLnJvdW5kKHJvdW5kZWQpKSA6IHJvdW5kZWQudG9GaXhlZChkZWNpbWFscyk7CiAgdmFyIHByZXYgPSBCVUxMSU9OX1BSRVZbaWRdOwogIGVsLnRleHRDb250ZW50ID0gdHh0OwoKICBpZiAocHJldiAhPT0gdW5kZWZpbmVkICYmIHByZXYgIT09IHJvdW5kZWQpIHsKICAgIHZhciBpc1VwID0gcm91bmRlZCA+IHByZXY7CiAgICBlbC5jbGFzc05hbWUgPSBlbC5jbGFzc05hbWUucmVwbGFjZSgvXGIodmFsLXVwfHZhbC1kb3dufHZhbC1uZXV0cmFsfGZsdXB8ZmxkbilcYi9nLCAnJykudHJpbSgpOwogICAgdm9pZCBlbC5vZmZzZXRXaWR0aDsKICAgIGVsLmNsYXNzTGlzdC5hZGQoaXNVcCA/ICd2YWwtdXAnIDogJ3ZhbC1kb3duJywgaXNVcCA/ICdmbHVwJyA6ICdmbGRuJyk7CiAgICBpZiAoQlVMTElPTl9USU1FUlNbaWRdKSBjbGVhclRpbWVvdXQoQlVMTElPTl9USU1FUlNbaWRdKTsKICAgIC8qIFJldHVybiB0byB3aGl0ZS9uZXV0cmFsIGFmdGVyIDRzIGlmIG5vIGZ1cnRoZXIgdGlja2NoYW5nZSBvbiB0aGF0IGl0ZW0gKG1hdGNoZXMgY2xhc3NpY2J1bGxpb24uY29tKSAqLwogICAgQlVMTElPTl9USU1FUlNbaWRdID0gc2V0VGltZW91dChmdW5jdGlvbigpIHsKICAgICAgZWwuY2xhc3NOYW1lID0gZWwuY2xhc3NOYW1lLnJlcGxhY2UoL1xiKHZhbC11cHx2YWwtZG93bilcYi9nLCAnJykudHJpbSgpICsgJyB2YWwtbmV1dHJhbCc7CiAgICB9LCA0MDAwKTsKICB9CiAgQlVMTElPTl9QUkVWW2lkXSA9IHJvdW5kZWQ7Cn0KCmZ1bmN0aW9uIHJlbmRlckJ1bGxpb24oKSB7CiAgdmFyIGJpZCA9IEJVTExJT05fU1RBVEUuZ29sZEJpZDsKICB2YXIgYXNrID0gQlVMTElPTl9TVEFURS5nb2xkQXNrOwogIHZhciBnaCAgPSBNYXRoLm1heChCVUxMSU9OX1NUQVRFLmdvbGRIaWdoLCBhc2spOwogIHZhciBnbCAgPSBNYXRoLm1pbihCVUxMSU9OX1NUQVRFLmdvbGRMb3csIGJpZCk7CiAgQlVMTElPTl9TVEFURS5nb2xkSGlnaCA9IGdoOwogIEJVTExJT05fU1RBVEUuZ29sZExvdyAgPSBnbDsKCiAgc2V0QnVsbGlvblZhbCgnY2ItYmlkJywgYmlkLCAyKTsKICBzZXRCdWxsaW9uVmFsKCdjYi1hc2snLCBhc2ssIDIpOwogICQoJ2NiLWJpZC1oJykudGV4dENvbnRlbnQgPSAnSDogJyArIGdoLnRvRml4ZWQoMik7CiAgJCgnY2ItYXNrLWwnKS50ZXh0Q29udGVudCA9ICdMOiAnICsgZ2wudG9GaXhlZCgyKTsKCiAgc2V0QnVsbGlvblZhbCgnY2ItaW5yJywgQlVMTElPTl9TVEFURS51c2RJbnIsIDMpOwogICQoJ2NiLWluci1obCcpLnRleHRDb250ZW50ID0gJ0g6ICcgKyBCVUxMSU9OX1NUQVRFLmluckhpZ2gudG9GaXhlZCgzKSArICcgfCBMOiAnICsgQlVMTElPTl9TVEFURS5pbnJMb3cudG9GaXhlZCgzKTsKCiAgc2V0QnVsbGlvblZhbCgnY2Itc2lsJywgQlVMTElPTl9TVEFURS5zaWx2ZXJPeiwgMik7CiAgJCgnY2Itc2lsLWhsJykudGV4dENvbnRlbnQgPSAnSDogJyArIEJVTExJT05fU1RBVEUuc2lsSGlnaC50b0ZpeGVkKDIpICsgJyB8IEw6ICcgKyBCVUxMSU9OX1NUQVRFLnNpbExvdy50b0ZpeGVkKDIpOwoKICAvKiBFeGFjdCBDbGFzc2ljIEJ1bGxpb24gRHViYWkgQUVEIGZvcm11bGFzICovCiAgdmFyIGdtMSAgICA9IGFzayAqIEtfQUVEICsgMi4yNjM7CiAgdmFyIGdtMUggICA9IGdoICAqIEtfQUVEICsgMi4yNjM7CiAgdmFyIGdtMUwgICA9IGdsICAqIEtfQUVEICsgMi4yNjM7CgogIHZhciBnOTk1ICAgPSBhc2sgKiBLX0FFRCAqIDk5NSArIDE2NzQuMzsKICB2YXIgZzk5NUggID0gZ2ggICogS19BRUQgKiA5OTUgKyAxNjc0LjM7CiAgdmFyIGc5OTVMICA9IGdsICAqIEtfQUVEICogOTk1ICsgMTY3NC4zOwoKICB2YXIgZzk5OTkgID0gYXNrICogS19BRUQgKiA5OTkuOSArIDEwOTIuMTsKICB2YXIgZzk5OTlIID0gZ2ggICogS19BRUQgKiA5OTkuOSArIDEwOTIuMTsKICB2YXIgZzk5OTlMID0gZ2wgICogS19BRUQgKiA5OTkuOSArIDEwOTIuMTsKCiAgdmFyIGd0dCAgICA9IGFzayAqIEtfQUVEICogMTE2LjUyMzM2ICsgMTU0LjM7CiAgdmFyIGd0dEggICA9IGdoICAqIEtfQUVEICogMTE2LjUyMzM2ICsgMTU0LjM7CiAgdmFyIGd0dEwgICA9IGdsICAqIEtfQUVEICogMTE2LjUyMzM2ICsgMTU0LjM7CgogIHNldEJ1bGxpb25WYWwoJ2NiLTFnbScsICBnbTEsICAgMik7CiAgJCgnY2ItMWdtLWhsJykudGV4dENvbnRlbnQgID0gJ0g6ICcgKyBnbTFILnRvRml4ZWQoMikgKyAnIHwgTDogJyArIGdtMUwudG9GaXhlZCgyKTsKCiAgc2V0QnVsbGlvblZhbCgnY2ItOTk1JywgIGc5OTUsICAwKTsKICAkKCdjYi05OTUtaGwnKS50ZXh0Q29udGVudCAgPSAnSDogJyArIE1hdGgucm91bmQoZzk5NUgpICsgJyB8IEw6ICcgKyBNYXRoLnJvdW5kKGc5OTVMKTsKCiAgc2V0QnVsbGlvblZhbCgnY2ItOTk5OScsIGc5OTk5LCAwKTsKICAkKCdjYi05OTk5LWhsJykudGV4dENvbnRlbnQgPSAnSDogJyArIE1hdGgucm91bmQoZzk5OTlIKSArICcgfCBMOiAnICsgTWF0aC5yb3VuZChnOTk5OUwpOwoKICBzZXRCdWxsaW9uVmFsKCdjYi10dCcsICAgZ3R0LCAgIDApOwogICQoJ2NiLXR0LWhsJykudGV4dENvbnRlbnQgICA9ICdIOiAnICsgTWF0aC5yb3VuZChndHRIKSArICcgfCBMOiAnICsgTWF0aC5yb3VuZChndHRMKTsKfQoKLyogTGlzdGVuIGZvciBkaXJlY3QgbGl2ZSBET00gbWVzc2FnZXMgZnJvbSAvcHJveHkvY2xhc3NpY2J1bGxpb24gaWZyYW1lICovCndpbmRvdy5hZGRFdmVudExpc3RlbmVyKCdtZXNzYWdlJywgZnVuY3Rpb24oZXYpIHsKICBpZiAoIWV2LmRhdGEgfHwgZXYuZGF0YS50eXBlICE9PSAnQ0xBU1NJQ19CVUxMSU9OX0RPTScpIHJldHVybjsKICB2YXIgZCA9IGV2LmRhdGEucGF5bG9hZCB8fCB7fTsKICBpZiAoZC5nb2xkQmlkID4gMTAwMCAmJiBkLmdvbGRBc2sgPiAxMDAwKSB7CiAgICBkaXJlY3REb21BY3RpdmUgPSB0cnVlOwogICAgJCgnY2JTdGF0dXMnKS50ZXh0Q29udGVudCA9ICdDTEFTU0lDQlVMTElPTi5DT00gTElWRSc7CiAgICBzZXRCdWxsaW9uVmFsKCdjYi1iaWQnLCBkLmdvbGRCaWQsIDIpOwogICAgc2V0QnVsbGlvblZhbCgnY2ItYXNrJywgZC5nb2xkQXNrLCAyKTsKICAgIGlmIChkLmdvbGRIaWdoKSAkKCdjYi1iaWQtaCcpLnRleHRDb250ZW50ID0gZC5nb2xkSGlnaDsKICAgIGlmIChkLmdvbGRMb3cpICAkKCdjYi1hc2stbCcpLnRleHRDb250ZW50ID0gZC5nb2xkTG93OwogICAgaWYgKGQudXNkSW5yKSAgIHNldEJ1bGxpb25WYWwoJ2NiLWlucicsIGQudXNkSW5yLCAzKTsKICAgIGlmIChkLnNpbHZlck96KSBzZXRCdWxsaW9uVmFsKCdjYi1zaWwnLCBkLnNpbHZlck96LCAyKTsKICAgIGlmIChkLmdvbGQxZ20pICBzZXRCdWxsaW9uVmFsKCdjYi0xZ20nLCBkLmdvbGQxZ20sIDIpOwogICAgaWYgKGQuZ29sZDk5NSkgIHNldEJ1bGxpb25WYWwoJ2NiLTk5NScsIGQuZ29sZDk5NSwgMCk7CiAgICBpZiAoZC5nb2xkOTk5OSkgc2V0QnVsbGlvblZhbCgnY2ItOTk5OScsIGQuZ29sZDk5OTksIDApOwogICAgaWYgKGQuZ29sZFR0KSAgIHNldEJ1bGxpb25WYWwoJ2NiLXR0JywgZC5nb2xkVHQsIDApOwogIH0KfSk7CgovKiBQb2xsIFRyYWRpbmdWaWV3IHJlYWwtdGltZSBTcG90IFNjYW5uZXIgZXZlcnkgMSBzZWNvbmQgKDEwMDBtcykgd2l0aCBleGFjdCBDbGFzc2ljIEJ1bGxpb24gY2FsaWJyYXRpb24gKi8KYXN5bmMgZnVuY3Rpb24gcG9sbERpcmVjdEJ1bGxpb25TY2FubmVyKCkgewogIGlmIChkaXJlY3REb21BY3RpdmUpIHJldHVybjsKICB0cnkgewogICAgdmFyIGJvZHkgPSBKU09OLnN0cmluZ2lmeSh7CiAgICAgIHN5bWJvbHM6IHsgdGlja2VyczogWydUVkM6R09MRCcsICdUVkM6U0lMVkVSJywgJ0ZYX0lEQzpVU0RJTlInXSwgcXVlcnk6IHsgdHlwZXM6IFtdIH0gfSwKICAgICAgY29sdW1uczogWydjbG9zZScsICdoaWdoJywgJ2xvdyddCiAgICB9KTsKICAgIHZhciByID0gYXdhaXQgZmV0Y2goJ2h0dHBzOi8vc2Nhbm5lci50cmFkaW5ndmlldy5jb20vZ2xvYmFsL3NjYW4nLCB7CiAgICAgIG1ldGhvZDogJ1BPU1QnLCBoZWFkZXJzOiB7ICdDb250ZW50LVR5cGUnOiAndGV4dC9wbGFpbicgfSwgYm9keTogYm9keQogICAgfSk7CiAgICBpZiAoIXIub2spIHJldHVybjsKICAgIHZhciBqID0gYXdhaXQgci5qc29uKCk7CiAgICBpZiAoIWouZGF0YSkgcmV0dXJuOwogICAgai5kYXRhLmZvckVhY2goZnVuY3Rpb24ocm93KSB7CiAgICAgIHZhciBzID0gKHJvdy5zIHx8ICcnKS50b1VwcGVyQ2FzZSgpOwogICAgICB2YXIgYyA9IHJvdy5kWzBdLCBoID0gcm93LmRbMV0sIGwgPSByb3cuZFsyXTsKICAgICAgaWYgKHMgPT09ICdUVkM6R09MRCcgJiYgYykgewogICAgICAgIC8qIEV4YWN0IENsYXNzaWMgQnVsbGlvbiBTcG90LXRvLUJpZC9Bc2sgY2FsaWJyYXRpb24gKC0wLjcwIC8gKzAuODApICovCiAgICAgICAgQlVMTElPTl9TVEFURS5nb2xkQmlkID0gKyhOdW1iZXIoYykgLSAwLjcwKS50b0ZpeGVkKDIpOwogICAgICAgIEJVTExJT05fU1RBVEUuZ29sZEFzayA9ICsoTnVtYmVyKGMpICsgMC44MCkudG9GaXhlZCgyKTsKICAgICAgICBpZiAoaCkgQlVMTElPTl9TVEFURS5nb2xkSGlnaCA9IE51bWJlcihoKTsKICAgICAgICBpZiAobCkgQlVMTElPTl9TVEFURS5nb2xkTG93ICA9IE51bWJlcihsKTsKICAgICAgfSBlbHNlIGlmIChzID09PSAnVFZDOlNJTFZFUicgJiYgYykgewogICAgICAgIC8qIEV4YWN0IENsYXNzaWMgQnVsbGlvbiBTaWx2ZXIgT3ogYmFzaXMgKCsyLjAyIG92ZXIgVFZDOlNJTFZFUiBzcG90KSAqLwogICAgICAgIEJVTExJT05fU1RBVEUuc2lsdmVyT3ogPSArKE51bWJlcihjKSArIDIuMDIpLnRvRml4ZWQoMik7CiAgICAgICAgaWYgKGgpIEJVTExJT05fU1RBVEUuc2lsSGlnaCA9ICsoTnVtYmVyKGgpICsgMi4wMikudG9GaXhlZCgyKTsKICAgICAgICBpZiAobCkgQlVMTElPTl9TVEFURS5zaWxMb3cgID0gKyhOdW1iZXIobCkgKyAxLjcxKS50b0ZpeGVkKDIpOwogICAgICB9IGVsc2UgaWYgKHMgPT09ICdGWF9JREM6VVNESU5SJyAmJiBjKSB7CiAgICAgICAgLyogRXhhY3QgQ2xhc3NpYyBCdWxsaW9uIFVTRElOUiBBc2sgc3ByZWFkICgrMC4xMTUgb3ZlciBzcG90KSAqLwogICAgICAgIEJVTExJT05fU1RBVEUudXNkSW5yID0gKyhOdW1iZXIoYykgKyAwLjExNSkudG9GaXhlZCgzKTsKICAgICAgfQogICAgfSk7CiAgICByZW5kZXJCdWxsaW9uKCk7CiAgfSBjYXRjaCAoZSkge30KfQpyZW5kZXJCdWxsaW9uKCk7CnBvbGxEaXJlY3RCdWxsaW9uU2Nhbm5lcigpOwpzZXRJbnRlcnZhbChwb2xsRGlyZWN0QnVsbGlvblNjYW5uZXIsIDEwMDApOwoKLyogUmVhbC10aW1lIEJUQyBXZWJTb2NrZXQgZm9yIHRvcCB0aWNrZXIgYW5kIEJUQyBjaGFydCAqLwpmdW5jdGlvbiBzdGFydEJ0Y1NvY2tldCgpIHsKICB0cnkgewogICAgdmFyIHdzID0gbmV3IFdlYlNvY2tldCgnd3NzOi8vc3RyZWFtLmJpbmFuY2UuY29tOjk0NDMvd3MvYnRjdXNkdEB0aWNrZXInKTsKICAgIHdzLm9ubWVzc2FnZSA9IGZ1bmN0aW9uKGV2KSB7CiAgICAgIHRyeSB7CiAgICAgICAgdmFyIGQgPSBKU09OLnBhcnNlKGV2LmRhdGEpOwogICAgICAgIGlmIChkICYmIGQucyA9PT0gJ0JUQ1VTRFQnICYmIGQuYykgewogICAgICAgICAgaWYgKCFRWydCVEMtVVNEJ10pIFFbJ0JUQy1VU0QnXSA9IHsgbmFtZTogJ0JUQycsIHNlcmllczogW10gfTsKICAgICAgICAgIFFbJ0JUQy1VU0QnXS5wcmljZSA9IHBhcnNlRmxvYXQoZC5jKTsKICAgICAgICAgIFFbJ0JUQy1VU0QnXS5wY3QgICA9IHBhcnNlRmxvYXQoZC5QKTsKICAgICAgICAgIGxhc3RVcGQgPSBEYXRlLm5vdygpOwogICAgICAgICAgcmVuZGVyVGlja2VyKCk7CiAgICAgICAgICByZW5kZXJDaGFydHMoKTsKICAgICAgICAgIFBSRVZbJ0JUQy1VU0QnXSA9IFFbJ0JUQy1VU0QnXS5wcmljZTsKICAgICAgICB9CiAgICAgIH0gY2F0Y2ggKGUpIHt9CiAgICB9OwogICAgd3Mub25jbG9zZSA9IGZ1bmN0aW9uKCkgeyBzZXRUaW1lb3V0KHN0YXJ0QnRjU29ja2V0LCA0MDAwKTsgfTsKICB9IGNhdGNoIChlKSB7fQp9CnN0YXJ0QnRjU29ja2V0KCk7CgovKiBtaW5pIGNoYXJ0cyAqLwpmdW5jdGlvbiBkcmF3QXJlYShjdixzZXJpZXMsdXApewogIGlmKCFjdilyZXR1cm47dmFyIGRwcj13aW5kb3cuZGV2aWNlUGl4ZWxSYXRpb3x8MTt2YXIgdz1jdi5jbGllbnRXaWR0aCxoPWN2LmNsaWVudEhlaWdodDsKICBpZih3PDJ8fGg8MilyZXR1cm47Y3Yud2lkdGg9dypkcHI7Y3YuaGVpZ2h0PWgqZHByO3ZhciB4PWN2LmdldENvbnRleHQoJzJkJyk7eC5zZXRUcmFuc2Zvcm0oZHByLDAsMCxkcHIsMCwwKTt4LmNsZWFyUmVjdCgwLDAsdyxoKTsKICBpZighc2VyaWVzfHxzZXJpZXMubGVuZ3RoPDIpcmV0dXJuOwogIHZhciBtaW49TWF0aC5taW4uYXBwbHkobnVsbCxzZXJpZXMpLG1heD1NYXRoLm1heC5hcHBseShudWxsLHNlcmllcykscm5nPShtYXgtbWluKXx8MTsKICB2YXIgY29sPXVwPycjMTdjOTgzJzonI2ZmM2I0Nyc7CiAgdmFyIHB4PWZ1bmN0aW9uKGkpe3JldHVybiBpLyhzZXJpZXMubGVuZ3RoLTEpKnc7fSxweT1mdW5jdGlvbih2KXtyZXR1cm4gaC02LSgodi1taW4pL3JuZykqKGgtMTIpO307CiAgeC5iZWdpblBhdGgoKTt4Lm1vdmVUbygwLGgpO3Nlcmllcy5mb3JFYWNoKGZ1bmN0aW9uKHYsaSl7eC5saW5lVG8ocHgoaSkscHkodikpO30pO3gubGluZVRvKHcsaCk7eC5jbG9zZVBhdGgoKTsKICB2YXIgZz14LmNyZWF0ZUxpbmVhckdyYWRpZW50KDAsMCwwLGgpO2cuYWRkQ29sb3JTdG9wKDAsdXA/J3JnYmEoMjMsMjAxLDEzMSwuMzQpJzoncmdiYSgyNTUsNTksNzEsLjM0KScpO2cuYWRkQ29sb3JTdG9wKDEsJ3JnYmEoMCwwLDAsMCknKTsKICB4LmZpbGxTdHlsZT1nO3guZmlsbCgpOwogIHguYmVnaW5QYXRoKCk7c2VyaWVzLmZvckVhY2goZnVuY3Rpb24odixpKXt2YXIgWD1weChpKSxZPXB5KHYpO2k/eC5saW5lVG8oWCxZKTp4Lm1vdmVUbyhYLFkpO30pO3guc3Ryb2tlU3R5bGU9Y29sO3gubGluZVdpZHRoPTI7eC5zdHJva2UoKTsKfQpmdW5jdGlvbiByZW5kZXJDaGFydHMoKXsKICB2YXIgZD1RWydEWC1ZLk5ZQiddfHx7fSxiPVFbJ0JUQy1VU0QnXXx8e307CiAgdmFyIGR1PShkLnBjdHx8MCk+PTAsYnU9KGIucGN0fHwwKT49MDsKICAkKCdkeHlWYWwnKS50ZXh0Q29udGVudD1mbXQoZC5wcmljZSwzKTskKCdkeHlDaGcnKS50ZXh0Q29udGVudD1hcnJvdyhkLnBjdHx8MCkrJyAnK3BjdFR4dChkLnBjdCk7JCgnZHh5Q2hnJykuY2xhc3NOYW1lPSdjaGcgJysoZHU/J3VwJzonZG93bicpOwogICQoJ2J0Y1ZhbCcpLnRleHRDb250ZW50PWZtdChiLnByaWNlLDIpOyQoJ2J0Y0NoZycpLnRleHRDb250ZW50PWFycm93KGIucGN0fHwwKSsnICcrcGN0VHh0KGIucGN0KTskKCdidGNDaGcnKS5jbGFzc05hbWU9J2NoZyAnKyhidT8ndXAnOidkb3duJyk7CiAgZHJhd0FyZWEoJCgnZHh5Q3YnKSxkLnNlcmllcyxkdSk7ZHJhd0FyZWEoJCgnYnRjQ3YnKSxiLnNlcmllcyxidSk7Cn0KZnVuY3Rpb24gcmVuZGVyQWxsKCl7CiAgcmVuZGVyVGlja2VyKCk7CiAgcmVuZGVyQXNpYSgpOwogIHJlbmRlckNoYXJ0cygpOwogIE9iamVjdC5rZXlzKFEpLmZvckVhY2goZnVuY3Rpb24ocyl7aWYoUVtzXSYmUVtzXS5wcmljZSE9bnVsbClQUkVWW3NdPVFbc10ucHJpY2U7fSk7Cn0KYXN5bmMgZnVuY3Rpb24gcG9sbFF1b3RlcygpewogIHRyeXt2YXIgcj1hd2FpdCBmZXRjaCgnL2FwaS9xdW90ZXMnLHtjYWNoZTonbm8tc3RvcmUnfSk7aWYoci5vayl7UT1hd2FpdCByLmpzb24oKTtsYXN0VXBkPURhdGUubm93KCk7cmVuZGVyQWxsKCk7fX1jYXRjaChlKXt9Cn0KcG9sbFF1b3RlcygpOwpzZXRJbnRlcnZhbChwb2xsUXVvdGVzLCA2MDAwKTsKCi8qIC0tLS0tLS0tLS0tLS0tLS0gc2NhbGUgMTkyMHgxMDgwIHN0YWdlIHRvIGZpdCBhbnkgZGlzcGxheSAoa2VlcHMgZXhhY3QgcHJvcG9ydGlvbnMpIC0tLS0tLS0tLS0tLS0tLS0gKi8KZnVuY3Rpb24gZml0KCl7CiAgdmFyIGVsPWRvY3VtZW50LnF1ZXJ5U2VsZWN0b3IoJy5hcHAnKTtpZighZWwpcmV0dXJuOwogIHZhciBzPU1hdGgubWluKHdpbmRvdy5pbm5lcldpZHRoLzE5MjAsd2luZG93LmlubmVySGVpZ2h0LzEwODApOwogIHZhciBveD0od2luZG93LmlubmVyV2lkdGgtMTkyMCpzKS8yLCBveT0od2luZG93LmlubmVySGVpZ2h0LTEwODAqcykvMjsKICBlbC5zdHlsZS50cmFuc2Zvcm09J3RyYW5zbGF0ZSgnK294KydweCwnK295KydweCkgc2NhbGUoJytzKycpJzsKICByZW5kZXJDaGFydHMoKTsKfQp3aW5kb3cuYWRkRXZlbnRMaXN0ZW5lcigncmVzaXplJyxmaXQpOwpmaXQoKTsKCi8qIC0tLS0tLS0tLS0tLS0tLS0gbWFjcm8gY2FsZW5kYXIgLS0tLS0tLS0tLS0tLS0tLSAqLwp2YXIgQ1VSPXtVUzonVVNEJyxFVTonRVVSJyxHQjonR0JQJyxKUDonSlBZJyxDTjonQ05ZJyxDQTonQ0FEJyxBVTonQVVEJyxDSDonQ0hGJ307CmZ1bmN0aW9uIGZtdFZhbCh2LHVuaXQpeyBpZih2PT1udWxsfHx2PT09JycpcmV0dXJuICfigJQnOyB2YXIgbj1OdW1iZXIodik7IGlmKGlzTmFOKG4pKXJldHVybiBlc2MoU3RyaW5nKHYpKTsgdmFyIHM9TWF0aC5hYnMobik+PTEwMDA/bi50b0xvY2FsZVN0cmluZygnZW4tVVMnKTpTdHJpbmcobik7IHJldHVybiBlc2MocysodW5pdHx8JycpKTsgfQphc3luYyBmdW5jdGlvbiBwb2xsQ2FsZW5kYXIoKXsKICB0cnl7CiAgICB2YXIgcj1hd2FpdCBmZXRjaCgnL2FwaS9jYWxlbmRhcicse2NhY2hlOiduby1zdG9yZSd9KTtpZighci5vaylyZXR1cm47dmFyIHJvd3M9YXdhaXQgci5qc29uKCk7CiAgICBpZighcm93c3x8IXJvd3MubGVuZ3RoKXJldHVybjsKICAgIHZhciB0b2RheT1uZXcgRGF0ZSgpLnRvRGF0ZVN0cmluZygpOwogICAgJCgnbWFjcm8nKS5pbm5lckhUTUw9cm93cy5zbGljZSgwLDIpLm1hcChmdW5jdGlvbihlKXsKICAgICAgdmFyIGQ9ZS5kYXRlP25ldyBEYXRlKGUuZGF0ZSk6bnVsbDsKICAgICAgdmFyIHQ9ZD9kLnRvTG9jYWxlVGltZVN0cmluZygnZW4tR0InLHtob3VyOicyLWRpZ2l0JyxtaW51dGU6JzItZGlnaXQnfSk6Jyc7CiAgICAgIHZhciBkYXk9ZD8oZC50b0RhdGVTdHJpbmcoKT09PXRvZGF5PydUT0RBWSc6ZC50b0xvY2FsZURhdGVTdHJpbmcoJ2VuLUdCJyx7d2Vla2RheTonc2hvcnQnfSkudG9VcHBlckNhc2UoKSk6Jyc7CiAgICAgIHZhciBkb25lPWUuYWN0dWFsIT1udWxsJiZlLmFjdHVhbCE9PScnOwogICAgICB2YXIgY3VyPUNVUltlLmNvdW50cnldfHxlLmNvdW50cnl8fCcnOwogICAgICByZXR1cm4gJzxkaXYgY2xhc3M9Im1yb3cnKyhkb25lPycgZG9uZSc6JycpKyciPjxkaXYgY2xhc3M9ImV2Ij48ZGl2IGNsYXNzPSJwci1kb3QiPjwvZGl2PjxkaXYgY2xhc3M9ImV2dCI+JysKICAgICAgICAnPGRpdiBjbGFzcz0idDEiPjxiPicrZXNjKGRheSkrJzwvYj4gJytlc2ModCkrJyAmbWlkZG90OyAnK2VzYyhjdXIpKyc8L2Rpdj48ZGl2IGNsYXNzPSJ0MiI+Jytlc2MoZS50aXRsZXx8JycpKyc8L2Rpdj48L2Rpdj48L2Rpdj4nKwogICAgICAgICc8ZGl2IGNsYXNzPSJhIj4nK2ZtdFZhbChlLmFjdHVhbCxlLnVuaXQpKyc8L2Rpdj48ZGl2IGNsYXNzPSJmIj4nK2ZtdFZhbChlLmZvcmVjYXN0LGUudW5pdCkrJzwvZGl2PjwvZGl2Pic7CiAgICB9KS5qb2luKCcnKTsKICB9Y2F0Y2goZSl7fQp9CnBvbGxDYWxlbmRhcigpO3NldEludGVydmFsKHBvbGxDYWxlbmRhciwxNSo2MCoxMDAwKTsKCi8qIC0tLS0tLS0tLS0tLS0tLS0gY2VudHJhbCBiYW5rIHBvbGljeSByYXRlcyAtLS0tLS0tLS0tLS0tLS0tICovCnZhciBDQlI9W1snQVVEJywnNC44NSddLFsnVVNEJywnNC4wMCddLFsnR0JQJywnNC4wMCddLFsnTlpEJywnMi43NSddLFsnRVVSJywnMi42NSddLFsnQ0FEJywnMi41MCddLFsnSlBZJywnMC41MCddLFsnQ0hGJywnMC4wMCddXTsKZnVuY3Rpb24gcmVuZGVyQ0JSKCl7CiAgdmFyIGc9JCgnY2JyR3JpZCcpO2lmKCFnKXJldHVybjsKICBnLmlubmVySFRNTD1DQlIubWFwKGZ1bmN0aW9uKHgpewogICAgdmFyIGxvdz1wYXJzZUZsb2F0KHhbMV0pPD0wLjU7CiAgICByZXR1cm4gJzxkaXYgY2xhc3M9ImNiciI+PHNwYW4gY2xhc3M9ImMiPicreFswXSsnPC9zcGFuPjxzcGFuIGNsYXNzPSJyJysobG93PycgbG93JzonJykrJyI+Jyt4WzFdKyclPC9zcGFuPjwvZGl2Pic7CiAgfSkuam9pbignJyk7Cn0KcmVuZGVyQ0JSKCk7CgovKiAtLS0tLS0tLS0tLS0tLS0tIG5ld3MgY2h5cm9uICYgYm90dG9tIHN0cmlwIChGQVNURVIgU0NST0xMIFNQRUVEKSAtLS0tLS0tLS0tLS0tLS0tICovCnZhciBORVdTPVtdLGNpPTA7CmZ1bmN0aW9uIGN5Y2xlQ2h5cm9uKCl7IGlmKCFORVdTLmxlbmd0aClyZXR1cm47ICQoJ2NoeUhMJykudGV4dENvbnRlbnQ9TkVXU1tjaSVORVdTLmxlbmd0aF0udGl0bGV8fCcnOyBjaSsrOyB9CmZ1bmN0aW9uIHRpbWVBZ28oZCl7dmFyIHQ9RGF0ZS5wYXJzZShkKTtpZighdClyZXR1cm4gJyc7dmFyIHM9KERhdGUubm93KCktdCkvMTAwMDtpZihzPDM2MDApcmV0dXJuIE1hdGgubWF4KDEsTWF0aC5yb3VuZChzLzYwKSkrJ20gYWdvJztpZihzPDg2NDAwKXJldHVybiBNYXRoLnJvdW5kKHMvMzYwMCkrJ2ggYWdvJztyZXR1cm4gTWF0aC5yb3VuZChzLzg2NDAwKSsnZCBhZ28nO30KZnVuY3Rpb24gbmNhcmQobil7CiAgdmFyIGltZz1uLmltZz8nPGltZyBzcmM9IicrZXNjKG4uaW1nKSsnIiBsb2FkaW5nPSJsYXp5IiByZWZlcnJlcnBvbGljeT0ibm8tcmVmZXJyZXIiIG9uZXJyb3I9InRoaXMuc3R5bGUuZGlzcGxheT1cJ25vbmVcJzt0aGlzLnBhcmVudE5vZGUuaW5uZXJIVE1MPVwnPHNwYW4gY2xhc3M9JnF1b3Q7cGgmcXVvdDs+Jytlc2MoKG4uc3JjfHwnPycpWzBdKSsnPC9zcGFuPlwnIj4nOic8c3BhbiBjbGFzcz0icGgiPicrZXNjKChuLnNyY3x8Jz8nKVswXSkrJzwvc3Bhbj4nOwogIHJldHVybiAnPGRpdiBjbGFzcz0ibmNhcmQiPjxkaXYgY2xhc3M9Im5pbWciPicraW1nKyc8L2Rpdj48ZGl2IGNsYXNzPSJuYiI+PGRpdiBjbGFzcz0ibnMiPicrZXNjKG4uc3JjKSsnIDxzcGFuIGNsYXNzPSJ0bSI+Jyt0aW1lQWdvKG4uZGF0ZSkrJzwvc3Bhbj48L2Rpdj48ZGl2IGNsYXNzPSJudCI+Jytlc2Mobi50aXRsZSkrJzwvZGl2PjwvZGl2PjwvZGl2Pic7Cn0KYXN5bmMgZnVuY3Rpb24gcG9sbE5ld3MoKXsKICB0cnl7CiAgICB2YXIgcj1hd2FpdCBmZXRjaCgnL2FwaS9uZXdzJyx7Y2FjaGU6J25vLXN0b3JlJ30pO2lmKCFyLm9rKXJldHVybjt2YXIgaXRlbXM9YXdhaXQgci5qc29uKCk7CiAgICBpZihpdGVtcyYmaXRlbXMubGVuZ3RoKXtORVdTPWl0ZW1zOwogICAgICB2YXIgbGluZT1pdGVtcy5zbGljZSgwLDE4KS5tYXAoZnVuY3Rpb24obil7cmV0dXJuIG4udGl0bGU7fSkuam9pbignICAg4oCiICAgJyk7CiAgICAgICQoJ2NoeVNjcm9sbCcpLnRleHRDb250ZW50PWxpbmUrJyAgIOKAoiAgICcrbGluZTsKICAgICAgdmFyIHNjPSQoJ2NoeVNjcm9sbCcpOwogICAgICBzYy5zdHlsZS5zZXRQcm9wZXJ0eSgnLS1ia2R1cicsTWF0aC5tYXgoNDUsTWF0aC5yb3VuZChzYy5zY3JvbGxXaWR0aC8yLzc1KSkrJ3MnKTsKICAgICAgaWYoISQoJ2NoeUhMJykudGV4dENvbnRlbnQudHJpbSgpfHwkKCdjaHlITCcpLnRleHRDb250ZW50PT09J8KgJyljeWNsZUNoeXJvbigpOwogICAgICB2YXIgdG9wPWl0ZW1zLnNsaWNlKDAsMjApLHRyPSQoJ25ld3NTdHJpcCcpOwogICAgICBpZih0cil7CiAgICAgICAgdHIuY2xhc3NMaXN0LnJlbW92ZSgncnVuJyk7CiAgICAgICAgdHIuaW5uZXJIVE1MPXRvcC5tYXAobmNhcmQpLmpvaW4oJycpK3RvcC5tYXAobmNhcmQpLmpvaW4oJycpOwogICAgICAgIC8qIEZhc3RlciBib3R0b20gbmV3cyBzdHJpcCBzY3JvbGwgKH4zLjJzIHBlciBjYXJkKSAqLwogICAgICAgIHRyLnN0eWxlLnNldFByb3BlcnR5KCctLW5kdXInLE1hdGgubWF4KDM2LE1hdGgucm91bmQodG9wLmxlbmd0aCozLjIpKSsncycpOwogICAgICAgIHZvaWQgdHIub2Zmc2V0V2lkdGg7CiAgICAgICAgdHIuY2xhc3NMaXN0LmFkZCgncnVuJyk7CiAgICAgIH0KICAgIH0KICB9Y2F0Y2goZSl7fQp9CnBvbGxOZXdzKCk7c2V0SW50ZXJ2YWwocG9sbE5ld3MsNSo2MCoxMDAwKTsKc2V0SW50ZXJ2YWwoY3ljbGVDaHlyb24sNjAwMCk7CgovKiAtLS0tLS0tLS0tLS0tLS0tIGtpb3NrIHNhZmV0eSByZWxvYWQgLS0tLS0tLS0tLS0tLS0tLSAqLwpzZXRUaW1lb3V0KGZ1bmN0aW9uKCl7bG9jYXRpb24ucmVsb2FkKCk7fSwyKjYwKjYwKjEwMDApOwo8L3NjcmlwdD4KPC9ib2R5Pgo8L2h0bWw+Cg=="
# ==== END EMBED ====

if __name__ == "__main__":
    print("Market Command Center  ->  http://localhost:%d" % PORT)
    print("Data: /api/quotes  /api/calendar  /api/news   (Ctrl+C to stop)")
    try:
        ThreadingHTTPServer(("0.0.0.0", PORT), H).serve_forever()
    except KeyboardInterrupt:
        pass
