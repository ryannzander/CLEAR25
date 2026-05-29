/* ============================================================
   CSRF helper — must load before any other app-*.js script that
   makes non-GET fetch calls. Wraps window.fetch so that:
     1. Same-origin requests automatically include the CSRF token
        from the `csrftoken` cookie as `X-CSRFToken`.
     2. Cross-origin requests are untouched.
     3. Safe methods (GET / HEAD / OPTIONS) are untouched.
   ============================================================ */
(function () {
    "use strict";

    function getCookie(name) {
        const prefix = name + "=";
        const cookies = document.cookie ? document.cookie.split("; ") : [];
        for (const c of cookies) {
            if (c.startsWith(prefix)) {
                return decodeURIComponent(c.slice(prefix.length));
            }
        }
        return null;
    }

    function isSameOrigin(url) {
        try {
            const u = new URL(url, window.location.href);
            return u.origin === window.location.origin;
        } catch (e) {
            return true; // relative URLs are same-origin
        }
    }

    const SAFE_METHODS = new Set(["GET", "HEAD", "OPTIONS"]);
    const _originalFetch = window.fetch.bind(window);

    window.fetch = function (input, init) {
        init = init || {};
        const method = (init.method || (typeof input === "object" && input.method) || "GET").toUpperCase();
        const url = typeof input === "string" ? input : (input && input.url) || "";

        if (!SAFE_METHODS.has(method) && isSameOrigin(url)) {
            const token = getCookie("csrftoken");
            if (token) {
                const headers = new Headers(init.headers || {});
                if (!headers.has("X-CSRFToken")) {
                    headers.set("X-CSRFToken", token);
                }
                init.headers = headers;
            }
            init.credentials = init.credentials || "same-origin";
        }

        return _originalFetch(input, init);
    };

    // Expose for callers that want to read the token directly.
    window.getCsrfToken = function () {
        return getCookie("csrftoken");
    };
})();
