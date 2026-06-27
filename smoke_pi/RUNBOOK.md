# 🔥 Smoke Collector — Pi Recovery Runbook

Personal cheat-sheet to bring the smoke collector + dashboard back after a
reboot, power loss, or SD re-image. Keep a copy of this somewhere off the Pi.

## My setup
| | |
|---|---|
| **SSH in** | `ssh zanderclaw@oclaw`  (or `ssh zanderclaw@<ip>`) |
| **Folder** | `/home/zanderclaw/smoke_pi` |
| **Python venv** | `/home/zanderclaw/smoke_pi/.venv` |
| **Dashboard** | `http://oclaw.local:8077/`  (or `http://<pi-ip>:8077/`) |
| **Find the real IP** | `ip route get 1.1.1.1 \| awk '{print $7; exit}'` |

---

## A · Pi just rebooted (files still on it)
If you set up systemd (section **C**), everything auto-starts — just open the
dashboard URL, done. Otherwise start it by hand:
```bash
cd ~/smoke_pi
.venv/bin/python collector.py --sources bluesky,firework        # pull the latest runs
nohup .venv/bin/python serve.py >/tmp/smoke-serve.log 2>&1 &     # dashboard, survives logout
```
Then open `http://oclaw.local:8077/` from your PC.

---

## B · Fresh Pi / SD re-imaged (from zero)
**1. Get the code on the Pi** — clone (best, easy updates):
```bash
cd ~ && git clone https://github.com/ryannzander/CLEAR25.git
cd CLEAR25 && git checkout feature/smoke-collector-pi    # until PR #21 is merged into main
ln -s ~/CLEAR25/smoke_pi ~/smoke_pi                       # optional: keep the ~/smoke_pi path
```
*…or copy from the PC:* `scp -r C:\Users\rybot\Desktop\CLEAR25\smoke_pi zanderclaw@oclaw:~/`

**2. System packages** (Python + the GRIB/NetCDF C libs):
```bash
sudo apt-get update
sudo apt-get install -y python3 python3-venv python3-pip \
                        libeccodes0 libeccodes-dev libhdf5-dev libnetcdf-dev
```
**3. Python env + deps:**
```bash
cd ~/smoke_pi
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```
**4. Smoke-test it:**
```bash
.venv/bin/python collector.py --discover                 # prints 2 source URLs = networking OK
.venv/bin/python collector.py --sources bluesky,firework # first real slices
.venv/bin/python serve.py                                # dashboard at http://<pi-ip>:8077/
```

---

## C · Auto-start + auto-collect (do ONCE — survives every reboot)
Edit the two paths in each unit under `~/smoke_pi/systemd/` to point at your
folder + venv:
```
WorkingDirectory=/home/zanderclaw/smoke_pi
ExecStart=/home/zanderclaw/smoke_pi/.venv/bin/python collector.py --sources bluesky,firework
# (and the serve.py one for the dashboard service)
```
Then install + enable:
```bash
sudo cp ~/smoke_pi/systemd/* /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now smoke-dashboard.service      # dashboard always on
sudo systemctl enable --now smoke-collector.timer        # collect every 3 h
```
After this, **a reboot brings it all back automatically** — nothing to do.

---

## D · Is it working?
```bash
systemctl status smoke-dashboard.service     # dashboard up?
systemctl status smoke-collector.timer       # timer scheduled? next run?
journalctl -u smoke-collector.service -n 30  # last collection log
.venv/bin/python collector.py --status       # JSON status dump
```

---

## Gotchas (things that already bit me)
- **`hostname -I` shows `127.0.1.1`** → that's loopback, *not* the real IP. Use
  `ip route get 1.1.1.1 | awk '{print $7; exit}'`.
- **Dashboard from the PC** → use the Pi's real IP or `oclaw.local`, **never** `localhost`.
- **"already collected; skip" right after copying files** = leftover demo data. Clear it:
  ```bash
  rm -f ~/smoke_pi/web/data/status.json ~/smoke_pi/web/data/latest_*.json ~/smoke_pi/web/data/history.json
  ```
- **Map empty / `max 0.0`** = no smoke over Ontario/Québec right now. Correct, not broken.
- **`pip install` fails on `pygrib`** = missing eccodes → re-run the `apt-get` in B-2.
- **`pip` refuses with "externally-managed-environment"** = you skipped the venv. Use `.venv/bin/pip`.

---

## My data = my training set
Every model run is saved as a slice in `~/smoke_pi/web/data/slices/`. Back it up
now and then (this is the whole point — it can't be re-downloaded later):
```bash
# from the PC:
rsync -a zanderclaw@oclaw:~/smoke_pi/web/data/slices/ ./smoke-slices-backup/
```
For long-term collecting, point `web/data/` at a **USB SSD** — SD cards wear out
under constant writes.
