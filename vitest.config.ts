import { defineConfig } from "vitest/config";

export default defineConfig({
  test: {
    include: ["crawler/test/**/*.test.ts"],
  },
});
