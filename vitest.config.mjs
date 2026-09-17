import { defineConfig } from "vitest/config";

export default defineConfig({
    test: {
        // jsdom for the DOM-facing test; the pure-logic tests need no
        // environment but share the config.
        environment: "jsdom",
        include: ["tests/web/**/*.test.js"],
        coverage: {
            include: ["sentinella/web/static/lib.js"],
        },
    },
});
