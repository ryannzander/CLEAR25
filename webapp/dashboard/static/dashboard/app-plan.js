/* ============================================================
   CLEAR · Ontario Smoke-Plume Tracker
   Animated IDW-interpolated PM2.5 surface over a Leaflet basemap.

   Pipeline: GET /api/plan/frames/ -> for the displayed frame, inverse-distance-
   weight the cleaned sensor points onto a coarse grid -> paint the grid to an
   offscreen canvas -> show it as a Leaflet imageOverlay stretched to the bbox
   (the browser bilinearly smooths it). Frames are interpolated on demand and
   their canvases cached, so scrubbing/playback is instant after first paint.
   ============================================================ */
(function () {
    "use strict";

    // ---- Config ---------------------------------------------------------
    var GRID_COLS = 200;          // interpolation resolution (stretched + smoothed by Leaflet)
    var GRID_ROWS = 150;
    var IDW_POWER = 2;            // inverse-distance exponent (for the value / colour)
    var CUTOFF_DEG = 1.2;         // sensors beyond this (deg, lat-corrected) don't contribute
    var MAX_ALPHA = 0.82;
    var PLAY_MS = 100;            // ms per frame during playback (hourly frames)
    // Static 2024–2025 HOURLY dataset (compiled by scripts/gen_plume_2024_2025.py).
    // Gzip-committed and decompressed client-side (DecompressionStream). No live
    // API, no cost. Bump the ?v= when the .gz is regenerated.
    var PLUME_DATA_URL = "/static/dashboard/plume_2024_2025.json.gz?v=1";

    // PM2.5 (µg/m³) -> color stops (EPA AQI category colors).
    var RAMP = [
        [0,   [46, 204, 113]],   // green
        [12,  [241, 196, 15]],   // yellow
        [35,  [230, 126, 34]],   // orange
        [55,  [231, 76, 60]],    // red
        [150, [142, 68, 173]],   // purple
        [250, [126, 0, 35]],     // maroon
    ];

    // ---- State ----------------------------------------------------------
    var map, overlay = null, sensorLayer = null;
    var bbox = null;
    var canvasCache = {};        // step -> dataURL (only used by the dead IDW path)
    var cur = 0, playing = false, playTimer = null, showSensors = false;
    // ---- Sparse hourly dataset (counting-sort index, memory-bounded) ----------
    // The asset is station-keyed (coords once + a flat [hour,pm] list per station).
    // On load we counting-sort all points into per-hour buckets backed by typed
    // arrays, so a frame's points are materialized on demand in O(active sensors)
    // without ever holding ~4.7M point objects at once.
    var nSteps = 0, t0ms = 0, stepSeconds = 3600;
    var stations = [];           // [{id,lat,lon}]
    var stepStart = null;        // Int32Array(nSteps+1): first point offset per hour
    var stationIdxByPair = null; // Int32Array(total): station index of each point
    var pmByPair = null;         // Float32Array(total): pm value of each point
    // Clip the interpolated surface to the Ontario + Québec boundary — kills the
    // bbox rectangle and focuses the two provinces. U.S. sensors still inform the
    // interpolation near the border; they're just not drawn.
    var clipRegion = true;
    // ECCC RDAQA model layer (a single current gridded analysis surface).
    var mode = "observed";       // "observed" (PurpleAir, animated) | "model" (ECCC)
    var eccc = null;             // { mesh: {rows,cols,bbox,values}, run } or null
    var ecccOverlay = null;

    // ---- DOM ------------------------------------------------------------
    var $ = function (id) { return document.getElementById(id); };
    var els = {};

    // ---- Sparse-index helpers -------------------------------------------
    // Counting-sort the per-station [hour,pm] lists into per-hour buckets.
    function buildIndex(series) {
        var counts = new Int32Array(nSteps), total = 0, s, k, arr;
        for (s = 0; s < series.length; s++) {
            arr = series[s];
            for (k = 0; k < arr.length; k += 2) { counts[arr[k]]++; total++; }
        }
        stepStart = new Int32Array(nSteps + 1);
        for (var i = 0; i < nSteps; i++) stepStart[i + 1] = stepStart[i] + counts[i];
        var cursor = stepStart.slice(0, nSteps);   // mutable copy of the offsets
        stationIdxByPair = new Int32Array(total);
        pmByPair = new Float32Array(total);
        for (s = 0; s < series.length; s++) {
            arr = series[s];
            for (k = 0; k < arr.length; k += 2) {
                var pos = cursor[arr[k]]++;
                stationIdxByPair[pos] = s;
                pmByPair[pos] = arr[k + 1];
            }
        }
        return total;
    }

    // Materialize one hour's sensor points on demand (cheap; not cached so long
    // playback never accumulates ~4.7M objects).
    function getPoints(step) {
        if (!stepStart) return [];
        var a = stepStart[step], b = stepStart[step + 1], pts = new Array(b - a);
        for (var j = a, n = 0; j < b; j++, n++) {
            var st = stations[stationIdxByPair[j]];
            pts[n] = { lat: st.lat, lon: st.lon, pm: pmByPair[j] };
        }
        return pts;
    }

    // Hour with the highest median PM2.5 (among hours with enough sensors) — so
    // the view auto-opens on the worst smoke episode in 2024–2025.
    function findPeakStep(minCount) {
        var best = 0, bestMed = -1, tmp = [];
        for (var step = 0; step < nSteps; step++) {
            var a = stepStart[step], b = stepStart[step + 1], n = b - a;
            if (n < minCount) continue;
            tmp.length = n;
            for (var j = a, t = 0; j < b; j++, t++) tmp[t] = pmByPair[j];
            tmp.sort(function (x, y) { return x - y; });
            var med = n % 2 ? tmp[(n - 1) / 2] : (tmp[n / 2 - 1] + tmp[n / 2]) / 2;
            if (med > bestMed) { bestMed = med; best = step; }
        }
        return bestMed >= 0 ? best : 0;
    }

    // The asset is a raw .gz; decompress client-side. If the server already
    // decompressed it (Content-Encoding: gzip), the bytes are plain JSON — detect
    // via the gzip magic number so we work identically on dev and on Vercel.
    function decodeMaybeGzip(buf) {
        var bytes = new Uint8Array(buf);
        if (bytes.length >= 2 && bytes[0] === 0x1f && bytes[1] === 0x8b) {
            if (typeof DecompressionStream === "undefined") {
                return Promise.reject("This browser lacks DecompressionStream (needed to read the gzip dataset).");
            }
            var stream = new Response(bytes).body.pipeThrough(new DecompressionStream("gzip"));
            return new Response(stream).text().then(function (t) { return JSON.parse(t); });
        }
        return Promise.resolve(JSON.parse(new TextDecoder("utf-8").decode(bytes)));
    }

    function stepTimeMs(step) { return t0ms + step * stepSeconds * 1000; }

    function rampColor(pm) {
        if (pm <= RAMP[0][0]) return RAMP[0][1];
        for (var i = 1; i < RAMP.length; i++) {
            if (pm <= RAMP[i][0]) {
                var a = RAMP[i - 1], b = RAMP[i];
                var t = (pm - a[0]) / (b[0] - a[0]);
                return [
                    Math.round(a[1][0] + t * (b[1][0] - a[1][0])),
                    Math.round(a[1][1] + t * (b[1][1] - a[1][1])),
                    Math.round(a[1][2] + t * (b[1][2] - a[1][2])),
                ];
            }
        }
        return RAMP[RAMP.length - 1][1];
    }

    // ---- Ontario clip ---------------------------------------------------
    // The surface is shown only inside the province (no hard bbox rectangle, no
    // U.S. coverage). PROVINCE_POLYGONS (global from ontario-boundary.js) is an
    // array of [lon,lat] rings; a point is "in Ontario" if it falls inside any
    // ring. Per-ring bbox skips the ray-cast for far-away cells. If the asset
    // failed to load we degrade to no clip rather than a blank map.
    var _ringBoxes = null;
    function inRegion(lon, lat) {
        if (typeof PROVINCE_POLYGONS === "undefined") return true;
        if (!_ringBoxes) {
            _ringBoxes = PROVINCE_POLYGONS.map(function (ring) {
                var b = { minx: 180, maxx: -180, miny: 90, maxy: -90 };
                for (var i = 0; i < ring.length; i++) {
                    var p = ring[i];
                    if (p[0] < b.minx) b.minx = p[0];
                    if (p[0] > b.maxx) b.maxx = p[0];
                    if (p[1] < b.miny) b.miny = p[1];
                    if (p[1] > b.maxy) b.maxy = p[1];
                }
                return b;
            });
        }
        for (var k = 0; k < PROVINCE_POLYGONS.length; k++) {
            var bb = _ringBoxes[k];
            if (lon < bb.minx || lon > bb.maxx || lat < bb.miny || lat > bb.maxy) continue;
            var ring = PROVINCE_POLYGONS[k], inside = false, n = ring.length;
            for (var i = 0, j = n - 1; i < n; j = i++) {
                var xi = ring[i][0], yi = ring[i][1], xj = ring[j][0], yj = ring[j][1];
                if (((yi > lat) !== (yj > lat)) &&
                    (lon < (xj - xi) * (lat - yi) / (yj - yi) + xi)) inside = !inside;
            }
            if (inside) return true;
        }
        return false;
    }

    // Grid -> Ontario inside/outside mask, cached (the grid is identical per frame).
    var _maskKey = null, _mask = null;
    function regionMask(west, east, north, south, cols, rows) {
        var key = clipRegion + "," + west + "," + east + "," + north + "," + south + "," + cols + "," + rows;
        if (_mask && _maskKey === key) return _mask;
        var m = new Uint8Array(cols * rows);
        if (!clipRegion) { m.fill(1); _mask = m; _maskKey = key; return m; }
        for (var y = 0; y < rows; y++) {
            var lat = north - (y + 0.5) / rows * (north - south);
            for (var x = 0; x < cols; x++) {
                var lon = west + (x + 0.5) / cols * (east - west);
                m[y * cols + x] = inRegion(lon, lat) ? 1 : 0;
            }
        }
        _mask = m; _maskKey = key;
        return m;
    }

    // Bucket sensors into a coarse grid so each cell only scans nearby points.
    function buildBuckets(points, midLatCos) {
        var bs = CUTOFF_DEG; // bucket size = cutoff radius
        var buckets = {};
        for (var i = 0; i < points.length; i++) {
            var p = points[i];
            var bx = Math.floor((p.lon * midLatCos) / bs);
            var by = Math.floor(p.lat / bs);
            var key = bx + ":" + by;
            (buckets[key] || (buckets[key] = [])).push(p);
        }
        return { buckets: buckets, bs: bs };
    }

    // Interpolate one frame to a dataURL (cached), clipped to Ontario.
    // Colour = IDW of PM2.5; opacity = a smooth Gaussian "coverage" that merges
    // neighbouring sensors into one continuous field and fades out where data
    // thins (so no per-sensor blobs, no hard edge).
    function renderFrame(index) {
        if (canvasCache[index]) return canvasCache[index];
        var pts = getPoints(index);
        var west = bbox.nwlng, east = bbox.selng, north = bbox.nwlat, south = bbox.selat;
        var midLatCos = Math.cos((north + south) / 2 * Math.PI / 180);

        var bk = buildBuckets(pts, midLatCos), buckets = bk.buckets, bs = bk.bs;
        var mask = regionMask(west, east, north, south, GRID_COLS, GRID_ROWS);
        var cutoff2 = CUTOFF_DEG * CUTOFF_DEG;
        var flatAlpha = Math.round(MAX_ALPHA * 255);

        var cv = document.createElement("canvas");
        cv.width = GRID_COLS; cv.height = GRID_ROWS;
        var ctx = cv.getContext("2d");
        var img = ctx.createImageData(GRID_COLS, GRID_ROWS);
        var data = img.data;

        for (var y = 0; y < GRID_ROWS; y++) {
            var lat = north - (y + 0.5) / GRID_ROWS * (north - south);
            var by = Math.floor(lat / bs);
            for (var x = 0; x < GRID_COLS; x++) {
                var idx = y * GRID_COLS + x, o = idx * 4;
                if (!mask[idx]) { data[o + 3] = 0; continue; }   // outside Ontario
                var lon = west + (x + 0.5) / GRID_COLS * (east - west);
                var bx = Math.floor((lon * midLatCos) / bs);

                var wsum = 0, vsum = 0, exact = null;
                for (var gx = bx - 1; gx <= bx + 1; gx++) {
                    for (var gy = by - 1; gy <= by + 1; gy++) {
                        var arr = buckets[gx + ":" + gy];
                        if (!arr) continue;
                        for (var k = 0; k < arr.length; k++) {
                            var p = arr[k];
                            var ddx = (lon - p.lon) * midLatCos, ddy = lat - p.lat;
                            var d2 = ddx * ddx + ddy * ddy;
                            if (d2 > cutoff2) continue;
                            if (d2 < 1e-9) { exact = p.pm; continue; }
                            var w = 1 / Math.pow(d2, IDW_POWER / 2);
                            wsum += w; vsum += w * p.pm;
                        }
                        
                    }
                }

                if (wsum === 0 && exact === null) { data[o + 3] = 0; continue; }
                var pm = exact !== null ? exact : vsum / wsum;
                var c = rampColor(pm);
                // Flat opacity where there's data — no Gaussian coverage fade.
                data[o] = c[0]; data[o + 1] = c[1]; data[o + 2] = c[2];
                data[o + 3] = flatAlpha;
            }
        }
        ctx.putImageData(img, 0, 0);
        var url = cv.toDataURL();
        canvasCache[index] = url;
        return url;
    }

    function frameBounds() {
        // Leaflet imageOverlay bounds: [[south, west], [north, east]]
        return [[bbox.selat, bbox.nwlng], [bbox.nwlat, bbox.selng]];
    }

    // ---- ECCC RDAQA model surface (already gridded -> direct raster, no IDW) --
    function meshBounds(mesh) {
        var b = mesh.bbox;
        return [[b.selat, b.nwlng], [b.nwlat, b.selng]];
    }

    function renderMeshURL(mesh) {
        // values are row-major, north->south rows, west->east cols -> paint
        // directly: pixel (c, r) = values[r*cols + c]. Clipped to Ontario so the
        // model surface follows the province too, not the bbox rectangle.
        var rows = mesh.rows, cols = mesh.cols, vals = mesh.values || [];
        var b = mesh.bbox, west = b.nwlng, east = b.selng, north = b.nwlat, south = b.selat;
        var cv = document.createElement("canvas");
        cv.width = cols; cv.height = rows;
        var ctx = cv.getContext("2d");
        var img = ctx.createImageData(cols, rows);
        var d = img.data;
        for (var r = 0; r < rows; r++) {
            var lat = north - (r + 0.5) / rows * (north - south);
            for (var col = 0; col < cols; col++) {
                var i = r * cols + col, o = i * 4, v = vals[i];
                if (v === null || v === undefined || (typeof v === "number" && isNaN(v))) { d[o + 3] = 0; continue; }
                var lon = west + (col + 0.5) / cols * (east - west);
                if (!inRegion(lon, lat)) { d[o + 3] = 0; continue; }
                var c = rampColor(v);
                d[o] = c[0]; d[o + 1] = c[1]; d[o + 2] = c[2]; d[o + 3] = 209; // ~0.82
            }
        }
        ctx.putImageData(img, 0, 0);
        return cv.toDataURL();
    }

    function clearPurpleAir() {
        if (overlay) { map.removeLayer(overlay); overlay = null; }
        if (sensorLayer) { map.removeLayer(sensorLayer); sensorLayer = null; }
    }
    function clearEccc() {
        if (ecccOverlay) { map.removeLayer(ecccOverlay); ecccOverlay = null; }
    }

    function showModel() {
        clearPurpleAir();
        var url = renderMeshURL(eccc.mesh);
        if (!ecccOverlay) {
            ecccOverlay = L.imageOverlay(url, meshBounds(eccc.mesh), { opacity: 1, interactive: false }).addTo(map);
        } else {
            ecccOverlay.setUrl(url); ecccOverlay.addTo(map);
        }
        var live = (eccc.mesh.values || []).filter(function (v) { return v !== null && v !== undefined; }).length;
        els.clock.textContent = "ECCC RDAQA · 10 km analysis";
        els.rel.textContent = eccc.run ? ("run " + eccc.run) : "";
        els.sensorCount.textContent = live.toLocaleString() + " cells";
        els.transport.classList.add("disabled");
    }

    function setMode(m) {
        if (m === mode) return;
        if (m === "model" && (!eccc || !eccc.mesh)) return;
        mode = m;
        els.modeObserved.classList.toggle("active", m === "observed");
        els.modeModel.classList.toggle("active", m === "model");
        if (m === "model") {
            pause();
            showModel();
        } else {
            clearEccc();
            els.transport.classList.remove("disabled");
            if (nSteps) showFrame(cur);
        }
    }

    function loadEccc() {
        fetch("/api/eccc/?kind=analysis")
            .then(function (r) { return r.ok ? r.json() : null; })
            .then(function (d) {
                var mesh = d && d.data && d.data.mesh;
                if (!mesh || !mesh.values || !mesh.rows) {
                    els.modeModel.disabled = true;
                    els.modeModel.title = "No ECCC model surface ingested yet";
                    return;
                }
                eccc = { mesh: mesh, run: (d.data.run || "") };
                els.modeModel.disabled = false;
                els.modeModel.title = "ECCC RDAQA 10 km analysis (model nowcast)";
                // Deep-link: /plan/?mode=model opens straight on the model surface.
                try {
                    if (new URLSearchParams(window.location.search).get("mode") === "model") setMode("model");
                } catch (e) { /* URLSearchParams unsupported -> ignore */ }
            })
            .catch(function () { els.modeModel.disabled = true; });
    }

    function relTime(iso, latestIso) {
        var t = new Date(iso).getTime(), latest = new Date(latestIso).getTime();
        var mins = Math.round((latest - t) / 60000);
        return mins <= 0 ? "live" : "T−" + mins + " min";
    }

    function fmtClock(ms) {
        var d = new Date(ms);
        if (isNaN(d.getTime())) return String(ms);
        // Hourly frames -> date + hour (UTC, so the calendar day doesn't shift).
        var day = d.toLocaleDateString([], { year: "numeric", month: "short", day: "numeric", timeZone: "UTC" });
        var hh = ("0" + d.getUTCHours()).slice(-2);
        return day + " · " + hh + ":00 UTC";
    }

    function showFrame(index) {
        if (!nSteps) return;
        cur = Math.max(0, Math.min(index, nSteps - 1));
        var pts = getPoints(cur);
        drawSensors(pts);   // plotted sensor readings (interpolated surface removed)
        els.slider.value = cur;
        els.clock.textContent = fmtClock(stepTimeMs(cur));
        els.rel.textContent = (cur + 1).toLocaleString() + " / " + nSteps.toLocaleString();
        els.frameIdx.textContent = (cur + 1).toLocaleString();
        els.sensorCount.textContent = pts.length.toLocaleString();
    }

    function drawSensors(pts) {
        if (sensorLayer) { map.removeLayer(sensorLayer); sensorLayer = null; }
        var markers = [];
        pts = pts || [];
        for (var i = 0; i < pts.length; i++) {
            var p = pts[i];
            if (clipRegion && !inRegion(p.lon, p.lat)) continue;  // Ontario + Québec only
            var c = rampColor(p.pm);
            markers.push(L.circleMarker([p.lat, p.lon], {
                radius: 4, stroke: false, fillColor: "rgb(" + c[0] + "," + c[1] + "," + c[2] + ")",
                fillOpacity: 0.85,
            }));
        }
        sensorLayer = L.layerGroup(markers).addTo(map);
    }

    function play() {
        if (!nSteps) return;
        playing = true; els.play.textContent = "⏸";
        clearInterval(playTimer);
        playTimer = setInterval(function () {
            var next = cur + 1;
            if (next >= nSteps) next = 0;
            showFrame(next);
        }, PLAY_MS);
    }
    function pause() { playing = false; els.play.textContent = "▶"; clearInterval(playTimer); }

    function setOverlayMsg(title, body, spin) {
        els.ovTitle.textContent = title;
        els.ovBody.textContent = body;
        els.ovSpin.classList.toggle("hidden", !spin);
        els.overlay.classList.remove("hidden");
    }

    function initMap() {
        map = L.map("plan-map", { preferCanvas: true, zoomControl: false, attributionControl: true })
            .setView([49.5, -85], 5);
        L.control.zoom({ position: "bottomright" }).addTo(map);
        L.tileLayer("https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png", {
            attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OSM</a> &copy; <a href="https://carto.com/">CARTO</a> · PM2.5 © PurpleAir',
            maxZoom: 12,
        }).addTo(map);
    }

    function pctl(sorted, p) {
        var i = Math.floor(p * (sorted.length - 1));
        return sorted[Math.max(0, Math.min(sorted.length - 1, i))];
    }

    function fitToBbox() {
        if (!bbox) return;
        // Fit to where sensors actually are (the dense southern-Ontario / Great
        // Lakes / St-Lawrence cloud) rather than the full station extent — a
        // handful of far-north Québec sensors (to ~62°N) would otherwise zoom the
        // whole continent out. Trim ~2.5% of stations off each edge so the bulk
        // frames nicely; fall back to min/max then the bbox.
        var pts = stations;
        if (pts.length >= 20) {
            var lats = [], lons = [], i;
            for (i = 0; i < pts.length; i++) { lats.push(pts[i].lat); lons.push(pts[i].lon); }
            lats.sort(function (a, b) { return a - b; });
            lons.sort(function (a, b) { return a - b; });
            map.fitBounds(
                [[pctl(lats, 0.025), pctl(lons, 0.025)], [pctl(lats, 0.975), pctl(lons, 0.975)]],
                { padding: [24, 24], maxZoom: 7 });
        } else if (pts.length >= 3) {
            var minLa = 90, maxLa = -90, minLo = 180, maxLo = -180;
            for (i = 0; i < pts.length; i++) {
                var p = pts[i];
                if (p.lat < minLa) minLa = p.lat;
                if (p.lat > maxLa) maxLa = p.lat;
                if (p.lon < minLo) minLo = p.lon;
                if (p.lon > maxLo) maxLo = p.lon;
            }
            map.fitBounds([[minLa, minLo], [maxLa, maxLo]], { padding: [24, 24], maxZoom: 7 });
        } else {
            map.fitBounds(frameBounds(), { padding: [10, 10], maxZoom: 7 });
        }
    }

    // Load the static 2024–2025 HOURLY PurpleAir dataset (no live API, zero cost):
    // {t0, step_seconds, n_steps, bbox, stations:[{id,lat,lon}], series:[[h,pm,...]]}.
    // The .gz is fetched as bytes, decompressed, then counting-sorted into per-hour
    // buckets so frames are built on demand (see buildIndex / getPoints).
    function load() {
        setOverlayMsg("Loading plume data…", "Decompressing the 2024–2025 hourly dataset (~16 MB).", true);
        fetch(PLUME_DATA_URL)
            .then(function (r) { return r.ok ? r.arrayBuffer() : Promise.reject(r.status); })
            .then(decodeMaybeGzip)
            .then(function (d) {
                bbox = d.bbox;
                clipRegion = true;   // clip to Ontario + Québec
                stations = d.stations || [];
                nSteps = d.n_steps || 0;
                stepSeconds = d.step_seconds || 3600;
                t0ms = Date.parse(d.t0);
                if (!stations.length || !nSteps || isNaN(t0ms)) {
                    els.statusPill.textContent = "no data";
                    setOverlayMsg("No 2024–2025 data", "The dataset is empty or malformed.", false);
                    return;
                }
                buildIndex(d.series || []);
                els.frameTotal.textContent = nSteps.toLocaleString();
                els.slider.max = Math.max(0, nSteps - 1);
                els.overlay.classList.add("hidden");
                els.statusPill.textContent = nSteps.toLocaleString() + " hrs · 2024–2025";
                els.age.textContent = "2024–2025";
                fitToBbox();
                // Start on the peak-smoke hour so the worst episode is visible at once.
                showFrame(findPeakStep(30));
            })
            .catch(function (e) {
                els.statusPill.textContent = "error";
                setOverlayMsg("Could not load 2024–2025 data", String(e), false);
            });
    }

    function wire() {
        els = {
            slider: $("slider"), play: $("play"), clock: $("clock"), rel: $("rel"),
            frameIdx: $("frame-idx"), frameTotal: $("frame-total"), sensorCount: $("sensor-count"),
            age: $("age"), showSensors: $("show-sensors"), statusPill: $("status-pill"),
            overlay: $("overlay"), ovTitle: $("ov-title"), ovBody: $("ov-body"), ovSpin: $("ov-spin"),
            transport: $("transport"), modeObserved: $("mode-observed"), modeModel: $("mode-model"),
        };
        els.modeModel.disabled = true;  // enabled by loadEccc() once a model surface exists
        els.slider.addEventListener("input", function () { pause(); showFrame(parseInt(this.value, 10)); });
        els.play.addEventListener("click", function () { playing ? pause() : play(); });
        els.modeObserved.addEventListener("click", function () { setMode("observed"); });
        els.modeModel.addEventListener("click", function () { setMode("model"); });
        els.showSensors.addEventListener("change", function () {
            showSensors = this.checked;
            if (nSteps) { showSensors ? drawSensors(getPoints(cur)) : drawSensors([]); }
        });
    }

    document.addEventListener("DOMContentLoaded", function () {
        wire();
        initMap();
        load();
        // (live ECCC model layer disabled for the 2023 historical replay)
    });
})();
