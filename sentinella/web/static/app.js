/* ─────────────────────────────────────────────────────────────────────────
   Sentinella Web Dashboard — Client-side JavaScript
   ───────────────────────────────────────────────────────────────────────── */

(() => {
    "use strict";

    // ── Constants ───────────────────────────────────────────────────────
    const MAX_HISTORY = 60;
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
    let lastSnapshot = null;
    // SORT_KEYS / DEFAULT_SORT_BY come from lib.js, which mirrors
    // PROCESS_SORT_KEYS in sentinella/core/sort.py.
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

    // Cards whose data comes from a single module, so a disabled module can be
    // labelled "not monitored" rather than rendering a misleading zero.
    const CARD_MODULES = {
        "cpu-card": "cpu",
        "memory-card": "memory",
        "network-card": "network",
        "disk-card": "disk",
        "sensors-card": "sensors",
        "users-card": "users",
        "containers-card": "containers",
        "process-card": "processes",
    };

    function applyModuleAvailability() {
        if (!activeModules) return;
        Object.entries(CARD_MODULES).forEach(([cardId, mod]) => {
            const card = document.getElementById(cardId);
            if (!card) return;
            card.classList.toggle("module-disabled", !activeModules.includes(mod));
        });
    }

    // ── Utilities ───────────────────────────────────────────────────────
    // No escapeHtml here on purpose: every host-supplied value reaches the DOM
    // through textContent, which cannot execute markup. A helper invites the
    // string-concatenation habit that would make one necessary.

    // Session-only by default: the key dies with the tab unless the user
    // explicitly opts into persisting it on this device.
    function storeKey(key, persist) {
        sessionStorage.setItem("sentinella_api_key", obfuscateKey(key));
        if (persist) {
            localStorage.setItem("sentinella_api_key", obfuscateKey(key));
        } else {
            localStorage.removeItem("sentinella_api_key");
        }
    }

    function loadKey() {
        return deobfuscateKey(
            sessionStorage.getItem("sentinella_api_key") || localStorage.getItem("sentinella_api_key") || ""
        );
    }

    function clearKey() {
        sessionStorage.removeItem("sentinella_api_key");
        localStorage.removeItem("sentinella_api_key");
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
        SORT_KEYS,
        DEFAULT_SORT_BY,
        sortProcesses,
        isRunningContainer,
        reconnectDelay,
        obfuscateKey,
        deobfuscateKey,
    } = window.SentinellaLib;

    sortKey = DEFAULT_SORT_BY;
    sortAsc = !SORT_KEYS[DEFAULT_SORT_BY].descending;

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
    function createChart(canvasId, label, borderColor, bgColor) {
        const ctx = document.getElementById(canvasId);
        if (!ctx) return null;
        return new Chart(ctx, {
            type: "line",
            data: {
                labels: timeLabels(),
                datasets: [{
                    label: label,
                    data: [],
                    borderColor: borderColor,
                    backgroundColor: bgColor,
                    borderWidth: 2,
                    fill: true,
                    tension: 0.4,
                    pointRadius: 0,
                    pointHoverRadius: 3,
                }],
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                animation: { duration: 300 },
                plugins: {
                    legend: { display: false },
                    tooltip: {
                        backgroundColor: "rgba(20, 27, 45, 0.95)",
                        titleColor: "#00d2ff",
                        bodyColor: "#e6f1ff",
                        borderColor: "rgba(0, 210, 255, 0.3)",
                        borderWidth: 1,
                        cornerRadius: 8,
                        padding: 8,
                    },
                },
                scales: {
                    x: { display: false },
                    y: {
                        display: true,
                        min: 0,
                        grid: {
                            color: "rgba(255, 255, 255, 0.04)",
                            drawBorder: false,
                        },
                        ticks: {
                            color: "#5a6785",
                            font: { size: 10, family: "'JetBrains Mono', monospace" },
                        },
                    },
                },
            },
        });
    }

    function createNetChart(canvasId) {
        const ctx = document.getElementById(canvasId);
        if (!ctx) return null;
        return new Chart(ctx, {
            type: "line",
            data: {
                labels: timeLabels(),
                datasets: [
                    {
                        label: "Sent",
                        data: [],
                        borderColor: "#00d2ff",
                        backgroundColor: "rgba(0, 210, 255, 0.08)",
                        borderWidth: 2,
                        fill: true,
                        tension: 0.4,
                        pointRadius: 0,
                    },
                    {
                        label: "Received",
                        data: [],
                        borderColor: "#64ffda",
                        backgroundColor: "rgba(100, 255, 218, 0.08)",
                        borderWidth: 2,
                        fill: true,
                        tension: 0.4,
                        pointRadius: 0,
                    },
                ],
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                animation: { duration: 300 },
                plugins: {
                    legend: {
                        display: true,
                        labels: {
                            color: "#8892b0",
                            font: { size: 10, family: "'JetBrains Mono', monospace" },
                            boxWidth: 12,
                        },
                    },
                    tooltip: {
                        backgroundColor: "rgba(20, 27, 45, 0.95)",
                        titleColor: "#00d2ff",
                        bodyColor: "#e6f1ff",
                        borderColor: "rgba(0, 210, 255, 0.3)",
                        borderWidth: 1,
                        cornerRadius: 8,
                        callbacks: {
                            label: (ctx) => ctx.dataset.label + ": " + humanBytesRate(ctx.parsed.y),
                        },
                    },
                },
                scales: {
                    x: { display: false },
                    y: {
                        display: true,
                        min: 0,
                        grid: {
                            color: "rgba(255, 255, 255, 0.04)",
                            drawBorder: false,
                        },
                        ticks: {
                            color: "#5a6785",
                            font: { size: 10, family: "'JetBrains Mono', monospace" },
                            callback: (v) => humanBytes(v),
                        },
                    },
                },
            },
        });
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
        document.getElementById("ram-bar").style.width = (mem.percent || 0) + "%";
        if ((mem.percent || 0) > 80) {
            document.getElementById("ram-bar").classList.add("warn");
        } else {
            document.getElementById("ram-bar").classList.remove("warn");
        }

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
        document.getElementById("net-connections").textContent = (net.connections_count || 0) + " conn";

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

        const io = disk.io || {};
        document.getElementById("disk-read").textContent = humanBytes(io.read_bytes || 0);
        document.getElementById("disk-write").textContent = humanBytes(io.write_bytes || 0);
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
        document.getElementById("user-count").textContent = users.length;
        const seen = new Set();
        const uniqueUsers = users.filter((u) => {
            if (seen.has(u.name)) return false;
            seen.add(u.name);
            return true;
        });
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
    let agentSortBy = DEFAULT_SORT_BY;

    function updateProcessNote(shown, total) {
        const note = document.getElementById("process-note");
        if (!note) return;
        if (total > shown && sortKey !== agentSortBy) {
            note.textContent =
                `Showing ${shown} of ${total} — ranked by ${agentSortBy} on the agent, ` +
                `then sorted by ${sortKey} here.`;
            note.hidden = false;
        } else if (total > shown) {
            note.textContent = `Showing ${shown} of ${total}, ranked by ${agentSortBy}.`;
            note.hidden = false;
        } else {
            note.textContent = "";
            note.hidden = true;
        }
    }

    function updateProcesses(snap) {
        const procs = snap.processes || [];
        // process_count is the host total; procs is truncated for display.
        const total = snap.process_count || procs.length;
        document.getElementById("proc-count").textContent = total + " processes";

        // Sort. sort_processes() lower-cases before comparing names, so this
        // must too or the two dashboards interleave differently.
        const sorted = sortProcesses(procs, sortKey, sortAsc);

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

    // ── Process table sorting ───────────────────────────────────────────
    const processTable = document.getElementById("process-table");

    function handleSort(th) {
        const key = th.getAttribute("data-sort");
        if (sortKey === key) {
            sortAsc = !sortAsc;
        } else {
            sortKey = key;
            // PID and Name ascend by default, the numeric columns descend —
            // same as PROCESS_SORT_KEYS rather than always descending.
            sortAsc = !(SORT_KEYS[key] || SORT_KEYS[DEFAULT_SORT_BY]).descending;
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
        modal.style.display = "flex";
        // Distinguish "wrong key" from "server restarted" — previously both
        // just re-showed a blank form.
        const err = document.getElementById("auth-error");
        if (err) {
            err.textContent = message || "";
            err.style.display = message ? "block" : "none";
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
            modal.style.display = "none";
        }
    }

    // Keeps Tab from leaving the modal while it's open — without this, a
    // keyboard user can tab out to the dimmed dashboard behind it.
    function trapAuthModalFocus(e) {
        if (e.key !== "Tab") return;
        const modal = document.getElementById("auth-modal");
        if (!modal || modal.style.display !== "flex") return;
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

    // Exponential backoff, so a downed server is not polled every 3 s forever.
    function scheduleReconnect(badge) {
        if (reconnectAttempts >= RECONNECT_MAX_ATTEMPTS) {
            badge.textContent = "Disconnected";
            badge.title = "Server unreachable.";
            // Telling someone to reload is a dead end when a click will do.
            showRetryButton();
            return;
        }
        const delay = reconnectDelay(
            reconnectAttempts, RECONNECT_DELAY, RECONNECT_MAX_DELAY, RECONNECT_MAX_ATTEMPTS
        );
        reconnectAttempts += 1;
        badge.textContent = `Reconnecting in ${Math.round(delay / 1000)}s…`;
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
        ws = new WebSocket(url);

        ws.onopen = () => {
            // Send API key as first message (avoids leaking in URL/logs)
            ws.send(apiKey);

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
                    document.getElementById("app-version").textContent = "v" + (d.version || "?");
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
                    if (d.sort_by && SORT_KEYS[d.sort_by]) {
                        agentSortBy = d.sort_by;
                        if (lastSnapshot) updateProcesses(lastSnapshot);
                    }
                    // Server default only applies when the user has no saved choice.
                    if (!localStorage.getItem("theme") && d.theme === "light") {
                        document.body.classList.add("light-theme");
                        updateChartTheme();
                    }
                })
                .catch(() => {});
        };

        ws.onmessage = (event) => {
            try {
                const snap = JSON.parse(event.data);
                // First frame = the key was accepted. Setting this in onopen
                // meant a first-attempt wrong key got the "your key may have
                // changed" message meant for a rotated one.
                if (!hadSuccessfulAuth) {
                    hadSuccessfulAuth = true;
                    const badge = document.getElementById("status-badge");
                    badge.textContent = "Live";
                    badge.title = "";
                    badge.classList.add("connected");
                }
                lastSnapshot = snap;
                updateHeader(snap);
                updateCpu(snap);
                updateMemory(snap);
                updateNetwork(snap);
                updateDisk(snap);
                updateSensors(snap);
                updateUsers(snap);
                updateContainers(snap);
                updateProcesses(snap);
                document.body.classList.remove("loading");
                document.getElementById("last-update").textContent =
                    "Last update: " + new Date().toLocaleTimeString();
            } catch (err) {
                console.error("Failed to parse snapshot:", err);
            }
        };

        ws.onclose = (event) => {
            const badge = document.getElementById("status-badge");
            badge.classList.remove("connected");
            document.body.classList.add("disconnected");

            if (event.code === 4001) {
                badge.textContent = "Auth Required";
                clearKey();
                reconnectAttempts = 0;
                showAuthModal(
                    hadSuccessfulAuth
                        ? "Connection rejected. The API key may have changed — please re-enter it."
                        : "That API key was not accepted."
                );
            } else if (event.code === 1013) {
                badge.textContent = "Server Busy";
                scheduleReconnect(badge);
            } else {
                scheduleReconnect(badge);
            }
        };

        ws.onerror = () => {
            ws.close();
        };
    }

    // ── Clock ───────────────────────────────────────────────────────────
    setInterval(() => {
        document.getElementById("clock").textContent = new Date().toLocaleTimeString();
    }, 1000);

    // ── Theme Management ────────────────────────────────────────────────
    function updateChartTheme() {
        const isLight = document.body.classList.contains("light-theme");
        const gridColor = isLight ? "rgba(0, 0, 0, 0.06)" : "rgba(255, 255, 255, 0.04)";
        const tickColor = isLight ? "#64748b" : "#5a6785";

        // The dataset lines were fixed dark-theme colours, so #00d2ff sat at
        // roughly 1.3:1 on the light card while the axes around it flipped.
        const lineColors = isLight
            ? { cpu: "#0369a1", mem: "#7c3aed", sent: "#0369a1", recv: "#0f766e" }
            : { cpu: "#00d2ff", mem: "#c084fc", sent: "#00d2ff", recv: "#64ffda" };
        if (cpuChart) cpuChart.data.datasets[0].borderColor = lineColors.cpu;
        if (memChart) memChart.data.datasets[0].borderColor = lineColors.mem;
        if (netChart) {
            netChart.data.datasets[0].borderColor = lineColors.sent;
            netChart.data.datasets[1].borderColor = lineColors.recv;
        }

        [cpuChart, memChart, netChart].forEach((chart) => {
            if (!chart) return;
            chart.options.scales.y.grid.color = gridColor;
            chart.options.scales.y.ticks.color = tickColor;
            chart.options.plugins.tooltip.backgroundColor = isLight ? "rgba(255, 255, 255, 0.95)" : "rgba(20, 27, 45, 0.95)";
            chart.options.plugins.tooltip.bodyColor = isLight ? "#1e293b" : "#e6f1ff";
            chart.options.plugins.tooltip.borderColor = isLight ? "rgba(0, 0, 0, 0.1)" : "rgba(0, 210, 255, 0.3)";
            if (chart.options.plugins.legend.labels) {
                chart.options.plugins.legend.labels.color = tickColor;
            }
            chart.update();
        });
    }

    const themeBtn = document.getElementById("theme-toggle");
    if (themeBtn) {
        themeBtn.addEventListener("click", () => {
            document.body.classList.toggle("light-theme");
            const isLight = document.body.classList.contains("light-theme");
            localStorage.setItem("theme", isLight ? "light" : "dark");
            updateChartTheme();
        });

        // Restore saved theme on load (applied to body class in HTML, but update charts)
        const savedTheme = localStorage.getItem("theme");
        if (savedTheme === "light") {
            document.body.classList.add("light-theme");
        }
    }

    // ── Init ────────────────────────────────────────────────────────────
    cpuChart = createChart("cpu-chart", "CPU %", "#00d2ff", "rgba(0, 210, 255, 0.1)");
    memChart = createChart("mem-chart", "Memory %", "#c084fc", "rgba(192, 132, 252, 0.1)");
    netChart = createNetChart("net-chart");
    if (document.body.classList.contains("light-theme")) {
        updateChartTheme();
    }
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
