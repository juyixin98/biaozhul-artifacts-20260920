import { describe, expect, it } from 'vitest';
import { evaluatePreconditions, Selection } from '../../src/contract/conditions.js';
import {
  MalformedConditionError,
  NotModifiedError,
  PreconditionFailedError
} from '../../src/contract/errors.js';

const present = (opaque = 'v1-aaa', weak = false, lastModifiedMs = Date.parse('2026-01-15T09:00:00Z')): Selection => ({
  kind: 'present',
  representation: { etag: { opaque, weak }, lastModifiedMs }
});
const absent: Selection = { kind: 'absent' };

function expect412Code(fn: () => unknown, code: string): PreconditionFailedError {
  try {
    fn();
  } catch (err) {
    expect(err).toBeInstanceOf(PreconditionFailedError);
    const pe = err as PreconditionFailedError;
    expect(pe.code).toBe(code);
    return pe;
  }
  throw new Error('expected PreconditionFailedError');
}

describe('safe methods (GET/HEAD)', () => {
  it('If-None-Match weak comparison: matching weak tag yields 304', () => {
    expect(() =>
      evaluatePreconditions('GET', { ifNoneMatch: 'W/"v1-aaa"' }, present())
    ).toThrow(NotModifiedError);
  });

  it('If-None-Match weak comparison: non-matching tag proceeds', () => {
    const decision = evaluatePreconditions('GET', { ifNoneMatch: '"v9-zzz"' }, present());
    expect(decision.allowed).toBe(true);
    expect(decision.trace[0]?.result).toBe('no-match');
  });

  it('entity-tag validator wins over If-Modified-Since even when the date is ancient', () => {
    try {
      evaluatePreconditions(
        'GET',
        { ifNoneMatch: '"v1-aaa"', ifModifiedSince: 'Wed, 01 Jan 2020 00:00:00 GMT' },
        present()
      );
      throw new Error('expected NotModifiedError');
    } catch (err) {
      expect(err).toBeInstanceOf(NotModifiedError);
      const trace = (err as NotModifiedError).trace;
      expect(trace[0]?.header).toBe('If-None-Match');
      // The date validator must show up as explicitly ignored, not evaluated.
      expect(trace.some((s) => s.header === 'If-Modified-Since' && s.result === 'ignored')).toBe(true);
    }
  });

  it('non-matching If-None-Match suppresses If-Modified-Since entirely (200, not 304)', () => {
    const decision = evaluatePreconditions(
      'GET',
      { ifNoneMatch: '"v9-zzz"', ifModifiedSince: 'Wed, 01 Jan 2020 00:00:00 GMT' },
      present()
    );
    expect(decision.trace.find((s) => s.header === 'If-Modified-Since')?.result).toBe('ignored');
  });

  it('If-Modified-Since alone: unchanged representation yields 304', () => {
    expect(() =>
      evaluatePreconditions('GET', { ifModifiedSince: 'Thu, 15 Jan 2026 09:00:00 GMT' }, present())
    ).toThrow(NotModifiedError);
  });

  it('If-Modified-Since alone: newer representation proceeds', () => {
    const decision = evaluatePreconditions(
      'GET',
      { ifModifiedSince: 'Wed, 14 Jan 2026 09:00:00 GMT' },
      present()
    );
    expect(decision.trace[0]?.result).toBe('no-match');
  });

  it('a malformed If-Modified-Since is ignored rather than failing the request', () => {
    const decision = evaluatePreconditions('GET', { ifModifiedSince: 'garbage' }, present());
    expect(decision.trace[0]?.result).toBe('ignored');
  });
});

describe('state-changing methods (PUT/PATCH/DELETE)', () => {
  it('If-Match uses strong comparison: a weak candidate never matches', () => {
    const err = expect412Code(
      () => evaluatePreconditions('PUT', { ifMatch: 'W/"v1-aaa"' }, present()),
      'PRECONDITION_IF_MATCH_FAILED'
    );
    expect(err.trace[0]?.comparison).toBe('strong');
    expect(err.trace[0]?.actual).toBe('"v1-aaa"');
  });

  it('If-Match strong match proceeds', () => {
    const decision = evaluatePreconditions('PUT', { ifMatch: '"v1-aaa"' }, present());
    expect(decision.trace[0]?.result).toBe('match');
  });

  it('If-Match list: one strong match among several is enough', () => {
    const decision = evaluatePreconditions('PUT', { ifMatch: '"v0-x", "v1-aaa"' }, present());
    expect(decision.trace[0]?.result).toBe('match');
  });

  it('If-Match against an absent representation is false (412, not 404)', () => {
    const err = expect412Code(
      () => evaluatePreconditions('DELETE', { ifMatch: '*' }, absent),
      'PRECONDITION_IF_MATCH_FAILED'
    );
    expect(err.trace[0]?.actual).toBe('<no selected representation>');
  });

  it('If-None-Match uses weak comparison on writes and blocks on a match', () => {
    const err = expect412Code(
      () => evaluatePreconditions('PUT', { ifNoneMatch: 'W/"v1-aaa"' }, present()),
      'PRECONDITION_IF_NONE_MATCH_FAILED'
    );
    expect(err.trace[0]?.comparison).toBe('weak');
  });

  it('If-None-Match wildcard on an absent target passes (create-only)', () => {
    const decision = evaluatePreconditions('PUT', { ifNoneMatch: '*' }, absent);
    expect(decision.trace[0]?.result).toBe('no-match');
    expect(decision.allowed).toBe(true);
  });

  it('step 2 (If-None-Match) only runs after a passing step 1', () => {
    // Stale If-Match fails first; If-None-Match must never be evaluated.
    const err = expect412Code(
      () =>
        evaluatePreconditions(
          'PUT',
          { ifMatch: '"v9-stale"', ifNoneMatch: '"v1-aaa"' },
          present()
        ),
      'PRECONDITION_IF_MATCH_FAILED'
    );
    expect(err.trace.map((s) => s.header)).toEqual(['If-Match']);
  });

  it('If-Unmodified-Since fails when the representation was modified later', () => {
    expect412Code(
      () =>
        evaluatePreconditions(
          'PUT',
          { ifUnmodifiedSince: 'Wed, 14 Jan 2026 09:00:00 GMT' },
          present()
        ),
      'PRECONDITION_IF_UNMODIFIED_SINCE_FAILED'
    );
  });

  it('If-Modified-Since is ignored on state-changing requests', () => {
    const decision = evaluatePreconditions(
      'PATCH',
      { ifModifiedSince: 'Wed, 01 Jan 2020 00:00:00 GMT' },
      present()
    );
    expect(decision.trace[0]?.result).toBe('ignored');
  });

  it('a failed precondition on a present resource carries the current validators', () => {
    const err = expect412Code(
      () => evaluatePreconditions('PUT', { ifMatch: '"v9-stale"' }, present('v2-live')),
      'PRECONDITION_IF_MATCH_FAILED'
    );
    expect(err.currentValidator?.etag).toBe('"v2-live"');
    expect(err.currentValidator?.lastModified).toMatch(/GMT$/);
  });
});

describe('malformed condition headers', () => {
  it('malformed If-Match is a 400 category, distinct from a 412 miss', () => {
    try {
      evaluatePreconditions('PUT', { ifMatch: 'junk' }, present());
      throw new Error('expected throw');
    } catch (err) {
      expect(err).toBeInstanceOf(MalformedConditionError);
      expect((err as MalformedConditionError).statusCode).toBe(400);
    }
  });

  it('malformed If-None-Match is a 400 even when it would otherwise match', () => {
    expect(() => evaluatePreconditions('GET', { ifNoneMatch: '*, "x"' }, present()))
      .toThrow(MalformedConditionError);
  });
});
