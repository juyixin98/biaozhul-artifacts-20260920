import { DomainError } from '../errors.js';
import type {
  CompositeContract,
  DerivedExpression,
  FieldDeclaration,
  FieldRef,
  NodeDeclaration,
  ParamValue,
} from '../types.js';

/**
 * ============================================================================
 * Raw contract shapes (what callers POST / register)
 * ============================================================================
 */
export interface RawNode {
  id: unknown;
  source: unknown;
  params?: Record<string, unknown>;
  necessity?: unknown;
  timeoutMs?: unknown;
  snapshot?: unknown;
}

export interface RawField {
  path: unknown;
  required?: unknown;
  ref?: unknown;
}

export interface RawContract {
  name?: unknown;
  version?: unknown;
  nodes?: unknown;
  fields?: unknown;
}

const ID_PATTERN = /^[A-Za-z][\w-]{0,63}$/;

function invalid(message: string, details?: Record<string, unknown>): DomainError {
  return new DomainError({
    category: 'INVALID_CONTRACT',
    code: 'CONTRACT_INVALID',
    message,
    httpStatus: 500,
    details,
  });
}

function asObject(value: unknown, where: string): Record<string, unknown> {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) {
    throw invalid(`${where} must be an object`, { received: typeof value });
  }
  return value as Record<string, unknown>;
}

/**
 * Parse and fully validate a raw contract declaration.
 * Throws DomainError(category=INVALID_CONTRACT) on any structural problem.
 */
export function parseContract(raw: RawContract): CompositeContract {
  const root = asObject(raw, 'contract');

  const name = root['name'];
  if (typeof name !== 'string' || name.trim().length === 0) {
    throw invalid('contract.name must be a non-empty string');
  }

  const version = root['version'];
  if (typeof version !== 'number' || !Number.isInteger(version) || version < 1) {
    throw invalid('contract.version must be a positive integer');
  }

  if (!Array.isArray(root['nodes'])) {
    throw invalid('contract.nodes must be an array');
  }
  if (!Array.isArray(root['fields'])) {
    throw invalid('contract.fields must be an array');
  }

  const nodes = (root['nodes'] as unknown[]).map((n, i) =>
    parseNode(asObject(n, `nodes[${i}]`), i),
  );

  // Unique node ids.
  const nodeIds = new Set<string>();
  for (const node of nodes) {
    if (nodeIds.has(node.id)) {
      throw invalid(`duplicate node id: ${node.id}`);
    }
    nodeIds.add(node.id);
  }

  // A param reference to a node declared necessity:"optional" is itself
  // non-blocking by default (the call site may override with optional:false).
  const byId = new Map(nodes.map((n) => [n.id, n]));
  for (const node of nodes) {
    for (const [key, param] of Object.entries(node.params)) {
      if (param.kind === 'ref' && param.optional === undefined) {
        const target = byId.get(param.fromNode);
        if (!target) {
          throw invalid(
            `node ${node.id} param "${key}" references unknown node ${param.fromNode}`,
          );
        }
        if (target.required === 'optional') {
          param.optional = true;
          param.default = undefined;
        }
      }
    }
  }

  // Validate param refs point at declared nodes.
  for (const node of nodes) {
    for (const [key, param] of Object.entries(node.params)) {
      if (param.kind === 'ref' && !nodeIds.has(param.fromNode)) {
        throw invalid(
          `node ${node.id} param "${key}" references unknown node ${param.fromNode}`,
        );
      }
    }
  }

  // Dependency graph must be acyclic. Edge: dependency -> node.
  const edges = new Map<string, Set<string>>();
  for (const node of nodes) {
    const deps = new Set<string>();
    for (const param of Object.values(node.params)) {
      if (param.kind === 'ref') deps.add(param.fromNode);
    }
    edges.set(node.id, deps);
  }
  assertAcyclic(edges);

  const fields = (root['fields'] as unknown[]).map((f, i) =>
    parseField(asObject(f, `fields[${i}]`), i, nodeIds),
  );

  const fieldPaths = new Set<string>();
  for (const field of fields) {
    if (fieldPaths.has(field.path)) {
      throw invalid(`duplicate field path: ${field.path}`);
    }
    fieldPaths.add(field.path);
  }

  // Derived fields may reference earlier-declared fields; no field cycles.
  assertFieldClosure(fields);

  return { name: name.trim(), version, nodes, fields };
}

function parseNode(obj: Record<string, unknown>, index: number): NodeDeclaration {
  const id = obj['id'];
  if (typeof id !== 'string' || !ID_PATTERN.test(id)) {
    throw invalid(`nodes[${index}].id must match ${ID_PATTERN}`, { id });
  }
  const source = obj['source'];
  if (typeof source !== 'string' || source.trim().length === 0) {
    throw invalid(`node ${id}: source must be a non-empty string`);
  }

  let necessity: NodeDeclaration['required'] = 'required';
  if (obj['necessity'] !== undefined) {
    if (obj['necessity'] !== 'required' && obj['necessity'] !== 'optional') {
      throw invalid(`node ${id}: necessity must be "required" or "optional"`);
    }
    necessity = obj['necessity'];
  }

  let timeoutMs: number | undefined;
  if (obj['timeoutMs'] !== undefined) {
    timeoutMs = obj['timeoutMs'] as number;
    if (typeof timeoutMs !== 'number' || timeoutMs <= 0 || !Number.isFinite(timeoutMs)) {
      throw invalid(`node ${id}: timeoutMs must be a positive number`);
    }
  }

  let snapshotRequirement: NodeDeclaration['snapshotRequirement'];
  if (obj['snapshot'] !== undefined) {
    const s = obj['snapshot'];
    if (s !== 'exact' && s !== 'best-effort' && s !== 'any') {
      throw invalid(`node ${id}: snapshot must be exact|best-effort|any`);
    }
    snapshotRequirement = s;
  }

  const params: Record<string, ParamValue> = {};
  if (obj['params'] !== undefined) {
    const rawParams = asObject(obj['params'], `node ${id} params`);
    for (const [key, value] of Object.entries(rawParams)) {
      params[key] = parseParam(value, `node ${id} params.${key}`);
    }
  }

  return {
    id,
    source: source.trim(),
    params,
    required: necessity,
    timeoutMs,
    snapshotRequirement,
  };
}

function parseParam(value: unknown, where: string): ParamValue {
  const obj = asObject(value, where);
  if (obj['from'] !== undefined) {
    if (typeof obj['from'] !== 'string' || !ID_PATTERN.test(obj['from'])) {
      throw invalid(`${where}: "from" must be a node id`);
    }
    const property = obj['property'];
    if (property !== undefined && typeof property !== 'string') {
      throw invalid(`${where}: "property" must be a string`);
    }
    const optional = obj['optional'];
    if (optional !== undefined && typeof optional !== 'boolean') {
      throw invalid(`${where}: "optional" must be a boolean`);
    }
    return {
      kind: 'ref',
      fromNode: obj['from'],
      property: property as string | undefined,
      ...(optional === true ? { optional: true, default: obj['default'] } : {}),
    };
  }
  if ('value' in obj) {
    return { kind: 'literal', value: obj['value'] };
  }
  if (obj['request'] !== undefined) {
    if (typeof obj['request'] !== 'string' || obj['request'].length === 0) {
      throw invalid(`${where}: "request" must name a request parameter`);
    }
    return { kind: 'request', key: obj['request'] };
  }
  throw invalid(`${where}: param must be {"value": ...} or {"from": ..., "property": ...}`);
}

function parseField(
  obj: Record<string, unknown>,
  index: number,
  nodeIds: Set<string>,
): FieldDeclaration {
  const path = obj['path'];
  if (typeof path !== 'string' || path.trim().length === 0) {
    throw invalid(`fields[${index}].path must be a non-empty string`);
  }
  if (path.includes('..') || path.startsWith('.') || path.endsWith('.')) {
    throw invalid(`field path "${path}" is malformed`);
  }

  const required = obj['required'] ?? true;
  if (typeof required !== 'boolean') {
    throw invalid(`field ${path}: required must be boolean`);
  }

  if (obj['ref'] === undefined) {
    throw invalid(`field ${path}: ref is required`);
  }
  const ref = parseFieldRef(asObject(obj['ref'], `field ${path} ref`), path, nodeIds);

  return { path: path.trim(), required, ref };
}

function parseFieldRef(
  obj: Record<string, unknown>,
  fieldPath: string,
  nodeIds: Set<string>,
): FieldRef {
  const where = `field ${fieldPath}`;
  if (obj['from'] !== undefined) {
    if (typeof obj['from'] !== 'string' || !nodeIds.has(obj['from'])) {
      throw invalid(`${where}: ref.from must reference a declared node`);
    }
    const property = obj['property'];
    if (property !== undefined && typeof property !== 'string') {
      throw invalid(`${where}: ref.property must be a string`);
    }
    return {
      kind: 'source',
      fromNode: obj['from'],
      property: property as string | undefined,
      fallback: obj['fallback'],
    };
  }
  if (obj['constant'] !== undefined) {
    return { kind: 'constant', value: obj['constant'] };
  }
  if (obj['derive'] !== undefined) {
    return { kind: 'der', expression: parseExpression(obj['derive'], where) };
  }
  throw invalid(`${where}: ref must use from|constant|derive`);
}

function parseExpression(value: unknown, where: string): DerivedExpression {
  const obj = asObject(value, `${where} derive`);
  const op = obj['op'];
  const fields = obj['fields'];
  if (!Array.isArray(fields) || fields.length === 0 || fields.some((f) => typeof f !== 'string')) {
    throw invalid(`${where}: derive.fields must be a non-empty string[]`);
  }
  if (op === 'concat') {
    const sep = obj['separator'];
    if (sep !== undefined && typeof sep !== 'string') {
      throw invalid(`${where}: separator must be a string`);
    }
    return { op, fields: fields as string[], separator: sep as string | undefined };
  }
  if (op === 'add') {
    return { op, fields: fields as string[] };
  }
  throw invalid(`${where}: op must be concat|add`);
}

function assertAcyclic(edges: Map<string, Set<string>>): void {
  const state = new Map<string, 0 | 1 | 2>(); // 0 unvisited, 1 in stack, 2 done
  const stack: string[] = [];

  const visit = (node: string): void => {
    const mark = state.get(node) ?? 0;
    if (mark === 1) {
      const cycle = stack.slice(stack.indexOf(node)).concat(node).join(' -> ');
      throw invalid(`dependency cycle detected: ${cycle}`);
    }
    if (mark === 2) return;
    state.set(node, 1);
    stack.push(node);
    for (const dep of edges.get(node) ?? []) visit(dep);
    stack.pop();
    state.set(node, 2);
  };

  for (const node of edges.keys()) visit(node);
}

function assertFieldClosure(fields: FieldDeclaration[]): void {
  const declared = new Set<string>();
  for (const field of fields) {
    if (field.ref.kind === 'der') {
      for (const dep of field.ref.expression.fields) {
        if (!declared.has(dep)) {
          throw invalid(
            `field ${field.path}: derived expression references unknown or later field ${dep}`,
          );
        }
      }
    }
    declared.add(field.path);
  }
}

/** Direct dependency node ids of a node (from its params). */
export function nodeDependencies(node: NodeDeclaration): string[] {
  const deps: string[] = [];
  for (const param of Object.values(node.params)) {
    if (param.kind === 'ref') deps.push(param.fromNode);
  }
  return deps;
}

/**
 * Kahn topological ordering (dependencies first). Ties keep declaration order,
 * so scheduling is deterministic for run-log replay.
 */
export function topoSort(contract: CompositeContract): string[] {
  const indegree = new Map<string, number>();
  const dependents = new Map<string, string[]>();
  for (const node of contract.nodes) {
    indegree.set(node.id, 0);
    dependents.set(node.id, []);
  }
  for (const node of contract.nodes) {
    for (const dep of nodeDependencies(node)) {
      indegree.set(node.id, (indegree.get(node.id) ?? 0) + 1);
      dependents.get(dep)!.push(node.id);
    }
  }

  const ordered: string[] = [];
  const ready = contract.nodes.filter((n) => (indegree.get(n.id) ?? 0) === 0).map((n) => n.id);
  while (ready.length > 0) {
    const id = ready.shift()!;
    ordered.push(id);
    for (const dependent of dependents.get(id) ?? []) {
      const next = (indegree.get(dependent) ?? 0) - 1;
      indegree.set(dependent, next);
      if (next === 0) ready.push(dependent);
    }
  }
  if (ordered.length !== contract.nodes.length) {
    // Defensive: parser already rejects cycles.
    throw invalid('internal: failed to topologically sort acyclic contract');
  }
  return ordered;
}
