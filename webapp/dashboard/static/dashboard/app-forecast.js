/* ============================================================
   CLEAR · Toronto Smoke Forecast (read-only)
   Renders the wind-aware model forecast from /api/forecast/ into #smoke-forecast:
   the chance Toronto PM2.5 crosses 35 ug/m3 at 6/12/24/48 h ahead.
   READ-ONLY and explicitly NOT an alert — independent of the 3-rule engine.
   Best-effort: any failure just leaves the card hidden; never breaks the dashboard.
   Layout lives in style.css (.fc-*); only data-driven values (bar width %,
   probability tone color) are set inline here.
   ============================================================ */
(function () {
    "use strict";

    var LABELS = { "6": "In 6 hours", "12": "In 12 hours", "24": "In 24 hours", "48": "In 2 days" };
    var ORDER = ["6", "12", "24", "48"];

    // probability -> bar/percentage tone color
    function tone(p) {
        if (p < 0.10) return "#34d399";
        if (p < 0.25) return "#fbbf24";
        if (p < 0.50) return "#fb923c";
        return "#f87171";
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
            '<div class="fc-row">' +
              '<div class="fc-label">' + label + '</div>' +
              '<div class="fc-track">' +
                '<div class="fc-bar-fill" style="width:' + pct + '%;background:' + t + ';"></div>' +
              '</div>' +
              '<div class="fc-pct" style="color:' + t + ';">' + pct + '%</div>' +
            '</div>';
    }

    function shell(inner) {
        return '' +
            '<div class="fc-card">' +
              '<div class="fc-head">' +
                '<div class="fc-title">Toronto Smoke Forecast</div>' +
                '<span class="fc-tag">Model · not an alert</span>' +
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
                '<div class="fc-warmup-text">' +
                  'Warming up — collecting the last ' + need + ' h of the surrounding field ' +
                  '(<strong>' + have + '/' + need + ' h</strong>). ' +
                  'Live probabilities appear once the window is full.' +
                '</div>' +
                '<div class="fc-warmup-track">' +
                  '<div class="fc-warmup-fill" style="width:' + pct + '%;"></div>' +
                '</div>';
        } else if (d.horizons) {
            var bars = ORDER.filter(function (k) { return d.horizons[k] != null; })
                .map(function (k) { return bar(LABELS[k] || (k + " h"), d.horizons[k]); }).join("");
            var thr = Number(d.elevated_threshold) || 35;   // coerce: defense-in-depth
            inner = bars +
                '<div class="fc-note">' +
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
