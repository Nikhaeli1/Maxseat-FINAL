#!/usr/bin/env python3
"""
sensor_sender.py — sends MaxSeat readings from the Pi to the website over HTTPS.

Vercel has no always-on WebSocket server, so the Pi now POSTs each reading to /api/sensor
instead of sending it over /ws. Pages pick the new reading up within a few seconds.

Use it two ways:

1. From your own uploader (e.g. ~/maxseat/uploader.py):

       from sensor_sender import send_update
       send_update({"puv_id": 1, "passengers": 12, "temp": 33.5,
                    "lat": 8.4822, "lng": 124.6472, "speed": "18 km/h"})

2. As a quick test / simulator from any computer:

       python3 sensor_sender.py --test            # one fake reading
       python3 sensor_sender.py --simulate 60     # a fake reading every 3 s for 60 s

Settings come from environment variables (or edit the defaults below):
    MAXSEAT_URL         https://your-project.vercel.app
    MAXSEAT_DEVICE_KEY  same value as SENSOR_API_KEY in the Vercel project settings
    MAXSEAT_PUV_ID      the vehicle's ID number in the system (default 1)

Needs: pip install requests
"""
import os
import random
import sys
import time

import requests

SITE_URL   = os.environ.get("MAXSEAT_URL", "https://your-project.vercel.app").rstrip("/")
DEVICE_KEY = os.environ.get("MAXSEAT_DEVICE_KEY", "")
PUV_ID     = int(os.environ.get("MAXSEAT_PUV_ID", "1"))

_session = requests.Session()


def send_update(payload: dict, retries: int = 2, timeout: float = 8.0) -> bool:
    """POST one reading. Returns True if the server saved it.
    Never raises, so a bad connection on the road can't crash the uploader."""
    body = dict(payload)
    body.setdefault("puv_id", PUV_ID)
    headers = {"X-Device-Key": DEVICE_KEY} if DEVICE_KEY else {}
    for attempt in range(retries + 1):
        try:
            r = _session.post(f"{SITE_URL}/api/sensor", json=body, headers=headers, timeout=timeout)
            if r.status_code == 200:
                return True
            if r.status_code in (400, 401, 404):        # wrong key / unknown vehicle: retrying won't help
                print(f"[sender] rejected ({r.status_code}): {r.text[:200]}")
                return False
            print(f"[sender] server error {r.status_code}, retrying…")
        except requests.RequestException as e:
            print(f"[sender] no connection ({e.__class__.__name__}), retrying…")
        time.sleep(1.5 * (attempt + 1))
    return False


def _fake_reading(passengers: int) -> dict:
    return {
        "puv_id":     PUV_ID,
        "passengers": passengers,
        "temp":       round(random.uniform(30.0, 36.5), 1),
        "lat":        8.4822 + random.uniform(-0.01, 0.01),
        "lng":        124.6472 + random.uniform(-0.01, 0.01),
        "speed":      f"{random.randint(0, 40)} km/h",
        "loc_name":   "Test run",
    }


if __name__ == "__main__":
    if "--test" in sys.argv:
        ok = send_update(_fake_reading(random.randint(5, 25)))
        print("Saved ✓" if ok else "Not saved ✗")
    elif "--simulate" in sys.argv:
        i = sys.argv.index("--simulate")
        seconds = float(sys.argv[i + 1]) if len(sys.argv) > i + 1 else 60
        end, pax = time.time() + seconds, 10
        while time.time() < end:
            pax = max(0, pax + random.choice([-2, -1, 0, 1, 2, 3]))
            ok = send_update(_fake_reading(pax))
            print(f"passengers={pax:2d}  {'saved' if ok else 'FAILED'}")
            time.sleep(3)
    else:
        print(__doc__)
