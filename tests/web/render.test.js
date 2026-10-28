/**
 * End-to-end-ish render test.
 *
 * Loads the real page, runs the real app.js against a stubbed WebSocket, and
 * feeds it a snapshot — the closest thing to opening the dashboard without a
 * browser. This is what would have caught a card update reaching for an id
 * that isn't in the markup.
 */
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import vm from 'node:vm';

import { beforeEach, describe, expect, it, vi } from 'vitest';

const STATIC = path.join(
    path.dirname(fileURLToPath(import.meta.url)),
    '../../sentinella/web/static',
);
const html = readFileSync(path.join(STATIC, 'index.html'), 'utf8');
const libSource = readFileSync(path.join(STATIC, 'lib.js'), 'utf8');
const cssSource = readFileSync(path.join(STATIC, 'style.css'), 'utf8');

const SNAPSHOT = {
    timestamp: 1700000000,
    process_count: 604,
    system_info: {
        hostname: 'test-host',
        os_name: 'Linux',
        os_version: '6.1',
        architecture: 'x86_64',
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
            { mountpoint: '/', used: 300, total: 1000, percent: 30 },
            { mountpoint: '/data', used: 900, total: 1000, percent: 90 },
        ],
        io: { read_bytes: 1024, write_bytes: 2048 },
    },
    network: {
        connections_count: 7,
        interfaces: [
            { name: 'eth0', bytes_sent: 1000, bytes_recv: 2000, addrs: ['10.0.0.5'] },
            { name: 'lo0', bytes_sent: 5, bytes_recv: 5, addrs: ['127.0.0.1'] },
        ],
    },
    sensors: {
        temperatures: [{ label: 'Core 0', current: 91.4, high: null, critical: null }],
        fans: [{ label: 'Fan 1', current: 2200 }],
        battery: { percent: 95, power_plugged: true },
    },
    users: [{ name: 'amy', terminal: 'ttys000' }],
    containers: {
        docker_available: true,
        lxc_available: false,
        containers: [
            { name: 'web', status: 'running', image: 'nginx', runtime: 'docker' },
            { name: 'db', status: 'exited', image: 'postgres', runtime: 'docker' },
        ],
    },
    processes: [
        {
            pid: 1,
            name: 'init',
            username: 'root',
            cpu_percent: 0.5,
            memory_percent: 0.1,
            memory_rss: 1024,
            num_threads: 1,
            status: 'sleeping',
        },
        {
            pid: 2,
            name: 'hog',
            username: 'amy',
            cpu_percent: 95.0,
            memory_percent: 40.0,
            memory_rss: 1024 ** 3,
            num_threads: 12,
            status: 'running',
        },
    ],
};

// What /health sends: the agent's PROCESS_SORT_KEYS, as definitions.
const AGENT_SORT_KEYS = {
    cpu: { field: 'cpu_percent', descending: true },
    memory: { field: 'memory_percent', descending: true },
    pid: { field: 'pid', descending: false },
    name: { field: 'name', descending: false },
};

let sockets;
let charts;

// Imported as a module (fresh for every boot) rather than eval'd as text, so
// the coverage run can attribute what runs to app.js.
async function bootDashboard({ withStylesheet = false, health = {} } = {}) {
    document.documentElement.innerHTML = html;
    if (withStylesheet) {
        // The page links style.css, which jsdom doesn't fetch; inline it.
        const style = document.createElement('style');
        style.textContent = cssSource;
        document.head.appendChild(style);
    }
    sockets = [];
    charts = [];

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
        constructor(_ctx, config) {
            this.data = config.data;
            this.options = config.options;
            charts.push(this);
        }
        update() {}
    };
    window.fetch = vi.fn(() => Promise.resolve({ json: () => Promise.resolve(health) }));

    // lib.js the way the browser loads it, then app.js.
    const ctx = { self: window, btoa: globalThis.btoa, atob: globalThis.atob };
    vm.createContext(ctx);
    vm.runInContext(libSource, ctx);
    vi.resetModules();
    await import('../../sentinella/web/static/app.js');
    return sockets[0];
}

function deliver(ws, snapshot) {
    ws.onmessage({ data: JSON.stringify(snapshot) });
}

describe('dashboard render', () => {
    beforeEach(async () => {
        await bootDashboard();
    });

    it('connects and offers the API key as the first frame', () => {
        const ws = sockets[0];
        expect(ws.url).toContain('/ws/live');
        expect(ws.sent.length).toBe(0); // nothing sent before the socket opens
        ws.onopen();
        expect(ws.sent.length).toBe(1);
    });

    it('does not claim to be Live before the server has answered', () => {
        // Saying "Live" in onopen flashed a connected badge at a user whose
        // key was about to be rejected.
        sockets[0].onopen();
        expect(document.getElementById('status-badge').textContent).toBe('Authenticating…');
    });

    it('renders every card from one snapshot', () => {
        const ws = sockets[0];
        ws.onopen();
        deliver(ws, SNAPSHOT);

        expect(document.getElementById('hostname').textContent).toBe('test-host');
        expect(document.getElementById('uptime').textContent).toContain('1d 1h 1m');
        expect(document.getElementById('cpu-overall').textContent).toBe('42.5%');
        expect(document.getElementById('cpu-cores').textContent).toBe('2P / 2L');
        expect(document.getElementById('cpu-load').textContent).toBe('1.50 / 1.25 / 1.00');
        expect(document.getElementById('mem-overall').textContent).toBe('50.0%');
        expect(document.getElementById('mem-used').textContent).toBe('4.0 GB');
        expect(document.getElementById('net-connections').textContent).toBe('7 conn');
        expect(document.getElementById('user-count').textContent).toBe('1');
        expect(document.getElementById('container-count').textContent).toBe('1 / 2');
        // The host total, not the length of the truncated list.
        expect(document.getElementById('proc-count').textContent).toBe('604 processes');
        expect(document.getElementById('status-badge').textContent).toBe('Live');
    });

    it('hides the loading state once data arrives', () => {
        const ws = sockets[0];
        ws.onopen();
        expect(document.body.classList.contains('loading')).toBe(true);
        deliver(ws, SNAPSHOT);
        expect(document.body.classList.contains('loading')).toBe(false);
    });

    it('excludes loopback from the interface list', () => {
        const ws = sockets[0];
        ws.onopen();
        deliver(ws, SNAPSHOT);
        const names = [...document.querySelectorAll('.iface-name')].map((e) => e.textContent);
        expect(names).toEqual(['eth0']);
    });

    it('colours a hot sensor as critical using the fallback scale', () => {
        const ws = sockets[0];
        ws.onopen();
        deliver(ws, SNAPSHOT);
        const value = document.querySelector('#temp-list .sensor-value');
        expect(value.textContent).toBe('91°C');
        expect(value.className).toContain('metric-crit');
    });

    it('marks a full partition critical and a quiet one fine', () => {
        const ws = sockets[0];
        ws.onopen();
        deliver(ws, SNAPSHOT);
        const pcts = [...document.querySelectorAll('.partition-pct')];
        expect(pcts[0].className).toContain('metric-ok');
        expect(pcts[1].className).toContain('metric-crit');
    });

    it('treats a threshold value itself as the higher severity', () => {
        // level_for_percent() is `pct < warn` -> good, so exactly 50 is warn.
        const ws = sockets[0];
        ws.onopen();
        deliver(ws, {
            ...SNAPSHOT,
            disk: {
                ...SNAPSHOT.disk,
                partitions: [{ mountpoint: '/', used: 500, total: 1000, percent: 50 }],
            },
        });
        expect(document.querySelector('.partition-pct').className).toContain('metric-warn');
    });

    it('distinguishes a running container from a stopped one', () => {
        const ws = sockets[0];
        ws.onopen();
        deliver(ws, SNAPSHOT);
        const statuses = [...document.querySelectorAll('.container-status')];
        expect(statuses[0].className).toContain('status-running');
        expect(statuses[1].className).toContain('status-stopped');
    });

    it('puts host-supplied text in the DOM as text, never as markup', () => {
        const ws = sockets[0];
        ws.onopen();
        const existingImages = [...document.querySelectorAll('img')];
        deliver(ws, {
            ...SNAPSHOT,
            processes: [
                {
                    pid: 1,
                    name: '<img src=x onerror=alert(1)>',
                    username: 'root',
                    cpu_percent: 1,
                    memory_percent: 1,
                    memory_rss: 1,
                    num_threads: 1,
                    status: 'running',
                },
            ],
        });
        const cell = document.querySelector('.proc-name');
        expect(cell.textContent).toContain('<img src=x');
        expect(cell.querySelector('img')).toBeNull();
        expect([...document.querySelectorAll('img')]).toEqual(existingImages);
    });

    it('survives a snapshot with every optional field missing', () => {
        const ws = sockets[0];
        ws.onopen();
        expect(() => deliver(ws, { timestamp: 1 })).not.toThrow();
    });

    it('shows a dash for load average when the platform reports none', () => {
        // Windows has no getloadavg; a partial tuple used to throw on toFixed.
        const ws = sockets[0];
        ws.onopen();
        deliver(ws, { ...SNAPSHOT, cpu: { ...SNAPSHOT.cpu, load_avg_5: null } });
        expect(document.getElementById('cpu-load').textContent).toBe('—');
    });

    it('offers a Retry button once the reconnect budget is spent', () => {
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
        const badge = document.getElementById('status-badge');
        expect(badge.textContent).toBe('Disconnected');
        expect(document.getElementById('retry-btn')).not.toBeNull();
    });

    it('asks for the key again when the server rejects it', () => {
        const ws = sockets[0];
        ws.onopen();
        ws.onclose({ code: 4001 });
        expect(document.getElementById('auth-modal').hidden).toBe(false);
        // First attempt: the message must not blame a key rotation.
        expect(document.getElementById('auth-error').textContent).toContain('not accepted');
    });

    it('blames a rotation only after a previously working connection', () => {
        const ws = sockets[0];
        ws.onopen();
        deliver(ws, SNAPSHOT); // authentication actually succeeded
        ws.onclose({ code: 4001 });
        expect(document.getElementById('auth-error').textContent).toContain('may have changed');
    });

    it('says Live again after a reconnect', () => {
        // The badge only flipped to Live on the first frame of the page's
        // life, so every later reconnect left it on "Authenticating…" while
        // data was flowing.
        vi.useFakeTimers();
        try {
            const first = sockets[0];
            first.onopen();
            deliver(first, SNAPSHOT);
            first.onclose({ code: 1006 });
            vi.advanceTimersByTime(60000);
        } finally {
            vi.useRealTimers();
        }
        const second = sockets[sockets.length - 1];
        expect(second).not.toBe(sockets[0]);
        second.onopen();
        const badge = document.getElementById('status-badge');
        expect(badge.textContent).toBe('Authenticating…');
        deliver(second, SNAPSHOT);
        expect(badge.textContent).toBe('Live');
        expect(badge.classList.contains('connected')).toBe(true);
    });

    it('hides the auth modal when the connection opens', () => {
        const ws = sockets[0];
        ws.onopen();
        ws.onclose({ code: 4001 });
        expect(document.getElementById('auth-modal').hidden).toBe(false);
        expect(document.getElementById('auth-error').hidden).toBe(false);
        sockets[sockets.length - 1].onopen();
        expect(document.getElementById('auth-modal').hidden).toBe(true);
    });

    it('does not claim to be reconnecting before the first connection', () => {
        expect(document.getElementById('conn-toast').hidden).toBe(true);
    });

    it('shows the reconnecting toast only while a reconnect is pending', () => {
        vi.useFakeTimers();
        try {
            const ws = sockets[0];
            ws.onopen();
            deliver(ws, SNAPSHOT);
            const toast = document.getElementById('conn-toast');
            expect(toast.hidden).toBe(true);
            ws.onclose({ code: 1006 });
            expect(toast.hidden).toBe(false);
            expect(toast.textContent).toContain('Reconnecting');
            vi.advanceTimersByTime(60000);
            const next = sockets[sockets.length - 1];
            next.onopen();
            deliver(next, SNAPSHOT);
            expect(toast.hidden).toBe(true);
        } finally {
            vi.useRealTimers();
        }
    });

    it('does not say it is reconnecting when it is waiting for a key', () => {
        const ws = sockets[0];
        ws.onopen();
        ws.onclose({ code: 4001 });
        expect(document.getElementById('conn-toast').hidden).toBe(true);
    });

    it('keeps saying the server is busy while it waits to retry', () => {
        vi.useFakeTimers();
        try {
            const ws = sockets[0];
            ws.onopen();
            ws.onclose({ code: 1013 });
            expect(document.getElementById('status-badge').textContent).toMatch(/busy/i);
        } finally {
            vi.useRealTimers();
        }
    });

    it("does not let a stale socket's error close its replacement", () => {
        vi.useFakeTimers();
        try {
            const first = sockets[0];
            first.onopen();
            first.onclose({ code: 1006 });
            vi.advanceTimersByTime(60000);
            const second = sockets[sockets.length - 1];
            expect(second).not.toBe(first);
            first.onerror({ target: first });
            expect(second.readyState).toBe(1);
        } finally {
            vi.useRealTimers();
        }
    });

    it('counts people, not sessions, in the users card', () => {
        const ws = sockets[0];
        ws.onopen();
        deliver(ws, {
            ...SNAPSHOT,
            users: [
                { name: 'amy', terminal: 'ttys000' },
                { name: 'amy', terminal: 'ttys001' },
                { name: 'bob', terminal: 'ttys002' },
            ],
        });
        expect(document.getElementById('user-count').textContent).toBe('2');
        expect(document.querySelectorAll('.user-row').length).toBe(2);
    });

    it("colours the RAM bar from the agent's thresholds", () => {
        const ws = sockets[0];
        ws.onopen();
        // 80 is the default crit threshold; level_for_percent() treats the
        // threshold itself as the higher severity.
        deliver(ws, { ...SNAPSHOT, memory: { ...SNAPSHOT.memory, percent: 80 } });
        expect(document.getElementById('ram-bar').classList.contains('warn')).toBe(true);
        deliver(ws, { ...SNAPSHOT, memory: { ...SNAPSHOT.memory, percent: 79.9 } });
        expect(document.getElementById('ram-bar').classList.contains('warn')).toBe(false);
    });

    it("renders the other cards when one card's data is malformed", () => {
        const ws = sockets[0];
        ws.onopen();
        const errors = vi.spyOn(console, 'error').mockImplementation(() => {});
        try {
            deliver(ws, {
                ...SNAPSHOT,
                sensors: { temperatures: [{ label: 'bad', current: null }] },
            });
        } finally {
            errors.mockRestore();
        }
        expect(document.getElementById('proc-count').textContent).toBe('604 processes');
        expect(document.getElementById('container-count').textContent).toBe('1 / 2');
    });

    it('shows an unknown connection count as a dash, not zero', () => {
        const ws = sockets[0];
        ws.onopen();
        deliver(ws, { ...SNAPSHOT, network: { ...SNAPSHOT.network, connections_count: null } });
        expect(document.getElementById('net-connections').textContent).toBe('— conn');
    });
});

describe('dashboard theming and resilience', () => {
    const token = (name) =>
        getComputedStyle(document.body).getPropertyValue(`--chart-${name}`).trim();

    it('takes chart colours from the stylesheet, in both themes', async () => {
        await bootDashboard({ withStylesheet: true });
        const [cpu, mem, net] = charts;
        expect(token('cpu')).not.toBe('');
        expect(cpu.data.datasets[0].borderColor).toBe(token('cpu'));
        expect(mem.data.datasets[0].borderColor).toBe(token('mem'));
        expect(net.data.datasets[1].borderColor).toBe(token('recv'));
        expect(cpu.options.scales.y.grid.color).toBe(token('grid'));
        const dark = token('cpu');

        document.getElementById('theme-toggle').click();

        expect(token('cpu')).not.toBe(dark);
        expect(cpu.data.datasets[0].borderColor).toBe(token('cpu'));
        expect(cpu.data.datasets[0].backgroundColor).toBe(token('cpu-fill'));
        expect(cpu.options.plugins.tooltip.titleColor).toBe(token('tooltip-title'));
    });

    it("reports the theme toggle's state to assistive technology", async () => {
        await bootDashboard();
        const toggle = document.getElementById('theme-toggle');
        expect(toggle.getAttribute('aria-pressed')).toBe('false');
        toggle.click();
        expect(toggle.getAttribute('aria-pressed')).toBe('true');
        toggle.click();
        expect(toggle.getAttribute('aria-pressed')).toBe('false');
    });

    it('still works when the browser blocks storage', async () => {
        // Some privacy modes throw on any access to localStorage/sessionStorage.
        const blocked = () => {
            throw new DOMException('The operation is insecure.', 'SecurityError');
        };
        const saved = {};
        for (const name of ['localStorage', 'sessionStorage']) {
            saved[name] = Object.getOwnPropertyDescriptor(window, name);
            Object.defineProperty(window, name, { get: blocked, configurable: true });
        }
        try {
            await bootDashboard(); // throws, failing the test, if storage access escapes
            expect(() => document.getElementById('theme-toggle').click()).not.toThrow();
            const ws = sockets[0];
            expect(() => ws.onopen()).not.toThrow();
            expect(() => ws.onclose({ code: 4001 })).not.toThrow();
            document.getElementById('auth-key-input').value = 'k';
            const submit = new Event('submit', { cancelable: true });
            expect(() => document.getElementById('auth-form').dispatchEvent(submit)).not.toThrow();
        } finally {
            for (const [name, descriptor] of Object.entries(saved)) {
                Object.defineProperty(window, name, descriptor);
            }
        }
    });

    it("colours readings by the agent's thresholds, not its own", async () => {
        // With the default 50/80 scale a 15% partition is "ok"; this agent
        // says 10/20, so the dashboard must call it "warn".
        await bootDashboard({ health: { thresholds: { percent: [10, 20], temp: [70, 85] } } });
        const ws = sockets[0];
        ws.onopen();
        await new Promise((resolve) => setTimeout(resolve, 0));
        deliver(ws, {
            ...SNAPSHOT,
            disk: {
                ...SNAPSHOT.disk,
                partitions: [{ mountpoint: '/', used: 15, total: 100, percent: 15 }],
            },
        });
        expect(document.querySelector('.partition-pct').className).toContain('metric-warn');
    });

    it('says when the agent speaks a different API version', async () => {
        const warn = vi.spyOn(console, 'warn').mockImplementation(() => {});
        try {
            await bootDashboard({ health: { version: '9.0.0', api_version: 99 } });
            sockets[0].onopen();
            await new Promise((resolve) => setTimeout(resolve, 0));
            const footer = document.getElementById('app-version').textContent;
            expect(footer).toContain('v9.0.0');
            expect(footer).toMatch(/API v99/);
            expect(warn).toHaveBeenCalled();
        } finally {
            warn.mockRestore();
        }
    });

    it('stays quiet when the API versions match', async () => {
        const warn = vi.spyOn(console, 'warn').mockImplementation(() => {});
        try {
            await bootDashboard({ health: { version: '1.0.0', api_version: 1 } });
            sockets[0].onopen();
            await new Promise((resolve) => setTimeout(resolve, 0));
            expect(document.getElementById('app-version').textContent).toBe('v1.0.0');
            expect(warn).not.toHaveBeenCalled();
        } finally {
            warn.mockRestore();
        }
    });
});

describe('live status, rates and notes', () => {
    const withDisk = (timestamp, read, write) => ({
        ...SNAPSHOT,
        timestamp,
        disk: { ...SNAPSHOT.disk, io: { read_bytes: read, write_bytes: write } },
    });

    it('shows disk I/O as a rate over snapshot time, totals on hover', async () => {
        await bootDashboard();
        const ws = sockets[0];
        ws.onopen();
        deliver(ws, withDisk(100, 0, 0));
        const read = document.getElementById('disk-read');
        expect(read.textContent).toBe('—'); // one sample is not a rate
        deliver(ws, withDisk(102, 2048, 4096));
        expect(read.textContent).toBe('1.0 KB/s');
        expect(document.getElementById('disk-write').textContent).toBe('2.0 KB/s');
        expect(read.title).toMatch(/since boot/i);
        // The same snapshot again (cached on the agent) keeps the last rate.
        deliver(ws, withDisk(102, 2048, 4096));
        expect(read.textContent).toBe('1.0 KB/s');
    });

    it("stamps 'Last update' with when the agent collected the data", async () => {
        await bootDashboard();
        const ws = sockets[0];
        ws.onopen();
        deliver(ws, { ...SNAPSHOT, timestamp: 1700000000 });
        expect(document.getElementById('last-update').textContent).toBe(
            'Last update: ' + new Date(1700000000 * 1000).toLocaleTimeString(),
        );
    });

    it('flags data that has stopped advancing, and clears it when it moves', async () => {
        vi.useFakeTimers();
        try {
            await bootDashboard({ health: { refresh_interval: 1 } });
            const ws = sockets[0];
            ws.onopen();
            await vi.advanceTimersByTimeAsync(0); // let /health land
            deliver(ws, { ...SNAPSHOT, timestamp: 1 });
            const badge = document.getElementById('status-badge');
            expect(badge.textContent).toBe('Live');

            // Frames keep arriving but carry the same snapshot: a stuck collector.
            for (let i = 0; i < 5; i++) {
                await vi.advanceTimersByTimeAsync(1000);
                deliver(ws, { ...SNAPSHOT, timestamp: 1 });
            }
            expect(badge.textContent).toBe('Stale');
            expect(badge.title).toMatch(/no new data/i);
            expect(document.body.classList.contains('stale')).toBe(true);

            deliver(ws, { ...SNAPSHOT, timestamp: 2 });
            expect(badge.textContent).toBe('Live');
            expect(document.body.classList.contains('stale')).toBe(false);
        } finally {
            vi.useRealTimers();
        }
    });

    it('still notices a stall that starts right after a reconnect', async () => {
        vi.useFakeTimers();
        try {
            await bootDashboard({ health: { refresh_interval: 1 } });
            const first = sockets[0];
            first.onopen();
            await vi.advanceTimersByTimeAsync(0);
            deliver(first, { ...SNAPSHOT, timestamp: 7 });
            first.onclose({ code: 1006 });
            await vi.advanceTimersByTimeAsync(3000); // reconnect backoff
            const second = sockets[sockets.length - 1];
            second.onopen();
            await vi.advanceTimersByTimeAsync(0);
            // The same snapshot as before the drop, and then nothing new.
            deliver(second, { ...SNAPSHOT, timestamp: 7 });
            await vi.advanceTimersByTimeAsync(6000);
            expect(document.getElementById('status-badge').textContent).toBe('Stale');
        } finally {
            vi.useRealTimers();
        }
    });

    it('does not call a steadily updating stream stale', async () => {
        vi.useFakeTimers();
        try {
            await bootDashboard({ health: { refresh_interval: 1 } });
            const ws = sockets[0];
            ws.onopen();
            await vi.advanceTimersByTimeAsync(0);
            for (let t = 1; t <= 8; t++) {
                deliver(ws, { ...SNAPSHOT, timestamp: t });
                await vi.advanceTimersByTimeAsync(1000);
            }
            expect(document.getElementById('status-badge').textContent).toBe('Live');
        } finally {
            vi.useRealTimers();
        }
    });

    async function noteAfterSortingBy(key) {
        await bootDashboard({
            health: { sort_by: 'cpu', sort_keys: AGENT_SORT_KEYS },
        });
        const ws = sockets[0];
        ws.onopen();
        await new Promise((resolve) => setTimeout(resolve, 0));
        deliver(ws, SNAPSHOT); // 2 rows of 604
        document.querySelector(`#process-table th[data-sort="${key}"]`).click();
        return document.getElementById('process-note').textContent;
    }

    it('points to the setting when the agent could rank by the chosen key', async () => {
        const note = await noteAfterSortingBy('memory');
        expect(note).toContain('2 of 604');
        expect(note).toContain('processes.sort_by = "memory"');
    });

    it('says plainly when the agent cannot rank by the chosen key', async () => {
        const note = await noteAfterSortingBy('rss');
        expect(note).toContain('2 of 604');
        expect(note).toMatch(/can.t rank by rss/i);
    });
});

describe('definitions from the agent', () => {
    const pids = () => [...document.querySelectorAll('.proc-pid')].map((td) => td.textContent);

    it("keeps the agent's order until /health says how to sort", async () => {
        await bootDashboard(); // this /health answers with nothing
        const ws = sockets[0];
        ws.onopen();
        deliver(ws, SNAPSHOT); // pid 1 (0.5% CPU), then pid 2 (95% CPU)
        expect(pids()).toEqual(['1', '2']);
    });

    it("sorts by the agent's configured key, with the agent's direction", async () => {
        await bootDashboard({ health: { sort_by: 'memory', sort_keys: AGENT_SORT_KEYS } });
        const ws = sockets[0];
        ws.onopen();
        await new Promise((resolve) => setTimeout(resolve, 0));
        deliver(ws, SNAPSHOT);
        expect(pids()).toEqual(['2', '1']); // memory, descending
        const header = document.querySelector('#process-table th[data-sort="memory"]');
        expect(header.getAttribute('aria-sort')).toBe('descending');
    });

    it("greys out the cards of modules the agent doesn't monitor", async () => {
        await bootDashboard({ health: { active_modules: ['cpu', 'processes'] } });
        sockets[0].onopen();
        await new Promise((resolve) => setTimeout(resolve, 0));
        const disabled = (id) => document.getElementById(id).classList.contains('module-disabled');
        expect(disabled('cpu-card')).toBe(false);
        expect(disabled('process-card')).toBe(false);
        expect(disabled('memory-card')).toBe(true);
        expect(disabled('containers-card')).toBe(true);
    });
});
