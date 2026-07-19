/* ============================================================
   CLEAR · Toronto Smoke Forecast (read-only)
   Renders the wind-aware model forecast from /api/forecast/ into #smoke-forecast:
   the chance Toronto PM2.5 crosses 35 ug/m3 at 6/12/24/48 h ahead.
   READ-ONLY and explicitly NOT an alert — independent of the 3-rule engine.
   Best-effort: any failure just leaves the card hidden; never breaks the dashboard.
   ============================================================ */
(function () {
    "use strict";

    var LABELS = { "6": "In 6 hours", "12": "In 12 hours", "24": "In 24 hours", "48": "In 2 days" };
    var ORDER = ["6", "12", "24", "48"];

    // probability -> [bar color, background tint]
    function tone(p) {
        if (p < 0.10) return ["#34d399", "rgba(52,211,153,0.14)"];
        if (p < 0.25) return ["#fbbf24", "rgba(251,191,36,0.14)"];
        if (p < 0.50) return ["#fb923c", "rgba(251,146,60,0.16)"];
        return ["#f87171", "rgba(248,113,113,0.18)"];
    }

    function ago(sec) {
        if (sec == null) return "";
        if (sec < 90) return "just now";
        if (sec < 5400) return Math.round(sec / 60) + "m ago";
        return Math.round(sec / 3600) + "h ago";
    }

    function bar(label, prob) {
        var pct = Math.round(prob * 100);
        var t = tone(prob);
        return '' +
            '<div style="display:flex;align-items:center;gap:12px;margin:7px 0;">' +
              '<div style="width:82px;font-size:12px;color:var(--text-secondary,#a1a1aa);flex:none;">' + label + '</div>' +
              '<div style="flex:1;height:12px;border-radius:999px;background:rgba(255,255,255,0.06);overflow:hidden;">' +
                '<div style="height:100%;width:' + pct + '%;background:' + t[0] + ';border-radius:999px;transition:width .5s;"></div>' +
              '</div>' +
              '<div style="width:44px;text-align:right;font-size:13px;font-weight:700;color:' + t[0] + ';">' + pct + '%</div>' +
            '</div>';
    }

    function shell(inner) {
        return '' +
            '<div style="border:1px solid var(--border,#27272a);border-radius:16px;padding:18px 20px;' +
                 'background:var(--card-bg,rgba(255,255,255,0.02));margin:4px 0 6px;">' +
              '<div style="display:flex;align-items:center;justify-content:space-between;gap:10px;flex-wrap:wrap;margin-bottom:10px;">' +
                '<div style="font-size:15px;font-weight:700;color:var(--text-primary,#fafafa);">Toronto Smoke Forecast</div>' +
                '<span style="font-size:10px;font-weight:600;letter-spacing:.04em;text-transform:uppercase;' +
                     'color:#60a5fa;background:rgba(96,165,250,0.12);border:1px solid rgba(96,165,250,0.3);' +
                     'padding:2px 8px;border-radius:999px;">Model · not an alert</span>' +
              '</div>' + inner +
            '</div>';
    }

    function render(d) {
        var host = document.getElementById("smoke-forecast");
        if (!host || !d) return;

        var inner;
        if (d.warming_up) {
            var have = d.have || 0, need = d.need || 24;
            var pct = Math.round(have / need * 100);
            inner =
                '<div style="font-size:13px;color:var(--text-secondary,#a1a1aa);line-height:1.5;">' +
                  'Warming up — collecting the last ' + need + ' h of the surrounding field ' +
                  '(<strong style="color:var(--text-primary,#fafafa);">' + have + '/' + need + ' h</strong>). ' +
                  'Live probabilities appear once the window is full.' +
                '</div>' +
                '<div style="margin-top:10px;height:8px;border-radius:999px;background:rgba(255,255,255,0.06);overflow:hidden;">' +
                  '<div style="height:100%;width:' + pct + '%;background:#60a5fa;border-radius:999px;"></div>' +
                '</div>';
        } else if (d.horizons) {
            var bars = ORDER.filter(function (k) { return d.horizons[k] != null; })
                .map(function (k) { return bar(LABELS[k] || (k + " h"), d.horizons[k]); }).join("");
            var thr = Number(d.elevated_threshold) || 35;   // coerce: defense-in-depth
            inner = bars +
                '<div style="margin-top:11px;font-size:11px;color:var(--text-tertiary,#71717a);line-height:1.5;">' +
                  'Chance the Toronto-core median PM2.5 reaches ' + thr + ' µg/m³. ' +
                  'Updated ' + ago(d.age_seconds) + '. ' +
                  'Wind-aware model trained on NAPS reference stations, served on the live network — ' +
                  'a forecast, not an alert.' +
                '</div>';
        } else {
            return; // nothing to show
        }
        host.innerHTML = shell(inner);
        host.style.display = "block";
    }

    function load() {
        fetch("/api/forecast/")
            .then(function (r) { return r.ok ? r.json() : null; })
            .then(function (d) { if (d) render(d); })
            .catch(function () { /* best-effort; leave the card hidden */ });
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", load);
    } else {
        load();
    }
    // refresh alongside the dashboard's own cadence
    setInterval(load, 5 * 60 * 1000);
})();
