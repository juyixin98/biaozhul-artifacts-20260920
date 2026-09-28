/**
 * Executes every hand-authored JSON fixture in fixtures/replay through the
 * real HTTP stack via the shared replay runner. These are the replayable
 * scenarios called out in acceptance: lost-response replay, weak tags and
 * wildcard matching.
 */

import { describe, expect, it } from "vitest";
import { resolveFixturePaths, runFixture } from "../../scripts/replay.ts";

const fixtures = resolveFixturePaths("fixtures/replay");

describe("hand-authored replay fixtures over real HTTP", () => {
  expect(fixtures.length).toBeGreaterThan(0);
  for (const path of fixtures) {
    it(`replays ${path.split("/").pop()} with every assertion green`, async () => {
      const passed = await runFixture(path);
      expect(passed).toBe(true);
    }, 30_000);
  }
});
