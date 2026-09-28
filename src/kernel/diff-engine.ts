/**
 * Diff engine: orchestrates parsing and directional comparison.
 *
 * Directional model
 * -----------------
 * request  : OLD contract produces requests (old client), NEW contract consumes
 *            them (new server). A breaking request finding carries a request an
 *            old client could legitimately send that the new server rejects.
 * response : NEW contract produces responses (new server), OLD contract
 *            consumes them (old client). A breaking response finding carries a
 *            response the new server could return that an old client cannot
 *            handle.
 *
 * Parameter identity is `${in}:${name}` — the same name moving between query
 * and header is a PARAM_LOCATION_CHANGED, not a remove+add.
 */
import { randomUUID } from 'node:crypto';
import { ContractParser } from '../parser/contract-parser.js';
import type {
  DiffResult,
  Direction,
  Finding,
  JsonValue,
  NormalizedOperation,
  NormalizedParameter,
  NormalizedSchema,
  TraceStep,
  Uncertainty,
  Witness,
} from '../core/types.js';
import {
  compareSchemas,
  type Segment,
} from './schema-compare.js';
import { sampleFromSchema } from './witness.js';

export interface EngineDiff {
  result: DiffResult;
  /** parser rejects hard (non-object doc); surfaced to the HTTP layer as 422 */
  error?: { message: string };
}

const PARAM_ROOT_CODES = {
  typeNarrowed: 'PARAM_TYPE_NARROWED',
  enumNarrowed: 'PARAM_ENUM_NARROWED',
  enumRelaxed: 'PARAM_ENUM_RELAXED',
  defaultChanged: 'PARAM_DEFAULT_CHANGED',
  defaultRemoved: 'PARAM_DEFAULT_REMOVED',
  defaultAdded: 'PARAM_DEFAULT_ADDED',
} as const;

const REQUEST_BODY_ROOT_CODES = {
  typeNarrowed: 'REQUEST_BODY_TYPE_NARROWED',
  enumNarrowed: 'REQUEST_BODY_ENUM_NARROWED',
  enumRelaxed: 'REQUEST_BODY_ENUM_RELAXED',
  defaultChanged: 'REQUEST_BODY_DEFAULT_CHANGED',
  defaultRemoved: 'REQUEST_BODY_DEFAULT_REMOVED',
  defaultAdded: 'REQUEST_BODY_DEFAULT_ADDED',
} as const;

const REQUEST_FIELD_CODES = {
  typeNarrowed: 'REQUEST_BODY_FIELD_TYPE_NARROWED',
  enumNarrowed: 'REQUEST_BODY_FIELD_ENUM_NARROWED',
  enumRelaxed: 'REQUEST_BODY_FIELD_ENUM_RELAXED',
  defaultChanged: 'REQUEST_BODY_FIELD_DEFAULT_CHANGED',
  defaultRemoved: 'REQUEST_BODY_FIELD_DEFAULT_REMOVED',
  defaultAdded: 'REQUEST_BODY_FIELD_DEFAULT_ADDED',
} as const;

const RESPONSE_ROOT_CODES = {
  typeNarrowed: 'RESPONSE_TYPE_NARROWED',
  enumNarrowed: 'RESPONSE_ENUM_NARROWED',
  enumRelaxed: 'RESPONSE_ENUM_RELAXED',
  defaultChanged: 'RESPONSE_FIELD_DEFAULT_CHANGED',
  defaultRemoved: 'RESPONSE_FIELD_DEFAULT_CHANGED',
  defaultAdded: 'RESPONSE_FIELD_DEFAULT_CHANGED',
} as const;

const RESPONSE_FIELD_CODES = {
  typeNarrowed: 'RESPONSE_FIELD_TYPE_NARROWED',
  enumNarrowed: 'RESPONSE_FIELD_ENUM_NARROWED',
  enumRelaxed: 'RESPONSE_FIELD_ENUM_RELAXED',
  defaultChanged: 'RESPONSE_FIELD_DEFAULT_CHANGED',
  defaultRemoved: 'RESPONSE_FIELD_DEFAULT_CHANGED',
  defaultAdded: 'RESPONSE_FIELD_DEFAULT_CHANGED',
} as const;

export class DiffEngine {
  diff(oldDocument: unknown, newDocument: unknown, requestId: string = randomUUID()): EngineDiff {
    const steps: TraceStep[] = [];
    const uncertainties: Uncertainty[] = [];
    const findings: Finding[] = [];
    let stepIndex = 0;
    const trace = (phase: TraceStep['phase'], location: string, detail: string): void => {
      steps.push({ index: stepIndex += 1, phase, location, detail });
    };

    const parser = new ContractParser();

    trace('parse-old', '$', 'parsing old contract');
    let oldParsed;
    try {
      oldParsed = parser.parse(oldDocument);
    } catch (err) {
      return {
        result: this.emptyResult(requestId, steps, (err as Error).message),
        error: { message: (err as Error).message },
      };
    }
    uncertainties.push(...oldParsed.uncertainties);
    trace('parse-old', '$', `parsed ${oldParsed.contract.operations.size} operations`);

    trace('parse-new', '$', 'parsing new contract');
    let newParsed;
    try {
      newParsed = parser.parse(newDocument);
    } catch (err) {
      return {
        result: this.emptyResult(requestId, steps, (err as Error).message),
        error: { message: (err as Error).message },
      };
    }
    uncertainties.push(...newParsed.uncertainties);
    trace('parse-new', '$', `parsed ${newParsed.contract.operations.size} operations`);

    const oldContract = oldParsed.contract;
    const newContract = newParsed.contract;

    const allKeys = new Set([...oldContract.operations.keys(), ...newContract.operations.keys()]);
    let operationsCompared = 0;

    for (const opKey of [...allKeys].sort()) {
      const oldOp = oldContract.operations.get(opKey);
      const newOp = newContract.operations.get(opKey);
      const loc = opKey;

      if (oldOp && !newOp) {
        trace('compare', loc, 'operation removed');
        findings.push(this.finding(requestId, 'request', 'OPERATION_REMOVED', 'BREAKING', opKey,
          `operation ${opKey} exists in v${oldContract.version} but was removed in v${newContract.version}`,
          undefined, undefined,
          {
            location: opKey,
            example: { method: oldOp.method, path: oldOp.path },
            rationale: 'an old client calling this operation receives no matching route (typically 404/405)',
          }));
        continue;
      }
      if (!oldOp && newOp) {
        trace('compare', loc, 'operation added');
        findings.push(this.finding(requestId, 'request', 'OPERATION_ADDED', 'NON_BREAKING', opKey,
          `operation ${opKey} was added in v${newContract.version}`,
          undefined, undefined, null));
        continue;
      }
      if (!oldOp || !newOp) continue; // unreachable, keeps types narrow

      operationsCompared += 1;
      trace('compare', loc, 'comparing parameters, request body and responses');
      this.compareParameters(requestId, oldOp, newOp, findings, trace);
      this.compareRequestBody(requestId, oldOp, newOp, findings, uncertainties, trace);
      this.compareResponses(requestId, oldOp, newOp, findings, uncertainties, trace);
    }

    trace('finalize', '$', 'ordering findings and computing verdict');
    const ordered = this.orderFindings(findings);
    const stats = {
      operationsCompared,
      operationsAdded: [...allKeys].filter((k) => !oldContract.operations.has(k)).length,
      operationsRemoved: [...allKeys].filter((k) => !newContract.operations.has(k)).length,
      breaking: ordered.filter((f) => f.severity === 'BREAKING').length,
      nonBreaking: ordered.filter((f) => f.severity === 'NON_BREAKING').length,
      uncertain: ordered.filter((f) => f.severity === 'UNCERTAIN').length,
    };

    return {
      result: {
        requestId,
        oldTitle: oldContract.title,
        newTitle: newContract.title,
        oldVersion: oldContract.version,
        newVersion: newContract.version,
        compatible: stats.breaking === 0,
        findings: ordered,
        uncertainties,
        steps,
        stats,
      },
    };
  }

  // -- parameters ----------------------------------------------------------

  private compareParameters(
    requestId: string,
    oldOp: NormalizedOperation,
    newOp: NormalizedOperation,
    findings: Finding[],
    trace: (phase: TraceStep['phase'], location: string, detail: string) => void,
  ): void {
    const opKey = `${oldOp.method} ${oldOp.path}`;

    // Detect same-name, different-location parameters explicitly.
    // A pure MOVE requires both: the new key is absent old-side AND the old
    // same-name key no longer exists new-side. If the old key survives (e.g.
    // path:id stays while query:id appears), this is an addition, not a move.
    const relocated = new Set<string>();
    for (const newParam of newOp.parameters.values()) {
      if (oldOp.parameters.has(newParam.key)) continue;
      const sameName = [...oldOp.parameters.values()].find((p) => p.name === newParam.name);
      if (!sameName || newOp.parameters.has(sameName.key)) continue;
      relocated.add(sameName.key);
      relocated.add(newParam.key);
      const witnessValue = sampleFromSchema(sameName.schema);
      findings.push(this.finding(requestId, 'request', 'PARAM_LOCATION_CHANGED', 'BREAKING', opKey,
        `parameter "${newParam.name}" moved from ${sameName.in} to ${newParam.in}`,
        { name: sameName.name, in: sameName.in },
        { name: newParam.name, in: newParam.in },
        {
          location: `${opKey} [${sameName.in} param "${sameName.name}"]`,
          example: { name: sameName.name, in: sameName.in, value: witnessValue },
          rationale: `the old client sends "${sameName.name}" in the ${sameName.in}, but the new server only reads it from the ${newParam.in}; the value is silently ignored`,
        }));
      trace('compare', `${opKey}.parameters.${newParam.name}`, `location ${sameName.in} -> ${newParam.in}`);
    }

    for (const [key, newParam] of newOp.parameters) {
      if (relocated.has(key)) continue;
      const oldParam = oldOp.parameters.get(key);
      if (!oldParam) {
        const code = newParam.required ? 'REQUIRED_PARAM_ADDED' : 'OPTIONAL_PARAM_ADDED';
        const severity = newParam.required ? 'BREAKING' : 'NON_BREAKING';
        findings.push(this.finding(requestId, 'request', code, severity, opKey,
          `${newParam.required ? 'required' : 'optional'} ${newParam.in} parameter "${newParam.name}" was added`,
          undefined, { name: newParam.name, in: newParam.in, required: newParam.required },
          newParam.required
            ? {
                location: this.requestLocation(oldOp, newParam),
                example: this.minimalOldRequest(oldOp, newParam),
                rationale: `the old client's request omits the newly required ${newParam.in} parameter "${newParam.name}"`,
              }
            : null));
        continue;
      }
      this.compareSharedParameter(requestId, oldOp, oldParam, newParam, findings, trace);
    }

    for (const [key, oldParam] of oldOp.parameters) {
      if (relocated.has(key)) continue;
      if (!newOp.parameters.has(key)) {
        findings.push(this.finding(requestId, 'request', 'PARAM_REMOVED', 'NON_BREAKING', opKey,
          `${oldParam.in} parameter "${oldParam.name}" was removed; old clients may still send it and the server ignores it`,
          { name: oldParam.name, in: oldParam.in }, undefined, null));
      }
    }
  }

  private compareSharedParameter(
    requestId: string,
    op: NormalizedOperation,
    oldParam: NormalizedParameter,
    newParam: NormalizedParameter,
    findings: Finding[],
    trace: (phase: TraceStep['phase'], location: string, detail: string) => void,
  ): void {
    const opKey = `${op.method} ${op.path}`;
    const loc = `${opKey}.parameters.${newParam.key}`;

    if (!oldParam.required && newParam.required) {
      findings.push(this.finding(requestId, 'request', 'PARAM_BECAME_REQUIRED', 'BREAKING', opKey,
        `parameter "${newParam.name}" changed from optional to required`,
        false, true,
        {
          location: this.requestLocation(op, newParam),
          example: this.minimalOldRequest(op, newParam),
          rationale: `an old client can omit the optional ${newParam.in} parameter "${newParam.name}", but the new server rejects requests without it`,
        }));
      trace('compare', loc, 'optional -> required');
    } else if (oldParam.required && !newParam.required) {
      findings.push(this.finding(requestId, 'request', 'PARAM_BECAME_OPTIONAL', 'NON_BREAKING', opKey,
        `parameter "${newParam.name}" changed from required to optional`,
        true, false, null));
    }

    const subFindings = compareSchemas(oldParam.schema, newParam.schema, {
      hooks: {
        direction: 'request',
        rootCodes: PARAM_ROOT_CODES,
        fieldCodes: PARAM_ROOT_CODES, // params rarely nest; reuse param codes
        classifyConsumerOnlyField: () => ({ code: 'OPTIONAL_PARAM_ADDED', severity: 'NON_BREAKING' }),
        classifyProducerOnlyField: () => ({ code: 'PARAM_REMOVED', severity: 'NON_BREAKING' }),
        classifyRequirementFlip: () => ({ code: 'PARAM_BECAME_REQUIRED', severity: 'BREAKING' }),
        classifyEnumWidening: () => ({ code: 'PARAM_ENUM_EXTENDED', severity: 'NON_BREAKING' }),      },
      baseLocation: loc,
      renderWitness: (schemaPath, _segments, leaf, rationale) => ({
        location: this.requestLocation(op, newParam),
        // NB: leaf may legitimately be null (nullability tightening); do not
        // use ??, which would replace null with a fallback sample.
        example: { name: newParam.name, in: newParam.in, value: leaf === undefined ? sampleFromSchema(oldParam.schema) : leaf },
        rationale,
      }),
    });
    this.ingest(requestId, subFindings, findings, 'request', opKey);
  }

  // -- request body --------------------------------------------------------

  private compareRequestBody(
    requestId: string,
    oldOp: NormalizedOperation,
    newOp: NormalizedOperation,
    findings: Finding[],
    uncertainties: Uncertainty[],
    trace: (phase: TraceStep['phase'], location: string, detail: string) => void,
  ): void {
    const opKey = `${oldOp.method} ${oldOp.path}`;
    const loc = `${opKey}.requestBody`;
    const oldSchema = this.jsonSchema(oldOp.requestBody?.content ?? null);
    const newSchema = this.jsonSchema(newOp.requestBody?.content ?? null);
    if (oldSchema.kind === 'ambiguous' || newSchema.kind === 'ambiguous') {
      uncertainties.push({
        code: 'AMBIGUOUS_MEDIA_TYPE',
        location: loc,
        detail: 'request body content types differ or are non-JSON; subset comparison skipped',
      });
      return;
    }

    if (!oldOp.requestBody && newOp.requestBody) {
      const breaking = newOp.requestBody.required;
      findings.push(this.finding(requestId, 'request', 'REQUEST_BODY_ADDED',
        breaking ? 'BREAKING' : 'NON_BREAKING', opKey,
        `${breaking ? 'required' : 'optional'} request body was added`,
        undefined, { required: newOp.requestBody.required },
        breaking
          ? {
              location: opKey,
              example: this.requestEnvelope(oldOp, undefined),
              rationale: 'the old client sends no body, but the new server requires one',
            }
          : null));
      trace('compare', loc, breaking ? 'required body added' : 'optional body added');
      return;
    }
    if (oldOp.requestBody && !newOp.requestBody) {
      findings.push(this.finding(requestId, 'request', 'REQUEST_BODY_REMOVED', 'NON_BREAKING', opKey,
        'request body was removed; bodies sent by old clients are ignored',
        { required: oldOp.requestBody.required }, undefined, null));
      return;
    }
    if (!oldSchema.schema || !newSchema.schema) return;
    if (!oldOp.requestBody || !newOp.requestBody) return;

    if (!oldOp.requestBody.required && newOp.requestBody.required) {
      findings.push(this.finding(requestId, 'request', 'REQUEST_BODY_BECAME_REQUIRED', 'BREAKING', opKey,
        'request body changed from optional to required',
        false, true,
        {
          location: opKey,
          example: this.requestEnvelope(oldOp, undefined),
          rationale: 'an old client may omit the body, but the new server rejects bodyless requests',
        }));
      trace('compare', loc, 'body optional -> required');
    }

    const subFindings = compareSchemas(oldSchema.schema, newSchema.schema, {
      hooks: {
        direction: 'request',
        rootCodes: REQUEST_BODY_ROOT_CODES,
        fieldCodes: REQUEST_FIELD_CODES,
        classifyConsumerOnlyField: (_name, consumerRequired) => ({
          code: consumerRequired ? 'REQUEST_BODY_REQUIRED_FIELD_ADDED' : 'REQUEST_BODY_OPTIONAL_FIELD_ADDED',
          severity: consumerRequired ? 'BREAKING' : 'NON_BREAKING',
        }),
        classifyProducerOnlyField: () => ({ code: 'REQUEST_BODY_FIELD_REMOVED', severity: 'NON_BREAKING' }),
        classifyRequirementFlip: (_name, consumerRequired) => ({
          code: consumerRequired ? 'REQUEST_BODY_FIELD_BECAME_REQUIRED' : 'REQUEST_BODY_FIELD_BECAME_OPTIONAL',
          severity: consumerRequired ? 'BREAKING' : 'NON_BREAKING',
        }),
        classifyEnumWidening: (_added, atRoot) => ({
          code: atRoot ? 'REQUEST_BODY_ENUM_EXTENDED' : 'REQUEST_BODY_FIELD_ENUM_EXTENDED',
          severity: 'NON_BREAKING',
        }),
      },
      baseLocation: loc,
      renderWitness: (schemaPath, segments, leaf, rationale) => ({
        location: opKey,
        example: this.requestEnvelope(
          oldOp,
          leaf === undefined
            ? sampleFromSchema(oldSchema.schema as NormalizedSchema)
            : buildExampleAt(oldSchema.schema as NormalizedSchema, segments, leaf),
        ),
        rationale,
      }),
    });
    this.ingest(requestId, subFindings, findings, 'request', opKey);
  }

  // -- responses -----------------------------------------------------------

  private compareResponses(
    requestId: string,
    oldOp: NormalizedOperation,
    newOp: NormalizedOperation,
    findings: Finding[],
    uncertainties: Uncertainty[],
    trace: (phase: TraceStep['phase'], location: string, detail: string) => void,
  ): void {
    const opKey = `${oldOp.method} ${oldOp.path}`;

    for (const code of newOp.responses.keys()) {
      if (!oldOp.responses.has(code)) {
        const newResp = newOp.responses.get(code) as NonNullable<ReturnType<NormalizedOperation['responses']['get']>>;
        const schemaPick = this.jsonSchema(newResp.content);
        if (schemaPick.kind === 'ambiguous') {
          uncertainties.push({ code: 'AMBIGUOUS_MEDIA_TYPE', location: `${opKey}.responses.${code}`, detail: 'non-JSON/ambiguous content type skipped' });
        }
        findings.push(this.finding(requestId, 'response', 'RESPONSE_STATUS_ADDED', 'BREAKING', opKey,
          `response status ${code} can now be returned but is not documented for old clients`,
          undefined, code,
          {
            location: `${opKey} -> ${code}`,
            example: { status: Number(code) || code, body: schemaPick.schema ? sampleFromSchema(schemaPick.schema) : null },
            rationale: `the new server may answer ${code}; an old client with no branch for that status cannot handle it`,
          }));
        trace('compare', `${opKey}.responses.${code}`, 'new status code');
      }
    }
    for (const code of oldOp.responses.keys()) {
      if (!newOp.responses.has(code)) {
        findings.push(this.finding(requestId, 'response', 'RESPONSE_STATUS_REMOVED', 'NON_BREAKING', opKey,
          `response status ${code} is no longer documented; old client handling for it becomes unreachable but harmless`,
          code, undefined, null));
      }
    }

    for (const [code, newResp] of newOp.responses) {
      const oldResp = oldOp.responses.get(code);
      if (!oldResp) continue;
      const oldPick = this.jsonSchema(oldResp.content);
      const newPick = this.jsonSchema(newResp.content);
      const loc = `${opKey}.responses.${code}`;
      if (oldPick.kind === 'ambiguous' || newPick.kind === 'ambiguous') {
        uncertainties.push({ code: 'AMBIGUOUS_MEDIA_TYPE', location: loc, detail: 'content types differ or are non-JSON; skipped' });
        continue;
      }
      if (!oldPick.schema || !newPick.schema) continue;

      const subFindings = compareSchemas(newPick.schema, oldPick.schema, {
        hooks: {
          direction: 'response',
          rootCodes: RESPONSE_ROOT_CODES,
          fieldCodes: RESPONSE_FIELD_CODES,
          // field on consumer(=OLD) only: old models a field new never sends
          classifyConsumerOnlyField: (_name, consumerRequired) => ({
            code: consumerRequired ? 'RESPONSE_REQUIRED_FIELD_REMOVED' : 'RESPONSE_OPTIONAL_FIELD_REMOVED',
            severity: consumerRequired ? 'BREAKING' : 'NON_BREAKING',
          }),
          // field on producer(NEW) only: server adds a field old clients ignore
          classifyProducerOnlyField: () => ({ code: 'RESPONSE_FIELD_ADDED', severity: 'NON_BREAKING' }),
          // response: consumer = OLD. Old-required but new-optional means the
          // server may now omit a field old clients relied on -> BREAKING.
          classifyRequirementFlip: (_name, consumerRequired) => ({
            code: consumerRequired ? 'RESPONSE_FIELD_BECAME_OPTIONAL' : 'RESPONSE_FIELD_BECAME_REQUIRED',
            severity: consumerRequired ? 'BREAKING' : 'NON_BREAKING',
          }),
          classifyEnumWidening: (_added, atRoot) => ({
            code: atRoot ? 'RESPONSE_ENUM_EXTENDED' : 'RESPONSE_FIELD_ENUM_EXTENDED',
            severity: 'NON_BREAKING',
          }),
        },
        baseLocation: loc,
        renderWitness: (schemaPath, segments, leaf, rationale) => ({
          location: `${opKey} -> ${code}${schemaPath ? ` body${schemaPath.replace('$', '')}` : ''}`,
          example: {
            status: Number(code) || code,
            body:
              leaf === undefined
                ? sampleFromSchema(newPick.schema as NormalizedSchema)
                : buildExampleAt(newPick.schema as NormalizedSchema, segments, leaf),
          },
          rationale,
        }),
      });
      this.ingest(requestId, subFindings, findings, 'response', opKey);
    }
  }

  // -- helpers -------------------------------------------------------------

  private jsonSchema(
    content: Map<string, NormalizedSchema> | null,
  ): { schema: NormalizedSchema | null; kind: 'json' | 'empty' | 'ambiguous' } {
    if (!content || content.size === 0) return { schema: null, kind: 'empty' };
    const jsonKey = [...content.keys()].find((k) => k === 'application/json' || k.endsWith('+json'));
    if (jsonKey) return { schema: content.get(jsonKey) ?? null, kind: 'json' };
    return { schema: null, kind: 'ambiguous' };
  }

  private ingest(
    requestId: string,
    subFindings: ReturnType<typeof compareSchemas>,
    findings: Finding[],
    direction: Direction,
    opKey: string,
  ): void {
    for (const sf of subFindings) {
      findings.push({
        id: `${requestId}:${findings.length + 1}`,
        direction,
        code: sf.code,
        severity: sf.severity,
        operation: opKey,
        path: sf.schemaPath,
        message: sf.message,
        oldValue: sf.oldValue,
        newValue: sf.newValue,
        witness: sf.witness,
      });
    }
  }

  private finding(
    requestId: string,
    direction: Direction,
    code: Finding['code'],
    severity: Finding['severity'],
    operation: string,
    message: string,
    oldValue: JsonValue | undefined,
    newValue: JsonValue | undefined,
    witness: Witness | null,
  ): Finding {
    return {
      id: `${requestId}:placeholder`,
      direction,
      code,
      severity,
      operation,
      path: '',
      message,
      oldValue,
      newValue,
      witness,
    };
  }

  private orderFindings(findings: Finding[]): Finding[] {
    const rank = { BREAKING: 0, NON_BREAKING: 1, UNCERTAIN: 2 } as const;
    return findings
      .map((f, i) => ({ ...f, id: `${f.id.split(':')[0]}:${i + 1}` }))
      .sort((a, b) => {
        const bySeverity = rank[a.severity] - rank[b.severity];
        if (bySeverity !== 0) return bySeverity;
        const byOp = (a.operation ?? '').localeCompare(b.operation ?? '');
        if (byOp !== 0) return byOp;
        const byDir = a.direction.localeCompare(b.direction);
        if (byDir !== 0) return byDir;
        return a.code.localeCompare(b.code);
      })
      .map((f, i) => ({ ...f, id: `${f.id.split(':')[0]}:${i + 1}` }));
  }

  private emptyResult(requestId: string, steps: TraceStep[], _error: string): DiffResult {
    return {
      requestId,
      oldTitle: 'unparsable',
      newTitle: 'unparsable',
      oldVersion: '?',
      newVersion: '?',
      compatible: false,
      findings: [],
      uncertainties: [],
      steps,
      stats: { operationsCompared: 0, operationsAdded: 0, operationsRemoved: 0, breaking: 0, nonBreaking: 0, uncertain: 0 },
    };
  }

  private minimalOldRequest(op: NormalizedOperation, focus: NormalizedParameter): JsonValue {
    const query: Record<string, JsonValue> = {};
    const headers: Record<string, JsonValue> = {};
    for (const p of op.parameters.values()) {
      if (p.key === focus.key) continue; // witness: the newly-required param is omitted
      if (!p.required && p.in === 'query') continue;
      const value = sampleFromSchema(p.schema);
      if (p.in === 'query') query[p.name] = value;
      else if (p.in === 'header') headers[p.name] = value;
    }
    return {
      method: op.method,
      path: renderPath(op.path, op.parameters),
      query,
      headers,
      note: `${focus.in} parameter "${focus.name}" is absent`,
    };
  }

  private requestLocation(op: NormalizedOperation, p: NormalizedParameter): string {
    if (p.in === 'query') return `${op.method} ${renderPath(op.path, op.parameters)}?${p.name}=...`;
    if (p.in === 'header') return `${op.method} ${renderPath(op.path, op.parameters)} (header ${p.name})`;
    if (p.in === 'cookie') return `${op.method} ${renderPath(op.path, op.parameters)} (cookie ${p.name})`;
    return `${op.method} ${renderPath(op.path, op.parameters)}`;
  }

  private requestEnvelope(op: NormalizedOperation, body: JsonValue | undefined): JsonValue {
    return {
      method: op.method,
      path: renderPath(op.path, op.parameters),
      ...(body === undefined ? { body: null, note: 'no request body sent' } : { body }),
    };
  }
}

// -- witness example construction ------------------------------------------

function buildExampleAt(
  root: NormalizedSchema,
  segments: Segment[],
  leaf: JsonValue,
): JsonValue {
  const build = (schema: NormalizedSchema, segs: Segment[]): JsonValue => {
    if (segs.length === 0) return leaf;
    const head = segs[0];
    if (!head) return leaf;
    const rest = segs.slice(1);
    if (head.kind === 'item') {
      return schema.items ? [build(schema.items, rest)] : [leaf];
    }
    const prop = schema.properties.get(head.name);
    const obj: Record<string, JsonValue> = {};
    for (const requiredName of schema.required) {
      if (requiredName === head.name) continue;
      const requiredProp = schema.properties.get(requiredName);
      if (requiredProp) obj[requiredName] = sampleFromSchema(requiredProp, 1);
    }
    obj[head.name] = prop ? build(prop, rest) : leaf;
    return obj;
  };
  return build(root, segments);
}

function renderPath(pathTemplate: string, params: Map<string, NormalizedParameter>): string {
  return pathTemplate.replace(/\{([^}]+)\}/g, (_m, name: string) => {
    const p = [...params.values()].find((cand) => cand.name === name && cand.in === 'path');
    return p ? String(sampleFromSchema(p.schema)) : `_${name}_`;
  });
}
