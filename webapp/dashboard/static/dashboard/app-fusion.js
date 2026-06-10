/* ============================================================
   CLEAR · Network fusion badges
   Surfaces the READ-ONLY fusion readout (/api/plan/fusion/) on each dashboard
   city card: whether live PurpleAir confirms the alert, and whether ECCC's
   model forecasts smoke arriving from beyond the ~600 km station ring.
   Independent of the main alert flow — it only paints the #fusion-<city> chips.
   ============================================================ */
(function () {
    "use strict";

    function chip(text, color, bg) {
        return '<span style="display:inline-flex;align-items:center;gap:4px;'
            + 'font-size:10px;font-weight:600;padding:2px 8px;border-radius:999px;'
            + 'color:' + color + ';background:' + bg + ';border:1px solid ' + color + '33;">'
            + text + '</span>';
    }

    var PA = {
        confirmed:      ["PurpleAir confirms",  "#f87171", "rgba(248,113,113,0.12)"],
        unconfirmed:    ["PurpleAir: not seen", "#fbbf24", "rgba(251,191,36,0.12)"],
        purpleair_only: ["PurpleAir elevated",  "#fb923c", "rgba(251,146,60,0.12)"],
        agree_calm:     ["PurpleAir ✓",    "#34d399", "rgba(52,211,153,0.10)"],
        // no_purpleair -> no chip (city outside PurpleAir coverage)
    };

    function render(cities) {
        (cities || []).forEach(function (c) {
            var el = document.getElementById("fusion-" + c.city);
            if (!el) return;
            var chips = [];

            var ew = c.early_warning || {};
            if (ew.available && ew.incoming) {
                var o = ew.origins && ew.origins[0];
                var src = o ? (" · " + o.direction + " " + o.distance_km + "km") : "";
                chips.push(chip("⚠ ECCC smoke ~" + ew.arriving_in_hours + "h" + src,
                    "#fb923c", "rgba(251,146,60,0.14)"));
            }

            var pa = PA[c.status];
            if (pa) chips.push(chip(pa[0], pa[1], pa[2]));

            el.innerHTML = chips.join(" ");
        });
    }

    function load() {
        fetch("/api/plan/fusion/")
            .then(function (r) { return r.ok ? r.json() : null; })
            .then(function (d) { if (d) render(d.cities); })
            .catch(function () { /* fusion is best-effort; never break the dashboard */ });
    }

    document.addEventListener("DOMContentLoaded", function () {
        load();
        setInterval(load, 5 * 60 * 1000);  // refresh every 5 min
    });
})();
