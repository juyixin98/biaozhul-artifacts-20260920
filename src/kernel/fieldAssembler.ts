import { DomainError, type FailureDetail } from '../errors.js';
import type { EmittableEvent } from './events.js';
import type { NodeRuntime } from './runtime.js';
import type {
  CompositeContract,
  FieldDeclaration,
  FieldResult,
  FieldStatus,
} from '../types.js';

/**
 * Field assembly: turns settled node results into typed FieldResults with
 * explicit provenance and per-field reasons. Pure apart from emitting
 * field-resolved events.
 */
export class FieldAssembler {
  constructor(
    private readonly clock: () => number,
    private readonly forward: (event: EmittableEvent) => void,
  ) {}

  assemble(
    contract: CompositeContract,
    runtimes: Map<string, NodeRuntime>,
    runId: string,
  ): FieldResult[] {
    const byPath = new Map<string, FieldResult>();
    const results: FieldResult[] = [];
    for (const decl of contract.fields) {
      const field = this.resolveField(decl, runtimes, byPath);
      byPath.set(decl.path, field);
      results.push(field);
      this.forward({ ts: this.clock(), runId, type: 'field-resolved', field });
    }
    return results;
  }

  private resolveField(
    decl: FieldDeclaration,
    runtimes: Map<string, NodeRuntime>,
    priorFields: Map<string, FieldResult>,
  ): FieldResult {
    const base: FieldResult = {
      path: decl.path,
      required: decl.required,
      status: 'present',
      reasons: [],
    };

    if (decl.ref.kind === 'constant') {
      return { ...base, value: decl.ref.value, source: 'constant' };
    }

    if (decl.ref.kind === 'der') {
      return this.resolveDerived(decl, priorFields);
    }

    const rt = runtimes.get(decl.ref.fromNode);
    const provenance = decl.ref.property
      ? `${decl.ref.fromNode}.${decl.ref.property}`
      : decl.ref.fromNode;

    if (!rt || rt.record.status === 'succeeded') {
      const record = rt?.result?.record ?? {};
      const value = decl.ref.property ? record[decl.ref.property] : record;
      return { ...base, value, source: provenance };
    }

    // Node did not produce a value: distinguish a required failure from an
    // optional default, and never-invoked (skipped) from attempted failures.
    const reasons = rt.record.failure ? [rt.record.failure] : [];
    const attempted = rt.record.status !== 'skipped';

    if (decl.required) {
      const status: FieldStatus = attempted
        ? 'missing-required-failed'
        : 'missing-upstream-skipped';
      return { ...base, status, source: provenance, reasons };
    }

    if (decl.ref.fallback !== undefined) {
      return {
        ...base,
        status: 'missing-optional-default',
        value: decl.ref.fallback,
        source: `${provenance}::fallback`,
        reasons,
      };
    }
    return {
      ...base,
      status: attempted ? 'missing-optional-skipped' : 'missing-upstream-skipped',
      value: null,
      source: provenance,
      reasons,
    };
  }

  private resolveDerived(
    decl: FieldDeclaration,
    priorFields: Map<string, FieldResult>,
  ): FieldResult {
    if (decl.ref.kind !== 'der') {
      throw new Error('resolveDerived called with non-derived ref');
    }
    const expr = decl.ref.expression;
    const operands = expr.fields.map((path) => priorFields.get(path));
    const missing = operands.filter((f): f is FieldResult => !f || f.status !== 'present');

    const fail = (reasons: FailureDetail[]): FieldResult => ({
      path: decl.path,
      required: decl.required,
      status: 'derivation-failed',
      source: `derive:${expr.op}`,
      reasons,
    });

    if (missing.length > 0) {
      // Explicit derivation reason plus each operand's root causes, so the
      // chain from field back to the source failure stays visible.
      const reasons: FailureDetail[] = [
        derivedReason(expr, missing),
        ...missing.flatMap((f) => f.reasons),
      ];
      if (decl.required || decl.ref.optional !== true) {
        return fail(reasons);
      }
      return {
        path: decl.path,
        required: decl.required,
        status: 'missing-optional-skipped',
        value: null,
        source: `derive:${expr.op}`,
        reasons,
      };
    }

    try {
      const values = operands.map((f) => f!.value);
      const value =
        expr.op === 'concat'
          ? values.join(expr.separator ?? '')
          : values.reduce<number>((acc, v) => {
              if (typeof v !== 'number') {
                throw new DomainError({
                  category: 'COMPUTATION_FAILED',
                  code: 'DERIVED_TYPE_ERROR',
                  message: `add: operand is not numeric: ${JSON.stringify(v)}`,
                  httpStatus: 500,
                });
              }
              return acc + v;
            }, 0);
      return {
        path: decl.path,
        required: decl.required,
        status: 'present',
        value,
        source: `derive:${expr.op}`,
        reasons: [],
      };
    } catch (error) {
      return fail([
        error instanceof DomainError
          ? error.toFailure(`field:${decl.path}`)
          : new DomainError({
              category: 'COMPUTATION_FAILED',
              code: 'DERIVATION_THREW',
              message: `derivation of ${decl.path} failed: ${(error as Error).message}`,
              httpStatus: 500,
            }).toFailure(`field:${decl.path}`),
      ]);
    }
  }
}

function derivedReason(
  expr: { op: string; fields: string[] },
  missing: FieldResult[],
): FailureDetail {
  return {
    category: 'COMPUTATION_FAILED',
    code: 'DERIVED_OPERAND_MISSING',
    message: `cannot compute ${expr.op}: operand field(s) unavailable: ${missing
      .map((f) => f.path)
      .join(', ')}`,
    retryable: false,
    at: 'derivation',
  };
}
