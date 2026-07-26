/* ============================================================
   CLEAR · 2021–2025 Smoke-Plume Replay
   Animated IDW-interpolated PM2.5 surface over a Leaflet basemap.

   Data: the finalized, EPA/Barkjohn humidity-corrected PurpleAir record,
   compiled by scripts/gen_plume_finalized.py into one gzip asset per year plus
   a small manifest. Everything is a static file — no live API, no cost, no key.

   Pipeline: fetch plume_finalized_index.json -> fetch the year's asset named by it
   -> decompress client-side (DecompressionStream) -> counting-sort every reading
   into per-hour buckets backed by typed arrays -> for the displayed hour,
   inverse-distance-weight the sensor points onto a grid, paint that grid to an
   offscreen canvas, and show it as a Leaflet imageOverlay stretched to the bbox
   (the browser bilinearly smooths it). Sensor dots are drawn on top.
   ============================================================ */
(function () {
    "use strict";

    // ---- Config ---------------------------------------------------------
    var GRID_CELLS = 34000;       // ~ total interpolation cells; split by bbox aspect
    var IDW_POWER = 2;            // inverse-distance exponent
    var CUTOFF_DEG = 1.2;         // sensors beyond this (deg, lat-corrected) don't contribute
    var MAX_ALPHA = 0.82;
    var PLAY_MS = 100;            // ms per frame during playback (hourly frames)
    var FRAME_CACHE_MAX = 240;    // bounded: 8,760 hours/year would otherwise leak
    var STATIC_BASE = "/static/dashboard/";
    var INDEX_URL = STATIC_BASE + "plume_finalized_index.json?v=1";
    // Per-year filenames come from the manifest's `file` field rather than being
    // templated here, so the asset naming is entirely the generator's concern.
    // (It matters: the names must not shadow a same-stem sibling — see the
    // WhiteNoise note in scripts/gen_plume_finalized.py.)

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
    var map, overlay = null, sensorLayer = null, provinceLayer = null;
    var manifest = null, year = null, yearMeta = null;
    var bbox = null, gridCols = 200, gridRows = 170;
    var cur = 0, playing = false, playTimer = null, showSensors = true;
    var loading = false, fitted = false;

    // ---- Sparse hourly index (counting-sorted, memory-bounded) ----------
    // The asset is station-keyed (coords once + a delta-encoded [Δhour, pm] list
    // per station). On load every reading is counting-sorted into per-hour
    // buckets backed by typed arrays, so a frame's points are materialized on
    // demand in O(active sensors) — never ~10M point objects at once.
    // Station index and value are both Uint16 (4 B/point): the largest year has
    // 1,556 stations, and pm is stored in tenths (0.1 µg/m³ up to 6553.5).
    var nSteps = 0, t0ms = 0, stepSeconds = 3600;
    var stations = [];           // [{id,lat,lon}]
    var stepStart = null;        // Int32Array(nSteps+1): first point offset per hour
    var stationIdxByPair = null; // Uint16Array(total): station index of each point
    var pmByPair = null;         // Uint16Array(total): pm × 10

    // ---- DOM ------------------------------------------------------------
    var $ = function (id) { return document.getElementById(id); };
    var els = {};

    // ---- Frame cache (bounded ring) --------------------------------------
    var cacheMap = {}, cacheOrder = [];
    function cacheGet(k) { return cacheMap[k]; }
    function cachePut(k, v) {
        if (cacheMap[k] === undefined) {
            cacheOrder.push(k);
            if (cacheOrder.length > FRAME_CACHE_MAX) delete cacheMap[cacheOrder.shift()];
        }
        cacheMap[k] = v;
    }
    function cacheClear() { cacheMap = {}; cacheOrder = []; }

    // ---- Sparse-index helpers -------------------------------------------
    // Counting-sort the per-station delta-encoded lists into per-hour buckets.
    // `series[s]` is [h0, pm, Δh, pm, Δh, pm, ...] — the first hour is absolute,
    // every later one is a delta from the previous (see gen_plume_finalized.py).
    function buildIndex(series) {
        var counts = new Int32Array(nSteps), total = 0, s, k, arr, h;
        for (s = 0; s < series.length; s++) {
            arr = series[s];
            for (k = 0, h = 0; k < arr.length; k += 2) {
                h += arr[k];
                if (h >= 0 && h < nSteps) { counts[h]++; total++; }
            }
        }
        stepStart = new Int32Array(nSteps + 1);
        for (var i = 0; i < nSteps; i++) stepStart[i + 1] = stepStart[i] + counts[i];
        var cursor = stepStart.slice(0, nSteps);   // mutable copy of the offsets
        stationIdxByPair = new Uint16Array(total);
        pmByPair = new Uint16Array(total);
        for (s = 0; s < series.length; s++) {
            arr = series[s];
            for (k = 0, h = 0; k < arr.length; k += 2) {
                h += arr[k];
                if (h < 0 || h >= nSteps) continue;
                var pos = cursor[h]++;
                stationIdxByPair[pos] = s;
                pmByPair[pos] = Math.min(65535, Math.round(arr[k + 1] * 10));
            }
            series[s] = null;   // release as we go; the parsed arrays are large
        }
        return total;
    }

    // Materialize one hour's sensor points on demand (cheap; not cached, so long
    // playback never accumulates millions of objects).
    function getPoints(step) {
        if (!stepStart) return [];
        var a = stepStart[step], b = stepStart[step + 1], pts = new Array(b - a);
        for (var j = a, n = 0; j < b; j++, n++) {
            var st = stations[stationIdxByPair[j]];
            pts[n] = { lat: st.lat, lon: st.lon, pm: pmByPair[j] / 10 };
        }
        return pts;
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

    // ---- Province outline (reference only, NOT a clip) -------------------
    // The finalized network reaches from the Upper Midwest to the Gulf of St.
    // Lawrence and only 227 of its 1,742 sensors sit in Ontario or Québec, so the
    // surface is drawn everywhere and the provinces are merely outlined for
    // orientation. PROVINCE_POLYGONS (global, provinces-boundary.js) is an array
    // of [lon,lat] rings.
    function drawProvinces() {
        if (typeof PROVINCE_POLYGONS === "undefined" || provinceLayer) return;
        var lines = [];
        for (var k = 0; k < PROVINCE_POLYGONS.length; k++) {
            var ring = PROVINCE_POLYGONS[k], latlngs = [];
            for (var i = 0; i < ring.length; i++) latlngs.push([ring[i][1], ring[i][0]]);
            lines.push(L.polyline(latlngs, {
                color: "#8b8b96", weight: 1, opacity: 0.55, fill: false, interactive: false,
            }));
        }
        provinceLayer = L.layerGroup(lines).addTo(map);
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

    // Choose a grid whose cells are roughly square for this bbox. The finalized
    // footprint is ~38° of longitude by ~21° of latitude — far wider than the old
    // Ontario-only box — so a fixed 200×150 grid would stretch every cell.
    function sizeGrid() {
        var north = bbox.nwlat, south = bbox.selat, west = bbox.nwlng, east = bbox.selng;
        var midLatCos = Math.cos((north + south) / 2 * Math.PI / 180);
        var w = Math.max(1e-6, (east - west) * midLatCos), h = Math.max(1e-6, north - south);
        var cols = Math.round(Math.sqrt(GRID_CELLS * w / h));
        gridCols = Math.max(40, Math.min(400, cols));
        gridRows = Math.max(40, Math.min(400, Math.round(GRID_CELLS / gridCols)));
    }

    // Interpolate one hour to a dataURL (bounded cache).
    //
    // Colour is the IDW of PM2.5; opacity is flat wherever any sensor falls inside
    // CUTOFF_DEG and fully transparent outside it, so the surface has a defined
    // edge at the interpolation radius rather than a soft falloff.
    function renderFrame(index) {
        var hit = cacheGet(index);
        if (hit) return hit;
        var pts = getPoints(index);
        var west = bbox.nwlng, east = bbox.selng, north = bbox.nwlat, south = bbox.selat;
        var midLatCos = Math.cos((north + south) / 2 * Math.PI / 180);

        var bk = buildBuckets(pts, midLatCos), buckets = bk.buckets, bs = bk.bs;
        var cutoff2 = CUTOFF_DEG * CUTOFF_DEG;
        var flatAlpha = Math.round(MAX_ALPHA * 255);

        var cv = document.createElement("canvas");
        cv.width = gridCols; cv.height = gridRows;
        var ctx = cv.getContext("2d");
        var img = ctx.createImageData(gridCols, gridRows);
        var data = img.data;

        for (var y = 0; y < gridRows; y++) {
            var lat = north - (y + 0.5) / gridRows * (north - south);
            var by = Math.floor(lat / bs);
            for (var x = 0; x < gridCols; x++) {
                var o = (y * gridCols + x) * 4;
                var lon = west + (x + 0.5) / gridCols * (east - west);
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
                data[o] = c[0]; data[o + 1] = c[1]; data[o + 2] = c[2];
                data[o + 3] = flatAlpha;
            }
        }
        ctx.putImageData(img, 0, 0);
        var url = cv.toDataURL();
        cachePut(index, url);
        return url;
    }

    function frameBounds() {
        // Leaflet imageOverlay bounds: [[south, west], [north, east]]
        return [[bbox.selat, bbox.nwlng], [bbox.nwlat, bbox.selng]];
    }

    function fmtClock(ms) {
        var d = new Date(ms);
        if (isNaN(d.getTime())) return String(ms);
        var day = d.toLocaleDateString([], { year: "numeric", month: "short", day: "numeric", timeZone: "UTC" });
        var hh = ("0" + d.getUTCHours()).slice(-2);
        return day + " · " + hh + ":00 UTC";
    }

    function showFrame(index) {
        if (!nSteps) return;
        cur = Math.max(0, Math.min(index, nSteps - 1));
        var pts = getPoints(cur);

        var url = renderFrame(cur);
        if (!overlay) {
            overlay = L.imageOverlay(url, frameBounds(), { opacity: 1, interactive: false }).addTo(map);
        } else {
            overlay.setUrl(url);
        }
        drawSensors(showSensors ? pts : []);

        els.slider.value = cur;
        els.clock.textContent = fmtClock(stepTimeMs(cur));
        els.rel.textContent = (cur + 1).toLocaleString() + " / " + nSteps.toLocaleString();
        els.frameIdx.textContent = (cur + 1).toLocaleString();
        els.sensorCount.textContent = pts.length.toLocaleString();
    }

    function drawSensors(pts) {
        if (sensorLayer) { map.removeLayer(sensorLayer); sensorLayer = null; }
        pts = pts || [];
        if (!pts.length) return;
        var markers = new Array(pts.length);
        for (var i = 0; i < pts.length; i++) {
            var p = pts[i], c = rampColor(p.pm);
            markers[i] = L.circleMarker([p.lat, p.lon], {
                radius: 3, stroke: true, weight: 0.5, color: "rgba(0,0,0,0.55)",
                fillColor: "rgb(" + c[0] + "," + c[1] + "," + c[2] + ")",
                fillOpacity: 0.95, interactive: false,
            });
        }
        sensorLayer = L.layerGroup(markers).addTo(map);
    }

    function play() {
        if (!nSteps || loading) return;
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
            .setView([48, -80], 5);
        L.control.zoom({ position: "bottomright" }).addTo(map);
        L.tileLayer("https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png", {
            attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OSM</a> &copy; <a href="https://carto.com/">CARTO</a> · PM2.5 © PurpleAir (EPA-corrected)',
            maxZoom: 12,
        }).addTo(map);
    }

    // Fit once, to the manifest's global percentile-trimmed view, so switching
    // year never makes the map jump.
    function fitOnce() {
        if (fitted || !manifest) return;
        var v = manifest.view || manifest.extent;
        if (!v) return;
        map.fitBounds([[v.selat, v.nwlng], [v.nwlat, v.selng]], { padding: [24, 24], maxZoom: 7 });
        fitted = true;
    }

    // ---- Year loading ----------------------------------------------------
    function yearEntry(y) {
        var list = (manifest && manifest.years) || [];
        for (var i = 0; i < list.length; i++) if (list[i].year === y) return list[i];
        return null;
    }

    function buildYearButtons() {
        var list = (manifest && manifest.years) || [];
        while (els.years.firstChild) els.years.removeChild(els.years.firstChild);
        for (var i = 0; i < list.length; i++) {
            (function (entry) {
                var b = document.createElement("button");
                b.className = "seg";
                b.textContent = entry.year;
                b.title = entry.stations.toLocaleString() + " sensors · " +
                    entry.points.toLocaleString() + " readings · peak " +
                    entry.peak_time.slice(0, 10);
                b.setAttribute("data-year", entry.year);
                b.addEventListener("click", function () { loadYear(entry.year); });
                els.years.appendChild(b);
            })(list[i]);
        }
    }

    function markActiveYear() {
        var btns = els.years.querySelectorAll(".seg");
        for (var i = 0; i < btns.length; i++) {
            btns[i].classList.toggle("active", parseInt(btns[i].getAttribute("data-year"), 10) === year);
            btns[i].disabled = loading;
        }
    }

    function loadYear(y) {
        if (loading || y === year) return;
        var entry = yearEntry(y);
        if (!entry) return;
        pause();
        loading = true;
        markActiveYear();
        var mb = (entry.bytes_gz / 1048576).toFixed(1);
        setOverlayMsg("Loading " + y + "…",
            "Decompressing " + entry.points.toLocaleString() + " EPA-corrected readings (" + mb + " MB).", true);

        fetch(STATIC_BASE + entry.file + "?v=1")
            .then(function (r) { return r.ok ? r.arrayBuffer() : Promise.reject("HTTP " + r.status); })
            .then(decodeMaybeGzip)
            .then(function (d) {
                // Drop the previous year's index before building the new one.
                stepStart = stationIdxByPair = pmByPair = null;
                cacheClear();
                if (overlay) { map.removeLayer(overlay); overlay = null; }

                year = y; yearMeta = entry;
                bbox = d.bbox;
                stations = d.stations || [];
                nSteps = d.n_steps || 0;
                stepSeconds = d.step_seconds || 3600;
                t0ms = Date.parse(d.t0);
                if (!stations.length || !nSteps || isNaN(t0ms)) {
                    els.statusPill.textContent = "no data";
                    setOverlayMsg("No data for " + y, "The asset is empty or malformed.", false);
                    loading = false; markActiveYear();
                    return;
                }
                // The index packs station ids into a Uint16Array; refuse rather
                // than silently wrap if a future asset ever exceeds that.
                if (stations.length > 65535) {
                    els.statusPill.textContent = "error";
                    setOverlayMsg("Too many sensors for " + y,
                        stations.length.toLocaleString() + " stations exceeds the 65,535 index limit.", false);
                    loading = false; markActiveYear();
                    return;
                }
                sizeGrid();
                var total = buildIndex(d.series || []);
                d.series = null;

                els.frameTotal.textContent = nSteps.toLocaleString();
                els.slider.max = Math.max(0, nSteps - 1);
                els.statusPill.textContent = stations.length.toLocaleString() + " sensors · " +
                    total.toLocaleString() + " readings";
                els.age.textContent = String(y);
                els.overlay.classList.add("hidden");
                loading = false;
                markActiveYear();
                fitOnce();
                // Open on the year's worst smoke hour (precomputed in the manifest).
                showFrame(entry.peak_step || 0);
            })
            .catch(function (e) {
                loading = false;
                markActiveYear();
                els.statusPill.textContent = "error";
                setOverlayMsg("Could not load " + y, String(e), false);
            });
    }

    function load() {
        setOverlayMsg("Loading…", "Reading the plume manifest.", true);
        fetch(INDEX_URL)
            .then(function (r) { return r.ok ? r.json() : Promise.reject("HTTP " + r.status); })
            .then(function (idx) {
                manifest = idx;
                var list = idx.years || [];
                if (!list.length) {
                    setOverlayMsg("No plume data", "The manifest lists no years.", false);
                    return;
                }
                buildYearButtons();
                drawProvinces();
                fitOnce();
                // Default to the year with the highest peak median — the worst
                // smoke episode in the whole 2021–2025 record.
                var best = list[0];
                for (var i = 1; i < list.length; i++) {
                    if ((list[i].peak_median || 0) > (best.peak_median || 0)) best = list[i];
                }
                loadYear(best.year);
            })
            .catch(function (e) {
                els.statusPill.textContent = "error";
                setOverlayMsg("Could not load the plume manifest", String(e), false);
            });
    }

    function wire() {
        els = {
            slider: $("slider"), play: $("play"), clock: $("clock"), rel: $("rel"),
            frameIdx: $("frame-idx"), frameTotal: $("frame-total"), sensorCount: $("sensor-count"),
            age: $("age"), showSensors: $("show-sensors"), statusPill: $("status-pill"),
            overlay: $("overlay"), ovTitle: $("ov-title"), ovBody: $("ov-body"), ovSpin: $("ov-spin"),
            transport: $("transport"), years: $("years"),
        };
        els.showSensors.checked = showSensors;
        els.slider.addEventListener("input", function () { pause(); showFrame(parseInt(this.value, 10)); });
        els.play.addEventListener("click", function () { playing ? pause() : play(); });
        els.showSensors.addEventListener("change", function () {
            showSensors = this.checked;
            if (nSteps) drawSensors(showSensors ? getPoints(cur) : []);
        });
    }

    document.addEventListener("DOMContentLoaded", function () {
        wire();
        initMap();
        load();
    });
})();
