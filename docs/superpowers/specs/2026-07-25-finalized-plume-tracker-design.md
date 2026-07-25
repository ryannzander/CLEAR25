# Finalized EPA-corrected smoke-plume tracker (2021–2025)

**Date:** 2026-07-25
**Branch:** `feature/plume-finalized-2021-2025`
**Status:** implemented

Repoints `/plan/` from the uncorrected 2024–2025 PurpleAir replay to the finalized,
EPA/Barkjohn humidity-corrected 2021–2025 record in `ai_model_data_important`.

---

## 1. Why

The tracker's current asset (`plume_2024_2025.json.gz`) was compiled from downloads
that carried only `pm2.5_atm` — no CF=1 channel, no humidity — so the EPA correction
could not be applied and the values run roughly 2× hot. The finalized record fixes
three things at once:

1. **Values are `pm2.5_epa_corrected`** — the piecewise US EPA / Barkjohn et al. (2021)
   correction applied with per-hour relative humidity, the same correction the EPA
   AirNow Fire & Smoke Map uses.
2. **Sensor QC is already done** (`qc_faulty`), so a faulty sensor's whole year is
   excluded rather than leaking spikes into the interpolated surface.
3. **Latitude/longitude are columns in the file**, so the generator makes no network
   call at all. The 2024–2025 generator had to hit the PurpleAir metadata endpoint to
   recover coordinates.

It also extends coverage from two years to five, and from 595 sensors to 1,742.

## 2. Measured inputs

Established by a full streaming pass over all five merged CSVs (~2.5 GB), not estimated:

| Year | Sensors | Clean rows | ON/QC sensors |
|---|---:|---:|---:|
| 2021 | 243 | 1,472,335 | 21 |
| 2022 | 359 | 2,239,755 | 40 |
| 2023 | 772 | 4,257,676 | 110 |
| 2024 | 1,123 | 6,736,781 | 163 |
| 2025 | 1,556 | 10,174,002 | 199 |
| **Total** | **1,742 unique** | **24,880,549** | **227 unique** |

Footprint: lat 41.50–62.20, lon −95.97 to −57.92.

## 3. Decisions

| Decision | Choice | Rationale |
|---|---|---|
| Placement | Replace `/plan/` | The corrected record supersedes the old asset in every dimension. One tracker, one codebase. |
| Coverage | All 12 months, one file per year | Keeps 2021's winter inversions and 2022's cold-season peaks, which a fire-season-only cut would drop. 2022 had no summer smoke event at all. |
| Map extent | Full network, no clip | Only 227 of 1,742 sensors are in ON+QC. Clipping to the provinces discarded 87% of the network — precisely the upwind US Midwest/Great Lakes coverage that shows smoke *arriving*. Provinces are outlined for orientation only. |
| Rendering | IDW surface + sensor dots | The surface reads as a plume; the dots keep it honest about where observations actually exist. |
| Encoding | Delta-encoded hours | Measured, see below. |

### Encoding, measured on real 2022 data

| Encoding | gz bytes/pt | 5-yr projection |
|---|---:|---:|
| A — absolute `[h,pm,h,pm]` (what the old assets used) | 3.52 | 83.5 MB |
| **B — delta `[h₀,pm,Δh,pm]` (chosen)** | **1.22** | **28.9 MB** |
| C — columnar `{h:[],p:[]}` | 1.13 | 26.8 MB |
| D — binary varint + uint16 | 1.01 | 24.0 MB |

Delta encoding is a 2.9× reduction for a one-line change on each side — the frontend's
`buildIndex` already walked the array in pairs and simply accumulates instead of
assigning. B was chosen over C/D because the extra 2–5 MB does not justify a new
format and a riskier decoder in the most sensitive code path. **This is what makes all
twelve months of all five years fit**; at encoding A only the fire season would have.

## 4. Components

### `scripts/gen_plume_finalized.py` (new)

Pure stdlib. `--write` gated, dry-run by default, `--selftest` for unit tests.

- Reads `<data-dir>/PurpleAir<YEAR>_calibrated_merged_with_locations.csv`.
- Filter: `qc_faulty == "no"` and non-empty `pm2.5_epa_corrected` (handoff §3's
  standard analysis-ready filter). Values otherwise pass through untouched, including
  the few above 1000 µg/m³ — those are legitimate QC-passed readings produced by the
  correction's quadratic term from sub-1000 `cf_1` inputs, and the ramp simply
  saturates.
- Rows are **not** time-ordered within a sensor (the 2021 file's first row is
  2021-12-16), so every sensor's readings are sorted before delta encoding. Duplicate
  hours collapse to the last reading rather than encoding a zero delta.
- Memory: one `array('i')` of packed `step * 65536 + pm×10` ints per sensor (4 B per
  reading), so the largest year holds near 40 MB instead of the ~600 MB a list of
  Python tuples would cost. The series JSON is assembled as pre-formatted text so the
  ~10M-point array never exists as Python numbers.
- Emits `plume_2021.json.gz` … `plume_2025.json.gz` plus `plume_index.json`.

The manifest carries, per year: file name, `t0`, `n_steps` (8784 for leap-year 2024),
sensor count, point count, bbox, gzip size, and the **peak hour** — the hour with the
highest across-sensor median, minimum 30 sensors reporting. It also carries the global
`extent` and a 2.5%-trimmed `view`.

### `webapp/dashboard/static/dashboard/app-plan.js` (rewritten)

- Year selector built from the manifest; switching fetches that year's `.gz`.
- Delta decode in `buildIndex`, with each station's parsed array released as it is
  indexed.
- In-memory index narrowed to `Uint16Array` station index + `Uint16Array` pm×10
  (4 B/point), so 2025's 10.2M readings hold at ~41 MB rather than ~81 MB with
  `Int32`/`Float32`. Guarded at 65,535 stations on both sides.
- **Bounded frame cache.** The previous `canvasCache` grew without limit; at 8,760
  hours per year that is a leak the old two-year asset was already exposed to.
- IDW surface restored in `showFrame` with dots drawn over it and a toggle.
- **Coverage-based opacity** (added during implementation, after the first render
  showed the problem). Colour is the IDW value; opacity is a separate term where each
  sensor contributes a Gaussian kernel, the kernels are summed, and the sum passes
  through `1 − e^(−gain·Σ)`. A flat opacity made every isolated sensor paint a uniform
  disc the full width of the 1.2° cutoff with a hard rim — a bubble that implied the
  same confidence 130 km out as directly overhead. Keying opacity to the *nearest*
  sensor instead over-corrected into a field of separate blobs. The summed-kernel form
  is what makes a cluster saturate into one continuous plume while a lone sensor still
  fades: σ = 0.55°, gain = 1.6.
- Grid dimensions derived from the bbox aspect (`sizeGrid`) — the footprint is now
  ~38° lon × 21° lat, so the old fixed 200×150 grid would stretch every cell.
- `PROVINCE_POLYGONS` repurposed from a clip mask to an `L.polyline` outline.
- Fits **once** to the manifest's global trimmed view, so switching year never makes
  the map jump.
- Opens on the year with the highest peak median across the whole record.

### `webapp/dashboard/templates/dashboard/plan.html`

Retitled to 2021–2025, subtitle notes EPA correction and the wider footprint, year
selector unhidden (reusing the existing `.layerseg`/`.seg` CSS the hidden ECCC toggle
defined), "show sensors" unhidden and default-on, legend labelled EPA-corrected.

## 5. Isolation

Static assets plus one page. **No changes to** `evaluate.py`, the 3-rule engine, the
alert bands, `CachedResult`, `views/plan.py`, or `/api/plan/refresh|frames|fusion/`
(`api_plan_fusion` powers the dashboard city chips and is independent of the tracker's
data source). No new dependencies, no new CSP hosts, no runtime network calls.

`plume_2024_2025.json.gz` is **retained**: it is the default input for
`scripts/poc_smoke_model.py` and `scripts/train_smoke_lstm.py`. Deleting it would break
the ML pipeline. `plume_2023.json` is likewise retained as the coordinate fallback for
`gen_plume_2024_2025.py`. The new `plume_2023.json.gz` does not collide with it.

## 6. Caveats, stated honestly

- Sensor coordinates are each sensor's **current** position, which for older years may
  differ from where it stood at the time of measurement (handoff §8). Most relevant to
  2021.
- 227 of 1,742 sensors are in ON+QC. This is a regional transport view, not a Canadian
  monitoring network.
- The 2023 peak *hour* by across-sensor median is Jun 6, while the QC summary's peak
  *day* by network mean is Jun 28. Both are real: the handoff documents two waves
  (Jun 6–7 and Jun 25–30), and an hourly median and a daily mean rank them differently.
- Decoding 2025 allocates a large transient (a ~57 MB JSON string plus the parsed
  arrays) before the typed index is built and the originals are released. Measured
  settled heap holding the largest year is 44 MB, but the spike during decode is
  several times that — comfortable on desktop, heavy on mobile.

## 7. Verification

- `--selftest`: delta round-trip (including unsorted input, duplicate hours, single
  readings, and the 1576.5 µg/m³ maximum), year-boundary and 2024-leap-day step
  indexing, and `peak_step`'s median and sensor-floor behaviour.
- **Reconciliation gate**, run automatically after every year is built: the written
  `.gz` is decompressed and decoded, its point count asserted equal to what was read
  from the CSV, and every station's hours asserted strictly increasing and in range.
- Peak hours cross-checked against the QC summaries' peak days.
- Headless-browser check of the rendered page: surface, dots, year switching, playback,
  and console cleanliness.

### Results

All five years reconciled exactly against the independent scan — 1,472,335 / 2,239,755 /
4,257,676 / 6,736,781 / 10,174,002, total **24,880,549**, with every station's hours
strictly increasing. Total 26.7 MB gz (below the 28.9 MB projection). Extent matches the
scan to five decimals. `n_steps` is 8784 for 2024 and 8760 elsewhere.

Peak hours: 2021-07-21T01Z (38.8), 2022-02-01T03Z (20.2), 2023-06-06T17Z (56.3),
2024-07-28T03Z (16.0), 2025-08-04T17Z (53.5). 2021 and 2022 match the QC summaries' peak
days directly. 2023, 2024 and 2025 differ from the daily-mean peak because an hourly
*median* ranks a broad regional blanket above a day when a few sites spiked — which is
the right criterion for a plume tracker, and is the same mean-vs-median distinction the
handoff draws in §5.

Browser-measured, 1440×900 headless Chrome:

| Check | Result |
|---|---|
| Frame render, 2023 (772 sensors) | 22 ms median, 25 ms max |
| Frame render, 2025 (1,556 sensors) | 32 ms median, 39 ms max |
| Playback interval | 100 ms — 3–4.5× headroom, dots on |
| Console errors/warnings | 0 |
| Settled heap holding 2025 | 44 MB |
| Heap after cycling all 5 years ×2 | 515 MB, self-collecting to 44 MB — garbage, not retention |
| Playback advance | 11 frames in 1 s from the manifest peak step |
| Routes | `/`, `/plan/`, `/dashboard/`, `/api/plan/fusion/` all 200 |
