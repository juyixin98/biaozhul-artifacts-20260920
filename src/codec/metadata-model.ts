/**
 * ERC-721/ERC-1155-style metadata model validation on top of the strict JSON
 * parse tree. Whitelist semantics: unknown top-level fields and unknown
 * encodings inside references are rejected rather than ignored.
 */
import type { JsonValue } from './json-strict.js';
import { AppError } from '../errors.js';

export interface MediaRef {
  field: string;
  uri: string;
  cidText: string;
  path: string[];
}

export interface NftMetadataModel {
  name: string;
  description: string | null;
  image: MediaRef;
  animation: MediaRef | null;
  externalUrl: string | null;
  attributes: { traitType: string | null; value: string | number | boolean; displayType: string | null }[];
  properties: Record<string, string>;
}

const KNOWN_FIELDS = new Set([
  'name',
  'description',
  'image',
  'animation_url',
  'external_url',
  'attributes',
  'properties',
]);

const KNOWN_DISPLAY = new Set(['string', 'number', 'boost_number', 'boost_percentage', 'date', 'duration']);

/**
 * Parse ipfs://<cid>(/path...) with the same restricted CID rules.
 * No userinfo, no host/gateway form, no query/fragment, percent-encoded
 * segments must be valid UTF-8.
 */
export function parseIpfsUri(raw: string, field: string): MediaRef {
  let uri = raw;
  if (typeof uri !== 'string' || uri.length === 0) {
    throw new AppError('E_METADATA_MODEL', `${field}: media URI must be a non-empty string`);
  }
  if (!uri.startsWith('ipfs://')) {
    const e = new AppError(
      'E_UNSUPPORTED_ENCODING',
      `${field}: only content-addressed ipfs:// URIs are verified here (no http gateway, no raw CID)`,
      400,
      { field, uriShorthand: uri.slice(0, 32) },
    );
    throw e;
  }
  const rest = uri.slice('ipfs://'.length);
  if (rest.includes('?') || rest.includes('#') || rest.includes('@')) {
    throw new AppError('E_METADATA_MODEL', `${field}: ipfs URI must not contain query, fragment or authority components`);
  }
  const slash = rest.indexOf('/');
  const cidText = slash === -1 ? rest : rest.slice(0, slash);
  const pathRaw = slash === -1 ? [] : rest.slice(slash + 1).split('/');
  if (cidText.length === 0) {
    throw new AppError('E_METADATA_MODEL', `${field}: ipfs URI is missing its CID`);
  }
  if (pathRaw.some((s) => s.length === 0)) {
    throw new AppError('E_METADATA_MODEL', `${field}: empty path segment in ipfs URI`);
  }
  // Percent-decode each segment to bytes, then decode as strict UTF-8:
  // this rejects malformed escapes and also rejects overlong/cesu8 encodings
  // such as %ED%A0%80 (a lone surrogate masquerading as UTF-8).
  const path = pathRaw.map((seg) => {
    const bytes: number[] = [];
    for (let i = 0; i < seg.length; i++) {
      const ch = seg[i]!;
      if (ch === '%') {
        const hex = seg.slice(i + 1, i + 3);
        if (!/^[0-9a-fA-F]{2}$/.test(hex)) {
          throw new AppError('E_METADATA_MODEL', `${field}: malformed percent-encoding in path`);
        }
        bytes.push(parseInt(hex, 16));
        i += 2;
      } else if (ch.charCodeAt(0) < 0x80) {
        bytes.push(ch.charCodeAt(0));
      } else {
        throw new AppError('E_METADATA_MODEL', `${field}: non-ASCII path characters must be percent-encoded`);
      }
    }
    try {
      return new TextDecoder('utf-8', { fatal: true }).decode(Uint8Array.from(bytes));
    } catch {
      throw new AppError('E_METADATA_MODEL', `${field}: percent-encoded path segment is not valid UTF-8`);
    }
  });
  return { field, uri, cidText, path };
}

function asObject(v: JsonValue, where: string): [string, JsonValue][] {
  if (v.kind !== 'object') throw new AppError('E_METADATA_MODEL', `${where} must be a JSON object`);
  return v.entries;
}

function asString(v: JsonValue, where: string): string {
  if (v.kind !== 'string') throw new AppError('E_METADATA_MODEL', `${where} must be a JSON string`);
  return v.value;
}

export function buildMetadataModel(root: JsonValue): NftMetadataModel {
  const entries = asObject(root, 'metadata root');

  let name: string | null = null;
  let description: string | null = null;
  let imageRef: MediaRef | null = null;
  let animationRef: MediaRef | null = null;
  let externalUrl: string | null = null;
  let attributes: NftMetadataModel['attributes'] | null = null;
  let properties: Record<string, string> | null = null;

  for (const [key, val] of entries) {
    if (!KNOWN_FIELDS.has(key)) {
      throw new AppError(
        'E_METADATA_MODEL',
        `unknown metadata field ${JSON.stringify(key)}; accepted fields: ${[...KNOWN_FIELDS].join(', ')}`,
        400,
        { field: key },
      );
    }
    switch (key) {
      case 'name': {
        name = asString(val, 'name');
        if (name.trim().length === 0) throw new AppError('E_METADATA_MODEL', 'name must not be empty');
        break;
      }
      case 'description':
        description = asString(val, 'description');
        break;
      case 'image':
        imageRef = parseIpfsUri(asString(val, 'image'), 'image');
        break;
      case 'animation_url':
        animationRef = parseIpfsUri(asString(val, 'animation_url'), 'animation_url');
        break;
      case 'external_url': {
        externalUrl = asString(val, 'external_url');
        let u: URL;
        try {
          u = new URL(externalUrl);
        } catch {
          throw new AppError('E_METADATA_MODEL', 'external_url is not a valid URL');
        }
        if (u.protocol !== 'https:' && u.protocol !== 'http:') {
          throw new AppError('E_METADATA_MODEL', 'external_url must use http: or https: (declared only; never fetched)');
        }
        break;
      }
      case 'attributes': {
        if (val.kind !== 'array') throw new AppError('E_METADATA_MODEL', 'attributes must be an array');
        attributes = val.items.map((item, i) => {
          const aEntries = asObject(item, `attributes[${i}]`);
          let traitType: string | null = null;
          let value: string | number | boolean | null = null;
          let displayType: string | null = null;
          for (const [ak, av] of aEntries) {
            if (ak === 'trait_type') {
              traitType = asString(av, `attributes[${i}].trait_type`);
            } else if (ak === 'value') {
              if (av.kind === 'string') value = av.value;
              else if (av.kind === 'boolean') value = av.value;
              else if (av.kind === 'number') value = typeof av.value === 'bigint' ? Number(av.value) : av.value;
              else throw new AppError('E_METADATA_MODEL', `attributes[${i}].value must be string/number/boolean`);
            } else if (ak === 'display_type') {
              displayType = asString(av, `attributes[${i}].display_type`);
              if (!KNOWN_DISPLAY.has(displayType)) {
                throw new AppError('E_METADATA_MODEL', `attributes[${i}].display_type ${JSON.stringify(displayType)} is not supported`);
              }
            } else {
              throw new AppError('E_METADATA_MODEL', `attributes[${i}] has unknown field ${JSON.stringify(ak)}`);
            }
          }
          if (value === null) throw new AppError('E_METADATA_MODEL', `attributes[${i}] is missing value`);
          return { traitType, value, displayType };
        });
        break;
      }
      case 'properties': {
        properties = {};
        for (const [pk, pv] of asObject(val, 'properties')) {
          properties[pk] = asString(pv, `properties.${pk}`);
        }
        break;
      }
    }
  }

  if (!name) throw new AppError('E_METADATA_MODEL', 'metadata is missing required "name"');
  if (!imageRef) throw new AppError('E_METADATA_MODEL', 'metadata is missing required "image" ipfs:// reference');

  return {
    name,
    description,
    image: imageRef,
    animation: animationRef,
    externalUrl,
    attributes: attributes ?? [],
    properties: properties ?? {},
  };
}
