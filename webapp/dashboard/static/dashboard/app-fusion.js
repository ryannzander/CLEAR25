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
            + 'font-size:11px;font-weight:600;padding:3px 9px;border-radius:999px;'
            + 'color:' + color + ';background:' + bg + ';border:1px solid ' + color + '40;">'
            + text + '</span>';
    }

    // status -> [color, bg, label suffix]
    var PA = {
        confirmed:      ["#f87171", "rgba(248,113,113,0.14)", "confirms"],
        unconfirmed:    ["#fbbf24", "rgba(251,191,36,0.12)",  "not seen"],
        purpleair_only: ["#fb923c", "rgba(251,146,60,0.12)",  "elevated"],
        agree_calm:     ["#34d399", "rgba(52,211,153,0.12)",  "✓"],
    };

    function render(cities) {
        (cities || []).forEach(function (c) {
            var el = document.getElementById("fusion-" + c.city);
            if (!el) return;
            var chips = [];

            // ECCC model: always show a state when the forecast is ingested.
            var ew = c.early_warning || {};
            if (ew.available) {
                if (ew.incoming) {
                    var o = ew.origins && ew.origins[0];
                    var src = o ? (" · " + o.direction + " " + o.distance_km + "km") : "";
                    chips.push(chip("⚠ ECCC smoke ~" + ew.arriving_in_hours + "h" + src,
                        "#fb923c", "rgba(251,146,60,0.16)"));
                } else {
                    chips.push(chip("ECCC ✓ clear", "#60a5fa", "rgba(96,165,250,0.10)"));
                }
            }

            // PurpleAir: show the live reading + agreement where there's coverage.
            if (c.status !== "no_purpleair" && c.purpleair_pm25 != null) {
                var m = PA[c.status] || PA.agree_calm;
                chips.push(chip("PurpleAir " + c.purpleair_pm25 + " " + m[2], m[0], m[1]));
            }

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
