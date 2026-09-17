/**
 * Unit tests for the dashboard's pure logic.
 *
 * app.js had none: the only "web tests" were Python assertions that grepped
 * the source for substrings, which break on any refactor and pass on any
 * behavioural regression. These exercise behaviour instead.
 */
import { describe, expect, it } from "vitest";

import lib from "../../sentinella/web/static/lib.js";

const PERCENT = [50, 80];
const TEMP = [70, 85];

describe("humanBytes", () => {
    it("scales through the units", () => {
        expect(lib.humanBytes(0)).toBe("0.0 B");
        expect(lib.humanBytes(1023)).toBe("1023.0 B");
        expect(lib.humanBytes(1024)).toBe("1.0 KB");
        expect(lib.humanBytes(1536)).toBe("1.5 KB");
        expect(lib.humanBytes(1024 ** 3)).toBe("1.0 GB");
    });

    it("stops at the largest unit rather than running off the end", () => {
        expect(lib.humanBytes(1024 ** 6)).toBe("1048576.0 TB");
    });

    it("handles a missing value as zero", () => {
        expect(lib.humanBytes(undefined)).toBe("0.0 B");
        expect(lib.humanBytes(null)).toBe("0.0 B");
    });
});

describe("humanBytesCompact", () => {
    it("drops the decimal above 1 unit, keeps it below", () => {
        expect(lib.humanBytesCompact(1024 * 83)).toBe("83KB");
        expect(lib.humanBytesCompact(0)).toBe("0.0B");
    });
});

describe("computeRate", () => {
    it("is bytes per second, not bytes per refresh", () => {
        expect(lib.computeRate(3000, 1000, 2)).toBe(1000);
    });

    it("returns 0 without a usable interval", () => {
        // No baseline yet, or two samples in the same instant.
        expect(lib.computeRate(3000, 1000, 0)).toBe(0);
        expect(lib.computeRate(3000, 1000, 0.05)).toBe(0);
    });

    it("treats a counter reset as zero, never a negative rate", () => {
        // Interface reconnect or agent restart rewinds the cumulative counter.
        expect(lib.computeRate(10, 999999, 2)).toBe(0);
    });
});

describe("formatUptime", () => {
    it("omits days below 24h and includes them above", () => {
        expect(lib.formatUptime(3661)).toBe("1h 1m");
        expect(lib.formatUptime(90061)).toBe("1d 1h 1m");
    });

    it("does not produce negative components", () => {
        expect(lib.formatUptime(-5)).toBe("0h 0m");
    });
});

describe("severity classes", () => {
    it("follows the agent's percentage thresholds", () => {
        expect(lib.classForPercent(49.9, PERCENT)).toBe("metric-ok");
        expect(lib.classForPercent(50, PERCENT)).toBe("metric-warn");
        expect(lib.classForPercent(79.9, PERCENT)).toBe("metric-warn");
        expect(lib.classForPercent(80, PERCENT)).toBe("metric-crit");
    });

    it("uses whatever thresholds it is given, not baked-in ones", () => {
        expect(lib.classForPercent(60, [90, 95])).toBe("metric-ok");
    });

    it("prefers a sensor's own trip points over the fallback scale", () => {
        // 55 °C is "warn" on a 50/80 percentage scale but well under this
        // sensor's 90 °C high point.
        expect(lib.classForTemp(55, 90, 100, TEMP)).toBe("metric-ok");
        expect(lib.classForTemp(95, 90, 100, TEMP)).toBe("metric-warn");
        expect(lib.classForTemp(101, 90, 100, TEMP)).toBe("metric-crit");
    });

    it("falls back to a fixed scale when the sensor reports no trip points", () => {
        expect(lib.classForTemp(40, null, null, TEMP)).toBe("metric-ok");
        expect(lib.classForTemp(75, null, null, TEMP)).toBe("metric-warn");
        expect(lib.classForTemp(90, null, null, TEMP)).toBe("metric-crit");
    });
});

describe("process sorting", () => {
    const procs = [
        { pid: 3, name: "beta", username: "root", cpu_percent: 1, memory_rss: 300 },
        { pid: 1, name: "Alpha", username: "amy", cpu_percent: 9, memory_rss: 100 },
        { pid: 2, name: "gamma", username: "zoe", cpu_percent: 5, memory_rss: 200 },
    ];

    it("matches PROCESS_SORT_KEYS' default directions", () => {
        expect(lib.SORT_KEYS.pid.descending).toBe(false);
        expect(lib.SORT_KEYS.name.descending).toBe(false);
        expect(lib.SORT_KEYS.cpu.descending).toBe(true);
        expect(lib.SORT_KEYS.memory.descending).toBe(true);
    });

    it("compares names case-insensitively, like sort_processes()", () => {
        // Sorting on raw values would put "Alpha" after "beta" here and make
        // the two dashboards interleave differently on the same host.
        const names = lib.sortProcesses(procs, "name", true).map((p) => p.name);
        expect(names).toEqual(["Alpha", "beta", "gamma"]);
    });

    it("sorts numerics in both directions", () => {
        expect(lib.sortProcesses(procs, "cpu", false).map((p) => p.cpu_percent)).toEqual([9, 5, 1]);
        expect(lib.sortProcesses(procs, "pid", true).map((p) => p.pid)).toEqual([1, 2, 3]);
    });

    it("does not mutate the caller's array", () => {
        const original = [...procs];
        lib.sortProcesses(procs, "pid", true);
        expect(procs).toEqual(original);
    });

    it("falls back to the default key for an unknown one", () => {
        expect(lib.sortProcesses(procs, "nonsense", false).map((p) => p.cpu_percent)).toEqual([
            9, 5, 1,
        ]);
    });

    it("treats missing fields as zero rather than throwing", () => {
        expect(() => lib.sortProcesses([{}, { pid: 1 }], "rss", false)).not.toThrow();
    });
});

describe("isRunningContainer", () => {
    it("accepts every spelling the runtimes use", () => {
        expect(lib.isRunningContainer({ status: "running" })).toBe(true);
        expect(lib.isRunningContainer({ status: "up" })).toBe(true);
        expect(lib.isRunningContainer({ status: "Running" })).toBe(true);
    });

    it("rejects everything else, including missing data", () => {
        expect(lib.isRunningContainer({ status: "exited" })).toBe(false);
        expect(lib.isRunningContainer({})).toBe(false);
        expect(lib.isRunningContainer(null)).toBe(false);
    });
});

describe("reconnectDelay", () => {
    it("backs off exponentially and caps", () => {
        expect(lib.reconnectDelay(0, 3000, 30000, 10)).toBe(3000);
        expect(lib.reconnectDelay(1, 3000, 30000, 10)).toBe(6000);
        expect(lib.reconnectDelay(3, 3000, 30000, 10)).toBe(24000);
        expect(lib.reconnectDelay(4, 3000, 30000, 10)).toBe(30000);
        expect(lib.reconnectDelay(9, 3000, 30000, 10)).toBe(30000);
    });

    it("gives up once the attempt budget is spent", () => {
        // null is the signal to stop retrying and offer the Retry button.
        expect(lib.reconnectDelay(10, 3000, 30000, 10)).toBeNull();
    });
});

describe("API key obfuscation", () => {
    it("round-trips, including non-ASCII keys", () => {
        for (const key of ["hunter2", "sécret-ké", "a", "x".repeat(200)]) {
            expect(lib.deobfuscateKey(lib.obfuscateKey(key))).toBe(key);
        }
    });

    it("does not leave the key readable in storage", () => {
        expect(lib.obfuscateKey("hunter2")).not.toContain("hunter2");
    });

    it("is a no-op on empty input", () => {
        expect(lib.obfuscateKey("")).toBe("");
        expect(lib.deobfuscateKey("")).toBe("");
    });

    it("returns the input unchanged when it is not valid base64", () => {
        // A value written by an older build, or hand-edited storage.
        expect(lib.deobfuscateKey("not base64!!")).toBe("not base64!!");
    });
});
