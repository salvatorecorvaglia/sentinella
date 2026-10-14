import { defineConfig } from "vitest/config";

export default defineConfig({
    test: {
        // jsdom for the DOM-facing test; the pure-logic tests need no
        // environment but share the config.
        environment: "jsdom",
        include: ["tests/web/**/*.test.js"],
        coverage: {
            provider: "v8",
            // app.js is covered through the render tests, which import it as a
            // module; theme-init.js through dashboard.test.js runs it in a vm
            // context, which v8 coverage cannot attribute, so it isn't listed.
            include: ["sentinella/web/static/lib.js", "sentinella/web/static/app.js"],
            reporter: ["text"],
            // A floor, not a target — raise it as coverage improves, never
            // lower it (same rule as fail_under in pyproject.toml).
            thresholds: { statements: 85, branches: 70, functions: 85, lines: 85 },
        },
    },
});
