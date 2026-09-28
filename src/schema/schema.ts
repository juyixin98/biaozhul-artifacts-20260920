/**
 * Schema 构造与自检。Schema 是静态成本契约的唯一事实来源：
 * 字段倍率与列表声明上界都在这里声明，估计器/执行器不得自行探测行数。
 */
import type { FieldDef, ObjectTypeDef, SchemaDocument } from '../contract/types.js';

export class SchemaError extends Error {
  constructor(message: string) {
    super(message);
    this.name = 'SchemaError';
  }
}

export function buildSchema(input: {
  queryType: string;
  types: ObjectTypeDef[];
  fragments?: SchemaDocument['fragments'];
}): SchemaDocument {
  const types: Record<string, ObjectTypeDef> = {};
  for (const t of input.types) {
    if (types[t.name]) throw new SchemaError(`duplicate type ${t.name}`);
    const seen = new Set<string>();
    for (const f of t.fields) {
      if (seen.has(f.name)) throw new SchemaError(`duplicate field ${t.name}.${f.name}`);
      seen.add(f.name);
      validateField(t.name, f, input.types);
    }
    types[t.name] = { name: t.name, fields: t.fields };
  }
  if (!types[input.queryType]) throw new SchemaError(`query type ${input.queryType} missing`);

  const fragments: SchemaDocument['fragments'] = {};
  for (const frag of Object.values(input.fragments ?? {})) {
    if (fragments[frag.name]) throw new SchemaError(`duplicate fragment ${frag.name}`);
    if (!types[frag.onType]) throw new SchemaError(`fragment ${frag.name} on unknown type ${frag.onType}`);
    fragments[frag.name] = frag;
  }
  return { queryType: input.queryType, types, fragments };
}

function validateField(typeName: string, f: FieldDef, allTypes: ObjectTypeDef[]): void {
  if (f.multiplier < 0 || !Number.isInteger(f.multiplier)) {
    throw new SchemaError(`${typeName}.${f.name}: multiplier must be a non-negative integer`);
  }
  if (f.list) {
    if (f.declaredUpperBound < 0 || !Number.isInteger(f.declaredUpperBound)) {
      throw new SchemaError(`${typeName}.${f.name}: declaredUpperBound must be a non-negative integer`);
    }
    if (!allTypes.some((t) => t.name === f.type)) {
      throw new SchemaError(`${typeName}.${f.name}: list element type ${f.type} unknown`);
    }
  }
}

export function findField(schema: SchemaDocument, typeName: string, fieldName: string): FieldDef | undefined {
  return schema.types[typeName]?.fields.find((f) => f.name === fieldName);
}

const PRIMITIVE_TYPES = new Set(['STRING', 'INT', 'FLOAT', 'BOOL', 'ID']);

export function isPrimitive(typeName: string): boolean {
  return PRIMITIVE_TYPES.has(typeName);
}
