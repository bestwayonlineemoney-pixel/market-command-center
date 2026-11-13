# Market Command Center

A live wall-display dashboard — Bloomberg live video, a scrolling price ticker,
Asia indices, live bullion rates (Classic Bullion style), a macro-economic
calendar, central-bank policy rates, and a live news strip.

It is a single self-contained Python file (standard library only): `wall-server.py`
serves the dashboard page and proxies all market data server-side
(`/api/quotes`, `/api/calendar`, `/api/news`, `/proxy/classicbullion`), so the
browser needs no API keys and hits no CORS walls. The dashboard HTML is embedded
inside the server, and an editable copy also lives in `market-wall.html`.

## Run locally (Windows display PC)

Keep `wall-server.py` in a folder and double-click `start-wall.bat`
(opens fullscreen Chrome at <http://localhost:8750/>).

## Deploy to the cloud (Render — free, always on)

This repo is Render-ready:

1. On [render.com](https://render.com) → **Add new → Blueprint** → connect this repo → **Apply**.
   `render.yaml` configures everything (free plan, `python wall-server.py`).
2. Wait ~2–3 min for the first deploy. You get a URL like
   `https://market-command-center.onrender.com`.
3. On the wall-display PC, put that URL into `open-dashboard.bat` and double-click it
   (no Python needed locally anymore).

The server binds `0.0.0.0` and reads `$PORT` from the environment, so it runs on
Render (or any Python host) unchanged. Any push to this repo auto-redeploys.

## Files

| File | Purpose |
|---|---|
| `wall-server.py` | The server + embedded dashboard + data proxies |
| `market-wall.html` | Editable copy of the dashboard page |
| `render.yaml` | Render blueprint (free web service) |
| `requirements.txt` | (empty — standard library only) |
| `runtime.txt` | Python version pin for Render |
| `start-wall.bat` | Local launcher (runs the server + opens kiosk Chrome) |
| `open-dashboard.bat` | Opens the hosted URL in kiosk Chrome |
