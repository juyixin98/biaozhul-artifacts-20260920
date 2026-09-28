import { describe, expect, it } from "vitest";
import { ConfigError, loadConfig } from "../src/config.ts";

describe("loadConfig", () => {
  it("applies defaults when environment is empty", () => {
    const cfg = loadConfig({});
    expect(cfg.databasePath).toBe("data/app.db");
    expect(cfg.port).toBe(3000);
    expect(cfg.host).toBe("127.0.0.1");
    expect(cfg.etagStrength).toBe("strong");
    expect(cfg.diagnostics).toBe(false);
    expect(cfg.busyTimeoutMs).toBe(5000);
  });

  it("parses explicit valid values including weak strength and diagnostics flags", () => {
    const cfg = loadConfig({
      DATABASE_PATH: ":memory:",
      PORT: "4500",
      HOST: "0.0.0.0",
      ETAG_STRENGTH: "weak",
      DIAGNOSTICS: "1",
      BUSY_TIMEOUT_MS: "1234",
    });
    expect(cfg).toMatchObject({
      databasePath: ":memory:",
      port: 4500,
      host: "0.0.0.0",
      etagStrength: "weak",
      diagnostics: true,
      busyTimeoutMs: 1234,
    });
    expect(loadConfig({ DIAGNOSTICS: "true" }).diagnostics).toBe(true);
  });

  it("rejects an out-of-range port with ConfigError", () => {
    expect(() => loadConfig({ PORT: "70000" })).toThrow(ConfigError);
    expect(() => loadConfig({ PORT: "0" })).toThrow(ConfigError);
    expect(() => loadConfig({ PORT: "abc" })).toThrow(ConfigError);
  });

  it("rejects an unknown etag strength", () => {
    expect(() => loadConfig({ ETAG_STRENGTH: "mighty" })).toThrow(ConfigError);
  });

  it("rejects a negative busy timeout", () => {
    expect(() => loadConfig({ BUSY_TIMEOUT_MS: "-1" })).toThrow(ConfigError);
  });
});
