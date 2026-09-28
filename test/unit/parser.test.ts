import { describe, expect, it } from "vitest";

import { parseRpcPayload } from "../../src/protocol/parser.js";
import {
  INVALID_PARAMS,
  INVALID_REQUEST,
  METHOD_NOT_FOUND,
  PARSE_ERROR,
} from "../../src/protocol/errors.js";

describe("parseRpcPayload — parse layer vs contract layer", () => {
  it("classifies malformed JSON as parse_error with code -32700", () => {
    const outcome = parseRpcPayload("{not json");
    expect(outcome.kind).toBe("parseError");
    if (outcome.kind !== "parseError") throw new Error("narrow");
    expect(outcome.code).toBe(PARSE_ERROR);
    expect(outcome.category).toBe("parse_error");
    expect(outcome.message).toBe("Parse error");
    expect(typeof outcome.reason).toBe("string");
  });

  it("classifies a valid JSON scalar as a CONTRACT failure (-32600), not a parse error", () => {
    for (const scalar of ["4", '"text"', "true", "null"]) {
      const outcome = parseRpcPayload(scalar);
      expect(outcome.kind).toBe("invalidEnvelope");
      if (outcome.kind !== "invalidEnvelope") throw new Error("narrow");
      expect(outcome.code).toBe(INVALID_REQUEST);
      expect(outcome.category).toBe("invalid_request");
    }
  });

  it("distinguishes an empty batch from a single object", () => {
    const empty = parseRpcPayload("[]");
    expect(empty.kind).toBe("emptyBatch");
    if (empty.kind !== "emptyBatch") throw new Error("narrow");
    expect(empty.code).toBe(INVALID_REQUEST);
    expect(empty.message).toBe(
      "Invalid Request: batch must not be empty",
    );

    const single = parseRpcPayload(
      JSON.stringify({ jsonrpc: "2.0", method: "math.add", id: 1 }),
    );
    expect(single.kind).toBe("single");
  });

  it("parses a non-empty batch while flagging each illegal element individually", () => {
    const outcome = parseRpcPayload(
      JSON.stringify([
        { jsonrpc: "2.0", method: "math.add", params: { a: 1, b: 2 }, id: 10 },
        42, // scalar batch element
        { jsonrpc: "1.0", method: "x", id: 11 }, // wrong version
        { jsonrpc: "2.0", method: 7, id: 12 }, // non-string method
        { jsonrpc: "2.0", method: "x", id: [1, 2] }, // structural id
        { jsonrpc: "2.0", method: "x", params: "no", id: 13 }, // scalar params
        { jsonrpc: "2.0", method: "log" }, // valid notification
      ]),
    );
    expect(outcome.kind).toBe("batch");
    if (outcome.kind !== "batch") throw new Error("narrow");
    expect(outcome.entries).toHaveLength(7);

    const valid = outcome.entries.filter((e) => e.kind === "message");
    const invalid = outcome.entries.filter((e) => e.kind === "invalid");
    expect(valid).toHaveLength(2);
    expect(invalid).toHaveLength(5);
    expect(invalid.map((e) => e.position)).toEqual([1, 2, 3, 4, 5]);
    for (const entry of invalid) {
      if (entry.kind !== "invalid") throw new Error("narrow");
      expect(entry.code).toBe(INVALID_REQUEST);
      expect(entry.category).toBe("invalid_request");
    }
  });

  it("accepts the full id boundary: string, finite number, zero, and null", () => {
    for (const id of ["x", 0, -0, 1.5, -99, null]) {
      const outcome = parseRpcPayload(
        JSON.stringify({ jsonrpc: "2.0", method: "echo", id }),
      );
      expect(outcome.kind).toBe("single");
    }
  });

  it("rejects NaN/Infinity-bearing ids and arrays/objects as ids", () => {
    // JSON has no NaN/Infinity tokens; arrays and plain objects are the
    // structural id shapes that must be rejected.
    const outcome = parseRpcPayload(
      JSON.stringify({ jsonrpc: "2.0", method: "echo", id: { a: 1 } }),
    );
    if (outcome.kind !== "single") throw new Error("narrow");
    expect(outcome.entry.kind).toBe("invalid");
  });

  it("treats a present id (even null) as a request and absent id as notification", () => {
    const withNullId = parseRpcPayload(
      JSON.stringify({ jsonrpc: "2.0", method: "echo", params: {}, id: null }),
    );
    if (withNullId.kind !== "single") throw new Error("narrow");
    expect(withNullId.entry.kind).toBe("message");
    if (withNullId.entry.kind !== "message") throw new Error("narrow");
    expect("id" in withNullId.entry.request).toBe(true);

    const notification = parseRpcPayload(
      JSON.stringify({ jsonrpc: "2.0", method: "echo", params: {} }),
    );
    if (notification.kind !== "single") throw new Error("narrow");
    if (notification.entry.kind !== "message") throw new Error("narrow");
    expect("id" in notification.entry.request).toBe(false);
  });
});

describe("stable error code constants", () => {
  it("exposes the reserved JSON-RPC codes", () => {
    expect(PARSE_ERROR).toBe(-32700);
    expect(INVALID_REQUEST).toBe(-32600);
    expect(METHOD_NOT_FOUND).toBe(-32601);
    expect(INVALID_PARAMS).toBe(-32602);
  });
});
