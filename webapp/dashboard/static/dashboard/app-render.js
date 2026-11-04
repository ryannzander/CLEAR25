/* ============================================================
   PM2.5 EWS — Rendering: table, city cards, accent, results
   ============================================================ */

// Display-only city names (the data keys stay unaccented).
const CITY_DISPLAY = { Montreal: "Montréal" };
function displayCity(city) { return CITY_DISPLAY[city] || city; }

// Predicted values arrive as int or float; show one decimal everywhere.
function fmtPm(v) {
    const n = Number(v);
    return Number.isFinite(n) ? n.toFixed(1) : String(v);
}

function renderTable(results) {
    const resultMap = {};
    if (results) results.forEach(r => { resultMap[r.id + (r.target_city || "")] = r; });

    let html = "";
    let currentCity = null;

    stations.forEach(st => {
        const city = st.target_city || "";
        if (city !== currentCity) {
            currentCity = city;
            html += `<div class="tier-sep">${displayCity(city)}</div>`;
        }

        const r = resultMap[st.id + city] || (results ? results.find(x => x.id === st.id && x.target_city === city) : null);
        const hasData = !!r;
        const pm = hasData ? r.pm25.toFixed(1) : "—";
        const pred = hasData ? r.predicted.toFixed(1) : "—";
        const lead = hasData ? fmtLead(r.lead) : "";
        let badge = "";
        if (hasData) {
            badge = alertBadge(r.level_name, { small: true });
        }

        html += `<div class="row${hasData ? "" : " no-data"}">
            <span class="td-id">${st.id}</span>
            <span class="td-station">${st.city_name}</span>
            <span class="td-dist">${st.distance.toFixed(0)} km</span>
            <span class="td-dir">${st.direction}</span>
            <span class="td-tier">T${st.tier}</span>
            <span class="td-pm">${pm}</span>
            <span class="td-pred">${pred}</span>
            <span class="td-level">${badge}</span>
            <span class="td-lead">${lead}</span>
        </div>`;
    });

    tableBody.innerHTML = html || `<div class="empty-state">
        <div class="empty-text">No stations loaded</div>
    </div>`;
}

// City card headline: the predicted PM2.5 as the card's big number.
function cardPm(value) {
    return `<span class="city-card-pm__label">Predicted</span>`
        + `<span class="city-card-pm__row">`
        + `<span class="city-card-pm__val">${fmtPm(value)}</span>`
        + `<span class="city-card-pm__unit">µg/m³</span>`
        + `</span>`;
}

// Lead-time ranges arrive as "8-24 hrs"; typeset the range with an en dash.
function fmtLead(lead) {
    return lead ? String(lead).replace(/(\d)\s*-\s*(\d)/, "$1–$2") : lead;
}

function updateCityCards(results, cityAlerts) {
    const cityNames = Object.keys(citiesInfo);

    cityNames.forEach(city => {
        const card = document.getElementById("card-" + city);
        if (!card) return;
        const levelEl = card.querySelector(".city-card-level");
        const pmEl = card.querySelector(".city-card-pm");
        const detailEl = card.querySelector(".city-card-detail");

        if (!results || results.length === 0) {
            card.style.removeProperty("--card-color");
            card.style.borderColor = "";
            levelEl.innerHTML = "";
            levelEl.textContent = "Waiting for data";
            levelEl.style.color = "";
            if (pmEl) { pmEl.textContent = ""; }
            detailEl.textContent = "";
            return;
        }

        const cityResults = results.filter(r => r.target_city === city);
        if (cityResults.length === 0) {
            card.style.removeProperty("--card-color");
            card.style.borderColor = "";
            levelEl.innerHTML = "";
            levelEl.textContent = "No data";
            levelEl.style.color = "";
            if (pmEl) { pmEl.textContent = ""; }
            detailEl.textContent = "";
            return;
        }

        const tier1City = cityResults.filter(r => r.tier === 1);
        const leadTime = fmtLead(tier1City.length > 0 ? tier1City[0].lead : cityResults[0].lead);

        const alert = cityAlerts && cityAlerts[city];
        if (alert) {
            const c = alertColor(alert.level_name);
            card.style.setProperty("--card-color", c);
            card.style.borderColor = "";
            levelEl.innerHTML = alertBadge(alert.level_name);
            levelEl.style.color = "";
            if (pmEl) {
                pmEl.innerHTML = cardPm(alert.predicted_pm25);
            }
            if (alert.alert) {
                // evaluate.py emits "rule1" | "rule2" | "rule3"; previously anything
                // that wasn't rule1 was labelled "Rule 2", mislabelling Rule 3.
                const ruleLabel = { rule1: "Rule 1", rule2: "Rule 2", rule3: "Rule 3" }[alert.rule] || "Alert";
                detailEl.textContent = `${ruleLabel} · ${leadTime ? leadTime + " lead · " : ""}${cityResults.length} stations`;
            } else {
                detailEl.textContent = `${cityResults.length} stations · ${leadTime ? leadTime + " lead" : "no alert"}`;
            }
        } else {
            const worst = cityResults[0];
            const c = alertColor(worst.level_name);
            card.style.setProperty("--card-color", c);
            card.style.borderColor = "";
            levelEl.innerHTML = alertBadge(worst.level_name);
            levelEl.style.color = "";
            if (pmEl) {
                pmEl.innerHTML = cardPm(worst.predicted);
            }
            detailEl.textContent = `${cityResults.length} stations · ${leadTime ? leadTime + " lead" : "via " + worst.station}`;
        }
    });

    if (results && results.length > 0) {
        statsRow.style.display = "grid";
        const worst = results[0];
        document.getElementById("stat-worst").textContent = worst.predicted.toFixed(1);
        document.getElementById("stat-worst").style.color = alertColor(worst.level_name);
        document.getElementById("stat-reporting").textContent = results.length;
        const tier1 = results.filter(r => r.tier === 1);
        document.getElementById("stat-lead").textContent = fmtLead(tier1.length > 0 ? tier1[0].lead : results[0].lead);
    } else {
        statsRow.style.display = "none";
    }
}

function updateAccentColor(results) {
    const root = document.documentElement;
    if (!results || results.length === 0) {
        root.style.setProperty("--accent-color", "#71717a");
        root.style.setProperty("--accent-text", "#fff");
        return;
    }
    const worst = results[0];
    root.style.setProperty("--accent-color", alertColor(worst.level_name));
    root.style.setProperty("--accent-text", alertInk(worst.level_name));
}

function handleResults(results, label, cityAlerts) {
    lastResults = results;
    lastCityAlerts = cityAlerts || null;
    updateAccentColor(results);
    renderTable(results);
    updateCityCards(results, cityAlerts);
    if (map) updateMapMarkers(results);
    const count = results ? results.length : 0;
    statusEl.textContent = `${label} · ${count} stations reporting`;
    mapStatus.textContent = `${label} · ${count} stations`;
}
