/* ─────────────────────────────────────────────────────────────────────────
   Sentinella Web Dashboard — pure helpers

   Split out of app.js so they can be unit tested. app.js is one big IIFE
   wired to the DOM and a WebSocket; everything in here is a pure function of
   its arguments, which is exactly the part worth pinning down with tests.

   Loaded as a plain script before app.js (exposing window.SentinellaLib) and as a
   CommonJS module under Node, so the browser needs no build step.
   ───────────────────────────────────────────────────────────────────────── */
(function (root, factory) {
    "use strict";
    const api = factory();
    if (typeof module === "object" && module.exports) {
        module.exports = api;
    } else {
        root.SentinellaLib = api;
    }
})(typeof self !== "undefined" ? self : globalThis, function () {
    "use strict";

    // Mirrors human_bytes() in sentinella/core/utils.py.
    function humanBytes(bytes) {
        const units = ["B", "KB", "MB", "GB", "TB"];
        let i = 0;
        let val = Number(bytes) || 0;
        while (Math.abs(val) >= 1024 && i < units.length - 1) {
            val /= 1024;
            i++;
        }
        return val.toFixed(1) + " " + units[i];
    }

    // Mirrors human_bytes_compact() — the process table is dense enough that
    // "83MB" reads better than "83.4 MB".
    function humanBytesCompact(bytes) {
        const units = ["B", "KB", "MB", "GB", "TB"];
        let i = 0;
        let val = Number(bytes) || 0;
        while (Math.abs(val) >= 1024 && i < units.length - 1) {
            val /= 1024;
            i++;
        }
        return (val >= 1 ? val.toFixed(0) : val.toFixed(1)) + units[i];
    }

    function humanBytesRate(bytes) {
        return humanBytes(bytes) + "/s";
    }

    // Bytes/second between two cumulative counter readings. Returns 0 when
    // there is no usable baseline, or when the counter reset (interface
    // reconnect, agent restart) — a negative delta is not a negative rate.
    function computeRate(current, previous, dtSeconds) {
        if (!(dtSeconds > 0.1)) return 0;
        return Math.max(0, (current - previous) / dtSeconds);
    }

    function formatUptime(seconds) {
        const total = Math.max(0, Number(seconds) || 0);
        const d = Math.floor(total / 86400);
        const h = Math.floor((total % 86400) / 3600);
        const m = Math.floor((total % 3600) / 60);
        const parts = [];
        if (d > 0) parts.push(d + "d");
        parts.push(h + "h");
        parts.push(m + "m");
        return parts.join(" ");
    }

    // Severity. The thresholds are passed in because the agent ships its own
    // over /health — see PERCENT_THRESHOLDS / TEMP_THRESHOLDS in core/utils.py.
    function classForPercent(pct, thresholds) {
        const [warn, crit] = thresholds;
        if (pct < warn) return "metric-ok";
        if (pct < crit) return "metric-warn";
        return "metric-crit";
    }

    function bgClassForPercent(pct, thresholds) {
        const [warn, crit] = thresholds;
        if (pct < warn) return "bg-ok";
        if (pct < crit) return "bg-warn";
        return "bg-crit";
    }

    // Temperatures are °C, not percentages: prefer the sensor's own trip
    // points and only fall back to a fixed scale. Mirrors level_for_temp().
    function classForTemp(celsius, high, critical, thresholds) {
        if (critical && celsius >= critical) return "metric-crit";
        if (high && celsius >= high) return "metric-warn";
        if (!high && !critical) {
            const [warn, crit] = thresholds;
            if (celsius >= crit) return "metric-crit";
            if (celsius >= warn) return "metric-warn";
        }
        return "metric-ok";
    }

    // Mirrors PROCESS_SORT_KEYS in sentinella/core/sort.py: same key names, same
    // field mapping, same default direction.
    const SORT_KEYS = {
        pid: { field: "pid", descending: false },
        name: { field: "name", descending: false },
        user: { field: "username", descending: false },
        cpu: { field: "cpu_percent", descending: true },
        memory: { field: "memory_percent", descending: true },
        rss: { field: "memory_rss", descending: true },
        threads: { field: "num_threads", descending: true },
    };
    const DEFAULT_SORT_BY = "cpu";

    // sort_processes() lower-cases before comparing names, so this must too or
    // the two dashboards interleave differently on the same host.
    function compareProcesses(spec, ascending) {
        const direction = ascending ? 1 : -1;
        return function (a, b) {
            const va = a[spec.field];
            const vb = b[spec.field];
            if (typeof va === "string" || typeof vb === "string") {
                return (
                    direction *
                    String(va || "")
                        .toLowerCase()
                        .localeCompare(String(vb || "").toLowerCase())
                );
            }
            return direction * ((va || 0) - (vb || 0));
        };
    }

    function sortProcesses(procs, sortKey, ascending) {
        const spec = SORT_KEYS[sortKey] || SORT_KEYS[DEFAULT_SORT_BY];
        return [...procs].sort(compareProcesses(spec, ascending));
    }

    // Mirrors ContainerInfo.is_running / RUNNING_STATUSES in core/models.py.
    const RUNNING_STATUSES = new Set(["running", "up"]);

    function isRunningContainer(c) {
        return RUNNING_STATUSES.has(String((c && c.status) || "").toLowerCase());
    }

    // Exponential backoff, so a downed server is not polled forever at a fixed
    // interval. Returns null once the attempt budget is spent.
    function reconnectDelay(attempts, base, max, maxAttempts) {
        if (attempts >= maxAttempts) return null;
        return Math.min(base * 2 ** attempts, max);
    }

    // NOT encryption. Light obfuscation so the key is not sitting in storage
    // as readable plaintext; the salt ships in this file, so anyone with
    // devtools or an XSS foothold can recover it. The real controls are HTTPS
    // and keeping the key out of URLs — see SECURITY.md.
    const OBFUSCATION_SALT = "sentinella_obfuscation_salt";

    function xorWithSalt(text) {
        let result = "";
        for (let i = 0; i < text.length; i++) {
            result += String.fromCharCode(
                text.charCodeAt(i) ^ OBFUSCATION_SALT.charCodeAt(i % OBFUSCATION_SALT.length)
            );
        }
        return result;
    }

    function obfuscateKey(text) {
        if (!text) return "";
        return btoa(xorWithSalt(text));
    }

    function deobfuscateKey(ciphertext) {
        if (!ciphertext) return "";
        try {
            return xorWithSalt(atob(ciphertext));
        } catch (e) {
            return ciphertext;
        }
    }

    return {
        humanBytes,
        humanBytesCompact,
        humanBytesRate,
        computeRate,
        formatUptime,
        classForPercent,
        bgClassForPercent,
        classForTemp,
        SORT_KEYS,
        DEFAULT_SORT_BY,
        compareProcesses,
        sortProcesses,
        RUNNING_STATUSES,
        isRunningContainer,
        reconnectDelay,
        obfuscateKey,
        deobfuscateKey,
    };
});
