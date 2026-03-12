/* ============================================================
   PM2.5 EWS — Map: Leaflet integration and station markers
   ============================================================ */

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
        L.tileLayer("https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png", {
            attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OSM</a> &copy; <a href="https://carto.com/">CARTO</a>',
            maxZoom: 18,
        }).addTo(map);
        updateMapMarkers(lastResults);
        map.invalidateSize();
    });
}

function createCircleIcon(color, size, pulse) {
    const pulseRing = pulse
        ? `<div class="marker-pulse" style="position:absolute;inset:-6px;border-radius:50%;border:2px solid ${color};opacity:0.5;animation:markerPulse 2s ease-out infinite;"></div>`
        : "";
    return L.divIcon({
        className: "marker-icon",
        html: `<div style="position:relative;width:${size}px;height:${size}px;">
            ${pulseRing}
            <div style="width:${size}px;height:${size}px;border-radius:50%;background:${color};border:2px solid rgba(255,255,255,0.4);box-shadow:0 0 10px ${color}88;transition:all 0.3s;"></div>
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
        return {
            color: alert.level_hex, level: alert.level_name,
            predicted: alert.predicted_pm25, hex: alert.level_hex,
            textColor: alert.level_text_color,
            lead: cityResults[0].lead, station: cityResults[0].station,
            count: cityResults.length, isAlert: alert.alert, rule: alert.rule,
        };
    }
    const worst = cityResults[0];
    return { color: worst.level_hex, level: worst.level_name, predicted: worst.predicted, hex: worst.level_hex, textColor: worst.level_text_color, lead: worst.lead, station: worst.station, count: cityResults.length };
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
                <div class="popup-row"><span class="popup-label">Alert Level</span><span class="popup-val"><span class="badge" style="background:${alert.hex};color:${alert.textColor};font-size:9px;padding:2px 8px">${alert.level}</span></span></div>
                <div class="popup-row"><span class="popup-label">Earliest Warning</span><span class="popup-val">${alert.lead || "—"}</span></div>
                <div class="popup-row"><span class="popup-label">Stations</span><span class="popup-val">${alert.count} reporting</span></div>
            `;
        } else {
            popupContent += `<div style="color:#71717a;font-size:12px;margin-top:4px;">No data available yet</div>`;
        }
        m.bindPopup(popupContent);
        mapMarkers.push(m);
    }

    // Station markers
    stations.forEach(st => {
        if (st.lat == null || st.lon == null) return;
        const city = st.target_city || "";
        const r = resultMap[st.id + city];
        let color = "#52525b";
        let size = 8;
        let popupExtra = "";
        let shouldPulse = false;

        if (r) {
            color = r.level_hex;
            size = 12;
            shouldPulse = r.level_name === "EXTREME" || r.level_name === "VERY HIGH";
            popupExtra = `
                <div class="popup-divider"></div>
                <div class="popup-row"><span class="popup-label">PM2.5</span><span class="popup-val">${r.pm25.toFixed(1)} µg/m³</span></div>
                <div class="popup-row"><span class="popup-label">Predicted</span><span class="popup-val" style="color:${r.level_hex}">${r.predicted.toFixed(1)} µg/m³</span></div>
                <div class="popup-row"><span class="popup-label">Level</span><span class="popup-val"><span class="badge" style="background:${r.level_hex};color:${r.level_text_color};font-size:9px;padding:2px 8px">${r.level_name}</span></span></div>
                <div class="popup-row"><span class="popup-label">Lead Time</span><span class="popup-val">${r.lead}</span></div>
            `;
        }

        const marker = L.marker([st.lat, st.lon], { icon: createCircleIcon(color, size, shouldPulse) }).addTo(map);
        marker.bindPopup(`
            <div class="popup-name">${st.city_name}</div>
            <div class="popup-meta">${st.id}</div>
            <div class="popup-meta">${city} · ${st.distance.toFixed(0)} km ${st.direction} · Tier ${st.tier}</div>
            ${popupExtra}
        `);

        if (r && citiesInfo[city]) {
            const ci = citiesInfo[city];
            const line = L.polyline([[st.lat, st.lon], [ci.lat, ci.lon]], {
                color: r.level_hex, weight: 1.5, opacity: 0.25, dashArray: "4 6",
            }).addTo(map);
            mapMarkers.push(line);
        }
        mapMarkers.push(marker);
    });

    if (stations.length > 0) {
        const lats = stations.filter(s => s.lat).map(s => s.lat);
        const lons = stations.filter(s => s.lon).map(s => s.lon);
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
        handleResults(data.results, "Demo: All cities", data.city_alerts);
    } catch (e) {
        mapStatus.textContent = `Error: ${e}`;
    }
}
