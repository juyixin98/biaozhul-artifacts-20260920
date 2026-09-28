import { describe, expect, it } from "vitest";
import {
  buildStrongETag,
  canonicalTag,
  ETagSyntaxError,
  parseETag,
  parseETagList,
  renderETag,
  strongCompare,
  weakCompare,
} from "../../src/contract/etag.js";

describe("ETag parsing", () => {
  it("parses strong opaque tags", () => {
    expect(parseETag('"v1-abc"')).toEqual({ tag: '"v1-abc"', weak: false });
  });

  it("parses weak tags and records the indicator", () => {
    expect(parseETag('W/"v1-abc"')).toEqual({ tag: '"v1-abc"', weak: true });
    expect(parseETag('w/"v1-abc"')).toEqual({ tag: '"v1-abc"', weak: true });
  });

  it("tolerates surrounding OWS but rejects inner whitespace", () => {
    expect(parseETag('  "v1"  ').tag).toBe('"v1"');
    expect(() => parseETag('"v 1"')).toThrow(ETagSyntaxError);
    expect(() => parseETag("v1")).toThrow(ETagSyntaxError);
    expect(() => parseETag('"v1')).toThrow(ETagSyntaxError);
  });

  it("rejects empty and wildcard-mixed lists", () => {
    expect(() => parseETagList("")).toThrow();
    expect(parseETagList("*")).toEqual({ tags: [], star: true });
    expect(() => parseETagList('*, "v1"')).toThrow(ETagSyntaxError);
  });

  it("parses multi-tag lists", () => {
    const list = parseETagList('"v1", W/"v2", "v3"');
    expect(list.star).toBe(false);
    expect(list.tags.map((t) => t.tag)).toEqual(['"v1"', '"v2"', '"v3"']);
    expect(list.tags.map((t) => t.weak)).toEqual([false, true, false]);
  });
});

describe("ETag comparison", () => {
  const strong = parseETag('"v3-x"');
  const weak = parseETag('W/"v3-x"');

  it("weak comparison ignores weakness bits", () => {
    expect(weakCompare(strong, weak)).toBe(true);
    expect(weakCompare(weak, weak)).toBe(true);
  });

  it("strong comparison requires both sides strong", () => {
    expect(strongCompare(strong, strong)).toBe(true);
    expect(strongCompare(strong, weak)).toBe(false);
    expect(strongCompare(weak, strong)).toBe(false);
    expect(strongCompare(weak, weak)).toBe(false);
  });

  it("different opaque tags never match", () => {
    expect(weakCompare(parseETag('"v3-x"'), parseETag('"v4-x"'))).toBe(false);
  });
});

describe("ETag generation", () => {
  it("builds deterministic versioned strong validators", () => {
    // sha256("alpha") first 12 = 8ed3f6ad685b (verified with coreutils)
    expect(buildStrongETag(1, "alpha")).toBe('"v1-8ed3f6ad685b"');
    expect(buildStrongETag(2, "beta")).toBe('"v2-f44e64e75f39"');
  });

  it("renders weak emission with W/ prefix and canonicalizes back", () => {
    const strong = buildStrongETag(1, "alpha");
    expect(renderETag(strong, "weak")).toBe('W/"v1-8ed3f6ad685b"');
    expect(canonicalTag(renderETag(strong, "weak"))).toBe(strong);
    expect(canonicalTag(strong)).toBe(strong);
  });
});
