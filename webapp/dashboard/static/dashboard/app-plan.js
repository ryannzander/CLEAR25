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
    var COV_SIGMA = 0.35;         // Gaussian coverage radius (deg) -> smooth, merged opacity
    var COV_FULL = 0.5;           // coverage at/above this -> full opacity (else fades to clear)
    var MAX_ALPHA = 0.82;
    var PLAY_MS = 650;            // ms per frame during playback

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
    var frames = [];             // [{captured_at, sensor_count, points:[{lat,lon,pm}]}]
    var bbox = null;
    var canvasCache = {};        // index -> dataURL
    var cur = 0, playing = false, playTimer = null, showSensors = false;
    // ECCC RDAQA model layer (a single current gridded analysis surface).
    var mode = "observed";       // "observed" (PurpleAir, animated) | "model" (ECCC)
    var eccc = null;             // { mesh: {rows,cols,bbox,values}, run } or null
    var ecccOverlay = null;

    // ---- DOM ------------------------------------------------------------
    var $ = function (id) { return document.getElementById(id); };
    var els = {};

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
    // U.S. coverage). ONTARIO_POLYGONS (global from ontario-boundary.js) is an
    // array of [lon,lat] rings; a point is "in Ontario" if it falls inside any
    // ring. Per-ring bbox skips the ray-cast for far-away cells. If the asset
    // failed to load we degrade to no clip rather than a blank map.
    var _ringBoxes = null;
    function inOntario(lon, lat) {
        if (typeof ONTARIO_POLYGONS === "undefined") return true;
        if (!_ringBoxes) {
            _ringBoxes = ONTARIO_POLYGONS.map(function (ring) {
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
        for (var k = 0; k < ONTARIO_POLYGONS.length; k++) {
            var bb = _ringBoxes[k];
            if (lon < bb.minx || lon > bb.maxx || lat < bb.miny || lat > bb.maxy) continue;
            var ring = ONTARIO_POLYGONS[k], inside = false, n = ring.length;
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
    function ontarioMask(west, east, north, south, cols, rows) {
        var key = west + "," + east + "," + north + "," + south + "," + cols + "," + rows;
        if (_mask && _maskKey === key) return _mask;
        var m = new Uint8Array(cols * rows);
        for (var y = 0; y < rows; y++) {
            var lat = north - (y + 0.5) / rows * (north - south);
            for (var x = 0; x < cols; x++) {
                var lon = west + (x + 0.5) / cols * (east - west);
                m[y * cols + x] = inOntario(lon, lat) ? 1 : 0;
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
        var pts = frames[index].points || [];
        var west = bbox.nwlng, east = bbox.selng, north = bbox.nwlat, south = bbox.selat;
        var midLatCos = Math.cos((north + south) / 2 * Math.PI / 180);

        var bk = buildBuckets(pts, midLatCos), buckets = bk.buckets, bs = bk.bs;
        var mask = ontarioMask(west, east, north, south, GRID_COLS, GRID_ROWS);
        var cutoff2 = CUTOFF_DEG * CUTOFF_DEG;
        var invTwoSigma2 = 1 / (2 * COV_SIGMA * COV_SIGMA);

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

                var wsum = 0, vsum = 0, cov = 0, exact = null;
                for (var gx = bx - 1; gx <= bx + 1; gx++) {
                    for (var gy = by - 1; gy <= by + 1; gy++) {
                        var arr = buckets[gx + ":" + gy];
                        if (!arr) continue;
                        for (var k = 0; k < arr.length; k++) {
                            var p = arr[k];
                            var ddx = (lon - p.lon) * midLatCos, ddy = lat - p.lat;
                            var d2 = ddx * ddx + ddy * ddy;
                            if (d2 > cutoff2) continue;
                            cov += Math.exp(-d2 * invTwoSigma2);
                            if (d2 < 1e-9) { exact = p.pm; continue; }
                            var w = 1 / Math.pow(d2, IDW_POWER / 2);
                            wsum += w; vsum += w * p.pm;
                        }
                    }
                }

                if (cov === 0 || (wsum === 0 && exact === null)) { data[o + 3] = 0; continue; }
                var pm = exact !== null ? exact : vsum / wsum;
                var c = rampColor(pm);
                var alpha = MAX_ALPHA * (cov >= COV_FULL ? 1 : cov / COV_FULL);
                data[o] = c[0]; data[o + 1] = c[1]; data[o + 2] = c[2];
                data[o + 3] = Math.round(alpha * 255);
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
                if (!inOntario(lon, lat)) { d[o + 3] = 0; continue; }
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
            if (frames.length) showFrame(cur);
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

    function fmtClock(iso) {
        var d = new Date(iso);
        return d.toLocaleString([], { month: "short", day: "numeric",
            hour: "2-digit", minute: "2-digit" });
    }

    function showFrame(index) {
        if (!frames.length) return;
        cur = Math.max(0, Math.min(index, frames.length - 1));
        var f = frames[cur];
        var url = renderFrame(cur);
        if (!overlay) {
            overlay = L.imageOverlay(url, frameBounds(), { opacity: 1, interactive: false }).addTo(map);
        } else {
            overlay.setUrl(url);
        }
        els.slider.value = cur;
        els.clock.textContent = fmtClock(f.captured_at);
        els.rel.textContent = relTime(f.captured_at, frames[frames.length - 1].captured_at);
        els.frameIdx.textContent = cur + 1;
        els.sensorCount.textContent = (f.sensor_count || (f.points || []).length).toLocaleString();
        if (showSensors) drawSensors(f);
    }

    function drawSensors(f) {
        if (sensorLayer) { map.removeLayer(sensorLayer); sensorLayer = null; }
        if (!showSensors) return;
        var markers = [];
        var pts = f.points || [];
        for (var i = 0; i < pts.length; i++) {
            var p = pts[i], c = rampColor(p.pm);
            markers.push(L.circleMarker([p.lat, p.lon], {
                radius: 2.5, stroke: false, fillColor: "rgb(" + c[0] + "," + c[1] + "," + c[2] + ")",
                fillOpacity: 0.9,
            }));
        }
        sensorLayer = L.layerGroup(markers).addTo(map);
    }

    function play() {
        if (!frames.length) return;
        playing = true; els.play.textContent = "⏸";
        clearInterval(playTimer);
        playTimer = setInterval(function () {
            var next = cur + 1;
            if (next >= frames.length) next = 0;
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

    function fitToBbox() {
        if (!bbox) return;
        // Fit to where sensors actually are (the dense southern-Ontario / Great
        // Lakes cloud) rather than the whole Ontario bbox, most of which is empty
        // far-north with no sensors — otherwise the surface looks like a small
        // rectangle lost in a continent-wide view. Falls back to the bbox.
        var f = frames.length ? frames[frames.length - 1] : null;
        var pts = (f && f.points) || [];
        if (pts.length >= 3) {
            var minLa = 90, maxLa = -90, minLo = 180, maxLo = -180;
            for (var i = 0; i < pts.length; i++) {
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

    function load() {
        fetch("/api/plan/frames/")
            .then(function (r) { return r.json(); })
            .then(function (d) {
                bbox = d.bbox;
                frames = d.frames || [];
                els.frameTotal.textContent = frames.length;
                els.slider.max = Math.max(0, frames.length - 1);
                if (!frames.length) {
                    els.statusPill.textContent = "no data yet";
                    setOverlayMsg("No plume frames yet",
                        "The 6-hour buffer is empty. Once the /api/plan/refresh/ cron has run a few times, frames will appear here automatically.",
                        false);
                    return;
                }
                els.overlay.classList.add("hidden");
                els.statusPill.textContent = frames.length + " frames · " +
                    (d.buffer_hours || 6) + " h";
                var latest = frames[frames.length - 1];
                els.age.textContent = fmtClock(latest.captured_at);
                fitToBbox();
                showFrame(frames.length - 1); // start on the most recent
            })
            .catch(function (e) {
                els.statusPill.textContent = "error";
                setOverlayMsg("Could not load plume data", String(e), false);
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
            if (frames.length) { showSensors ? drawSensors(frames[cur]) : drawSensors({ points: [] }); }
        });
    }

    document.addEventListener("DOMContentLoaded", function () {
        wire();
        initMap();
        load();
        loadEccc();
    });
})();
