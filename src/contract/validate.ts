/**
 * Contract parsing & validation (the 契约解析 boundary).
 *
 * Failures here are INPUT_ERROR / CONTRACT_INVALID — they happen before any
 * source is called. The graph must be a DAG with unique call ids, existing
 * dependencies and unique output fields.
 */
import type { CompositeContract } from './types.js';
import { inputError } from '../kernel/errors.js';

export function validateContract(contract: CompositeContract): void {
  const ids = new Set<string>();
  const outputs = new Set<string>();

  if (!contract.name || typeof contract.name !== 'string') {
    throw inputError('CONTRACT_INVALID', 'contract.name must be a non-empty string');
  }
  if (!Array.isArray(contract.calls) || contract.calls.length === 0) {
    throw inputError('CONTRACT_INVALID', `contract ${contract.name ?? '?'} declares no calls`);
  }

  for (const call of contract.calls) {
    if (!call.id || typeof call.id !== 'string') {
      throw inputError('CONTRACT_INVALID', 'every call needs a string id');
    }
    if (ids.has(call.id)) {
      throw inputError('CONTRACT_DUPLICATE_NODE', `duplicate call id: ${call.id}`, { callId: call.id });
    }
    ids.add(call.id);
    if (!call.source || !call.method) {
      throw inputError('CONTRACT_INVALID', `call ${call.id} needs source and method`);
    }
    if (typeof call.buildRequest !== 'function') {
      throw inputError('CONTRACT_INVALID', `call ${call.id} needs a buildRequest function`);
    }
    for (const field of call.fields) {
      if (!field.output) {
        throw inputError('CONTRACT_INVALID', `call ${call.id} has a field without an output name`);
      }
      if (outputs.has(field.output)) {
        throw inputError(
          'CONTRACT_DUPLICATE_FIELD',
          `duplicate output field: ${field.output}`,
          { field: field.output },
        );
      }
      outputs.add(field.output);
      if (field.requirement !== 'required' && field.requirement !== 'optional') {
        throw inputError(
          'CONTRACT_INVALID',
          `field ${field.output} must declare requirement 'required' or 'optional'`,
        );
      }
    }
  }

  for (const call of contract.calls) {
    for (const dep of call.dependencies) {
      if (!ids.has(dep)) {
        throw inputError(
          'CONTRACT_UNKNOWN_DEPENDENCY',
          `call ${call.id} depends on unknown node ${dep}`,
          { callId: call.id, dependency: dep },
        );
      }
      if (dep === call.id) {
        throw inputError('CONTRACT_CYCLE', `call ${call.id} depends on itself`);
      }
    }
  }

  assertAcyclic(contract);
}

/** Kahn's algorithm — also yields a deterministic topological hint if needed. */
function assertAcyclic(contract: CompositeContract): void {
  const indegree = new Map<string, number>();
  const dependents = new Map<string, string[]>();
  for (const call of contract.calls) {
    indegree.set(call.id, call.dependencies.length);
    for (const dep of call.dependencies) {
      const list = dependents.get(dep) ?? [];
      list.push(call.id);
      dependents.set(dep, list);
    }
  }
  const queue = contract.calls.filter((c) => (indegree.get(c.id) ?? 0) === 0).map((c) => c.id);
  let visited = 0;
  while (queue.length > 0) {
    const id = queue.shift()!;
    visited += 1;
    for (const next of dependents.get(id) ?? []) {
      const d = (indegree.get(next) ?? 0) - 1;
      indegree.set(next, d);
      if (d === 0) queue.push(next);
    }
  }
  if (visited !== contract.calls.length) {
    throw inputError('CONTRACT_CYCLE', `contract ${contract.name} contains a dependency cycle`);
  }
}

/** Validate raw request input against the declared input fields. */
export function validateInput(
  contract: CompositeContract,
  raw: Record<string, unknown>,
): Record<string, string | number> {
  const out: Record<string, string | number> = {};
  for (const field of contract.input) {
    const value = raw[field.name];
    if (value === undefined || value === null) {
      if (field.required) {
        throw inputError('INPUT_MISSING_FIELD', `missing required input: ${field.name}`, {
          field: field.name,
        });
      }
      continue;
    }
    if (field.type === 'string') {
      if (typeof value !== 'string' || value.length === 0) {
        throw inputError('INPUT_TYPE_ERROR', `input ${field.name} must be a non-empty string`, {
          field: field.name,
          actual: typeof value,
        });
      }
      out[field.name] = value;
    } else {
      if (typeof value !== 'number' || Number.isNaN(value)) {
        throw inputError('INPUT_TYPE_ERROR', `input ${field.name} must be a number`, {
          field: field.name,
          actual: typeof value,
        });
      }
      out[field.name] = value;
    }
  }
  return out;
}
