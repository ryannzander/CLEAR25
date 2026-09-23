/* ============================================================
   PM2.5 EWS — Map: Leaflet integration and station markers
   ============================================================ */

function cartoDarkTileUrl() {
    // CARTO watermarks keyless tiles ("API KEY REQUIRED"); the key comes from
    // the CARTO_BASEMAPS_KEY setting via a <meta> tag in the page head.
    var meta = document.querySelector('meta[name="carto-basemaps-key"]');
    var key = meta ? meta.content.trim() : "";
    return "https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png" +
        (key ? "?key=" + encodeURIComponent(key) : "");
}

function initMap() {
    if (map) {
        map.invalidateSize();
        updateMapMarkers(lastResults);
        return;
    }
    // Defer so map-container has valid dimensions after tab becomes visible
    requestAnimationFrame(() => {
        if (map) return;
        map = L.map("map-container", { zoomControl: false, attributionControl: true }).setView([52, -96], 4);
        L.control.zoom({ position: "bottomright" }).addTo(map);
        L.tileLayer(cartoDarkTileUrl(), {
            attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OSM</a> &copy; <a href="https://carto.com/">CARTO</a>',
            maxZoom: 18,
        }).addTo(map);
        updateMapMarkers(lastResults);
        map.invalidateSize();
    });
}

function createCircleIcon(color, size, pulse, variant) {
    const pulseRing = pulse
        ? `<div class="marker-pulse" style="position:absolute;inset:-6px;border-radius:50%;border:2px solid ${color};opacity:0.5;animation:markerPulse 2s ease-out infinite;"></div>`
        : "";
    // Marker shape encodes the network: Canadian NAPS = circle; US EPA = solid diamond;
    // approximate (derived position, rare) = dashed diamond.
    let shape;
    if (variant === "epa") {
        shape = `border-radius:2px;transform:rotate(45deg);border:2px solid rgba(255,255,255,0.6);`;
    } else if (variant === "derived") {
        shape = `border-radius:2px;transform:rotate(45deg);border:2px dashed rgba(255,255,255,0.7);`;
    } else {
        shape = `border-radius:50%;border:2px solid rgba(255,255,255,0.4);`;
    }
    return L.divIcon({
        className: "marker-icon",
        html: `<div style="position:relative;width:${size}px;height:${size}px;">
            ${pulseRing}
            <div style="width:${size}px;height:${size}px;${shape}background:${color};box-shadow:0 0 10px ${color}88;transition:all 0.3s;"></div>
        </div>`,
        iconSize: [size, size],
        iconAnchor: [size / 2, size / 2],
    });
}

function getCityAlertInfo(results, cityName) {
    if (!results) return { color: "#fff", level: "No Data", predicted: null, hex: "#fff" };
    const cityResults = results.filter(r => r.target_city === cityName);
    if (cityResults.length === 0) return { color: "#fff", level: "No Data", predicted: null, hex: "#fff" };

    const alert = lastCityAlerts && lastCityAlerts[cityName];
    if (alert) {
        const c = alertColor(alert.level_name);
        return {
            color: c, level: alert.level_name,
            predicted: alert.predicted_pm25, hex: c,
            textColor: alertInk(alert.level_name),
            lead: cityResults[0].lead, station: cityResults[0].station,
            count: cityResults.length, isAlert: alert.alert, rule: alert.rule,
        };
    }
    const worst = cityResults[0];
    const wc = alertColor(worst.level_name);
    return { color: wc, level: worst.level_name, predicted: worst.predicted, hex: wc, textColor: alertInk(worst.level_name), lead: worst.lead, station: worst.station, count: cityResults.length };
}

function updateMapMarkers(results) {
    if (!map) return;
    mapMarkers.forEach(m => map.removeLayer(m));
    mapMarkers = [];

    const resultMap = {};
    if (results) results.forEach(r => { resultMap[r.id + (r.target_city || "")] = r; });

    // City prediction bubbles
    for (const [name, info] of Object.entries(citiesInfo)) {
        const alert = getCityAlertInfo(results, name);
        const bubble = L.circle([info.lat, info.lon], {
            radius: 60000,
            color: alert.color,
            weight: 2,
            opacity: 0.6,
            fillColor: alert.color,
            fillOpacity: 0.12,
            dashArray: results ? null : "6 4",
            interactive: false,
        }).addTo(map);
        mapMarkers.push(bubble);
    }

    // City center markers
    for (const [name, info] of Object.entries(citiesInfo)) {
        const alert = getCityAlertInfo(results, name);
        const hasData = alert.predicted !== null;
        const dotColor = hasData ? alert.color : "#fff";

        const m = L.marker([info.lat, info.lon], {
            icon: L.divIcon({
                className: "marker-icon",
                html: `<div style="position:relative;width:22px;height:22px;">
                    <div class="marker-pulse" style="position:absolute;inset:-8px;border-radius:50%;border:2px solid ${dotColor};opacity:0.4;animation:markerPulse 3s ease-out infinite;"></div>
                    <div style="width:22px;height:22px;border-radius:50%;background:${dotColor};border:3px solid white;box-shadow:0 0 16px ${dotColor}88;transition:all 0.4s;"></div>
                </div>`,
                iconSize: [22, 22],
                iconAnchor: [11, 11],
            }),
            zIndexOffset: 1000,
        }).addTo(map);

        let popupContent = `<div class="popup-name">${info.label || name}</div><div class="popup-divider"></div>`;
        if (hasData) {
            popupContent += `
                <div class="popup-row"><span class="popup-label">Predicted PM2.5</span><span class="popup-val" style="color:${alert.hex};font-size:16px;">${alert.predicted.toFixed(1)} µg/m³</span></div>
                <div class="popup-row"><span class="popup-label">Alert Level</span><span class="popup-val">${alertBadge(alert.level, { small: true })}</span></div>
                <div class="popup-row"><span class="popup-label">Earliest Warning</span><span class="popup-val">${alert.lead || "—"}</span></div>
                <div class="popup-row"><span class="popup-label">Stations</span><span class="popup-val">${alert.count} reporting</span></div>
            `;
        } else {
            popupContent += `<div style="color:#71717a;font-size:12px;margin-top:4px;">No data available yet</div>`;
        }
        m.bindPopup(popupContent);
        mapMarkers.push(m);
    }

    // Station markers: always render the full network catalog (gray = no current reading),
    // and color the stations that have a live result via resultMap below. Only fall back to
    // results when the catalog (/api/stations/) failed to load, so the map is never empty.
    const stationsToShow = (stations && stations.length > 0)
        ? stations.filter(s => s.lat != null && s.lon != null)
        : (results || []).filter(r => r.lat != null && r.lon != null);
    stationsToShow.forEach(st => {
        const city = st.target_city || "";
        const r = resultMap[(st.id || st.station) + city] || resultMap[st.id + city];
        let color = "#52525b";
        let size = 8;
        let popupExtra = "";
        let shouldPulse = false;

        if (r) {
            color = alertColor(r.level_name);
            size = 12;
            shouldPulse = r.level_name === "EXTREME" || r.level_name === "VERY HIGH";
            popupExtra = `
                <div class="popup-divider"></div>
                <div class="popup-row"><span class="popup-label">PM2.5</span><span class="popup-val">${r.pm25.toFixed(1)} µg/m³</span></div>
                <div class="popup-row"><span class="popup-label">Predicted</span><span class="popup-val" style="color:${color}">${r.predicted.toFixed(1)} µg/m³</span></div>
                <div class="popup-row"><span class="popup-label">Level</span><span class="popup-val">${alertBadge(r.level_name, { small: true })}</span></div>
                <div class="popup-row"><span class="popup-label">Lead Time</span><span class="popup-val">${r.lead}</span></div>
            `;
        }

        const variant = st.coord_source === "epa" ? "epa" : (st.coord_source === "derived" ? "derived" : "circle");
        const marker = L.marker([st.lat, st.lon], { icon: createCircleIcon(color, size, shouldPulse, variant) }).addTo(map);
        const name = st.city_name || st.station || "Station";
        const dist = st.distance ?? st.dist ?? 0;
        const dir = st.direction ?? st.dir ?? "";
        let sourceNote = "";
        if (variant === "epa") {
            sourceNote = `<div class="popup-meta" style="color:#60a5fa;">US EPA network</div>`;
        } else if (variant === "derived") {
            sourceNote = `<div class="popup-meta" style="color:#fbbf24;">Approx. position (from distance/direction)</div>`;
        }
        marker.bindPopup(`
            <div class="popup-name">${name}</div>
            <div class="popup-meta">${st.id || st.station}</div>
            <div class="popup-meta">${city} · ${dist.toFixed(0)} km ${dir} · Tier ${st.tier ?? ""}</div>
            ${sourceNote}
            ${popupExtra}
        `);

        if (r && citiesInfo[city]) {
            const ci = citiesInfo[city];
            const line = L.polyline([[st.lat, st.lon], [ci.lat, ci.lon]], {
                color: alertColor(r.level_name), weight: 1.5, opacity: 0.25, dashArray: "4 6",
            }).addTo(map);
            mapMarkers.push(line);
        }
        mapMarkers.push(marker);
    });

    const forBounds = stationsToShow.length > 0 ? stationsToShow : stations;
    if (forBounds.length > 0) {
        const lats = forBounds.filter(s => s.lat).map(s => s.lat);
        const lons = forBounds.filter(s => s.lon).map(s => s.lon);
        if (lats.length) {
            map.fitBounds([
                [Math.min(...lats) - 1, Math.min(...lons) - 1],
                [Math.max(...lats) + 1, Math.max(...lons) + 1],
            ], { padding: [20, 20] });
        }
    }
}

async function mapRunDemo() {
    mapStatus.textContent = "Loading demo...";
    try {
        const resp = await fetch("/api/demo/");
        const data = await resp.json();
        handleResults(data.results, "Demo: sample ambient readings", data.city_alerts);
    } catch (e) {
        mapStatus.textContent = `Error: ${e}`;
    }
}
