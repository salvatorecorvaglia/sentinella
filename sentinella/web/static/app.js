/* ─────────────────────────────────────────────────────────────────────────
   Sentinella Web Dashboard — Client-side JavaScript
   ───────────────────────────────────────────────────────────────────────── */

(() => {
    "use strict";

    // ── Constants ───────────────────────────────────────────────────────
    const MAX_HISTORY = 60;
    // The payload contract this page was written against; the agent reports
    // its own in /health (API_VERSION in sentinella/core/constants.py).
    const API_VERSION = 1;
    const RECONNECT_DELAY = 3000;
    const RECONNECT_MAX_DELAY = 30000;
    // After this many failures the page stops retrying and says so, rather
    // than hammering a server that is clearly gone.
    const RECONNECT_MAX_ATTEMPTS = 10;

    // ── State ───────────────────────────────────────────────────────────
    let ws = null;
    let cpuChart = null;
    let memChart = null;
    let netChart = null;
    let cpuHistory = [];
    let memHistory = [];
    let netSentHistory = [];
    let netRecvHistory = [];
    let prevNetSent = 0;
    let prevNetRecv = 0;
    let prevNetTime = 0;
    let prevIfaceData = {};
    // Previous disk counters and the rates derived from them (see updateDisk).
    let prevDisk = null;
    let diskRates = null;
    let lastSnapshot = null;
    // Stale detection: when the snapshot timestamp last moved, by this
    // browser's clock (the agent's clock may be set differently).
    let lastSnapshotTimestamp = null;
    let lastAdvanceMs = 0;
    let stale = false;
    // Overridden from /health; a stream that stops advancing for several
    // intervals is flagged as stale.
    let refreshInterval = 2;
    // From /health: { key: { field, descending } } for every key the agent can
    // rank the whole host by. Empty until it answers.
    let agentSortSpecs = {};
    let healthLoaded = false;
    // null = the agent's own order, until /health says what it sorted by.
    let sortKey = null;
    let sortAsc = true;
    // null until /health answers; then the set of modules this agent monitors.
    let activeModules = null;
    let reconnectAttempts = 0;
    let reconnectTimer = null;
    let hadSuccessfulAuth = false;
    // Overridden from /health with the agent's processes.max_display, so the
    // dashboard, the TUI and `sentinella print` all show the same number of rows.
    let maxDisplay = 40;
    // Overridden from /health with the agent's DASHBOARD_LIMITS, so the cards
    // and the TUI panels truncate the same lists at the same point.
    let displayLimits = {
        partitions: 5, interfaces: 5, temperatures: 6,
        fans: 4, containers: 8, users: 5,
    };
    // Defaults only: /health ships the agent's real thresholds so this page
    // colours a reading exactly the way the TUI and `sentinella print` do, rather
    // than keeping a second copy in step by comment.
    let thresholds = { percent: [50, 80], temp: [70, 85] };

    // Each card names its module in the markup (data-module), so a disabled
    // module reads as "not monitored" rather than as a misleading zero.
    function applyModuleAvailability() {
        if (!activeModules) return;
        document.querySelectorAll("[data-module]").forEach((card) => {
            card.classList.toggle("module-disabled", !activeModules.includes(card.dataset.module));
        });
    }

    // ── Utilities ───────────────────────────────────────────────────────
    // No escapeHtml here on purpose: every host-supplied value reaches the DOM
    // through textContent, which cannot execute markup. A helper invites the
    // string-concatenation habit that would make one necessary.

    // Web Storage throws on *any* access in some privacy modes and embedded
    // contexts. Nothing it holds is essential — a remembered key or theme —
    // so a blocked store reads as empty and writes are dropped.
    const safeStorage = {
        get(area, key) {
            try {
                return window[area].getItem(key);
            } catch (e) {
                return null;
            }
        },
        set(area, key, value) {
            try {
                window[area].setItem(key, value);
            } catch (e) {
                /* storage unavailable */
            }
        },
        remove(area, key) {
            try {
                window[area].removeItem(key);
            } catch (e) {
                /* storage unavailable */
            }
        },
    };

    // Session-only by default: the key dies with the tab unless the user
    // explicitly opts into persisting it on this device.
    function storeKey(key, persist) {
        safeStorage.set("sessionStorage", "sentinella_api_key", obfuscateKey(key));
        if (persist) {
            safeStorage.set("localStorage", "sentinella_api_key", obfuscateKey(key));
        } else {
            safeStorage.remove("localStorage", "sentinella_api_key");
        }
    }

    function loadKey() {
        return deobfuscateKey(
            safeStorage.get("sessionStorage", "sentinella_api_key") ||
                safeStorage.get("localStorage", "sentinella_api_key") ||
                ""
        );
    }

    function clearKey() {
        safeStorage.remove("sessionStorage", "sentinella_api_key");
        safeStorage.remove("localStorage", "sentinella_api_key");
    }

    // ── Shared pure helpers ─────────────────────────────────────────────
    // Defined in lib.js so they can be unit tested; app.js itself is a DOM and
    // WebSocket shell that cannot be exercised without a browser.
    const {
        humanBytes,
        humanBytesCompact,
        humanBytesRate,
        computeRate,
        formatUptime,
        WEB_SORT_KEYS,
        sortProcesses,
        isRunningContainer,
        reconnectDelay,
        obfuscateKey,
        deobfuscateKey,
    } = window.SentinellaLib;

    // The agent's definition of a sort key, or a dashboard-only column's.
    function sortSpec(key) {
        return agentSortSpecs[key] || WEB_SORT_KEYS[key] || null;
    }

    // Thresholds are agent-supplied (see /health), so these wrap the pure
    // versions with the values currently in force.
    function classForPercent(pct) {
        return window.SentinellaLib.classForPercent(pct, thresholds.percent);
    }

    function bgClassForPercent(pct) {
        return window.SentinellaLib.bgClassForPercent(pct, thresholds.percent);
    }

    function classForTemp(celsius, high, critical) {
        return window.SentinellaLib.classForTemp(celsius, high, critical, thresholds.temp);
    }

    // Says how many rows a display limit hid. Lives in a sibling of the list,
    // not inside it: the update functions compare container.children.length
    // against the data length to decide whether to rebuild the rows.
    function showMoreNote(listId, hidden, noun) {
        const note = document.getElementById(listId + "-more");
        if (!note) return;
        if (hidden > 0) {
            note.textContent = `+${hidden} more ${noun}`;
            note.hidden = false;
        } else {
            note.textContent = "";
            note.hidden = true;
        }
    }

    const EMPTY_LABELS = Array.from({ length: MAX_HISTORY }, () => "");

    function timeLabels() {
        return EMPTY_LABELS;
    }

    // ── Chart Setup ─────────────────────────────────────────────────────
    // Colours come from the --chart-* tokens in style.css (one place, both
    // themes); a canvas can't read CSS itself, so they're looked up here.
    function chartPalette() {
        const style = getComputedStyle(document.body);
        const token = (name) => style.getPropertyValue(`--chart-${name}`).trim();
        return {
            cpu: token("cpu"),
            cpuFill: token("cpu-fill"),
            mem: token("mem"),
            memFill: token("mem-fill"),
            sent: token("sent"),
            sentFill: token("sent-fill"),
            recv: token("recv"),
            recvFill: token("recv-fill"),
            grid: token("grid"),
            tick: token("tick"),
            legend: token("legend"),
            tooltipBg: token("tooltip-bg"),
            tooltipTitle: token("tooltip-title"),
            tooltipText: token("tooltip-text"),
            tooltipBorder: token("tooltip-border"),
        };
    }

    const AXIS_FONT = { size: 10, family: "'JetBrains Mono', monospace" };

    function tooltipOptions(palette) {
        return {
            backgroundColor: palette.tooltipBg,
            titleColor: palette.tooltipTitle,
            bodyColor: palette.tooltipText,
            borderColor: palette.tooltipBorder,
            borderWidth: 1,
            cornerRadius: 8,
            padding: 8,
        };
    }

    function lineDataset(label, color, fill) {
        return {
            label: label,
            data: [],
            borderColor: color,
            backgroundColor: fill,
            borderWidth: 2,
            fill: true,
            tension: 0.4,
            pointRadius: 0,
            pointHoverRadius: 3,
        };
    }

    function createChart(canvasId, datasets, { legend = false, formatTick = null, formatTooltip = null } = {}) {
        const ctx = document.getElementById(canvasId);
        if (!ctx) return null;
        const palette = chartPalette();
        const tooltip = tooltipOptions(palette);
        if (formatTooltip) tooltip.callbacks = { label: formatTooltip };
        const ticks = { color: palette.tick, font: AXIS_FONT };
        if (formatTick) ticks.callback = formatTick;
        return new Chart(ctx, {
            type: "line",
            data: { labels: timeLabels(), datasets: datasets },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                animation: { duration: 300 },
                plugins: {
                    legend: legend
                        ? { display: true, labels: { color: palette.legend, font: AXIS_FONT, boxWidth: 12 } }
                        : { display: false },
                    tooltip: tooltip,
                },
                scales: {
                    x: { display: false },
                    y: {
                        display: true,
                        min: 0,
                        grid: { color: palette.grid, drawBorder: false },
                        ticks: ticks,
                    },
                },
            },
        });
    }

    // Which palette entries colour each chart's datasets, in dataset order.
    const CHART_SERIES = new Map();

    function createPercentChart(canvasId, label, series) {
        const palette = chartPalette();
        const chart = createChart(canvasId, [
            lineDataset(label, palette[series], palette[series + "Fill"]),
        ]);
        if (chart) CHART_SERIES.set(chart, [series]);
        return chart;
    }

    function createNetChart(canvasId) {
        const palette = chartPalette();
        const chart = createChart(
            canvasId,
            [
                lineDataset("Sent", palette.sent, palette.sentFill),
                lineDataset("Received", palette.recv, palette.recvFill),
            ],
            {
                legend: true,
                formatTick: (v) => humanBytes(v),
                formatTooltip: (ctx) => ctx.dataset.label + ": " + humanBytesRate(ctx.parsed.y),
            }
        );
        if (chart) CHART_SERIES.set(chart, ["sent", "recv"]);
        return chart;
    }

    // ── Update Functions ────────────────────────────────────────────────
    function updateHeader(snap) {
        const si = snap.system_info || {};
        document.getElementById("hostname").textContent = si.hostname || "";
        document.getElementById("os-info").textContent =
            (si.os_name || "") + " " + (si.os_version || "") + " (" + (si.architecture || "") + ")";
        document.getElementById("uptime").textContent = "⏱ " + formatUptime(si.uptime_seconds || 0);
        document.getElementById("clock").textContent = new Date().toLocaleTimeString();
    }

    function updateCpu(snap) {
        const cpu = snap.cpu || {};
        const overall = cpu.percent_overall || 0;

        // Value
        const el = document.getElementById("cpu-overall");
        el.textContent = overall.toFixed(1) + "%";
        el.className = "card-value " + classForPercent(overall);

        // Details
        document.getElementById("cpu-cores").textContent =
            (cpu.core_count_physical || "?") + "P / " + (cpu.core_count_logical || "?") + "L";
        document.getElementById("cpu-freq").textContent =
            cpu.frequency_current_mhz ? cpu.frequency_current_mhz.toFixed(0) + " MHz" : "—";
        // All three or none: a platform reporting a partial tuple would throw
        // on .toFixed() of null. Mirrors the same guard in cpu_widget.py.
        const hasLoad =
            cpu.load_avg_1 != null && cpu.load_avg_5 != null && cpu.load_avg_15 != null;
        document.getElementById("cpu-load").textContent = hasLoad
            ? cpu.load_avg_1.toFixed(2) + " / " + cpu.load_avg_5.toFixed(2) + " / " + cpu.load_avg_15.toFixed(2)
            : "—";

        // Core bars
        const coreBars = document.getElementById("cpu-core-bars");
        const cores = cpu.percent_per_core || [];

        // Rebuild containers if counts mismatch to avoid layout thrashing
        if (coreBars.children.length !== cores.length) {
            coreBars.innerHTML = cores
                .map((_, i) => {
                    return `<div class="core-bar">
                        <span class="core-bar-label">C${i}</span>
                        <div class="core-bar-track"><div class="core-bar-fill"></div></div>
                    </div>`;
                })
                .join("");
        }

        // Update in-place
        cores.forEach((pct, i) => {
            const safePct = Number(pct) || 0;
            const fill = coreBars.children[i].querySelector(".core-bar-fill");
            if (fill) {
                fill.style.width = safePct + "%";
                fill.className = "core-bar-fill " + bgClassForPercent(safePct);
            }
        });

        // Chart
        cpuHistory.push(overall);
        if (cpuHistory.length > MAX_HISTORY) cpuHistory.shift();
        if (cpuChart) {
            cpuChart.data.datasets[0].data = cpuHistory;
            cpuChart.options.scales.y.max = 100;
            cpuChart.update("none");
        }
    }

    function updateMemory(snap) {
        const mem = snap.memory || {};

        const el = document.getElementById("mem-overall");
        el.textContent = (mem.percent || 0).toFixed(1) + "%";
        el.className = "card-value " + classForPercent(mem.percent || 0);

        document.getElementById("mem-used").textContent = humanBytes(mem.used || 0);
        document.getElementById("mem-total").textContent = humanBytes(mem.total || 0);
        document.getElementById("mem-avail").textContent = humanBytes(mem.available || 0);

        // Progress bars
        const ramBar = document.getElementById("ram-bar");
        ramBar.style.width = (mem.percent || 0) + "%";
        // The same crit threshold every other reading uses, from /health.
        ramBar.classList.toggle("warn", (mem.percent || 0) >= thresholds.percent[1]);

        document.getElementById("swap-bar").style.width = (mem.swap_percent || 0) + "%";

        // Chart
        memHistory.push(mem.percent || 0);
        if (memHistory.length > MAX_HISTORY) memHistory.shift();
        if (memChart) {
            memChart.data.datasets[0].data = memHistory;
            memChart.options.scales.y.max = 100;
            memChart.update("none");
        }
    }

    function updateNetwork(snap) {
        const net = snap.network || {};
        // null = the agent's platform wouldn't say; not the same as zero.
        const conns = net.connections_count == null ? "—" : net.connections_count;
        document.getElementById("net-connections").textContent = conns + " conn";

        const allInterfaces = net.interfaces || [];
        const interfaces = allInterfaces
            .filter((i) => !i.name.startsWith("lo"))
            .slice(0, displayLimits.interfaces);
        const container = document.getElementById("net-interfaces");

        // Forget interfaces that have gone away, or VPN/tether churn grows this
        // map for the life of the tab. Ported from NetworkWidget, which already
        // does exactly this.
        const liveNames = new Set(allInterfaces.map((i) => i.name));
        Object.keys(prevIfaceData).forEach((name) => {
            if (!liveNames.has(name)) delete prevIfaceData[name];
        });

        // Calculate total rates
        let totalSent = 0;
        let totalRecv = 0;
        // Every non-loopback interface, not just the listed ones: the chart is
        // host throughput, so truncating the list must not change it.
        allInterfaces.filter((i) => !i.name.startsWith("lo")).forEach((iface) => {
            totalSent += iface.bytes_sent || 0;
            totalRecv += iface.bytes_recv || 0;
        });

        // Chart in bytes/second — the tooltip and axis both label it "/s",
        // so the delta must be divided by the time actually elapsed rather
        // than plotted as bytes-per-refresh-interval.
        const nowMs = Date.now();
        const dt = (nowMs - prevNetTime) / 1000.0;
        if (prevNetTime > 0) {
            netSentHistory.push(computeRate(totalSent, prevNetSent, dt));
            netRecvHistory.push(computeRate(totalRecv, prevNetRecv, dt));
            if (netSentHistory.length > MAX_HISTORY) netSentHistory.shift();
            if (netRecvHistory.length > MAX_HISTORY) netRecvHistory.shift();
        }
        prevNetSent = totalSent;
        prevNetRecv = totalRecv;
        prevNetTime = nowMs;

        // Update chart
        if (netChart && netSentHistory.length > 0) {
            netChart.data.datasets[0].data = netSentHistory;
            netChart.data.datasets[1].data = netRecvHistory;
            netChart.update("none");
        }

        // Interface list - update in-place
        if (container.children.length !== interfaces.length) {
            container.innerHTML = interfaces
                .map(() => {
                    return `<div class="iface-row">
                        <span class="iface-name"></span>
                        <span class="iface-rate"></span>
                        <span class="iface-addr"></span>
                    </div>`;
                })
                .join("");
        }

        interfaces.forEach((iface, i) => {
            const row = container.children[i];
            const nameEl = row.querySelector(".iface-name");
            const rateEl = row.querySelector(".iface-rate");
            const addrEl = row.querySelector(".iface-addr");

            const addr = iface.addrs && iface.addrs.length > 0 ? iface.addrs[0] : "—";
            const name = iface.name;

            // Compute rates
            let rateSent = 0;
            let rateRecv = 0;
            const now = Date.now();
            if (prevIfaceData[name]) {
                const prev = prevIfaceData[name];
                const ifaceDt = (now - prev.time) / 1000.0;
                rateSent = computeRate(iface.bytes_sent, prev.sent, ifaceDt);
                rateRecv = computeRate(iface.bytes_recv, prev.recv, ifaceDt);
            }
            prevIfaceData[name] = {
                sent: iface.bytes_sent,
                recv: iface.bytes_recv,
                time: now
            };

            nameEl.textContent = name;
            rateEl.textContent = `▲ ${humanBytesRate(rateSent)}  ▼ ${humanBytesRate(rateRecv)}`;
            addrEl.textContent = addr;
        });

        const shownNonLo = allInterfaces.filter((i) => !i.name.startsWith("lo")).length;
        showMoreNote("net-interfaces", shownNonLo - interfaces.length, "interfaces");
    }

    function updateDisk(snap) {
        const disk = snap.disk || {};
        const partitions = (disk.partitions || []).slice(0, displayLimits.partitions);
        const container = document.getElementById("disk-partitions");

        // Partitions list - update in-place
        if (container.children.length !== partitions.length) {
            container.innerHTML = partitions
                .map(() => {
                    return `<div class="partition-row">
                        <span class="partition-mount"></span>
                        <div class="partition-usage">
                            <div class="partition-bar"><div class="partition-bar-fill"></div></div>
                            <span class="partition-pct"></span>
                        </div>
                        <span class="partition-size"></span>
                    </div>`;
                })
                .join("");
        }

        partitions.forEach((p, i) => {
            const row = container.children[i];
            const mountEl = row.querySelector(".partition-mount");
            const fillEl = row.querySelector(".partition-bar-fill");
            const pctEl = row.querySelector(".partition-pct");
            const sizeEl = row.querySelector(".partition-size");

            const safePct = Number(p.percent) || 0;

            mountEl.textContent = p.mountpoint;
            mountEl.title = p.mountpoint; // ellipsized in CSS; full path on hover
            fillEl.style.width = safePct + "%";
            fillEl.className = "partition-bar-fill " + bgClassForPercent(safePct);
            pctEl.textContent = safePct.toFixed(1) + "%";
            pctEl.className = "partition-pct " + classForPercent(safePct);
            sizeEl.textContent = `${humanBytes(p.used || 0)} / ${humanBytes(p.total || 0)}`;
        });

        showMoreNote(
            "disk-partitions", (disk.partitions || []).length - partitions.length, "partitions"
        );

        // The counters are totals since boot; shown bare, beside live bars,
        // they read as current throughput. Show the rate over *snapshot* time
        // (a repeated snapshot keeps the last rate), and the totals on hover.
        const io = disk.io || {};
        const current = {
            read: io.read_bytes || 0,
            write: io.write_bytes || 0,
            time: Number(snap.timestamp) || 0,
        };
        if (prevDisk && current.time > prevDisk.time) {
            const dt = current.time - prevDisk.time;
            diskRates = {
                read: computeRate(current.read, prevDisk.read, dt),
                write: computeRate(current.write, prevDisk.write, dt),
            };
        }
        if (!prevDisk || current.time > prevDisk.time) prevDisk = current;

        const readEl = document.getElementById("disk-read");
        const writeEl = document.getElementById("disk-write");
        readEl.textContent = diskRates ? humanBytesRate(diskRates.read) : "—";
        writeEl.textContent = diskRates ? humanBytesRate(diskRates.write) : "—";
        readEl.title = "Read since boot: " + humanBytes(current.read);
        writeEl.title = "Written since boot: " + humanBytes(current.write);
    }

    function updateSensors(snap) {
        const sensors = snap.sensors || {};

        // Temperatures
        const temps = (sensors.temperatures || []).slice(0, displayLimits.temperatures);
        const tempContainer = document.getElementById("temp-list");
        if (temps.length) {
            let header = tempContainer.querySelector(".sensor-section-header");
            if (!header || tempContainer.children.length !== (temps.length + 1)) {
                let html = `<div class="sensor-section-header">Temperatures</div>`;
                temps.forEach(() => {
                    html += `<div class="sensor-row">
                        <span class="sensor-label"></span>
                        <span class="sensor-value"></span>
                    </div>`;
                });
                tempContainer.innerHTML = html;
            }

            temps.forEach((t, i) => {
                const row = tempContainer.children[i + 1];
                const labelEl = row.querySelector(".sensor-label");
                const valEl = row.querySelector(".sensor-value");

                labelEl.textContent = t.label;
                valEl.textContent = t.current.toFixed(0) + "°C";
                valEl.className = "sensor-value " + classForTemp(t.current, t.high, t.critical);
            });
        } else {
            tempContainer.innerHTML = "";
        }
        showMoreNote(
            "temp-list", (sensors.temperatures || []).length - temps.length, "temperatures"
        );

        // Fans
        const fans = (sensors.fans || []).slice(0, displayLimits.fans);
        const fanContainer = document.getElementById("fan-list");
        if (fans.length) {
            let header = fanContainer.querySelector(".sensor-section-header");
            if (!header || fanContainer.children.length !== (fans.length + 1)) {
                let html = `<div class="sensor-section-header">Fans</div>`;
                fans.forEach(() => {
                    html += `<div class="sensor-row">
                        <span class="sensor-label"></span>
                        <span class="sensor-value"></span>
                    </div>`;
                });
                fanContainer.innerHTML = html;
            }

            fans.forEach((f, i) => {
                const row = fanContainer.children[i + 1];
                const labelEl = row.querySelector(".sensor-label");
                const valEl = row.querySelector(".sensor-value");

                labelEl.textContent = f.label;
                valEl.textContent = (Number(f.current) || 0) + " RPM";
            });
        } else {
            fanContainer.innerHTML = "";
        }
        showMoreNote("fan-list", (sensors.fans || []).length - fans.length, "fans");

        // Battery
        const bat = sensors.battery;
        const batContainer = document.getElementById("battery-info");
        if (bat) {
            let header = batContainer.querySelector(".sensor-section-header");
            if (!header || batContainer.children.length !== 2) {
                batContainer.innerHTML = `<div class="sensor-section-header">Battery</div>
                <div class="sensor-row">
                    <span class="sensor-label"></span>
                    <span class="sensor-value"></span>
                </div>`;
            }
            const row = batContainer.children[1];
            const labelEl = row.querySelector(".sensor-label");
            const valEl = row.querySelector(".sensor-value");

            labelEl.textContent = bat.power_plugged ? "⚡ Plugged" : "🔋 Battery";
            // An unknown charge is not a flat battery; the TUI and `fetch` both
            // print "Unknown" rather than 0%.
            if (bat.percent == null) {
                valEl.textContent = "Unknown";
                valEl.className = "sensor-value";
            } else {
                valEl.textContent = bat.percent.toFixed(0) + "%";
                valEl.className =
                    "sensor-value " + (bat.percent > 20 ? "metric-ok" : "metric-crit");
            }
        } else {
            batContainer.innerHTML = "";
        }
    }

    function updateUsers(snap) {
        const users = snap.users || [];
        const seen = new Set();
        const uniqueUsers = users.filter((u) => {
            if (seen.has(u.name)) return false;
            seen.add(u.name);
            return true;
        });
        document.getElementById("user-count").textContent = uniqueUsers.length;
        const filteredUsers = uniqueUsers.slice(0, displayLimits.users);

        const container = document.getElementById("user-list");
        if (container.children.length !== filteredUsers.length) {
            container.innerHTML = filteredUsers
                .map(() => {
                    return `<div class="user-row">
                        <span class="user-name"></span>
                        <span class="user-terminal"></span>
                    </div>`;
                })
                .join("");
        }

        filteredUsers.forEach((u, i) => {
            const row = container.children[i];
            const nameEl = row.querySelector(".user-name");
            const termEl = row.querySelector(".user-terminal");

            nameEl.textContent = u.name;
            termEl.textContent = u.terminal || "—";
        });

        showMoreNote("user-list", uniqueUsers.length - filteredUsers.length, "users");
    }

    function updateContainers(snap) {
        const ct = snap.containers || {};
        const containers = ct.containers || [];
        const card = document.getElementById("containers-card");

        // No runtime installed is not "zero containers" — the TUI hides the
        // panel outright, so say "not available" rather than showing an empty
        // list that reads as a healthy host with nothing running.
        const runtimeAvailable = Boolean(ct.docker_available || ct.lxc_available);
        if (card) card.classList.toggle("runtime-unavailable", !runtimeAvailable);
        if (!runtimeAvailable) {
            document.getElementById("container-count").textContent = "—";
            document.getElementById("container-list").innerHTML =
                '<div class="empty-note">No container runtime detected</div>';
            return;
        }
        const running = containers.filter(isRunningContainer).length;
        document.getElementById("container-count").textContent = running + " / " + containers.length;

        const container = document.getElementById("container-list");
        if (containers.length) {
            const showList = containers.slice(0, displayLimits.containers);

            if (container.children.length !== showList.length || container.querySelector(".no-containers")) {
                container.innerHTML = showList
                    .map(() => {
                        return `<div class="container-row">
                            <span class="container-runtime"></span>
                            <span class="container-name"></span>
                            <span class="container-status"></span>
                            <span class="container-image"></span>
                        </div>`;
                    })
                    .join("");
            }

            showList.forEach((c, i) => {
                const row = container.children[i];
                const runtimeEl = row.querySelector(".container-runtime");
                const nameEl = row.querySelector(".container-name");
                const statusEl = row.querySelector(".container-status");
                const imageEl = row.querySelector(".container-image");

                const isRunning = isRunningContainer(c);

                runtimeEl.textContent = c.runtime;
                nameEl.textContent = c.name;
                statusEl.textContent = c.status;
                statusEl.className = `container-status ${isRunning ? "status-running" : "status-stopped"}`;
                imageEl.textContent = c.image;
            });
            showMoreNote("container-list", containers.length - showList.length, "containers");
        } else {
            container.innerHTML = '<div class="no-containers empty-note">No containers detected</div>';
            showMoreNote("container-list", 0, "containers");
        }
    }

    // How the agent ranked the list before truncating it. Sorting here can
    // only reorder what arrived, so any other key is a sort *within* that
    // slice, not a true ranking of the host — say so rather than implying
    // "top 25 by RSS" when it is "top 25 by CPU, reordered by RSS".
    let agentSortBy = null;

    function updateProcessNote(shown, total) {
        const note = document.getElementById("process-note");
        if (!note) return;
        if (total <= shown) {
            note.textContent = "";
            note.hidden = true;
            return;
        }
        const base = agentSortBy
            ? `Showing ${shown} of ${total}, ranked by ${agentSortBy} on the agent`
            : `Showing ${shown} of ${total}, as ranked by the agent`;
        if (sortKey === null || sortKey === agentSortBy) {
            note.textContent = base + ".";
        } else if (healthLoaded && !(sortKey in agentSortSpecs)) {
            // A column only this page sorts by: say it can't be a host-wide top-N.
            note.textContent =
                `${base}. The agent can't rank by ${sortKey}, ` +
                `so this only reorders these ${shown}.`;
        } else if (healthLoaded) {
            note.textContent =
                `${base}, reordered by ${sortKey} here. ` +
                `Set processes.sort_by = "${sortKey}" to rank the whole host by it.`;
        } else {
            note.textContent = `${base}, reordered by ${sortKey} here.`;
        }
        note.hidden = false;
    }

    function updateProcesses(snap) {
        const procs = snap.processes || [];
        // process_count is the host total; procs is truncated for display.
        const total = snap.process_count || procs.length;
        document.getElementById("proc-count").textContent = total + " processes";

        // Sort. sort_processes() lower-cases before comparing names, so this
        // must too or the two dashboards interleave differently.
        const sorted = sortProcesses(procs, sortSpec(sortKey), sortAsc);

        // Update header sort indicators
        document.querySelectorAll("#process-table th[data-sort]").forEach((th) => {
            const key = th.getAttribute("data-sort");
            let baseText = th.getAttribute("data-base");
            if (!baseText) {
                baseText = th.textContent.replace(/ [▲▼]/g, "");
                th.setAttribute("data-base", baseText);
            }
            if (key === sortKey) {
                th.textContent = baseText + (sortAsc ? " ▲" : " ▼");
                th.setAttribute("aria-sort", sortAsc ? "ascending" : "descending");
            } else {
                th.textContent = baseText;
                th.removeAttribute("aria-sort");
            }
        });

        const tbody = document.getElementById("process-tbody");
        const showProcs = sorted.slice(0, maxDisplay);

        if (tbody.children.length !== showProcs.length) {
            tbody.innerHTML = showProcs
                .map(() => {
                    return `<tr>
                        <td class="proc-pid"></td>
                        <td class="proc-name"></td>
                        <td class="proc-user"></td>
                        <td class="proc-cpu"></td>
                        <td class="proc-mem"></td>
                        <td class="proc-rss"></td>
                        <td class="proc-threads"></td>
                        <td class="proc-status"></td>
                    </tr>`;
                })
                .join("");
        }

        showProcs.forEach((p, i) => {
            const row = tbody.children[i];
            const pidEl = row.querySelector(".proc-pid");
            const nameEl = row.querySelector(".proc-name");
            const userEl = row.querySelector(".proc-user");
            const cpuEl = row.querySelector(".proc-cpu");
            const memEl = row.querySelector(".proc-mem");
            const rssEl = row.querySelector(".proc-rss");
            const threadsEl = row.querySelector(".proc-threads");
            const statusEl = row.querySelector(".proc-status");

            const cpuPct = p.cpu_percent || 0;
            const memPct = p.memory_percent || 0;

            pidEl.textContent = p.pid;
            nameEl.textContent = (p.name || "").substring(0, 30);
            userEl.textContent = (p.username || "—").substring(0, 12);

            cpuEl.textContent = cpuPct.toFixed(1);
            cpuEl.className = "proc-cpu " + classForPercent(cpuPct);

            memEl.textContent = memPct.toFixed(1);
            memEl.className = "proc-mem " + classForPercent(memPct);

            rssEl.textContent = humanBytesCompact(p.memory_rss || 0);
            threadsEl.textContent = p.num_threads || 0;

            statusEl.textContent = (p.status || "").substring(0, 10);
        });

        updateProcessNote(showProcs.length, total);
    }

    const CARD_UPDATES = [
        updateHeader, updateCpu, updateMemory, updateNetwork, updateDisk,
        updateSensors, updateUsers, updateContainers, updateProcesses,
    ];

    // ── Process table sorting ───────────────────────────────────────────
    const processTable = document.getElementById("process-table");

    function handleSort(th) {
        const key = th.getAttribute("data-sort");
        if (sortKey === key) {
            sortAsc = !sortAsc;
        } else {
            sortKey = key;
            // Each key's own default direction: PID and Name ascend, the
            // numeric columns descend (the agent's definitions, for its keys).
            const spec = sortSpec(key);
            sortAsc = spec ? !spec.descending : true;
        }
        if (lastSnapshot) {
            updateProcesses(lastSnapshot);
        }
    }

    processTable.addEventListener("click", (e) => {
        const th = e.target.closest("th[data-sort]");
        if (th) handleSort(th);
    });

    processTable.addEventListener("keydown", (e) => {
        if (e.key === "Enter" || e.key === " ") {
            const th = e.target.closest("th[data-sort]");
            if (th) {
                e.preventDefault();
                handleSort(th);
            }
        }
    });

    // ── WebSocket ───────────────────────────────────────────────────────
    function showAuthModal(message) {
        const modal = document.getElementById("auth-modal");
        if (!modal) return;
        modal.hidden = false;
        // Distinguish "wrong key" from "server restarted" — previously both
        // just re-showed a blank form.
        const err = document.getElementById("auth-error");
        if (err) {
            err.textContent = message || "";
            err.hidden = !message;
        }
        const input = document.getElementById("auth-key-input");
        if (input) {
            input.value = "";
            input.focus();
        }
    }

    function hideAuthModal() {
        const modal = document.getElementById("auth-modal");
        if (modal) {
            modal.hidden = true;
        }
    }

    // Keeps Tab from leaving the modal while it's open — without this, a
    // keyboard user can tab out to the dimmed dashboard behind it.
    function trapAuthModalFocus(e) {
        if (e.key !== "Tab") return;
        const modal = document.getElementById("auth-modal");
        if (!modal || modal.hidden) return;
        const focusable = modal.querySelectorAll(
            'input, button, [href], select, textarea, [tabindex]:not([tabindex="-1"])'
        );
        if (focusable.length === 0) return;
        const first = focusable[0];
        const last = focusable[focusable.length - 1];
        if (e.shiftKey && document.activeElement === first) {
            e.preventDefault();
            last.focus();
        } else if (!e.shiftKey && document.activeElement === last) {
            e.preventDefault();
            first.focus();
        }
    }
    document.addEventListener("keydown", trapAuthModalFocus);

    // Shown only while a reconnect is actually pending — not on first load,
    // not while waiting for a key, not once the retry budget is spent.
    function showToast(message) {
        const toast = document.getElementById("conn-toast");
        if (!toast) return;
        toast.textContent = message || "";
        toast.hidden = !message;
    }

    // Exponential backoff, so a downed server is not polled every 3 s forever.
    // *reason* prefixes the countdown ("Server busy") so a 1013 still says why.
    function scheduleReconnect(badge, reason) {
        if (reconnectAttempts >= RECONNECT_MAX_ATTEMPTS) {
            badge.textContent = "Disconnected";
            badge.title = "Server unreachable.";
            showToast("");
            // Telling someone to reload is a dead end when a click will do.
            showRetryButton();
            return;
        }
        const delay = reconnectDelay(
            reconnectAttempts, RECONNECT_DELAY, RECONNECT_MAX_DELAY, RECONNECT_MAX_ATTEMPTS
        );
        reconnectAttempts += 1;
        const seconds = Math.round(delay / 1000);
        badge.textContent = reason
            ? `${reason} — retrying in ${seconds}s…`
            : `Reconnecting in ${seconds}s…`;
        showToast("Connection lost. Reconnecting…");
        clearTimeout(reconnectTimer);
        reconnectTimer = setTimeout(connect, delay);
    }

    function showRetryButton() {
        const badge = document.getElementById("status-badge");
        if (!badge || document.getElementById("retry-btn")) return;
        const btn = document.createElement("button");
        btn.id = "retry-btn";
        btn.className = "retry-btn";
        btn.type = "button";
        btn.textContent = "Retry";
        btn.addEventListener("click", () => {
            reconnectAttempts = 0;
            btn.remove();
            badge.textContent = "Connecting…";
            connect();
        });
        badge.insertAdjacentElement("afterend", btn);
    }

    function hideRetryButton() {
        const btn = document.getElementById("retry-btn");
        if (btn) btn.remove();
    }

    function connect() {
        // Never leave a pending retry racing against a manual reconnect.
        clearTimeout(reconnectTimer);
        if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) {
            ws.onclose = null;
            ws.close();
        }
        const proto = location.protocol === "https:" ? "wss:" : "ws:";
        const params = new URLSearchParams(location.search);
        let apiKey = params.get("api_key");

        if (apiKey) {
            // A key handed over in the URL is session-scoped by default.
            storeKey(apiKey, false);
            params.delete("api_key");
            const newUrl = location.pathname + (params.toString() ? "?" + params.toString() : "");
            window.history.replaceState({}, document.title, newUrl);
        } else {
            apiKey = loadKey();
        }

        const url = proto + "//" + location.host + "/ws/live";
        // Handlers close over *this* socket, not the module-level `ws`: a late
        // event from a replaced socket must not act on its successor.
        const socket = new WebSocket(url);
        ws = socket;
        // Per connection, not per page: every reconnect has to earn "Live"
        // again with a frame of its own.
        let receivedFrame = false;

        socket.onopen = () => {
            // Send API key as first message (avoids leaking in URL/logs)
            socket.send(apiKey);

            // Not "Live" yet: the server has not validated the key. Saying so
            // here flashed a connected badge at a user whose key was about to
            // be rejected.
            const badge = document.getElementById("status-badge");
            badge.textContent = "Authenticating…";
            badge.title = "";
            document.body.classList.remove("disconnected");
            reconnectAttempts = 0;
            hideRetryButton();
            hideAuthModal();

            // Fetch version, active modules and the server's default theme
            fetch("/health")
                .then((r) => r.json())
                .then((d) => {
                    let versionText = "v" + (d.version || "?");
                    // Payload contract version (API_VERSION in core/constants.py).
                    if (d.api_version !== undefined && d.api_version !== API_VERSION) {
                        versionText += ` · API v${d.api_version} (this page expects v${API_VERSION})`;
                        console.warn(
                            `Sentinella agent speaks API v${d.api_version}; this dashboard ` +
                                `expects v${API_VERSION}. Reload, or update the older side.`
                        );
                    }
                    document.getElementById("app-version").textContent = versionText;
                    activeModules = d.active_modules || null;
                    applyModuleAvailability();
                    if (Number(d.max_display) > 0) {
                        maxDisplay = Number(d.max_display);
                        if (lastSnapshot) updateProcesses(lastSnapshot);
                    }
                    if (d.display_limits) {
                        displayLimits = { ...displayLimits, ...d.display_limits };
                    }
                    if (d.thresholds) {
                        thresholds = { ...thresholds, ...d.thresholds };
                    }
                    if (Number(d.refresh_interval) > 0) {
                        refreshInterval = Number(d.refresh_interval);
                    }
                    healthLoaded = true;
                    if (d.sort_keys && typeof d.sort_keys === "object" && !Array.isArray(d.sort_keys)) {
                        agentSortSpecs = d.sort_keys;
                    }
                    if (d.sort_by && agentSortSpecs[d.sort_by]) {
                        agentSortBy = d.sort_by;
                        // Show the agent's ranking as the active sort, unless
                        // the user has already picked a column.
                        if (sortKey === null) {
                            sortKey = d.sort_by;
                            sortAsc = !agentSortSpecs[d.sort_by].descending;
                        }
                    }
                    if (lastSnapshot) updateProcesses(lastSnapshot);
                    // Server default only applies when the user has no saved choice.
                    if (!safeStorage.get("localStorage", "theme") && d.theme === "light") {
                        applyTheme(true);
                    }
                })
                .catch(() => {});
        };

        socket.onmessage = (event) => {
            let snap;
            try {
                snap = JSON.parse(event.data);
            } catch (err) {
                console.error("Failed to parse snapshot:", err);
                return;
            }
            // First frame on this connection = the key was accepted. Setting
            // this in onopen meant a wrong key flashed "Live" before the
            // server rejected it.
            if (!receivedFrame) {
                receivedFrame = true;
                // Page-wide: picks the 4001 message ("may have changed" vs
                // "not accepted") for any later rejection.
                hadSuccessfulAuth = true;
                const badge = document.getElementById("status-badge");
                badge.textContent = "Live";
                badge.title = "";
                badge.classList.add("connected");
                showToast("");
                // Start the stale clock even if this frame repeats the last
                // snapshot seen before a reconnect.
                lastAdvanceMs = Date.now();
            }
            if (snap.timestamp !== lastSnapshotTimestamp) {
                lastSnapshotTimestamp = snap.timestamp;
                lastAdvanceMs = Date.now();
                setStale(false);
            }
            lastSnapshot = snap;
            // Each card on its own: one malformed field used to throw out of
            // the whole handler and leave every later card on stale data.
            for (const update of CARD_UPDATES) {
                try {
                    update(snap);
                } catch (err) {
                    console.error(`Failed to render ${update.name}:`, err);
                }
            }
            document.body.classList.remove("loading");
            // When the agent collected it — not when it arrived, which looks
            // fresh even while the agent keeps resending an old snapshot.
            const collected = Number(snap.timestamp) > 0 ? new Date(snap.timestamp * 1000) : new Date();
            document.getElementById("last-update").textContent =
                "Last update: " + collected.toLocaleTimeString();
        };

        socket.onclose = (event) => {
            // Disconnected supersedes stale; the next connection starts fresh.
            setStale(false);
            lastAdvanceMs = 0;
            const badge = document.getElementById("status-badge");
            badge.classList.remove("connected");
            document.body.classList.add("disconnected");

            if (event.code === 4001) {
                badge.textContent = "Auth Required";
                showToast("");
                clearKey();
                reconnectAttempts = 0;
                showAuthModal(
                    hadSuccessfulAuth
                        ? "Connection rejected. The API key may have changed — please re-enter it."
                        : "That API key was not accepted."
                );
            } else if (event.code === 1013) {
                scheduleReconnect(badge, "Server busy");
            } else {
                scheduleReconnect(badge);
            }
        };

        socket.onerror = () => {
            socket.close();
        };
    }

    // ── Clock ───────────────────────────────────────────────────────────
    // The stream counts as stale when the snapshot hasn't advanced for three
    // refresh intervals (at least 5 s): frames that keep arriving with the
    // same snapshot mean the agent's collection is stuck, and "Live" would lie.
    function staleAfterMs() {
        return Math.max(3 * refreshInterval, 5) * 1000;
    }

    function setStale(isStale) {
        if (isStale === stale) return;
        stale = isStale;
        document.body.classList.toggle("stale", isStale);
        const badge = document.getElementById("status-badge");
        if (!badge) return;
        badge.classList.toggle("stale", isStale);
        badge.classList.toggle("connected", !isStale);
        if (isStale) {
            const seconds = Math.round((Date.now() - lastAdvanceMs) / 1000);
            badge.textContent = "Stale";
            badge.title = `No new data for ${seconds}s — the agent may be stuck collecting.`;
        } else {
            badge.textContent = "Live";
            badge.title = "";
        }
    }

    setInterval(() => {
        document.getElementById("clock").textContent = new Date().toLocaleTimeString();
        if (lastAdvanceMs && Date.now() - lastAdvanceMs >= staleAfterMs()) {
            setStale(true);
        }
    }, 1000);

    // ── Theme Management ────────────────────────────────────────────────
    // theme-init.js has already applied a saved choice before first paint;
    // this keeps the charts and the toggle's state in step with the body.
    function updateChartTheme() {
        const palette = chartPalette();
        CHART_SERIES.forEach((series, chart) => {
            series.forEach((name, i) => {
                chart.data.datasets[i].borderColor = palette[name];
                chart.data.datasets[i].backgroundColor = palette[name + "Fill"];
            });
            chart.options.scales.y.grid.color = palette.grid;
            chart.options.scales.y.ticks.color = palette.tick;
            Object.assign(chart.options.plugins.tooltip, tooltipOptions(palette));
            if (chart.options.plugins.legend.labels) {
                chart.options.plugins.legend.labels.color = palette.legend;
            }
            chart.update();
        });
    }

    const themeBtn = document.getElementById("theme-toggle");

    function applyTheme(isLight) {
        document.body.classList.toggle("light-theme", isLight);
        if (themeBtn) themeBtn.setAttribute("aria-pressed", String(isLight));
        updateChartTheme();
    }

    if (themeBtn) {
        themeBtn.addEventListener("click", () => {
            const isLight = !document.body.classList.contains("light-theme");
            safeStorage.set("localStorage", "theme", isLight ? "light" : "dark");
            applyTheme(isLight);
        });
    }

    // ── Init ────────────────────────────────────────────────────────────
    cpuChart = createPercentChart("cpu-chart", "CPU %", "cpu");
    memChart = createPercentChart("mem-chart", "Memory %", "mem");
    netChart = createNetChart("net-chart");
    applyTheme(document.body.classList.contains("light-theme"));
    const authForm = document.getElementById("auth-form");
    if (authForm) {
        authForm.addEventListener("submit", (e) => {
            e.preventDefault();
            const input = document.getElementById("auth-key-input");
            if (input) {
                const key = input.value.trim();
                const remember = document.getElementById("auth-remember");
                storeKey(key, Boolean(remember && remember.checked));
                hideAuthModal();
                connect();
            }
        });
    }

    connect();
})();
