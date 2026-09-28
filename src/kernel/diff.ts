import { resolveRef } from '../contract/ref-resolver.js';
import type { ContractModel, JsonObject, JsonValue, OperationModel, ParamModel, RawSchema } from '../contract/types.js';
import { diffSchemas } from './schema-diff.js';
import { newCtx, sample } from './value-space.js';
import type { DiffResult, Direction, Finding, SchemaResolver, Witness } from './types.js';

export interface DocumentBundle {
  model: ContractModel;
  /** Raw parsed document, needed to resolve $refs. */
  raw: JsonObject;
}

/** Adapt the contract module's ref resolver to the kernel interface. */
function resolverOf(bundle: DocumentBundle): SchemaResolver {
  return {
    resolve(ref: string, stack: readonly string[]) {
      const r = resolveRef(bundle.raw, ref, [...stack]);
      return { node: r.node, missing: r.missing, cycle: r.cycle, external: r.external };
    },
  };
}

interface FindingDraft extends Omit<Finding, 'id' | 'direction'> {
  direction?: Direction;
}

class FindingCollector {
  readonly request: Finding[] = [];
  readonly response: Finding[] = [];

  add(draft: Omit<Finding, 'id'>): void {
    const f = draft as Finding;
    if (draft.direction === 'response') this.response.push(f);
    else this.request.push(f);
  }
}

function paramKey(p: ParamModel): string {
  return `${p.in}:${p.name}`;
}

/** Wrap a fully-validated body schema instance as a request witness. */
function requestBodyWrap(method: string, path: string): (instance: JsonValue) => JsonValue {
  return (instance) => ({ request: { method: method.toUpperCase(), path, body: instance } });
}

/** Wrap a fully-validated parameter schema instance in its carrier. */
function requestParamWrap(method: string, path: string, carrier: string, name: string): (instance: JsonValue) => JsonValue {
  return (instance) => ({ request: { method: method.toUpperCase(), path, [carrier]: { [name]: instance } } });
}

function responseWrap(status: string): (instance: JsonValue) => JsonValue {
  return (instance) => ({ response: { status, body: instance } });
}

/**
 * Diff two normalized contracts. Produces directional findings with concrete
 * minimal witnesses; never throws on schema uncertainty — unknown constructs
 * become `undetermined` findings instead.
 */
export function diffContracts(oldDoc: DocumentBundle, newDoc: DocumentBundle): DiffResult {
  const collector = new FindingCollector();
  const oldResolver = resolverOf(oldDoc);
  const newResolver = resolverOf(newDoc);
  const oldOps = new Map(oldDoc.model.operations.map((op) => [op.id, op]));
  const newOps = new Map(newDoc.model.operations.map((op) => [op.id, op]));

  for (const [id, oldOp] of oldOps) {
    const newOp = newOps.get(id);
    if (!newOp) {
      operationRemoved(collector, oldOp);
      continue;
    }
    diffOperation(collector, oldOp, newOp, oldResolver, newResolver);
  }
  // Operations only in NEW are additions: non-breaking, not recorded.

  const oldExt = new Set(oldDoc.model.extensions);
  const newExt = new Set(newDoc.model.extensions);
  const extensionNotes: DiffResult['extensionNotes'] = [];
  for (const e of newExt) if (!oldExt.has(e)) extensionNotes.push({ extension: e, change: 'added' });
  for (const e of oldExt) if (!newExt.has(e)) extensionNotes.push({ extension: e, change: 'removed' });

  assignIds(collector.request);
  assignIds(collector.response);
  const all = [...collector.request, ...collector.response];
  return {
    requestFindings: collector.request,
    responseFindings: collector.response,
    extensionNotes,
    compatible: !all.some((f) => f.severity === 'breaking'),
  };
}

function assignIds(findings: Finding[]): void {
  findings.forEach((f, i) => {
    f.id = `F${String(i + 1).padStart(3, '0')}`;
  });
}

function operationRemoved(c: FindingCollector, op: OperationModel): void {
  const path = concretePath(op.path);
  const witness: Witness = {
    description: `request to ${op.method.toUpperCase()} ${op.path}, an operation present in the OLD contract`,
    value: { request: { method: op.method.toUpperCase(), path } },
    acceptedBy: `OLD contract declares ${op.id}`,
    rejectedBy: 'NEW contract has no such operation (method/path pair removed)',
  };
  c.add({
    direction: 'request',
    category: 'OPERATION_REMOVED',
    severity: 'breaking',
    operation: op.id,
    location: `$.paths${op.path}.${op.method}`,
    message: `operation ${op.id} was removed; every OLD request targeting it is rejected by NEW`,
    oldSide: 'operation declared',
    newSide: 'operation absent',
    witness,
  });
}

function concretePath(pathTemplate: string): string {
  return pathTemplate.replace(/\{[^}]+\}/g, 'x');
}

function diffOperation(
  c: FindingCollector,
  oldOp: OperationModel,
  newOp: OperationModel,
  oldResolver: SchemaResolver,
  newResolver: SchemaResolver,
): void {
  diffParameters(c, oldOp, newOp, oldResolver, newResolver);
  diffRequestBody(c, oldOp, newOp, oldResolver, newResolver);
  diffResponses(c, oldOp, newOp, oldResolver, newResolver);
}

function diffParameters(
  c: FindingCollector,
  oldOp: OperationModel,
  newOp: OperationModel,
  oldResolver: SchemaResolver,
  newResolver: SchemaResolver,
): void {
  const oldByKey = new Map(oldOp.parameters.map((p) => [paramKey(p), p]));
  const newByKey = new Map(newOp.parameters.map((p) => [paramKey(p), p]));
  const makeWrap = (carrier: string, name: string) => requestParamWrap(oldOp.method, concretePath(oldOp.path), carrier, name);

  // Same parameter NAME at a DIFFERENT location: a move, not an add/remove.
  const oldByName = new Map<string, ParamModel[]>();
  for (const p of oldOp.parameters) {
    const list = oldByName.get(p.name) ?? [];
    list.push(p);
    oldByName.set(p.name, list);
  }
  const relocatedOldKeys = new Set<string>();
  const relocatedNewKeys = new Set<string>();
  for (const np of newOp.parameters) {
    const olds = (oldByName.get(np.name) ?? []).filter((p) => p.in !== np.in);
    if (olds.length > 0 && !newByKey.has(`${olds[0]!.in}:${np.name}`)) {
      const oldP = olds[0]!;
      relocatedOldKeys.add(paramKey(oldP));
      relocatedNewKeys.add(paramKey(np));
      paramLocationChanged(c, oldOp, oldP, np, makeWrap(oldP.in, oldP.name), oldResolver);
    }
  }

  for (const [key, oldP] of oldByKey) {
    if (relocatedOldKeys.has(key)) continue;
    const newP = newByKey.get(key);
    if (!newP) {
      paramRemoved(c, oldOp, oldP, makeWrap(oldP.in, oldP.name), oldResolver);
      continue;
    }
    paramCommon(c, oldOp, oldP, newP, oldResolver, newResolver, makeWrap(oldP.in, oldP.name));
  }

  for (const [key, newP] of newByKey) {
    if (relocatedNewKeys.has(key)) continue;
    if (!oldByKey.has(key) && newP.required) {
      // Newly appearing required parameter: old clients omit it, NEW rejects.
      paramRequiredWitness(c, oldOp, newP);
    }
  }
}

function paramLocationChanged(
  c: FindingCollector,
  oldOp: OperationModel,
  oldP: ParamModel,
  newP: ParamModel,
  wrap: (instance: JsonValue) => JsonValue,
  oldResolver: SchemaResolver,
): void {
  const instance = sample(oldP.schema, newCtx(oldResolver)) ?? 'x';
  c.add({
    direction: 'request',
    category: 'PARAM_LOCATION_CHANGED',
    severity: 'breaking',
    operation: oldOp.id,
    location: `parameters[${oldP.name}]: ${oldP.in} -> ${newP.in}`,
    message: `parameter "${oldP.name}" moved from ${oldP.in} to ${newP.in}; requests placing it where the OLD contract says are no longer understood`,
    oldSide: `"${oldP.name}" read from ${oldP.in}${oldP.required ? ' (required)' : ''}`,
    newSide: `"${newP.name}" read from ${newP.in}${newP.required ? ' (required)' : ''}`,
    witness: {
      description: `request carrying ${oldP.name} in ${oldP.in} as the OLD contract requires`,
      value: wrap(instance),
      acceptedBy: `OLD contract reads ${oldP.name} from ${oldP.in}`,
      rejectedBy: `NEW contract reads ${oldP.name} from ${newP.in}; the ${oldP.in} value is ignored${newP.required ? ` and ${newP.in} is now missing` : ''}`,
    },
  });
}

function paramRemoved(
  c: FindingCollector,
  oldOp: OperationModel,
  oldP: ParamModel,
  wrap: (instance: JsonValue) => JsonValue,
  oldResolver: SchemaResolver,
): void {
  const instance = sample(oldP.schema, newCtx(oldResolver)) ?? 'x';
  c.add({
    direction: 'request',
    category: 'PARAM_REMOVED',
    severity: 'undetermined',
    operation: oldOp.id,
    location: `parameters[${oldP.in}:${oldP.name}]`,
    message: `parameter "${oldP.name}" (${oldP.in}) was removed; a strict NEW server may reject requests that still carry it`,
    oldSide: `parameter declared${oldP.required ? ', required' : ''}`,
    newSide: 'parameter absent',
    uncertainty: 'OpenAPI does not state whether the NEW server rejects unknown parameters; lenient servers ignore them',
    witness: {
      description: `OLD request still carrying ${oldP.in} parameter ${oldP.name}`,
      value: wrap(instance),
      acceptedBy: `OLD contract declares ${oldP.name} in ${oldP.in}`,
      rejectedBy: 'unknown: NEW contract is silent on unknown parameters',
    },
  });
}

function paramRequiredWitness(
  c: FindingCollector,
  oldOp: OperationModel,
  newP: ParamModel,
): void {
  c.add({
    direction: 'request',
    category: 'PARAM_MADE_REQUIRED',
    severity: 'breaking',
    operation: oldOp.id,
    location: `parameters[${newP.in}:${newP.name}].required`,
    message: `parameter "${newP.name}" (${newP.in}) became required; OLD requests omitting it are rejected`,
    oldSide: 'parameter optional or absent',
    newSide: 'parameter required: true',
    witness: {
      description: `request omitting ${newP.in} parameter ${newP.name}`,
      value: { request: { method: oldOp.method.toUpperCase(), path: concretePath(oldOp.path) } },
      acceptedBy: 'OLD contract allows the parameter to be omitted',
      rejectedBy: `NEW contract requires ${newP.name} in ${newP.in}`,
    },
  });
}

function paramCommon(
  c: FindingCollector,
  oldOp: OperationModel,
  oldP: ParamModel,
  newP: ParamModel,
  oldResolver: SchemaResolver,
  newResolver: SchemaResolver,
  wrap: (instance: JsonValue) => JsonValue,
): void {
  if (!oldP.required && newP.required) {
    paramRequiredWitness(c, oldOp, newP);
  }
  if (oldP.hasDefault && newP.hasDefault && JSON.stringify(oldP.defaultValue) !== JSON.stringify(newP.defaultValue)) {
    c.add({
      direction: 'request',
      category: 'DEFAULT_VALUE_CHANGED',
      severity: 'informational',
      operation: oldOp.id,
      location: `parameters[${oldP.in}:${oldP.name}].schema.default`,
      message: `default of "${oldP.name}" changed ${JSON.stringify(oldP.defaultValue)} -> ${JSON.stringify(newP.defaultValue)}; omitted parameter resolves differently`,
      oldSide: `default ${JSON.stringify(oldP.defaultValue)}`,
      newSide: `default ${JSON.stringify(newP.defaultValue)}`,
      witness: {
        description: `request omitting ${oldP.name}`,
        value: wrap(null),
        acceptedBy: `OLD fills ${JSON.stringify(oldP.defaultValue)}`,
        rejectedBy: `NEW fills ${JSON.stringify(newP.defaultValue)} (semantic change, not a rejection)`,
      },
    });
  }
  diffSchemas('request', `parameters[${oldP.in}:${oldP.name}].schema`,
    oldP.schema, newP.schema, oldResolver, newResolver, wrap, (f) => c.add(f), oldOp.id);
}

function diffRequestBody(
  c: FindingCollector,
  oldOp: OperationModel,
  newOp: OperationModel,
  oldResolver: SchemaResolver,
  newResolver: SchemaResolver,
): void {
  const oldBody = oldOp.requestBody;
  const newBody = newOp.requestBody;
  const bodyWrap = requestBodyWrap(oldOp.method, concretePath(oldOp.path));

  if (oldBody && !newBody) {
    c.add({
      direction: 'request',
      category: 'REQUEST_BODY_REMOVED',
      severity: 'breaking',
      operation: oldOp.id,
      location: 'requestBody',
      message: 'requestBody removed; a conforming NEW server rejects the payload OLD clients send',
      oldSide: `requestBody declared (${oldBody.contentType}, required=${oldBody.required})`,
      newSide: 'requestBody absent',
      witness: {
        description: 'OLD request carrying a JSON body',
        value: { request: { method: oldOp.method.toUpperCase(), path: concretePath(oldOp.path), body: {} } },
        acceptedBy: 'OLD contract declares a requestBody',
        rejectedBy: 'NEW contract declares no requestBody',
      },
    });
    return;
  }
  if (!oldBody && newBody) {
    c.add({
      direction: 'request',
      category: 'REQUEST_BODY_MADE_REQUIRED',
      severity: newBody.required ? 'breaking' : 'informational',
      operation: oldOp.id,
      location: 'requestBody.required',
      message: newBody.required
        ? 'requestBody introduced as required; OLD requests without a body are rejected'
        : 'optional requestBody introduced; non-breaking',
      oldSide: 'no requestBody',
      newSide: `requestBody (required=${newBody.required})`,
      witness: {
        description: 'OLD request without a body',
        value: { request: { method: oldOp.method.toUpperCase(), path: concretePath(oldOp.path) } },
        acceptedBy: 'OLD contract has no requestBody',
        rejectedBy: newBody.required ? 'NEW contract requires a body' : 'NEW body is optional; accepted',
      },
    });
    if (newBody.required) return;
  }
  if (oldBody && newBody) {
    if (!oldBody.required && newBody.required) {
      c.add({
        direction: 'request',
        category: 'REQUEST_BODY_MADE_REQUIRED',
        severity: 'breaking',
        operation: oldOp.id,
        location: 'requestBody.required',
        message: 'requestBody became required; OLD requests without a body are rejected',
        oldSide: 'required=false',
        newSide: 'required=true',
        witness: {
          description: 'request without a body',
          value: { request: { method: oldOp.method.toUpperCase(), path: concretePath(oldOp.path) } },
          acceptedBy: 'OLD contract allows an omitted body',
          rejectedBy: 'NEW contract requires a body',
        },
      });
    }
    if (oldBody.contentType !== newBody.contentType) {
      c.add({
        direction: 'request',
        category: 'RESPONSE_CONTENT_TYPE_REMOVED', // reused category, request direction
        severity: 'undetermined',
        operation: oldOp.id,
        location: 'requestBody.content',
        message: `request content type changed ${oldBody.contentType} -> ${newBody.contentType}`,
        oldSide: oldBody.contentType,
        newSide: newBody.contentType,
        uncertainty: 'content-type negotiation may reject OLD payloads; no witness derived in this subset',
        witness: {
          description: 'OLD request content type',
          value: { request: { method: oldOp.method.toUpperCase(), path: concretePath(oldOp.path), contentType: oldBody.contentType } },
          acceptedBy: `OLD accepts ${oldBody.contentType}`,
          rejectedBy: 'unknown',
        },
      });
    }
    diffSchemas(
      'request',
      'requestBody.content.schema',
      oldBody.schema,
      newBody.schema,
      oldResolver,
      newResolver,
      bodyWrap,
      (f) => c.add(f),
      oldOp.id,
    );
  }
}

function diffResponses(
  c: FindingCollector,
  oldOp: OperationModel,
  newOp: OperationModel,
  oldResolver: SchemaResolver,
  newResolver: SchemaResolver,
): void {
  const oldByStatus = new Map(oldOp.responses.map((r) => [r.status, r]));
  const newByStatus = new Map(newOp.responses.map((r) => [r.status, r]));

  // NEW status that OLD clients have no documented handling for -> breaking.
  for (const [status, newR] of newByStatus) {
    if (status === 'default') continue; // adding a catch-all is additive
    if (!oldByStatus.has(status) && !oldByStatus.has('default')) {
      c.add({
        direction: 'response',
        category: 'STATUS_CODE_SPLIT',
        severity: 'breaking',
        operation: oldOp.id,
        location: `responses.${status}`,
        message: `response status ${status} is newly produced; OLD clients only handle ${[...oldByStatus.keys()].join(', ')}`,
        oldSide: `statuses ${[...oldByStatus.keys()].join(', ')}`,
        newSide: `includes ${status}`,
        witness: {
          description: `NEW response with status ${status}`,
          value: { response: { status, body: null } },
          acceptedBy: `NEW contract documents ${status}`,
          rejectedBy: `OLD client has no response (and no default) for ${status}`,
        },
      });
    }
    const oldR = oldByStatus.get(status);
    if (oldR) {
      compareResponsePayload(c, oldOp, status, oldR.schema, newR.schema, oldResolver, newResolver, oldR.contentType, newR.contentType);
    }
  }

  // OLD status disappearing from the contract -> undetermined.
  for (const [status] of oldByStatus) {
    if (status === 'default') continue;
    if (!newByStatus.has(status) && !newByStatus.has('default')) {
      c.add({
        direction: 'response',
        category: 'STATUS_CODE_REMOVED',
        severity: 'undetermined',
        operation: oldOp.id,
        location: `responses.${status}`,
        message: `response status ${status} is no longer documented; the NEW server may still return it out of contract`,
        oldSide: `status ${status} documented`,
        newSide: 'status absent and no default response',
        uncertainty: 'documented removal does not prove the server stopped emitting the status',
        witness: {
          description: `response with status ${status} as documented in the OLD contract`,
          value: { response: { status, body: null } },
          acceptedBy: `OLD clients handle ${status}`,
          rejectedBy: 'unknown: NEW contract neither produces nor disclaims it',
        },
      });
    }
  }
}

function compareResponsePayload(
  c: FindingCollector,
  oldOp: OperationModel,
  status: string,
  oldSchema: RawSchema | undefined,
  newSchema: RawSchema | undefined,
  oldResolver: SchemaResolver,
  newResolver: SchemaResolver,
  oldCt?: string,
  newCt?: string,
): void {
  if (oldCt && newCt && oldCt !== newCt) {
    c.add({
      direction: 'response',
      category: 'RESPONSE_CONTENT_TYPE_REMOVED',
      severity: 'breaking',
      operation: oldOp.id,
      location: `responses.${status}.content`,
      message: `response ${status} content type changed ${oldCt} -> ${newCt}; OLD JSON-only clients cannot parse it`,
      oldSide: oldCt,
      newSide: newCt,
      witness: {
        description: `NEW ${status} response in ${newCt}`,
        value: { response: { status, contentType: newCt, body: null } },
        acceptedBy: `NEW contract produces ${newCt}`,
        rejectedBy: `OLD client only accepts ${oldCt}`,
      },
    });
  }
  diffSchemas(
    'response',
    `responses.${status}.content.schema`,
    oldSchema,
    newSchema,
    oldResolver,
    newResolver,
    responseWrap(status),
    (f) => c.add(f),
    oldOp.id,
  );
}
