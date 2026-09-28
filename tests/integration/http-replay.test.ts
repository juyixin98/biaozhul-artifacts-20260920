/**
 * Replays every scenario in tests/fixtures/http-cases.json through the full
 * HTTP stack and asserts concrete outcomes — status codes, exact ETags,
 * versions, body fields and failure categories — not mere reachability.
 *
 * Placeholders in header values are resolved per scenario:
 *   __INITIAL__ : ETag captured immediately after the setup steps
 *   __CURRENT__ : ETag fetched fresh immediately before the step
 */
import { describe, expect, it } from 'vitest';
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { buildTempApp, injectStep, currentEtag, ReplayStep, TempApp } from './replay.js';
import { FROZEN_ETAGS } from '../helpers/oracle.js';

interface FixtureFile {
  scenarios: Array<{
    name: string;
    setup: ReplayStep[];
    steps: ReplayStep[];
  }>;
}

const fixturePath = fileURLToPath(new URL('../fixtures/http-cases.json', import.meta.url));
const fixture = JSON.parse(readFileSync(fixturePath, 'utf8')) as FixtureFile;

function opaqueOf(etag: string): string {
  return etag.replace(/^(W\/)?"/, '').replace(/"$/, '');
}

function resolveHeaders(
  headers: Record<string, string>,
  initial: string,
  current: string,
  currentWeak: string
): Record<string, string> {
  const out: Record<string, string> = {};
  for (const [k, v] of Object.entries(headers)) {
    out[k] = v
      .replaceAll('__INITIAL__', opaqueOf(initial))
      .replaceAll('__CURRENT__', opaqueOf(current))
      .replaceAll('__CURRENT_WEAK__', opaqueOf(currentWeak));
  }
  return out;
}

async function runScenario(env: TempApp, scenario: FixtureFile['scenarios'][number]): Promise<void> {
  // Setup steps are unconditional creates.
  for (const setup of scenario.setup) {
    const res = await injectStep(env.app, { ...setup, headers: setup.headers ?? {} });
    expect(res.status, `setup ${setup.method} ${setup.path}`).toBe(201);
  }

  const resourcePaths = new Set([...scenario.setup, ...scenario.steps].map((s) => s.path));
  const anyPath = [...resourcePaths][0]!;
  const initialEtag = scenario.setup.length > 0 ? await currentEtag(env.app, anyPath) : '';

  for (const step of scenario.steps) {
    const serialized = JSON.stringify(step.headers);
    const needsCurrent = serialized.includes('__CURRENT__') || serialized.includes('__CURRENT_WEAK__');
    const currentEtagValue = needsCurrent ? await currentEtag(env.app, step.path) : '';
    const currentWeakValue = serialized.includes('__CURRENT_WEAK__')
      ? await currentEtag(env.app, step.path, true)
      : '';
    const resolved: ReplayStep = {
      ...step,
      headers: resolveHeaders(step.headers ?? {}, initialEtag, currentEtagValue, currentWeakValue)
    };

    // A fixed run id ties this step's log lines to the input for diagnosis.
    const runId = `replay-${scenario.name.slice(0, 20).replace(/\s+/g, '-')}-${step.method}`;
    resolved.headers['X-Request-Id'] = runId;

    const res = await injectStep(env.app, resolved);

    // Core assertion: exact status, with the scenario's comment on failure.
    expect(res.status, `${scenario.name} :: ${step.method} ${step.path} — ${step.comment ?? ''}`)
      .toBe(step.expectStatus);

    if (step.expectEtag !== undefined) {
      expect(res.headers.etag).toBe(step.expectEtag);
    }
    if (step.expectVersion !== undefined) {
      expect(res.headers['x-resource-version']).toBe(String(step.expectVersion));
    }
    if (step.expectErrorCode) {
      const body = res.bodyJson as { error?: { code?: string } };
      expect(body?.error?.code, 'failure category must be explicit').toBe(step.expectErrorCode);
    }
    if (step.expectBodyField) {
      const [field, expected] = step.expectBodyField;
      const body = res.bodyJson as { body?: Record<string, unknown> };
      expect(body?.body?.[field]).toBe(expected);
    }
  }
}

describe('HTTP fixture replay', () => {
  for (const scenario of fixture.scenarios) {
    it(`scenario: ${scenario.name}`, async () => {
      const env = buildTempApp();
      try {
        await runScenario(env, scenario);
      } finally {
        env.cleanup();
      }
    });
  }

  it('frozen reference ETags match the fixture constants (oracle self-check)', () => {
    expect(FROZEN_ETAGS.widgetV1).toBe('"v1-d00ddb0d"');
    expect(FROZEN_ETAGS.widgetV2Count2).toBe('"v2-ce5ede1f"');
  });

  it('log buffer correlates every condition decision with its run id and basis', async () => {
    const env = buildTempApp(true);
    try {
      await injectStep(env.app, {
        method: 'PUT', path: '/resources/logcheck', body: { v: 1 },
        headers: { 'X-Request-Id': 'run-A' }
      });
      const etag = await currentEtag(env.app, '/resources/logcheck');
      await injectStep(env.app, {
        method: 'PUT', path: '/resources/logcheck', body: { v: 2 },
        headers: { 'If-Match': '"v0-stale"', 'X-Request-Id': 'run-B' }
      });
      await injectStep(env.app, {
        method: 'GET', path: '/resources/logcheck',
        headers: { 'If-None-Match': etag, 'X-Request-Id': 'run-C' }
      });

      const entries = env.logs.entries();
      const runB = entries.filter((e) => e.runId === 'run-B');
      const runC = entries.filter((e) => e.runId === 'run-C');

      // Run B: a 412 with the strong-comparison basis recorded.
      const bFail = runB.find((e) => e.event === 'precondition.failed');
      expect(bFail).toBeDefined();
      const bTrace = (bFail!.fields.trace as Array<{ comparison: string; result: string; basis: string }>);
      expect(bTrace[0]?.comparison).toBe('strong');
      expect(bTrace[0]?.result).toBe('no-match');
      expect(bTrace[0]?.basis).toMatch(/strong comparison/);

      // Run C: a 304 via weak comparison.
      const c304 = runC.find((e) => e.event === 'read.not_modified');
      expect(c304).toBeDefined();
      const cTrace = runC.find((e) => e.event === 'read.conditions');
      expect((cTrace!.fields.steps as Array<{ comparison: string }>)[0]?.comparison).toBe('weak');

      // A failed condition is logged at warn with an explicit code, never as success.
      expect(bFail!.level).toBe('warn');
      expect((bFail!.fields as { code: string }).code).toBe('PRECONDITION_IF_MATCH_FAILED');
    } finally {
      env.cleanup();
    }
  });
});
