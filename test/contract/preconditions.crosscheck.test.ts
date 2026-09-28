import { describe, expect, it } from "vitest";
import { evaluatePreconditions, type CurrentRepresentation } from "../../src/contract/preconditions.js";
import type { ConditionInput, HttpMethod } from "../../src/contract/model.js";
import {
  BASE_MS,
  imf,
  oracleEvaluate,
  type OracleConditions,
  type OracleMethod,
  type OracleState,
} from "../oracle/preconditionOracle.ts";
import { logAssertion, scenarioHeader } from "../helpers/testLog.ts";

const E3 = '"v3-8ed3f6ad685b"';
const E2 = '"v2-f44e64e75f39"';

const existing: CurrentRepresentation = {
  exists: true,
  strongEtag: E3,
  version: 3,
  updatedAtMs: BASE_MS + 1000,
};
const absent: CurrentRepresentation = { exists: false, strongEtag: null, version: null, updatedAtMs: null };

interface Scenario {
  name: string;
  method: HttpMethod;
  state: CurrentRepresentation;
  conditions: ConditionInput;
  /** Expected evaluator verdict. */
  expectedVerdict: "proceed" | "not-modified" | "precondition-failed" | "malformed";
  expectedCategory?: string;
  expectedStatus: number;
  /** Stages that must appear in the trace, in order. */
  expectedStages: string[];
}

const scenarios: Scenario[] = [
  {
    name: "GET absent resource -> 404 handled by kernel (evaluator proceeds)",
    method: "GET",
    state: absent,
    conditions: {},
    expectedVerdict: "proceed",
    expectedStatus: 404,
    expectedStages: [],
  },
  {
    name: "GET existing unconditional -> proceed",
    method: "GET",
    state: existing,
    conditions: {},
    expectedVerdict: "proceed",
    expectedStatus: 200,
    expectedStages: [],
  },
  {
    name: "GET INM current strong tag -> 304",
    method: "GET",
    state: existing,
    conditions: { ifNoneMatch: E3 },
    expectedVerdict: "not-modified",
    expectedStatus: 304,
    expectedStages: ["if-none-match"],
  },
  {
    name: "GET INM weak form of current tag -> 304 via weak comparison",
    method: "GET",
    state: existing,
    conditions: { ifNoneMatch: `W/${E3}` },
    expectedVerdict: "not-modified",
    expectedStatus: 304,
    expectedStages: ["if-none-match"],
  },
  {
    name: "GET INM stale tag -> proceed",
    method: "GET",
    state: existing,
    conditions: { ifNoneMatch: E2 },
    expectedVerdict: "proceed",
    expectedStatus: 200,
    expectedStages: ["if-none-match"],
  },
  {
    name: "GET INM * on existing -> 304",
    method: "GET",
    state: existing,
    conditions: { ifNoneMatch: "*" },
    expectedVerdict: "not-modified",
    expectedStatus: 304,
    expectedStages: ["if-none-match"],
  },
  {
    name: "PUT absent with INM * (create guard) -> 412 if-none-match-exists",
    method: "PUT",
    state: existing,
    conditions: { ifNoneMatch: "*" },
    expectedVerdict: "precondition-failed",
    expectedCategory: "if-none-match-exists",
    expectedStatus: 412,
    expectedStages: ["if-none-match"],
  },
  {
    name: "PUT INM matching tag -> 412 (unsafe method never yields 304)",
    method: "PUT",
    state: existing,
    conditions: { ifNoneMatch: E3 },
    expectedVerdict: "precondition-failed",
    expectedCategory: "if-none-match-exists",
    expectedStatus: 412,
    expectedStages: ["if-none-match"],
  },
  {
    name: "PUT If-Match exact strong tag -> proceed",
    method: "PUT",
    state: existing,
    conditions: { ifMatch: E3 },
    expectedVerdict: "proceed",
    expectedStatus: 200,
    expectedStages: ["if-match"],
  },
  {
    name: "PUT If-Match weak form -> 412 (weak validator forbidden for strong compare)",
    method: "PUT",
    state: existing,
    conditions: { ifMatch: `W/${E3}` },
    expectedVerdict: "precondition-failed",
    expectedCategory: "if-match-mismatch",
    expectedStatus: 412,
    expectedStages: ["if-match"],
  },
  {
    name: "PUT If-Match stale tag -> 412 if-match-mismatch",
    method: "PUT",
    state: existing,
    conditions: { ifMatch: E2 },
    expectedVerdict: "precondition-failed",
    expectedCategory: "if-match-mismatch",
    expectedStatus: 412,
    expectedStages: ["if-match"],
  },
  {
    name: "PUT If-Match * existing -> proceed",
    method: "PUT",
    state: existing,
    conditions: { ifMatch: "*" },
    expectedVerdict: "proceed",
    expectedStatus: 200,
    expectedStages: ["if-match"],
  },
  {
    name: "PUT If-Match * absent -> 412 (precondition, distinct from 404)",
    method: "PUT",
    state: absent,
    conditions: { ifMatch: "*" },
    expectedVerdict: "precondition-failed",
    expectedCategory: "if-match-mismatch",
    expectedStatus: 412,
    expectedStages: ["if-match"],
  },
  {
    name: "GET If-Match weak form -> weak comparison allows it",
    method: "GET",
    state: existing,
    conditions: { ifMatch: `W/${E3}` },
    expectedVerdict: "proceed",
    expectedStatus: 200,
    expectedStages: ["if-match"],
  },
  {
    name: "GET IMS future date -> 304",
    method: "GET",
    state: existing,
    conditions: { ifModifiedSince: imf(BASE_MS + 5000) },
    expectedVerdict: "not-modified",
    expectedStatus: 304,
    expectedStages: ["if-modified-since"],
  },
  {
    name: "GET IMS past date -> proceed",
    method: "GET",
    state: existing,
    conditions: { ifModifiedSince: imf(BASE_MS - 5000) },
    expectedVerdict: "proceed",
    expectedStatus: 200,
    expectedStages: ["if-modified-since"],
  },
  {
    name: "GET INM(current)+IMS(past): INM wins -> 304 and IMS not evaluated",
    method: "GET",
    state: existing,
    conditions: { ifNoneMatch: E3, ifModifiedSince: imf(BASE_MS - 5000) },
    expectedVerdict: "not-modified",
    expectedStatus: 304,
    expectedStages: ["if-none-match"],
  },
  {
    name: "GET INM(stale)+IMS(past): INM present so IMS ignored -> proceed 200",
    method: "GET",
    state: existing,
    conditions: { ifNoneMatch: E2, ifModifiedSince: imf(BASE_MS - 5000) },
    expectedVerdict: "proceed",
    expectedStatus: 200,
    expectedStages: ["if-none-match", "if-modified-since"],
  },
  {
    name: "PUT If-Match passes + stale IUS: IUS skipped by fixed precedence",
    method: "PUT",
    state: existing,
    conditions: { ifMatch: E3, ifUnmodifiedSince: imf(BASE_MS - 5000) },
    expectedVerdict: "proceed",
    expectedStatus: 200,
    expectedStages: ["if-match"],
  },
  {
    name: "GET stale IUS -> 412 if-unmodified-since-modified",
    method: "GET",
    state: existing,
    conditions: { ifUnmodifiedSince: imf(BASE_MS - 5000) },
    expectedVerdict: "precondition-failed",
    expectedCategory: "if-unmodified-since-modified",
    expectedStatus: 412,
    expectedStages: ["if-unmodified-since"],
  },
  {
    name: "GET future IUS -> proceed",
    method: "GET",
    state: existing,
    conditions: { ifUnmodifiedSince: imf(BASE_MS + 5000) },
    expectedVerdict: "proceed",
    expectedStatus: 200,
    expectedStages: ["if-unmodified-since"],
  },
  {
    name: "malformed If-Match -> 400 malformed-if-match",
    method: "PUT",
    state: existing,
    conditions: { ifMatch: "not-a-tag" },
    expectedVerdict: "malformed",
    expectedCategory: "malformed-if-match",
    expectedStatus: 400,
    expectedStages: [],
  },
  {
    name: "malformed INM -> 400 malformed-if-none-match",
    method: "GET",
    state: existing,
    conditions: { ifNoneMatch: '"unterminated' },
    expectedVerdict: "malformed",
    expectedCategory: "malformed-if-none-match",
    expectedStatus: 400,
    expectedStages: [],
  },
  {
    name: "malformed IUS -> 400 malformed-date (fail-closed)",
    method: "PUT",
    state: existing,
    conditions: { ifUnmodifiedSince: "yesterday-ish" },
    expectedVerdict: "malformed",
    expectedCategory: "malformed-date",
    expectedStatus: 400,
    expectedStages: [],
  },
  {
    name: "malformed IMS on GET is ignored -> proceed",
    method: "GET",
    state: existing,
    conditions: { ifModifiedSince: "yesterday-ish" },
    expectedVerdict: "proceed",
    expectedStatus: 200,
    expectedStages: ["if-modified-since"],
  },
  {
    name: "DELETE If-Match exact -> proceed",
    method: "DELETE",
    state: existing,
    conditions: { ifMatch: E3 },
    expectedVerdict: "proceed",
    expectedStatus: 204,
    expectedStages: ["if-match"],
  },
  {
    name: "DELETE If-Match weak -> 412",
    method: "DELETE",
    state: existing,
    conditions: { ifMatch: `W/${E3}` },
    expectedVerdict: "precondition-failed",
    expectedCategory: "if-match-mismatch",
    expectedStatus: 412,
    expectedStages: ["if-match"],
  },
  {
    name: "DELETE absent with If-Match * -> 412 not 404",
    method: "DELETE",
    state: absent,
    conditions: { ifMatch: "*" },
    expectedVerdict: "precondition-failed",
    expectedCategory: "if-match-mismatch",
    expectedStatus: 412,
    expectedStages: ["if-match"],
  },
];

describe("precondition evaluator vs independent oracle", () => {
  for (const s of scenarios) {
    it(`${s.method} ${s.name}`, () => {
      scenarioHeader("oracle-crosscheck", s.name, { method: s.method });
      const verdict = evaluatePreconditions(s.method, s.conditions, s.state);

      expect(verdict.verdict).toBe(s.expectedVerdict);
      if (s.expectedCategory) {
        expect(verdict.verdict === "malformed" || verdict.verdict === "precondition-failed").toBe(true);
        if (verdict.verdict !== "proceed" && verdict.verdict !== "not-modified") {
          expect(verdict.category).toBe(s.expectedCategory);
        }
      }
      expect(verdict.steps.map((st) => st.stage)).toEqual(s.expectedStages);

      // Cross-check against the hand-written oracle with no shared code.
      const oracleState: OracleState | null = s.state.exists
        ? {
            exists: true,
            version: s.state.version!,
            etag: s.state.strongEtag!,
            updatedAtMs: s.state.updatedAtMs!,
          }
        : null;
      const oracleConds: OracleConditions = {
        ifMatch: s.conditions.ifMatch,
        ifNoneMatch: s.conditions.ifNoneMatch,
        ifUnmodifiedSince: s.conditions.ifUnmodifiedSince,
        ifModifiedSince: s.conditions.ifModifiedSince,
      };
      const expected = oracleEvaluate(s.method as OracleMethod, oracleConds, oracleState);

      // The evaluator reports "proceed" for absent resources; the kernel
      // maps that to 404. Align that single semantic boundary for comparison.
      const evaluatorVerdict =
        verdict.verdict === "proceed" && !s.state.exists ? "not-found" : verdict.verdict;
      expect(evaluatorVerdict).toBe(expected.verdict);

      if (expected.verdict !== "proceed" && expected.verdict !== "not-found") {
        expect(expected.status).toBe(s.expectedStatus);
      }
      if (expected.category !== "none") {
        const observedCat =
          verdict.verdict === "malformed" || verdict.verdict === "precondition-failed"
            ? verdict.category
            : s.state.exists
              ? "none"
              : "not-found";
        expect(observedCat).toBe(expected.category);
      }
      logAssertion("oracle-crosscheck", true, `${s.method} ${s.name} => ${expected.verdict}/${expected.category}`);
    });
  }
});
