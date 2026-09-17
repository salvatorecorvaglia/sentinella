/**
 * End-to-end-ish render test.
 *
 * Loads the real page, runs the real app.js against a stubbed WebSocket, and
 * feeds it a snapshot — the closest thing to opening the dashboard without a
 * browser. This is what would have caught a card update reaching for an id
 * that isn't in the markup.
 */
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import path from "node:path";
import vm from "node:vm";

import { beforeEach, describe, expect, it, vi } from "vitest";

const STATIC = path.join(path.dirname(fileURLToPath(import.meta.url)), "../../sentinella/web/static");
const html = readFileSync(path.join(STATIC, "index.html"), "utf8");
const libSource = readFileSync(path.join(STATIC, "lib.js"), "utf8");
const appSource = readFileSync(path.join(STATIC, "app.js"), "utf8");

const SNAPSHOT = {
    timestamp: 1700000000,
    process_count: 604,
    system_info: {
        hostname: "test-host",
        os_name: "Linux",
        os_version: "6.1",
        architecture: "x86_64",
        uptime_seconds: 90061,
    },
    cpu: {
        percent_overall: 42.5,
        percent_per_core: [40, 45],
        core_count_logical: 2,
        core_count_physical: 2,
        frequency_current_mhz: 2500,
        load_avg_1: 1.5,
        load_avg_5: 1.25,
        load_avg_15: 1.0,
    },
    memory: {
        total: 8 * 1024 ** 3,
        used: 4 * 1024 ** 3,
        available: 4 * 1024 ** 3,
        percent: 50,
        swap_percent: 10,
    },
    disk: {
        partitions: [
            { mountpoint: "/", used: 300, total: 1000, percent: 30 },
            { mountpoint: "/data", used: 900, total: 1000, percent: 90 },
        ],
        io: { read_bytes: 1024, write_bytes: 2048 },
    },
    network: {
        connections_count: 7,
        interfaces: [
            { name: "eth0", bytes_sent: 1000, bytes_recv: 2000, addrs: ["10.0.0.5"] },
            { name: "lo0", bytes_sent: 5, bytes_recv: 5, addrs: ["127.0.0.1"] },
        ],
    },
    sensors: {
        temperatures: [{ label: "Core 0", current: 91.4, high: null, critical: null }],
        fans: [{ label: "Fan 1", current: 2200 }],
        battery: { percent: 95, power_plugged: true },
    },
    users: [{ name: "amy", terminal: "ttys000" }],
    containers: {
        docker_available: true,
        lxc_available: false,
        containers: [
            { name: "web", status: "running", image: "nginx", runtime: "docker" },
            { name: "db", status: "exited", image: "postgres", runtime: "docker" },
        ],
    },
    processes: [
        { pid: 1, name: "init", username: "root", cpu_percent: 0.5, memory_percent: 0.1,
          memory_rss: 1024, num_threads: 1, status: "sleeping" },
        { pid: 2, name: "hog", username: "amy", cpu_percent: 95.0, memory_percent: 40.0,
          memory_rss: 1024 ** 3, num_threads: 12, status: "running" },
    ],
};

let sockets;

function bootDashboard() {
    document.documentElement.innerHTML = html;
    sockets = [];

    class FakeWebSocket {
        static OPEN = 1;
        static CONNECTING = 0;
        constructor(url) {
            this.url = url;
            this.readyState = FakeWebSocket.OPEN;
            this.sent = [];
            sockets.push(this);
        }
        send(data) {
            this.sent.push(data);
        }
        close() {
            this.readyState = 3;
        }
    }

    window.WebSocket = FakeWebSocket;
    window.Chart = class {
        constructor() {
            this.data = { datasets: [{}, {}] };
            this.options = {
                scales: { y: { grid: {}, ticks: {} } },
                plugins: { tooltip: {}, legend: { labels: {} } },
            };
        }
        update() {}
    };
    window.fetch = vi.fn(() => Promise.resolve({ json: () => Promise.resolve({}) }));

    // lib.js the way the browser loads it, then app.js.
    const ctx = { self: window, btoa: globalThis.btoa, atob: globalThis.atob };
    vm.createContext(ctx);
    vm.runInContext(libSource, ctx);
    window.eval(appSource);
    return sockets[0];
}

function deliver(ws, snapshot) {
    ws.onmessage({ data: JSON.stringify(snapshot) });
}

describe("dashboard render", () => {
    beforeEach(() => {
        bootDashboard();
    });

    it("connects and offers the API key as the first frame", () => {
        const ws = sockets[0];
        expect(ws.url).toContain("/ws/live");
        expect(ws.sent.length).toBe(0); // nothing sent before the socket opens
        ws.onopen();
        expect(ws.sent.length).toBe(1);
    });

    it("does not claim to be Live before the server has answered", () => {
        // Saying "Live" in onopen flashed a connected badge at a user whose
        // key was about to be rejected.
        sockets[0].onopen();
        expect(document.getElementById("status-badge").textContent).toBe("Authenticating…");
    });

    it("renders every card from one snapshot", () => {
        const ws = sockets[0];
        ws.onopen();
        deliver(ws, SNAPSHOT);

        expect(document.getElementById("hostname").textContent).toBe("test-host");
        expect(document.getElementById("uptime").textContent).toContain("1d 1h 1m");
        expect(document.getElementById("cpu-overall").textContent).toBe("42.5%");
        expect(document.getElementById("cpu-cores").textContent).toBe("2P / 2L");
        expect(document.getElementById("cpu-load").textContent).toBe("1.50 / 1.25 / 1.00");
        expect(document.getElementById("mem-overall").textContent).toBe("50.0%");
        expect(document.getElementById("mem-used").textContent).toBe("4.0 GB");
        expect(document.getElementById("net-connections").textContent).toBe("7 conn");
        expect(document.getElementById("user-count").textContent).toBe("1");
        expect(document.getElementById("container-count").textContent).toBe("1 / 2");
        // The host total, not the length of the truncated list.
        expect(document.getElementById("proc-count").textContent).toBe("604 processes");
        expect(document.getElementById("status-badge").textContent).toBe("Live");
    });

    it("hides the loading state once data arrives", () => {
        const ws = sockets[0];
        ws.onopen();
        expect(document.body.classList.contains("loading")).toBe(true);
        deliver(ws, SNAPSHOT);
        expect(document.body.classList.contains("loading")).toBe(false);
    });

    it("excludes loopback from the interface list", () => {
        const ws = sockets[0];
        ws.onopen();
        deliver(ws, SNAPSHOT);
        const names = [...document.querySelectorAll(".iface-name")].map((e) => e.textContent);
        expect(names).toEqual(["eth0"]);
    });

    it("colours a hot sensor as critical using the fallback scale", () => {
        const ws = sockets[0];
        ws.onopen();
        deliver(ws, SNAPSHOT);
        const value = document.querySelector("#temp-list .sensor-value");
        expect(value.textContent).toBe("91°C");
        expect(value.className).toContain("metric-crit");
    });

    it("marks a full partition critical and a quiet one fine", () => {
        const ws = sockets[0];
        ws.onopen();
        deliver(ws, SNAPSHOT);
        const pcts = [...document.querySelectorAll(".partition-pct")];
        expect(pcts[0].className).toContain("metric-ok");
        expect(pcts[1].className).toContain("metric-crit");
    });

    it("treats a threshold value itself as the higher severity", () => {
        // level_for_percent() is `pct < warn` -> good, so exactly 50 is warn.
        const ws = sockets[0];
        ws.onopen();
        deliver(ws, {
            ...SNAPSHOT,
            disk: {
                ...SNAPSHOT.disk,
                partitions: [{ mountpoint: "/", used: 500, total: 1000, percent: 50 }],
            },
        });
        expect(document.querySelector(".partition-pct").className).toContain("metric-warn");
    });

    it("distinguishes a running container from a stopped one", () => {
        const ws = sockets[0];
        ws.onopen();
        deliver(ws, SNAPSHOT);
        const statuses = [...document.querySelectorAll(".container-status")];
        expect(statuses[0].className).toContain("status-running");
        expect(statuses[1].className).toContain("status-stopped");
    });

    it("puts host-supplied text in the DOM as text, never as markup", () => {
        const ws = sockets[0];
        ws.onopen();
        deliver(ws, {
            ...SNAPSHOT,
            processes: [
                { pid: 1, name: "<img src=x onerror=alert(1)>", username: "root",
                  cpu_percent: 1, memory_percent: 1, memory_rss: 1, num_threads: 1,
                  status: "running" },
            ],
        });
        const cell = document.querySelector(".proc-name");
        expect(cell.textContent).toContain("<img src=x");
        expect(cell.querySelector("img")).toBeNull();
        expect(document.querySelectorAll("img").length).toBe(0);
    });

    it("survives a snapshot with every optional field missing", () => {
        const ws = sockets[0];
        ws.onopen();
        expect(() => deliver(ws, { timestamp: 1 })).not.toThrow();
    });

    it("shows a dash for load average when the platform reports none", () => {
        // Windows has no getloadavg; a partial tuple used to throw on toFixed.
        const ws = sockets[0];
        ws.onopen();
        deliver(ws, { ...SNAPSHOT, cpu: { ...SNAPSHOT.cpu, load_avg_5: null } });
        expect(document.getElementById("cpu-load").textContent).toBe("—");
    });

    it("offers a Retry button once the reconnect budget is spent", () => {
        const ws = sockets[0];
        ws.onopen();
        deliver(ws, SNAPSHOT);
        vi.useFakeTimers();
        try {
            for (let i = 0; i < 12; i++) {
                sockets[sockets.length - 1].onclose({ code: 1006 });
                vi.advanceTimersByTime(60000);
            }
        } finally {
            vi.useRealTimers();
        }
        const badge = document.getElementById("status-badge");
        expect(badge.textContent).toBe("Disconnected");
        expect(document.getElementById("retry-btn")).not.toBeNull();
    });

    it("asks for the key again when the server rejects it", () => {
        const ws = sockets[0];
        ws.onopen();
        ws.onclose({ code: 4001 });
        expect(document.getElementById("auth-modal").style.display).toBe("flex");
        // First attempt: the message must not blame a key rotation.
        expect(document.getElementById("auth-error").textContent).toContain("not accepted");
    });

    it("blames a rotation only after a previously working connection", () => {
        const ws = sockets[0];
        ws.onopen();
        deliver(ws, SNAPSHOT); // authentication actually succeeded
        ws.onclose({ code: 4001 });
        expect(document.getElementById("auth-error").textContent).toContain("may have changed");
    });
});
