/**
 * Contract layer — precondition evaluator (pure).
 *
 * Fixed precedence per RFC 9110 13.2.2 (and the MUST-ignore rules in
 * 13.1.3 / 13.1.4):
 *
 *   1. If-Match            (short-circuits on success: skips 2..4)
 *   2. If-Unmodified-Since (ignored when If-Match is present; fail-closed
 *                           on an invalid date for state-changing requests)
 *   3. If-None-Match       (match: 304 for safe methods, 412 for unsafe)
 *   4. If-Modified-Since   (GET/HEAD only; ignored when If-None-Match is
 *                           present; an invalid date is ignored per RFC)
 *
 * The evaluator knows nothing about storage or HTTP: existence/current
 * version are passed in. Every decision appends a traceable EvalStep.
 */

import {
  ETagSyntaxError,
  listMatches,
  parseETag,
  parseETagList,
  weakCompare,
  type ParsedETag,
} from "./etag.js";
import { HttpDateSyntaxError, parseHttpDate } from "./httpDate.js";
import { ifMatchComparisonMode, isSafeMethod } from "./methodPolicy.js";
import type {
  ConditionInput,
  EvalStep,
  HttpMethod,
  PreconditionVerdict,
} from "./model.js";

export interface CurrentRepresentation {
  readonly exists: boolean;
  /** Canonical strong tag (no W/), null when the resource does not exist. */
  readonly strongEtag: string | null;
  readonly version: number | null;
  readonly updatedAtMs: number | null;
}

function ok(steps: EvalStep[]): PreconditionVerdict {
  return { verdict: "proceed", steps };
}

export function evaluatePreconditions(
  method: HttpMethod,
  conditions: ConditionInput,
  current: CurrentRepresentation,
): PreconditionVerdict {
  const steps: EvalStep[] = [];
  const safe = isSafeMethod(method);

  // ---- Step 1: If-Match ------------------------------------------------
  if (conditions.ifMatch !== undefined && conditions.ifMatch !== "") {
    let list: { tags: ParsedETag[]; star: boolean };
    try {
      list = parseETagList(conditions.ifMatch);
    } catch (err) {
      if (err instanceof ETagSyntaxError) {
        return {
          verdict: "malformed",
          category: "malformed-if-match",
          detail: err.message,
          steps,
        };
      }
      throw err;
    }

    let imMatched: boolean;
    if (list.star) {
      imMatched = current.exists;
    } else {
      const mode = ifMatchComparisonMode(method);
      imMatched =
        current.exists &&
        current.strongEtag !== null &&
        listMatches(list, parseETag(current.strongEtag), mode);
    }

    steps.push({
      stage: "if-match",
      comparison: ifMatchComparisonMode(method),
      observed: current.strongEtag,
      star: list.star,
      candidates: list.tags.map((tag) => {
        const matched =
          current.exists &&
          current.strongEtag !== null &&
          (ifMatchComparisonMode(method) === "strong"
            ? !tag.weak && tag.tag === current.strongEtag
            : weakCompare(tag, parseETag(current.strongEtag)));
        return { raw: tag.weak ? `W/${tag.tag}` : tag.tag, weak: tag.weak, matched };
      }),
      result: imMatched ? "match" : "no-match",
    });

    if (!imMatched) {
      return {
        verdict: "precondition-failed",
        category: "if-match-mismatch",
        detail: list.star
          ? "If-Match: \"*\" but no current representation exists"
          : `If-Match matched no current validator (observed ${current.strongEtag ?? "<absent>"})`,
        steps,
      };
    }
    // RFC 9110 13.2.2 step 1 success: skip straight to step 5.
    return ok(steps);
  }

  // ---- Step 2: If-Unmodified-Since ------------------------------------
  // Ignored entirely when If-Match is present (handled by the early return
  // above). Fail-closed on malformed dates: silently dropping a write guard
  // would turn an unknown input into a success.
  if (conditions.ifUnmodifiedSince !== undefined && conditions.ifUnmodifiedSince !== "") {
    let suppliedMs: number;
    try {
      suppliedMs = parseHttpDate(conditions.ifUnmodifiedSince);
    } catch (err) {
      if (err instanceof HttpDateSyntaxError) {
        return {
          verdict: "malformed",
          category: "malformed-date",
          detail: err.message,
          steps,
        };
      }
      throw err;
    }

    // No representation means nothing could have been modified: pass.
    const modified =
      current.exists && current.updatedAtMs !== null && current.updatedAtMs > suppliedMs;
    steps.push({
      stage: "if-unmodified-since",
      suppliedDate: conditions.ifUnmodifiedSince,
      parsedMs: suppliedMs,
      observedUpdatedAtMs: current.updatedAtMs,
      result: modified ? "modified" : "unmodified",
    });
    if (modified) {
      return {
        verdict: "precondition-failed",
        category: "if-unmodified-since-modified",
        detail: `Representation modified at ${new Date(current.updatedAtMs!).toISOString()} is later than ${new Date(suppliedMs).toISOString()}`,
        steps,
      };
    }
  }

  // ---- Step 3: If-None-Match ------------------------------------------
  if (conditions.ifNoneMatch !== undefined && conditions.ifNoneMatch !== "") {
    let list: { tags: ParsedETag[]; star: boolean };
    try {
      list = parseETagList(conditions.ifNoneMatch);
    } catch (err) {
      if (err instanceof ETagSyntaxError) {
        return {
          verdict: "malformed",
          category: "malformed-if-none-match",
          detail: err.message,
          steps,
        };
      }
      throw err;
    }

    const matched =
      current.exists &&
      current.strongEtag !== null &&
      (list.star || listMatches(list, parseETag(current.strongEtag), "weak"));

    steps.push({
      stage: "if-none-match",
      comparison: "weak",
      observed: current.strongEtag,
      star: list.star,
      candidates: list.tags.map((tag) => ({
        raw: tag.weak ? `W/${tag.tag}` : tag.tag,
        weak: tag.weak,
        matched:
          current.exists &&
          current.strongEtag !== null &&
          weakCompare(tag, parseETag(current.strongEtag)),
      })),
      result: matched ? "match" : "no-match",
    });

    if (matched) {
      return safe
        ? { verdict: "not-modified", steps }
        : {
            verdict: "precondition-failed",
            category: "if-none-match-exists",
            detail: list.star
              ? "If-None-Match: \"*\" but a representation already exists"
              : `If-None-Match matched current validator ${current.strongEtag}`,
            steps,
          };
    }
  }

  // ---- Step 4: If-Modified-Since (safe methods only) ------------------
  if (
    safe &&
    conditions.ifModifiedSince !== undefined &&
    conditions.ifModifiedSince !== ""
  ) {
    // RFC 9110 13.1.3: ignored when If-None-Match is present (trace only).
    if (conditions.ifNoneMatch !== undefined && conditions.ifNoneMatch !== "") {
      steps.push({
        stage: "if-modified-since",
        suppliedDate: conditions.ifModifiedSince,
        parsedMs: null,
        observedUpdatedAtMs: current.updatedAtMs,
        result: "ignored-because-if-none-match",
      });
      return ok(steps);
    }

    let suppliedMs: number | null = null;
    try {
      suppliedMs = parseHttpDate(conditions.ifModifiedSince);
    } catch {
      // RFC 9110 13.1.3: an invalid If-Modified-Since is ignored.
      steps.push({
        stage: "if-modified-since",
        suppliedDate: conditions.ifModifiedSince,
        parsedMs: null,
        observedUpdatedAtMs: current.updatedAtMs,
        result: "ignored-malformed",
      });
      return ok(steps);
    }

    const modified =
      current.exists &&
      current.updatedAtMs !== null &&
      current.updatedAtMs > suppliedMs;
    steps.push({
      stage: "if-modified-since",
      suppliedDate: conditions.ifModifiedSince,
      parsedMs: suppliedMs,
      observedUpdatedAtMs: current.updatedAtMs,
      result: modified ? "modified" : "unmodified",
    });
    if (!modified && current.exists) {
      return { verdict: "not-modified", steps };
    }
  } else if (
    !safe &&
    conditions.ifModifiedSince !== undefined &&
    conditions.ifModifiedSince !== ""
  ) {
    steps.push({
      stage: "if-modified-since",
      suppliedDate: conditions.ifModifiedSince,
      parsedMs: null,
      observedUpdatedAtMs: current.updatedAtMs,
      result: "ignored-method",
    });
  }

  return ok(steps);
}
