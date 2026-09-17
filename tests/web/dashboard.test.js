/**
 * DOM-level tests for the dashboard shell.
 *
 * These load the real index.html and lib.js into jsdom, so they catch the
 * wiring mistakes a pure-logic test cannot: a helper app.js expects that lib.js
 * does not export, or a markup id that a card update reaches for.
 */
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import path from "node:path";
import vm from "node:vm";

import { beforeEach, describe, expect, it } from "vitest";

const STATIC = path.join(
    path.dirname(fileURLToPath(import.meta.url)),
    "../../sentinella/web/static"
);

const html = readFileSync(path.join(STATIC, "index.html"), "utf8");
const libSource = readFileSync(path.join(STATIC, "lib.js"), "utf8");
const appSource = readFileSync(path.join(STATIC, "app.js"), "utf8");

/** Evaluate lib.js the way a browser does — no `module`, so it sets window. */
function loadLibIntoWindow(win) {
    // The UMD factory assigns onto `self`, which here *is* the jsdom window —
    // so read the result off the window rather than off the vm context.
    const ctx = { self: win, btoa: win.btoa ?? globalThis.btoa, atob: win.atob ?? globalThis.atob };
    vm.createContext(ctx);
    vm.runInContext(libSource, ctx);
    return win.SentinellaLib;
}

describe("page markup", () => {
    beforeEach(() => {
        document.documentElement.innerHTML = html;
    });

    it("loads lib.js before app.js, which depends on it", () => {
        const scripts = [...document.querySelectorAll("script[src]")].map((s) =>
            s.getAttribute("src")
        );
        expect(scripts).toContain("/static/lib.js");
        expect(scripts.indexOf("/static/lib.js")).toBeLessThan(scripts.indexOf("/static/app.js"));
    });

    it("applies the saved theme before any renderable markup", () => {
        // Otherwise a light-theme user sees a dark flash on every load.
        const bodyStart = html.indexOf("<body");
        const themeScript = html.indexOf("theme-init.js");
        expect(themeScript).toBeGreaterThan(bodyStart);
        const preamble = html.slice(bodyStart, themeScript);
        expect(preamble).not.toContain("<header");
        expect(preamble).not.toContain("<main");
    });

    it("gives every element the update functions reach for", () => {
        const required = [
            "hostname", "os-info", "uptime", "clock",
            "cpu-overall", "cpu-cores", "cpu-freq", "cpu-load", "cpu-core-bars",
            "mem-overall", "mem-used", "mem-total", "mem-avail", "ram-bar", "swap-bar",
            "net-connections", "net-interfaces", "disk-partitions", "disk-read", "disk-write",
            "temp-list", "fan-list", "battery-info", "user-count", "user-list",
            "container-count", "container-list", "proc-count", "process-tbody",
            "status-badge", "last-update", "app-version", "auth-modal", "auth-form",
        ];
        for (const id of required) {
            expect(document.getElementById(id), `missing #${id}`).not.toBeNull();
        }
    });

    it("pairs every truncatable list with its '+N more' note", () => {
        for (const listId of [
            "net-interfaces", "disk-partitions", "temp-list",
            "fan-list", "user-list", "container-list",
        ]) {
            expect(
                document.getElementById(`${listId}-more`),
                `#${listId} has no -more sibling, so truncation would be silent`
            ).not.toBeNull();
        }
    });

    it("keeps the auth modal accessible to keyboard and screen readers", () => {
        const dialog = document.querySelector("#auth-modal .modal-content");
        expect(dialog.getAttribute("role")).toBe("dialog");
        expect(dialog.getAttribute("aria-modal")).toBe("true");
        expect(dialog.getAttribute("aria-labelledby")).toBe("auth-modal-title");
        expect(document.getElementById("auth-modal-title")).not.toBeNull();
        expect(appSource).toContain("trapAuthModalFocus");
    });

    it("marks sortable columns as buttons and leaves Status alone", () => {
        const sortable = [...document.querySelectorAll("#process-table th[data-sort]")];
        expect(sortable.length).toBeGreaterThan(0);
        for (const th of sortable) {
            expect(th.getAttribute("tabindex")).toBe("0");
            expect(th.getAttribute("role")).toBe("button");
        }
        const statusHeader = [...document.querySelectorAll("#process-table th")].at(-1);
        expect(statusHeader.hasAttribute("data-sort")).toBe(false);
    });
});

describe("app.js / lib.js contract", () => {
    it("exports everything app.js destructures from SentinellaLib", () => {
        const lib = loadLibIntoWindow(window);
        const block = appSource.slice(
            appSource.indexOf("const {"),
            appSource.indexOf("} = window.SentinellaLib;")
        );
        const needed = block
            .replace("const {", "")
            .split(",")
            .map((s) => s.trim())
            .filter(Boolean);

        expect(needed.length).toBeGreaterThan(5);
        for (const name of needed) {
            expect(lib[name], `lib.js does not export ${name}`).toBeDefined();
        }
    });

    it("reaches the thresholds helpers through the namespace too", () => {
        const lib = loadLibIntoWindow(window);
        for (const name of ["classForPercent", "bgClassForPercent", "classForTemp"]) {
            expect(typeof lib[name]).toBe("function");
            expect(appSource).toContain(`window.SentinellaLib.${name}(`);
        }
    });

    it("never builds DOM markup out of host-supplied values", () => {
        // Everything host-supplied goes through textContent, which cannot
        // execute markup. innerHTML is used only for fixed row scaffolding
        // (and loop indices), which is why no escaping helper is needed —
        // interpolating a process name or container image here would be a
        // stored-XSS hole fed by whatever the monitored machine is running.
        const HOST_SUPPLIED = [
            "name", "status", "image", "label", "mountpoint", "device",
            "username", "cmdline", "hostname", "addrs", "terminal", "runtime",
            "os_name", "os_version", "fstype",
        ];
        const assignments = appSource.match(/innerHTML\s*=[\s\S]*?;\s*\n/g) ?? [];
        for (const assignment of assignments) {
            for (const field of HOST_SUPPLIED) {
                expect(
                    new RegExp(`\\$\\{[^}]*\\b${field}\\b`).test(assignment),
                    `innerHTML interpolates a host-supplied value (${field}): ` +
                        assignment.slice(0, 90)
                ).toBe(false);
            }
        }
    });
});
