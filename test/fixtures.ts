/**
 * Static synthetic fixtures.
 *
 * The sha-256 prefixes below were generated OUTSIDE the system under test,
 * with coreutils (`printf '%s' <body> | sha256sum | cut -c1-12`), so expected
 * ETags do not come from the implementation's own hashing code:
 *
 *   alpha              8ed3f6ad685b
 *   beta               f44e64e75f39
 *   gamma              be9d587defa1
 *   delta              4f4a9410ffcd
 *   first-draft        e9788622a7d0
 *   concurrent-body-A  f4bcb0f46581
 *   concurrent-body-B  242e322e7ad1
 *   concurrent-body-C  2e91033212d5
 *   concurrent-body-D  7a513c0f8ecb
 */

export interface BodyFixture {
  readonly name: string;
  readonly body: string;
  /** First 12 hex chars of sha-256(body), from coreutils sha256sum. */
  readonly sha256Prefix12: string;
}

export const BODIES = {
  alpha: { name: "alpha", body: "alpha", sha256Prefix12: "8ed3f6ad685b" },
  beta: { name: "beta", body: "beta", sha256Prefix12: "f44e64e75f39" },
  gamma: { name: "gamma", body: "gamma", sha256Prefix12: "be9d587defa1" },
  delta: { name: "delta", body: "delta", sha256Prefix12: "4f4a9410ffcd" },
  firstDraft: { name: "first-draft", body: "first-draft", sha256Prefix12: "e9788622a7d0" },
  concurrentA: { name: "concurrent-body-A", body: "concurrent-body-A", sha256Prefix12: "f4bcb0f46581" },
  concurrentB: { name: "concurrent-body-B", body: "concurrent-body-B", sha256Prefix12: "242e322e7ad1" },
  concurrentC: { name: "concurrent-body-C", body: "concurrent-body-C", sha256Prefix12: "2e91033212d5" },
  concurrentD: { name: "concurrent-body-D", body: "concurrent-body-D", sha256Prefix12: "7a513c0f8ecb" },
} as const satisfies Record<string, BodyFixture>;

/** Expected strong validator derived from the independent hash. */
export function expectedStrongETag(version: number, fixture: BodyFixture): string {
  return `"v${version}-${fixture.sha256Prefix12}"`;
}

export const FIXED_RESOURCE_IDS = {
  notes: "notes/42",
  doc: "docs/spec",
  wildcard: "wildcard/resource",
  lost: "lost-response/doc",
  concurrent: "concurrent/ledger",
} as const;
