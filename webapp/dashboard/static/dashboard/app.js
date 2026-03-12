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

// Tab switching (sidebar)
document.querySelectorAll(".sidebar-tab").forEach(t => {
    t.addEventListener("click", () => {
        document.querySelector(".sidebar-tab.tab-active")?.classList.remove("tab-active");
        t.classList.add("tab-active");
        document.querySelectorAll(".tab-content").forEach(c => c.classList.remove("tab-visible"));
        document.getElementById("tab-" + t.dataset.tab).classList.add("tab-visible");
        if (t.dataset.tab === "map") {
            if (stations.length === 0) loadStations().then(() => initMap());
            else initMap();
        }
    });
});

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
        const data = await resp.json();
        stations = data.stations;
        citiesInfo = data.cities || {};
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
        handleResults(data.results, "Demo: All cities wildfire scenario", data.city_alerts);
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
            if (mins < 1) label = "Live data · just updated";
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
    if (!hasLive) {
        statusEl.textContent = "No live data yet — run demo or wait for next refresh";
    }
    initFeedbackBoard();
}
init();
