import { describe, expect, it } from "vitest";
import { formatHttpDate, parseHttpDate } from "../../src/contract/httpDate.js";

describe("HTTP-date parsing", () => {
  it("parses IMF-fixdate", () => {
    expect(parseHttpDate("Sun, 06 Nov 1994 08:49:37 GMT")).toBe(
      Date.UTC(1994, 10, 6, 8, 49, 37),
    );
  });

  it("round-trips through formatHttpDate", () => {
    const ms = Date.UTC(2026, 0, 15, 9, 30, 0);
    expect(parseHttpDate(formatHttpDate(ms))).toBe(ms);
    expect(formatHttpDate(ms)).toBe("Thu, 15 Jan 2026 09:30:00 GMT");
  });

  it("parses obsolete RFC 850 form with 1900s two-digit year", () => {
    expect(parseHttpDate("Sunday, 06-Nov-94 08:49:37 GMT")).toBe(
      Date.UTC(1994, 10, 6, 8, 49, 37),
    );
  });

  it("parses asctime form", () => {
    expect(parseHttpDate("Sun Nov  6 08:49:37 1994")).toBe(
      Date.UTC(1994, 10, 6, 8, 49, 37),
    );
  });

  it("rejects malformed dates including a wrong weekday label", () => {
    expect(() => parseHttpDate("not a date")).toThrow();
    expect(() => parseHttpDate("Mon, 06 Nov 1994 08:49:37 GMT")).toThrow(); // 1994-11-06 was Sunday
    expect(() => parseHttpDate("Sun, 31 Nov 1994 08:49:37 GMT")).toThrow();
    expect(() => parseHttpDate("Sun, 06 Nov 1994 25:49:37 GMT")).toThrow();
  });
});
