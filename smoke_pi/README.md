# CLEAR · Smoke Collector + Dashboard (Raspberry Pi)

A small always-on service that builds a **historical archive of wildfire-smoke
model output over Ontario + Québec** — because neither source keeps history
publicly (they serve only the latest runs). It polls both, clips each grid to an
ON/QC mesh (tens of KB instead of ~84 MB), and saves a timestamped slice. Months
of slices become a training set; the latest of each drives a live dashboard.

```
  BlueSky Canada (firesmoke.ca)  ─┐
   hourly PM2.5 smoke · NetCDF    │   collector.py        web dashboard
                                  ├─►  clip → ON/QC mesh ─► web/data/*.json ─► serve.py :8077
  FireWork / RAQDPS (ECCC MSC)   ─┘   dedupe by run         (status + slices)
   surface smoke PM2.5 · GRIB2        append slice
```

## Why a Pi
A Raspberry Pi is a full Linux box, so it reads **GRIB2 (`eccodes`) and NetCDF
(`xarray`)** — the exact libraries that don't fit a serverless lambda. It's cheap
to leave running 24/7, which is the whole point: the archive only grows while
it's collecting.

## Setup
```bash
# 1. clone (this lives in the CLEAR25 repo under smoke_pi/)
cd ~/CLEAR25/smoke_pi

# 2. system lib for GRIB2
sudo apt-get update && sudo apt-get install -y libeccodes0 libeccodes-dev

# 3. python env
python3 -m venv ~/CLEAR25/.venv
~/CLEAR25/.venv/bin/pip install -r requirements.txt

# 4. test it once (downloads one BlueSky + one FireWork slice)
~/CLEAR25/.venv/bin/python collector.py --sources bluesky,firework

# 5. start the dashboard (the free CARTO key removes the map-tile watermark:
#    https://carto.com/basemaps/apikey/)
CARTO_BASEMAPS_KEY=your-key ~/CLEAR25/.venv/bin/python serve.py --port 8077
#   → open http://<pi-ip>:8077/ from any device on your network
```

## Run it automatically (systemd)
Edit the two paths in each unit under `systemd/`, then:
```bash
sudo cp systemd/* /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now smoke-dashboard.service   # the web UI
sudo systemctl enable --now smoke-collector.timer     # collect every 3h
# check:
systemctl status smoke-collector.timer
journalctl -u smoke-collector.service -n 40
```
The collector de-dupes on the model run stamp, so the 3-hourly timer only
downloads when there's actually a new run.

## Storage
- Each slice is small (the ON/QC mesh, not the raw grid), so the archive grows
  slowly. Still, **point `web/data/` at a USB SSD**, not the SD card — frequent
  writes wear SD cards out.
- The raw 84 MB BlueSky / GRIB files are downloaded to a temp dir and deleted
  after clipping; only the mesh slice is kept.

## Data layout (`web/data/`, gitignored)
| file | what |
|---|---|
| `status.json` | live collector status (per source, disk, slice count) |
| `latest_<source>.json` | latest ON/QC smoke grid (drives the map) |
| `history.json` | recent collection events (drives the feed + sparklines) |
| `slices/<source>_<run>.json` | the archive — one slice per model run |

## Quick checks (works off-Pi too)
```bash
python collector.py --discover        # resolve latest run URLs, no download
python collector.py --status          # print status.json
python collector.py --sources bluesky # collect one source
```

## Notes
- **The archive starts now**, not in the past — it can't recover 2023 (that data
  was never publicly archived). For 2023 use the NAPS + PurpleAir observations.
- The Pi is great for *collecting* 24/7 but weak for *training* — copy the
  accumulated `slices/` to a laptop/cloud and train there. A natural target is
  the NAPS/PurpleAir observations (i.e. learn to correct the model toward what
  monitors actually measured — the CLEAR fusion goal).
