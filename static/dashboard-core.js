(function () {
    "use strict";

    const keys = Object.freeze({
        status: "keepup-monitor-status-filter",
        search: "keepup-monitor-search",
        category: "keepup-monitor-category-filter",
        sort: "keepup-monitor-sort",
        sounds: "keepup-dashboard-sounds",
    });

    function read(key, fallback = "") {
        try {
            return window.localStorage.getItem(keys[key]) ?? fallback;
        } catch (_error) {
            return fallback;
        }
    }

    function write(key, value) {
        try {
            window.localStorage.setItem(keys[key], String(value));
        } catch (_error) {
            // Keep the dashboard usable when browser storage is unavailable.
        }
    }

    function escapeHtml(value) {
        const div = document.createElement("div");
        div.textContent = value == null ? "" : String(value);
        return div.innerHTML;
    }

    function normalizeCategoryKey(value) {
        const label = String(value || "").trim();
        return label ? label.toLowerCase() : "__none__";
    }

    function isTransientNetworkError(error) {
        const message = String(error?.message || error || "").toLowerCase();
        return error instanceof TypeError || message.includes("network") || message.includes("failed to fetch");
    }

    window.KeepUpDashboard = Object.freeze({ read, write, escapeHtml, normalizeCategoryKey, isTransientNetworkError });
})();
