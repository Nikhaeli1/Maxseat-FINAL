# Deploying MaxSeat Alert on Vercel

## What changed (and why)

Vercel runs the app as short-lived "functions". Two things work differently from Railway:

1. **No shared memory.** Each request can land on a different server. So the database is now
   the only source of truth. Every page load reads fresh data, and every change is saved right
   away. Mobile login tokens and profile pictures now live in the database too.
2. **No always-on WebSocket hub.** Live updates now use **polling**: open pages ask
   `/api/live` every 4 seconds. The map, cards and tables update by themselves, same as before.
   The Pi sends readings with a normal HTTPS POST to `/api/sensor`.

Running locally with `uvicorn main:app` still uses the old WebSocket mode. Nothing to change there.

## One-time setup

### 1. Import the repo
- Go to vercel.com → **Add New → Project** → import the GitHub repo.
- Pick the branch with these changes.
- Framework preset: **FastAPI** (Vercel detects `main.py` by itself). No build command needed.

### 2. Add the database (Neon Postgres)
- In the project: **Storage → Create Database → Neon (Postgres)**.
- Region: **Singapore** (closest to CDO, and matches `vercel.json`).
- Connect it to the project. Vercel adds `DATABASE_URL` for you.
- Tables are created automatically on the first request.
- First login: `admin` / `admin@2026001`. Change it after.

> Moving old Railway data? Export the `ms_*` tables from Railway Postgres and import them into
> Neon (for example with `pg_dump --data-only -t 'ms_*'` then `psql`). If you skip this, the
> system starts fresh with only the admin account.

### 3. Environment variables
**Settings → Environment Variables**, for Production (and Preview if you use it):

| Name | Value | Needed? |
|---|---|---|
| `DATABASE_URL` | added by the Neon step | Yes |
| `SESSION_SECRET` | any long random text (e.g. run `python -c "import secrets;print(secrets.token_hex(32))"`) | Yes |
| `SENSOR_API_KEY` | another random text. The Pi must send the same value | Yes |
| `RESEND_API_KEY` | your Resend key, for emailing new accounts | Optional |
| `CARTO_API_KEY` | your CARTO Basemaps key (carto.com/basemaps/apikey), for the CARTO map style | Optional. Without it, maps use OpenStreetMap |

Redeploy after adding or changing variables.

### 4. Deploy
Push to the branch, or click **Redeploy**. Open the `.vercel.app` link and log in.

## Pi (sensor) changes

The Pi used to send readings over the WebSocket. Now it POSTs them:

```
POST https://<your-project>.vercel.app/api/sensor
Header:  X-Device-Key: <SENSOR_API_KEY>
Body:    {"puv_id": 1, "passengers": 12, "temp": 33.5,
          "lat": 8.4822, "lng": 124.6472, "speed": "18 km/h", "loc_name": "Divisoria"}
```

`plate` can be used instead of `puv_id`. Only send the fields you have.

Easiest way: copy `tools/sensor_sender.py` to the Pi (next to `uploader.py`), then:

```bash
pip install requests
export MAXSEAT_URL="https://<your-project>.vercel.app"
export MAXSEAT_DEVICE_KEY="<same as SENSOR_API_KEY>"
export MAXSEAT_PUV_ID=1
python3 sensor_sender.py --test          # should print "Saved ✓"
```

In `uploader.py`, replace the WebSocket send with:

```python
from sensor_sender import send_update
send_update({"passengers": count, "temp": temp_c, "lat": lat, "lng": lng, "speed": f"{kmh:.0f} km/h"})
```

**How often:** every 3 to 5 seconds is plenty (pages poll every 4 s). Sending faster only uses
up your Vercel and Neon limits.

The kiosk monitor: point its browser at the new `.vercel.app` URL.

## Free plan notes
- Every open page makes one small request every 4 s (paused while the tab is hidden), and the
  Pi makes one every few seconds. That's fine for demos and testing.
- Close dashboards you aren't using, and don't run the simulator for hours.
- Check **Usage** in the Vercel dashboard and **Monitoring** in Neon now and then.

## Quick checks if something's off
- **Login loops back to the login page:** `SESSION_SECRET` missing, or you opened an `http://` link. Use `https://`.
- **Pi gets 401:** the `X-Device-Key` doesn't match `SENSOR_API_KEY`.
- **Pi gets 404:** wrong `puv_id` or plate. Check the vehicle list in the system.
- **Data disappears:** `DATABASE_URL` isn't set, so it fell back to a temporary file. Check the logs for `[DB] WARNING`.
- **Logs:** Vercel project → **Logs**.

## Files added or changed
- `main.py`: reads from the database every request; `/api/sensor`, `/api/live`, `/avatar/<user>`;
  `SESSION_SECRET` and `SENSOR_API_KEY` from env.
- `database.py`: Neon-friendly connection; tables for login tokens and profile pictures.
- `static/js/ws-client.js`: polling mode (used on Vercel); WebSocket mode kept for local runs.
- `templates/layout.html`: tells the page which mode to use.
- `vercel.json` (Singapore region), `.python-version` (3.12), `.vercelignore`.
- `tools/sensor_sender.py`: Pi helper and test simulator.
