/**
 * Contract parser: raw OpenAPI 3.1 JSON -> NormalizedContract.
 *
 * Guarantees:
 * - $ref resolution is bounded: REF_MAX_HOPS limits chain length, cycles are
 *   reported as UNCERTainties (REF_CYCLE) rather than recursing forever.
 * - `x-*` extensions are captured verbatim and never influence judgment.
 * - Unsupported keywords (oneOf/anyOf/allOf/not, discriminators, ...) are
 *   reported as UNSUPPORTED_KEYWORD uncertainties instead of being guessed at.
 * - Parameter identity includes its location: same `name` in different `in`
 *   slots are distinct parameters.
 */
import type {
  JsonValue,
  NormalizedContract,
  NormalizedOperation,
  NormalizedParameter,
  NormalizedParameter as NParam,
  NormalizedRequestBody,
  NormalizedResponse,
  NormalizedSchema,
  ParameterLocation,
  SchemaType,
  Uncertainty,
} from '../core/types.js';

export const REF_MAX_HOPS = 32;

const HTTP_METHODS = new Set([
  'get', 'put', 'post', 'delete', 'options', 'head', 'patch', 'trace',
]);
const PARAM_LOCATIONS: ReadonlySet<string> = new Set([
  'query', 'header', 'path', 'cookie',
]);
const KNOWN_TYPES = new Set<SchemaType>([
  'string', 'number', 'integer', 'boolean', 'object', 'array', 'null',
]);
/** Keywords the subset understands; anything else structural is reported. */
const UNSUPPORTED_SCHEMA_KEYWORDS = [
  'oneOf', 'anyOf', 'allOf', 'not', 'if', 'then', 'else',
  'unevaluatedProperties', 'unevaluatedItems', 'prefixItems',
  'contains', 'patternProperties', 'dependentRequired', 'dependentSchemas',
  'propertyNames', 'discriminator',
] as const;

type Raw = Record<string, JsonValue | undefined>;

export interface ParseOutput {
  contract: NormalizedContract;
  uncertainties: Uncertainty[];
}

interface RefResolution {
  node: Raw;
  trail: string[];
  cycle: boolean;
  unresolved: boolean;
}

export class ContractParser {
  private uncertainties: Uncertainty[] = [];
  private root: Raw = {};

  parse(document: unknown): ParseOutput {
    this.uncertainties = [];
    if (typeof document !== 'object' || document === null || Array.isArray(document)) {
      this.uncertainties.push({
        code: 'INVALID_DOCUMENT',
        location: '$',
        detail: 'OpenAPI document must be a JSON object',
      });
      throw new ContractParseError('OpenAPI document must be a JSON object');
    }
    this.root = document as Raw;

    const openapi = stringOr(this.root['openapi']) ?? '';
    if (!/^3\.1\.\d+$/.test(openapi)) {
      this.uncertainties.push({
        code: 'UNSUPPORTED_SPEC_VERSION',
        location: '$.openapi',
        detail: `expected 3.1.x document, found "${openapi || 'missing'}"`,
      });
    }

    const info = asObject(this.root['info']);
    const title = (info && stringOr(info['title'])) || 'untitled';
    const version = (info && stringOr(info['version'])) || '0.0.0';

    const paths = asObject(this.root['paths']) ?? {};
    const operations = new Map<string, NormalizedOperation>();

    for (const [pathKey, pathItemRaw] of Object.entries(paths)) {
      if (pathKey.startsWith('x-')) continue; // path-level extensions are not judged
      const pathItem = this.resolveRef(pathItemRaw, `$.paths['${pathKey}']`);
      if (!pathItem) continue;
      const pathLevelParams = this.parseParameterList(
        pathItem.node['parameters'],
        `$.paths['${pathKey}'].parameters`,
      );
      for (const [method, opRaw] of Object.entries(pathItem.node)) {
        if (!HTTP_METHODS.has(method.toLowerCase())) continue;
        const opResolved = this.resolveRef(opRaw, `$.paths['${pathKey}'].${method}`);
        if (!opResolved) continue;
        const op = opResolved.node;
        const opParams = this.parseParameterList(
          op['parameters'],
          `$.paths['${pathKey}'].${method}.parameters`,
        );
        // operation-level params override path-level params with same key
        const parameters = new Map<string, NParam>(
          pathLevelParams.map((p) => [p.key, p] as const),
        );
        for (const p of opParams) parameters.set(p.key, p);

        const requestBody = this.parseRequestBody(
          op['requestBody'],
          `$.paths['${pathKey}'].${method}.requestBody`,
        );
        const { responses, defaultResponse } = this.parseResponses(
          op['responses'],
          `$.paths['${pathKey}'].${method}.responses`,
        );
        const operation: NormalizedOperation = {
          method: method.toUpperCase(),
          path: pathKey,
          operationId: stringOr(op['operationId']) ?? null,
          parameters,
          requestBody,
          responses,
          defaultResponse,
        };
        operations.set(`${operation.method} ${pathKey}`, operation);
      }
    }

    const contract: NormalizedContract = {
      openapi,
      title,
      version,
      operations,
    };
    return { contract, uncertainties: [...this.uncertainties] };
  }

  // -- parameters ----------------------------------------------------------

  private parseParameterList(
    raw: JsonValue | undefined,
    location: string,
  ): NormalizedParameter[] {
    if (raw === undefined) return [];
    if (!Array.isArray(raw)) {
      this.reportUnsupported(location, 'parameters must be an array');
      return [];
    }
    const out: NormalizedParameter[] = [];
    raw.forEach((item, i) => {
      const resolved = this.resolveRef(item, `${location}[${i}]`);
      if (!resolved) return;
      const p = resolved.node;
      const name = stringOr(p['name']);
      const inLoc = stringOr(p['in']);
      if (!name || !inLoc || !PARAM_LOCATIONS.has(inLoc)) {
        this.reportUnsupported(
          `${location}[${i}]`,
          `parameter requires string name and in(query|header|path|cookie), got name=${JSON.stringify(name)}, in=${JSON.stringify(inLoc)}`,
        );
        return;
      }
      const required = p['required'] === true || inLoc === 'path';
      const schemaLoc = `${location}[${i}].schema`;
      const schema = this.parseSchema(p['schema'], schemaLoc);
      out.push({
        name,
        in: inLoc as ParameterLocation,
        required,
        key: `${inLoc}:${name}`,
        schema,
        style: stringOr(p['style']) ?? null,
      });
    });
    return out;
  }

  // -- request body / responses -------------------------------------------

  private parseRequestBody(
    raw: JsonValue | undefined,
    location: string,
  ): NormalizedRequestBody | null {
    if (raw === undefined) return null;
    const resolved = this.resolveRef(raw, location);
    if (!resolved) {
      return { required: false, content: new Map() };
    }
    const body = resolved.node;
    const content = this.parseContent(body['content'], `${location}.content`);
    return { required: body['required'] === true, content };
  }

  private parseResponses(
    raw: JsonValue | undefined,
    location: string,
  ): {
    responses: Map<string, NormalizedResponse>;
    defaultResponse: NormalizedResponse | null;
  } {
    const responses = new Map<string, NormalizedResponse>();
    let defaultResponse: NormalizedResponse | null = null;
    const obj = asObject(raw);
    if (!obj) return { responses, defaultResponse };
    for (const [code, respRaw] of Object.entries(obj)) {
      if (code.startsWith('x-')) continue;
      const resolved = this.resolveRef(respRaw, `${location}['${code}']`);
      if (!resolved) continue;
      const resp = resolved.node;
      const content = this.parseContent(
        resp['content'],
        `${location}['${code}'].content`,
      );
      const normalized: NormalizedResponse = {
        statusCode: code,
        description: stringOr(resp['description']) ?? '',        content,
      };
      if (code === 'default') {
        defaultResponse = normalized;
      } else {
        responses.set(code, normalized);
      }
    }
    return { responses, defaultResponse };
  }

  private parseContent(
    raw: JsonValue | undefined,
    location: string,
  ): Map<string, NormalizedSchema> {
    const out = new Map<string, NormalizedSchema>();
    const obj = asObject(raw);
    if (!obj) return out;
    for (const [mediaType, mediaRaw] of Object.entries(obj)) {
      const media = asObject(mediaRaw);
      if (!media) continue;
      const schema = this.parseSchema(
        media['schema'],
        `${location}['${mediaType}'].schema`,
      );
      out.set(mediaType.toLowerCase(), schema);
    }
    return out;
  }

  // -- schemas -------------------------------------------------------------

  private parseSchema(raw: JsonValue | undefined, location: string): NormalizedSchema {
    return this.parseSchemaInternal(raw, location, []);
  }

  private parseSchemaInternal(
    raw: JsonValue | undefined,
    location: string,
    refStack: string[],
  ): NormalizedSchema {
    const empty = (extensions: Record<string, JsonValue> = {}): NormalizedSchema => ({
      types: [],
      enum: null,
      hasDefault: false,
      default: undefined,
      format: null,
      properties: new Map(),
      required: new Set(),
      items: null,
      refTrail: [],
      extensions,
    });

    if (raw === undefined || raw === null || typeof raw !== 'object' || Array.isArray(raw)) {
      return empty();
    }

    // Capture x-* extensions; they are never read by the judging layer.
    const extensions: Record<string, JsonValue> = {};
    for (const [k, v] of Object.entries(raw as Raw)) {
      if (k.startsWith('x-') && v !== undefined) extensions[k] = v;
    }

    // $ref: bounded resolution with cycle detection.
    const ref = stringOr((raw as Raw)['$ref']);
    if (ref !== undefined) {
      const base = empty(extensions);
      base.refTrail = [...refStack, ref];
      if (refStack.includes(ref)) {
        this.uncertainties.push({
          code: 'REF_CYCLE',
          location,
          detail: `$ref cycle detected: ${[...refStack, ref].join(' -> ')}`,
        });
        return base;
      }
      if (refStack.length >= REF_MAX_HOPS) {
        this.uncertainties.push({
          code: 'REF_DEPTH_LIMIT',
          location,
          detail: `$ref chain exceeded ${REF_MAX_HOPS} hops at ${ref}`,
        });
        return base;
      }
      const target = this.lookupRef(ref, location);
      if (!target) {
        this.uncertainties.push({
          code: 'UNRESOLVED_REF',
          location,
          detail: `cannot resolve $ref ${ref}`,
        });
        return base;
      }
      const merged = this.parseSchemaInternal(target, `${location}->${ref}`, [...refStack, ref]);
      return { ...merged, refTrail: base.refTrail };
    }

    this.checkUnsupportedKeywords(raw as Raw, location);

    const schema = empty(extensions);

    // type: string | ["string", "null"]
    const typeRaw = (raw as Raw)['type'];
    if (typeof typeRaw === 'string' && KNOWN_TYPES.has(typeRaw as SchemaType)) {
      schema.types = [typeRaw as SchemaType];
    } else if (Array.isArray(typeRaw)) {
      const types: SchemaType[] = [];
      for (const t of typeRaw) {
        if (typeof t === 'string' && KNOWN_TYPES.has(t as SchemaType)) types.push(t as SchemaType);
      }
      schema.types = types;
    }

    if (Array.isArray((raw as Raw)['enum'])) {
      schema.enum = ((raw as Raw)['enum'] as JsonValue[]).slice();
    }

    if ('default' in (raw as Raw)) {
      schema.hasDefault = true;
      schema.default = (raw as Raw)['default'];
    }

    schema.format = stringOr((raw as Raw)['format']) ?? null;

    const requiredRaw = (raw as Raw)['required'];
    if (Array.isArray(requiredRaw)) {
      for (const r of requiredRaw) if (typeof r === 'string') schema.required.add(r);
    }

    const props = asObject((raw as Raw)['properties']);
    if (props) {
      for (const [propName, propRaw] of Object.entries(props)) {
        schema.properties.set(
          propName,
          // Keep the ref stack across property descent: a $ref chain that
          // loops back through a property is still a cycle and must be bounded.
          this.parseSchemaInternal(
            propRaw,
            `${location}.properties['${propName}']`,
            refStack,
          ),
        );
      }
    }

    const itemsRaw = (raw as Raw)['items'];
    if (itemsRaw !== undefined) {
      schema.items = this.parseSchemaInternal(itemsRaw, `${location}.items`, refStack);
    }

    return schema;
  }

  // -- $ref resolution -----------------------------------------------------

  private resolveRef(
    raw: JsonValue | undefined,
    location: string,
  ): RefResolution | null {
    let node: JsonValue | undefined = raw;
    const trail: string[] = [];
    for (let hop = 0; hop <= REF_MAX_HOPS; hop += 1) {
      const obj = asObject(node);
      if (!obj) return obj ? { node: obj, trail, cycle: false, unresolved: false } : null;
      const ref = stringOr(obj['$ref']);
      if (ref === undefined) {
        return { node: obj, trail, cycle: false, unresolved: false };
      }
      if (trail.includes(ref)) {
        this.uncertainties.push({
          code: 'REF_CYCLE',
          location,
          detail: `$ref cycle detected: ${[...trail, ref].join(' -> ')}`,
        });
        return { node: obj, trail, cycle: true, unresolved: false };
      }
      if (hop === REF_MAX_HOPS) {
        this.uncertainties.push({
          code: 'REF_DEPTH_LIMIT',
          location,
          detail: `$ref chain exceeded ${REF_MAX_HOPS} hops at ${ref}`,
        });
        return { node: obj, trail, cycle: false, unresolved: false };
      }
      trail.push(ref);
      const target = this.lookupRef(ref, location);
      if (target === undefined) {
        this.uncertainties.push({
          code: 'UNRESOLVED_REF',
          location,
          detail: `cannot resolve $ref ${ref}`,
        });
        return { node: obj, trail, cycle: false, unresolved: true };
      }
      node = target;
    }
    return null;
  }

  /** Resolve a local `#/...` JSON pointer against the document root. */
  private lookupRef(ref: string, location: string): JsonValue | undefined {
    if (!ref.startsWith('#/')) {
      this.uncertainties.push({
        code: 'UNRESOLVED_REF',
        location,
        detail: `only local $ref pointers are supported, got ${ref}`,
      });
      return undefined;
    }
    const parts = ref
      .slice(2)
      .split('/')
      .map((p) => p.replace(/~1/g, '/').replace(/~0/g, '~'));
    let cur: JsonValue | undefined = this.root as JsonValue;
    for (const part of parts) {
      const obj = asObject(cur);
      if (!obj) return undefined;
      cur = obj[part];
      if (cur === undefined) return undefined;
    }
    return cur;
  }

  private checkUnsupportedKeywords(raw: Raw, location: string): void {
    for (const kw of UNSUPPORTED_SCHEMA_KEYWORDS) {
      if (raw[kw] !== undefined) {
        this.reportUnsupported(location, `keyword "${kw}" is outside the supported subset`);
      }
    }
  }

  private reportUnsupported(location: string, detail: string): void {
    this.uncertainties.push({ code: 'UNSUPPORTED_KEYWORD', location, detail });
  }
}

export class ContractParseError extends Error {}

// -- small JSON helpers -----------------------------------------------------

function asObject(v: JsonValue | undefined): Raw | null {
  if (typeof v === 'object' && v !== null && !Array.isArray(v)) {
    return v as Raw;
  }
  return null;
}

function stringOr(v: JsonValue | undefined): string | undefined {
  return typeof v === 'string' ? v : undefined;
}
