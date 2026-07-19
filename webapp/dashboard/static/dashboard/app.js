/* ============================================================
   PM2.5 EWS — Core: globals, push, data loading, utilities
   ============================================================ */

// Logger: debug suppressed in production, errors always surfaced
const _isDev = location.hostname === "localhost" || location.hostname === "127.0.0.1";
const logger = {
    debug: _isDev ? (...a) => console.log(...a) : () => {},
    error: (...a) => console.error(...a),
};

let stations = [];
let citiesInfo = {};
let map = null;
let mapMarkers = [];
let lastResults = null;
let lastCityAlerts = null;

// ═══════════════════════════════════════════════════════════════════════════
// DOM SETUP
// ═══════════════════════════════════════════════════════════════════════════

const tableBody = document.getElementById("table-body");
const statusEl = document.getElementById("status");
const statsRow = document.getElementById("stats-row");
const stationCount = document.getElementById("station-count");
const mapStatus = document.getElementById("map-status");

// Legacy hash deep-links (#tab-research/#tab-feedback/#tab-api/#tab-billing)
// used to switch in-page tabs. Those views are now real routes — redirect so
// old bookmarks/shared links keep working.
(function redirectLegacyTabHashes() {
    const routes = {
        "#tab-research": "/research/",
        "#tab-feedback": "/feedback/",
        "#tab-api": "/developers/",
        "#tab-billing": "/billing/",
    };
    const dest = routes[location.hash];
    if (dest) location.replace(dest);
})();

// Dashboard has two views of the same live data — the cards+table and the
// live map — toggled by URL hash (#map) so both are linkable and keyboard
// reachable. This only runs on the dashboard page (the tab elements exist there).
function showDashView(view) {
    const dash = document.getElementById("tab-dashboard");
    const mapTab = document.getElementById("tab-map");
    if (!dash || !mapTab) return;
    const showMap = view === "map";
    dash.classList.toggle("tab-visible", !showMap);
    mapTab.classList.toggle("tab-visible", showMap);
    document.querySelectorAll(".sidebar-tab[data-nav]").forEach(a => {
        const on = a.dataset.nav === (showMap ? "map" : "dashboard");
        a.classList.toggle("tab-active", on);
        if (on) a.setAttribute("aria-current", "page");
        else a.removeAttribute("aria-current");
    });
    if (showMap) {
        if (stations.length === 0) loadStations().then(() => initMap());
        else initMap();
    }
}
if (document.getElementById("tab-dashboard")) {
    showDashView(location.hash === "#map" ? "map" : "dashboard");
    window.addEventListener("hashchange", () => {
        showDashView(location.hash === "#map" ? "map" : "dashboard");
    });
}

// Research nav
document.querySelectorAll(".rnav").forEach(btn => {
    btn.addEventListener("click", () => {
        document.querySelector(".rnav-active").classList.remove("rnav-active");
        btn.classList.add("rnav-active");
        document.querySelectorAll(".research-section").forEach(s => s.classList.remove("research-visible"));
        document.getElementById("sec-" + btn.dataset.section).classList.add("research-visible");
    });
});

// Account dropdown
const accountToggle = document.getElementById("account-toggle");
const accountMenu = document.getElementById("account-menu");

if (accountToggle && accountMenu) {
    accountToggle.addEventListener("click", (e) => {
        e.stopPropagation();
        accountMenu.classList.toggle("open");
    });
    document.addEventListener("click", (e) => {
        if (!accountMenu.contains(e.target) && !accountToggle.contains(e.target)) {
            accountMenu.classList.remove("open");
        }
    });
}

// ═══════════════════════════════════════════════════════════════════════════
// DATA LOADING
// ═══════════════════════════════════════════════════════════════════════════

async function loadStations() {
    try {
        const resp = await fetch("/api/stations/");
        if (!resp.ok) {
            statusEl.textContent = `Error loading stations: HTTP ${resp.status}`;
            stations = [];
            citiesInfo = {};
            stationCount.textContent = "0 stations";
            document.getElementById("stat-total").textContent = "0";
            renderTable(null);
            return;
        }
        const data = await resp.json();
        stations = data.stations ?? [];
        citiesInfo = data.cities ?? {};
        stationCount.textContent = `${stations.length} stations across ${Object.keys(citiesInfo).length} cities`;
        document.getElementById("stat-total").textContent = stations.length;
        renderTable(null);
        if (map) updateMapMarkers(null);
    } catch (e) {
        statusEl.textContent = `Error loading stations: ${e}`;
    }
}

async function runDemo() {
    statusEl.textContent = "Loading demo scenario...";
    try {
        const resp = await fetch("/api/demo/");
        const data = await resp.json();
        handleResults(data.results, "Demo: sample ambient readings", data.city_alerts);
    } catch (e) {
        statusEl.textContent = `Error: ${e}`;
    }
}

async function loadLiveData() {
    statusEl.textContent = "Loading live data...";
    try {
        const resp = await fetch("/api/live/");
        const data = await resp.json();
        if (data.results && data.results.length > 0) {
            const age = data.age_seconds || 0;
            const mins = Math.floor(age / 60);
            let label;
            if (data.data_source === "demo_preview") {
                label = "Preview (demo readings — configure WAQI + refresh for live)";
            } else if (mins < 1) label = "Live data · just updated";
            else if (mins < 60) label = `Live data · updated ${mins} min ago`;
            else label = `Live data · updated ${Math.floor(mins / 60)}h ${mins % 60}m ago`;
            handleResults(data.results, label, data.city_alerts);
            return true;
        }
    } catch (e) { /* ignore */ }
    return false;
}

// ═══════════════════════════════════════════════════════════════════════════
// SHARED UTILITIES
// ═══════════════════════════════════════════════════════════════════════════

function escapeHtml(text) {
    const div = document.createElement("div");
    div.textContent = text;
    return div.innerHTML;
}

function timeAgo(isoString) {
    const date = new Date(isoString);
    const seconds = Math.floor((new Date() - date) / 1000);
    if (seconds < 60) return "just now";
    if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
    if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`;
    if (seconds < 604800) return `${Math.floor(seconds / 86400)}d ago`;
    return date.toLocaleDateString();
}

function showToast(message, type = "success") {
    const container = document.getElementById("toast-container");
    if (!container) return;
    const toast = document.createElement("div");
    const bgColor = type === "error" ? "#7f1d1d" : type === "warning" ? "#78350f" : "#14532d";
    const borderColor = type === "error" ? "#991b1b" : type === "warning" ? "#92400e" : "#166534";
    toast.style.cssText = `background:${bgColor};border:1px solid ${borderColor};color:#fff;padding:12px 16px;border-radius:8px;margin-top:8px;font-size:13px;display:flex;align-items:center;gap:8px;animation:slideIn 0.2s ease;box-shadow:0 4px 12px rgba(0,0,0,0.3);`;
    toast.innerHTML = `
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
            ${type === "error" ? '<circle cx="12" cy="12" r="10"/><line x1="15" y1="9" x2="9" y2="15"/><line x1="9" y1="9" x2="15" y2="15"/>' :
              type === "warning" ? '<path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/>' :
              '<polyline points="20 6 9 17 4 12"/>'}
        </svg>
        ${escapeHtml(message)}
    `;
    container.appendChild(toast);
    setTimeout(() => {
        toast.style.animation = "slideOut 0.2s ease";
        setTimeout(() => toast.remove(), 200);
    }, 3000);
}

// ═══════════════════════════════════════════════════════════════════════════
// INIT
// ═══════════════════════════════════════════════════════════════════════════

async function init() {
    await loadStations();
    const hasLive = await loadLiveData();
    if (!hasLive && statusEl) {
        statusEl.textContent = "No live data yet — run demo or wait for next refresh";
    }
    // Feedback board is its own route now; only init if its script is present.
    if (window.initFeedbackBoard) initFeedbackBoard();
}
// Only run the live-data bootstrap on the dashboard page (where the status/
// table DOM exists). Other routes share app.js for nav + account UI only.
if (document.getElementById("status")) {
    init();
}
