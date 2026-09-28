import { collectExtensions, parseContractDocument } from './loader.js';
import { resolvePointer } from './ref-resolver.js';
import type {
  BodyModel,
  ContractModel,
  JsonObject,
  JsonValue,
  OperationModel,
  ParamLocation,
  ParamModel,
  RawSchema,
  ResponseModel,
} from './types.js';
import { ContractParseError } from './loader.js';

const HTTP_METHODS = ['get', 'put', 'post', 'delete', 'patch', 'options', 'head', 'trace'];

function asObject(v: JsonValue | undefined): JsonObject | undefined {
  return typeof v === 'object' && v !== null && !Array.isArray(v) ? v : undefined;
}

function readParam(raw: JsonObject, position: string): ParamModel {
  const name = raw['name'];
  const loc = raw['in'];
  if (typeof name !== 'string') {
    throw new ContractParseError('parameter must have a string "name"', `${position}.name`);
  }
  if (loc !== 'query' && loc !== 'header' && loc !== 'path' && loc !== 'cookie') {
    throw new ContractParseError(`unsupported parameter location: ${JSON.stringify(loc)}`, `${position}.in`);
  }
  const required = raw['required'] === true;
  if (loc === 'path' && !required) {
    throw new ContractParseError(`path parameter "${name}" must be required: true`, `${position}.required`);
  }
  const schemaNode = asObject(raw['schema']) as RawSchema | undefined;
  const hasDefault = schemaNode !== undefined && !Array.isArray(schemaNode)
    && typeof schemaNode === 'object' && Object.prototype.hasOwnProperty.call(schemaNode, 'default');
  return {
    name,
    in: loc as ParamLocation,
    required,
    schema: schemaNode,
    hasDefault,
    defaultValue: hasDefault && typeof schemaNode === 'object' ? (schemaNode as JsonObject)['default'] : undefined,
  };
}

function mergeParameters(doc: JsonObject, pathParams: JsonValue[], opParams: JsonValue[], path: string): ParamModel[] {
  // Parameters are keyed by (name, in): same name at a DIFFERENT location is a
  // distinct parameter, never an override.
  const byKey = new Map<string, ParamModel>();
  const consume = (list: JsonValue[], scope: string): void => {
    list.forEach((item, i) => {
      if (typeof item !== 'object' || item === null || Array.isArray(item)) {
        throw new ContractParseError('parameter entry must be an object or $ref', `${scope}[${i}]`);
      }
      let raw = item as JsonObject;
      if (typeof raw['$ref'] === 'string') {
        const target = asObject(resolvePointer(doc, raw['$ref'] as string));
        if (!target) throw new ContractParseError(`unresolved parameter ref ${raw['$ref']}`, `${scope}[${i}]`);
        raw = target;
      }
      const p = readParam(raw, `${scope}[${i}]`);
      byKey.set(`${p.in}:${p.name}`, p);
    });
  };
  consume(pathParams, `$.paths.${path}.parameters`);
  consume(opParams, `$.paths.${path}.parameters`);
  return [...byKey.values()];
}

function readRequestBody(doc: JsonObject, rawBody: JsonObject | undefined, position: string): BodyModel | undefined {
  if (!rawBody) return undefined;
  let raw: JsonObject = rawBody;
  if (typeof raw['$ref'] === 'string') {
    const target = asObject(resolvePointer(doc, raw['$ref'] as string));
    if (!target) throw new ContractParseError(`unresolved requestBody ref`, position);
    raw = target;
  }
  const content = asObject(raw['content']);
  if (!content) return { required: raw['required'] === true, contentType: 'application/json' };
  const jsonType = Object.keys(content).find((ct) => ct.includes('json')) ?? Object.keys(content)[0];
  const media = jsonType ? asObject(content[jsonType]) : undefined;
  const schema = media ? (asObject(media['schema']) as RawSchema | undefined) : undefined;
  return { required: raw['required'] === true, contentType: jsonType ?? 'application/json', schema };
}

function readResponses(doc: JsonObject, raw: JsonValue | undefined, position: string): ResponseModel[] {
  const responses = asObject(raw);
  if (!responses) return [];
  const out: ResponseModel[] = [];
  for (const [status, mediaContainerRaw] of Object.entries(responses)) {
    let container = asObject(mediaContainerRaw);
    if (container && typeof container['$ref'] === 'string') {
      container = asObject(resolvePointer(doc, container['$ref'] as string));
    }
    if (!container) {
      out.push({ status });
      continue;
    }
    const content = asObject(container['content']);
    const jsonType = content ? Object.keys(content).find((ct) => ct.includes('json')) ?? Object.keys(content)[0] : undefined;
    const media = jsonType ? asObject(content![jsonType]) : undefined;
    const schema = media ? (asObject(media['schema']) as RawSchema | undefined) : undefined;
    out.push({ status, contentType: jsonType, schema });
  }
  return out;
}

/** Convert raw text (JSON or YAML) into the normalized model the kernel diffs. */
export function normalize(text: string): ContractModel {
  return normalizeDoc(parseContractDocument(text));
}

/** Normalize an already-parsed (and validated) document. */
export function normalizeDoc(doc: JsonObject): ContractModel {
  const paths = asObject(doc['paths']) ?? {};
  const operations: OperationModel[] = [];

  for (const [path, pathItemRaw] of Object.entries(paths)) {
    const pathItem = asObject(pathItemRaw);
    if (!pathItem) continue;
    const pathParams = Array.isArray(pathItem['parameters']) ? (pathItem['parameters'] as JsonValue[]) : [];
    for (const method of HTTP_METHODS) {
      const opRaw = asObject(pathItem[method]);
      if (!opRaw) continue;
      const opParams = Array.isArray(opRaw['parameters']) ? (opRaw['parameters'] as JsonValue[]) : [];
      const bodyRaw = asObject(opRaw['requestBody']);
      operations.push({
        id: `${method.toUpperCase()} ${path}`,
        method,
        path,
        parameters: mergeParameters(doc, pathParams, opParams, path),
        requestBody: readRequestBody(doc, bodyRaw, `$.paths.${path}.${method}.requestBody`),
        responses: readResponses(doc, opRaw['responses'], `$.paths.${path}.${method}.responses`),
      });
    }
  }

  const info = asObject(doc['info']);
  return {
    title: typeof info?.['title'] === 'string' ? info['title'] : '',
    version: typeof info?.['version'] === 'string' ? info['version'] : '',
    operations,
    extensions: collectExtensions(doc),
  };
}
