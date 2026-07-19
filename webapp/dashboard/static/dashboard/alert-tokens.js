/* ============================================================
   ALERT TOKENS — single source of truth for the 5-level scale
   in JS. Mirrors the CSS custom properties in style.css and the
   ALERT_LEVELS in services/evaluate.py. Display code should use
   alertColor()/alertBadge() keyed by LEVEL NAME instead of the
   backend's per-row `level_hex`, so a hex change never desyncs
   the UI and nothing relies on fragile inline-style matching.
   Every level pairs a color with a distinct ICON SHAPE and a
   text LABEL — meaning is never encoded by color alone.
   ============================================================ */
(function (global) {
    "use strict";

    var TOKENS = {
        "LOW":       { slug: "low",      color: "#22c55e", ink: "#000000", glow: "rgba(34, 197, 94, 0.25)" },
        "MODERATE":  { slug: "moderate", color: "#eab308", ink: "#000000", glow: "rgba(234, 179, 8, 0.25)" },
        "HIGH":      { slug: "high",     color: "#f97316", ink: "#000000", glow: "rgba(249, 115, 22, 0.25)" },
        "VERY HIGH": { slug: "veryhigh", color: "#dc2626", ink: "#ffffff", glow: "rgba(220, 38, 38, 0.30)" },
        "EXTREME":   { slug: "extreme",  color: "#7f1d1d", ink: "#ffffff", glow: "rgba(127, 29, 29, 0.35)" }
    };

    var NONE = { slug: "none", color: "#3f3f46", ink: "#e4e4e7", glow: "transparent" };

    // Distinct icon per level so color-blind users can tell levels
    // apart by shape as well as color. currentColor => inherits ink.
    var ICONS = {
        // circle — all clear
        low: '<svg class="alert-badge__icon" viewBox="0 0 12 12" aria-hidden="true"><circle cx="6" cy="6" r="4" fill="currentColor"/></svg>',
        // hollow diamond — watch
        moderate: '<svg class="alert-badge__icon" viewBox="0 0 12 12" aria-hidden="true"><path d="M6 1.5 10.5 6 6 10.5 1.5 6Z" fill="none" stroke="currentColor" stroke-width="1.6"/></svg>',
        // triangle — caution
        high: '<svg class="alert-badge__icon" viewBox="0 0 12 12" aria-hidden="true"><path d="M6 1.5 11 10.5H1Z" fill="currentColor"/></svg>',
        // triangle + exclamation — danger
        veryhigh: '<svg class="alert-badge__icon" viewBox="0 0 12 12" aria-hidden="true"><path d="M6 1 11.2 11H0.8Z" fill="currentColor"/><rect x="5.35" y="5" width="1.3" height="3" rx="0.4" fill="#fff"/><rect x="5.35" y="8.7" width="1.3" height="1.3" rx="0.4" fill="#fff"/></svg>',
        // octagon — emergency
        extreme: '<svg class="alert-badge__icon" viewBox="0 0 12 12" aria-hidden="true"><path d="M4 1h4l3 3v4l-3 3H4L1 8V4Z" fill="currentColor"/><rect x="5.35" y="3.6" width="1.3" height="3.1" rx="0.4" fill="#fff"/><rect x="5.35" y="7.5" width="1.3" height="1.3" rx="0.4" fill="#fff"/></svg>',
        none: ''
    };

    function token(name) {
        return TOKENS[(name || "").toUpperCase()] || NONE;
    }

    function alertColor(name) { return token(name).color; }
    function alertInk(name) { return token(name).ink; }
    function alertSlug(name) { return token(name).slug; }

    function escapeHtml(s) {
        return String(s == null ? "" : s).replace(/[&<>"]/g, function (c) {
            return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c];
        });
    }

    // Returns the reusable badge markup: color + icon + label.
    // opts.small => compact variant (table rows, map popups).
    function alertBadge(name, opts) {
        opts = opts || {};
        var t = token(name);
        var cls = "alert-badge" + (opts.small ? " alert-badge--sm" : "");
        var icon = ICONS[t.slug] || "";
        return '<span class="' + cls + '" data-level="' + t.slug + '">' +
            icon + '<span class="alert-badge__label">' + escapeHtml(name) + "</span></span>";
    }

    global.ALERT_TOKENS = TOKENS;
    global.alertColor = alertColor;
    global.alertInk = alertInk;
    global.alertSlug = alertSlug;
    global.alertBadge = alertBadge;
})(window);
