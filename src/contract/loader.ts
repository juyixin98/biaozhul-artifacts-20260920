import { parse as parseYaml } from 'yaml';
import type { JsonObject, JsonValue } from './types.js';

export class ContractParseError extends Error {
  readonly position: string;
  constructor(message: string, position: string) {
    super(message);
    this.name = 'ContractParseError';
    this.position = position;
  }
}

function isObject(v: unknown): v is JsonObject {
  return typeof v === 'object' && v !== null && !Array.isArray(v);
}

/**
 * Parse a contract document (JSON or YAML text) and perform minimal
 * structural validation of the OpenAPI 3.1 subset we support.
 */
export function parseContractDocument(text: string): JsonObject {
  let doc: unknown;
  try {
    doc = parseYaml(text); // yaml parses JSON too
  } catch (err) {
    throw new ContractParseError(`document is not valid YAML/JSON: ${(err as Error).message}`, '$');
  }
  if (!isObject(doc)) {
    throw new ContractParseError('document root must be an object', '$');
  }
  const openapi = doc['openapi'];
  if (typeof openapi !== 'string' || !openapi.startsWith('3.1')) {
    throw new ContractParseError(
      `only openapi 3.1.x documents are supported, got ${JSON.stringify(openapi)}`,
      '$.openapi',
    );
  }
  if (doc['paths'] !== undefined && !isObject(doc['paths'])) {
    throw new ContractParseError('paths must be an object', '$.paths');
  }
  return doc;
}

/** Collect every x-* extension key in the subtree, with its JSON-ish path. */
export function collectExtensions(node: JsonValue, path = '$', acc: string[] = []): string[] {
  if (Array.isArray(node)) {
    node.forEach((item, i) => collectExtensions(item, `${path}[${i}]`, acc));
    return acc;
  }
  if (isObject(node)) {
    for (const [key, value] of Object.entries(node)) {
      if (key.startsWith('x-')) acc.push(`${path}.${key}`);
      collectExtensions(value, `${path}.${key}`, acc);
    }
  }
  return acc;
}
