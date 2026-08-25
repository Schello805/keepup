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

    function compareMonitorCards(a, b, order) {
        const numeric = (card, key) => Number(card.dataset[key] || -1);
        const text = (card, key) => String(card.dataset[key] || "");
        const metric = (card, key, fallback) => {
            const value = Number(card.dataset[key]);
            return Number.isFinite(value) && value >= 0 ? value : fallback;
        };
        const createdTime = (card) => {
            const parsed = Date.parse(card.dataset.monitorCreated || "");
            return Number.isFinite(parsed) ? parsed : numeric(card, "monitorId");
        };
        if (order === "all") return numeric(a, "monitorOrder") - numeric(b, "monitorOrder");
        if (order === "name-asc") return text(a, "monitorName").localeCompare(text(b, "monitorName"), "de");
        if (order === "name-desc") return text(b, "monitorName").localeCompare(text(a, "monitorName"), "de");
        if (order === "response-desc") return metric(b, "monitorResponseTime", -1) - metric(a, "monitorResponseTime", -1);
        if (order === "response-asc") return metric(a, "monitorResponseTime", Number.MAX_SAFE_INTEGER) - metric(b, "monitorResponseTime", Number.MAX_SAFE_INTEGER);
        if (order === "uptime-desc") return metric(b, "monitorUptime", -1) - metric(a, "monitorUptime", -1);
        if (order === "uptime-asc") return metric(a, "monitorUptime", Number.MAX_SAFE_INTEGER) - metric(b, "monitorUptime", Number.MAX_SAFE_INTEGER);
        if (order === "created-desc") return createdTime(b) - createdTime(a);
        if (order === "created-asc") return createdTime(a) - createdTime(b);
        return numeric(a, "monitorStatusRank") - numeric(b, "monitorStatusRank")
            || text(a, "monitorName").localeCompare(text(b, "monitorName"), "de");
    }

    window.KeepUpDashboard = Object.freeze({ read, write, escapeHtml, normalizeCategoryKey, isTransientNetworkError, compareMonitorCards });
})();
