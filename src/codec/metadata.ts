/**
 * NFT metadata schema validation.
 *
 * Root metadata (ERC-721-style, JSON codec):
 *   {
 *     "name":       string (1..256 chars),
 *     "version":    integer >= 1        (metadata revision, strictly grows),
 *     "image":      CID string          (raw codec media block),
 *     "image_type": "image/png" | "image/jpeg" | "image/gif" | "image/webp",
 *     "image_size": integer >= 0        (must equal actual media byte length),
 *     "links":      [ link node, ... ]  (optional recursive json blocks)
 *   }
 *
 * Linked JSON blocks use the same shape except "version", which is forbidden
 * (only the root carries the revision number). No unknown top-level fields are
 * allowed — undeclared content cannot be validated.
 */
import { LIMITS } from "../config";
import { isCid } from "../crypto/cid";
import { JsonValue } from "./json-parse";

export interface LinkReference {
  cid: string;
  name?: string;
}

export interface ParsedMetadata {
  name: string;
  version: number | null;
  image: string;
  imageType: string;
  imageSize: number;
  links: LinkReference[];
}

function isPlainObject(v: JsonValue): v is { [key: string]: JsonValue } {
  return typeof v === "object" && v !== null && !Array.isArray(v);
}

const ALLOWED_FIELDS = new Set(["name", "version", "image", "image_type", "image_size", "links"]);

/**
 * @param root true for the token metadata root (requires version); false for
 *             linked json blocks (version forbidden).
 */
export function validateMetadataShape(value: JsonValue, root: boolean): ParsedMetadata {
  if (!isPlainObject(value)) {
    throw new Error(`metadata must be a JSON object, got ${value === null ? "null" : Array.isArray(value) ? "array" : typeof value}`);
  }
  for (const key of Object.keys(value)) {
    if (!ALLOWED_FIELDS.has(key)) {
      throw new Error(`unknown metadata field '${key}'`);
    }
  }

  const name = value.name;
  if (typeof name !== "string" || name.length === 0 || name.length > 256) {
    throw new Error("'name' must be a string of length 1..256");
  }

  let version: number | null = null;
  if (root) {
    if (typeof value.version !== "number" || !Number.isInteger(value.version) || value.version < 1) {
      throw new Error("'version' must be an integer >= 1");
    }
    version = value.version;
  } else if (value.version !== undefined) {
    throw new Error("'version' is only allowed on the root metadata");
  }

  if (!isCid(value.image)) {
    throw new Error("'image' must be a supported CID string (CIDv0 or CIDv1 b/z multibase)");
  }

  if (
    typeof value.image_type !== "string" ||
    !(LIMITS.SUPPORTED_IMAGE_TYPES as readonly string[]).includes(value.image_type)
  ) {
    throw new Error(
      `'image_type' must be one of ${LIMITS.SUPPORTED_IMAGE_TYPES.join(", ")}`
    );
  }

  if (
    typeof value.image_size !== "number" ||
    !Number.isInteger(value.image_size) ||
    value.image_size < 0 ||
    value.image_size > LIMITS.MAX_BLOCK_BYTES
  ) {
    throw new Error(`'image_size' must be an integer in 0..${LIMITS.MAX_BLOCK_BYTES}`);
  }

  let links: LinkReference[] = [];
  if (value.links !== undefined) {
    if (!Array.isArray(value.links)) throw new Error("'links' must be an array");
    links = value.links.map((entry, i) => validateLink(entry, i));
  }

  return {
    name,
    version,
    image: value.image as string,
    imageType: value.image_type,
    imageSize: value.image_size,
    links,
  };
}

function validateLink(entry: JsonValue, index: number): LinkReference {
  if (!isPlainObject(entry)) {
    throw new Error(`links[${index}] must be an object`);
  }
  const allowed = new Set(["cid", "name"]);
  for (const key of Object.keys(entry)) {
    if (!allowed.has(key)) throw new Error(`links[${index}]: unknown field '${key}'`);
  }
  if (!isCid(entry.cid)) {
    throw new Error(`links[${index}].cid must be a supported CID string`);
  }
  if (entry.name !== undefined && typeof entry.name !== "string") {
    throw new Error(`links[${index}].name must be a string`);
  }
  return { cid: entry.cid as string, name: entry.name as string | undefined };
}
