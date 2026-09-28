/**
 * 极简 GraphQL 类型系统：SCALAR / ENUM / OBJECT + NON_NULL / LIST 包装。
 * 不支持 INTERFACE / UNION（见 README 取舍说明）。
 */

import type {
  FieldNode,
  FragmentDefinitionNode,
  OperationDefinitionNode,
  TypeNode,
} from './ast.js';

/* ---------------------------------- 类型引用 ---------------------------------- */

export interface GraphQLContext {
  requestId: string;
  startedAt: number;
  readonly db: import('better-sqlite3').Database;
  [key: string]: unknown;
}

export interface ResolveInfo {
  fieldName: string;
  responseKey: string;
  fieldNodes: FieldNode[];
  parentTypeName: string;
  path: ReadonlyArray<string | number>;
  variableValues: Record<string, unknown>;
  fragments: Record<string, FragmentDefinitionNode>;
  operation: OperationDefinitionNode;
}

export type ResolverResult = unknown | Promise<unknown>;
export type ResolverFn = (
  source: unknown,
  args: Record<string, unknown>,
  context: GraphQLContext,
  info: ResolveInfo,
) => ResolverResult;

export type TypeRef =
  | { kind: 'NAMED'; name: string }
  | { kind: 'LIST'; ofType: TypeRef }
  | { kind: 'NON_NULL'; ofType: TypeRef };

export const nonNull = (ofType: TypeRef): TypeRef => ({ kind: 'NON_NULL', ofType });
export const list = (ofType: TypeRef): TypeRef => ({ kind: 'LIST', ofType });
export const named = (name: string): TypeRef => ({ kind: 'NAMED', name });

export function typeToString(type: TypeRef): string {
  switch (type.kind) {
    case 'NAMED':
      return type.name;
    case 'LIST':
      return `[${typeToString(type.ofType)}]`;
    case 'NON_NULL':
      return `${typeToString(type.ofType)}!`;
  }
}

export function isNonNull(type: TypeRef): boolean {
  return type.kind === 'NON_NULL';
}

export function nullabilityShape(type: TypeRef): string {
  /** 调试用：例如 [ID!]! => "NN(LIST(NN(ID)))" */
  switch (type.kind) {
    case 'NAMED':
      return type.name;
    case 'LIST':
      return `LIST(${nullabilityShape(type.ofType)})`;
    case 'NON_NULL':
      return `NN(${nullabilityShape(type.ofType)})`;
  }
}

export function typeRefFromAst(node: TypeNode): TypeRef {
  switch (node.kind) {
    case 'NamedType':
      return named(node.name.value);
    case 'ListType':
      return list(typeRefFromAst(node.type));
    case 'NonNullType':
      return nonNull(typeRefFromAst(node.type));
  }
}

/* ---------------------------------- 具名类型 ---------------------------------- */

export interface InputArgDef {
  name: string;
  type: TypeRef;
  defaultValue?: unknown;
  description?: string;
}

export interface FieldDef {
  name: string;
  type: TypeRef;
  args: Map<string, InputArgDef>;
  resolve?: ResolverFn;
  description?: string;
}

export interface GraphQLScalarType {
  kind: 'SCALAR';
  name: string;
  description?: string;
  /** 变量/运行时输入强制转换，失败抛 Error */
  parseValue: (value: unknown) => unknown;
  /** 序列化解析器返回值 */
  serialize: (value: unknown) => unknown;
}

export interface GraphQLEnumType {
  kind: 'ENUM';
  name: string;
  values: ReadonlySet<string>;
  description?: string;
}

export interface GraphQLObjectType {
  kind: 'OBJECT';
  name: string;
  fields: Map<string, FieldDef>;
  description?: string;
}

export type GraphQLNamedType =
  | GraphQLScalarType
  | GraphQLEnumType
  | GraphQLObjectType;

export interface GraphQLSchema {
  queryType: GraphQLObjectType;
  mutationType: GraphQLObjectType | null;
  getType(name: string): GraphQLNamedType | undefined;
}

/* ---------------------------------- 内建标量 ---------------------------------- */

const INT32_MIN = -2147483648;
const INT32_MAX = 2147483647;

function parseIntValue(value: unknown): number {
  if (typeof value === 'boolean') {
    throw new Error('Int cannot represent boolean value');
  }
  let parsed: number;
  if (typeof value === 'number') {
    if (!Number.isInteger(value)) throw new Error('Int cannot represent non-integer value');
    parsed = value;
  } else if (typeof value === 'string') {
    if (!/^-?\d+$/.test(value)) throw new Error('Int cannot represent non-integer string');
    parsed = Number(value);
  } else {
    throw new Error('Int cannot represent non-numeric value');
  }
  if (parsed < INT32_MIN || parsed > INT32_MAX) {
    throw new Error('Int cannot represent value outside 32-bit signed range');
  }
  return parsed;
}

export const GraphQLInt: GraphQLScalarType = {
  kind: 'SCALAR',
  name: 'Int',
  parseValue: parseIntValue,
  serialize: (value) => parseIntValue(value),
};

export const GraphQLFloat: GraphQLScalarType = {
  kind: 'SCALAR',
  name: 'Float',
  parseValue: (value) => {
    if (typeof value === 'boolean') throw new Error('Float cannot represent boolean');
    if (typeof value === 'number') return value;
    if (typeof value === 'string' && value.trim() !== '') {
      const n = Number(value);
      if (Number.isFinite(n)) return n;
    }
    throw new Error('Float cannot represent non-numeric value');
  },
  serialize: (value) => {
    if (typeof value === 'number') return value;
    if (typeof value === 'string') return Number(value);
    throw new Error('Float cannot serialize non-numeric value');
  },
};

export const GraphQLString: GraphQLScalarType = {
  kind: 'SCALAR',
  name: 'String',
  parseValue: (value) => {
    if (typeof value === 'string') return value;
    throw new Error('String cannot represent non-string value');
  },
  serialize: (value) => {
    if (typeof value === 'string') return value;
    if (typeof value === 'number' || typeof value === 'boolean') return String(value);
    throw new Error('String cannot serialize non-coercible value');
  },
};

export const GraphQLBoolean: GraphQLScalarType = {
  kind: 'SCALAR',
  name: 'Boolean',
  parseValue: (value) => {
    if (typeof value === 'boolean') return value;
    throw new Error('Boolean cannot represent non-boolean value');
  },
  serialize: (value) => {
    if (typeof value === 'boolean') return value;
    throw new Error('Boolean cannot serialize non-boolean value');
  },
};

export const GraphQLID: GraphQLScalarType = {
  kind: 'SCALAR',
  name: 'ID',
  parseValue: (value) => {
    if (typeof value === 'string') return value;
    if (typeof value === 'number' && Number.isInteger(value)) return String(value);
    throw new Error('ID cannot represent value: expected string or integer');
  },
  serialize: (value) => {
    if (typeof value === 'string') return value;
    if (typeof value === 'number' && Number.isInteger(value)) return String(value);
    throw new Error('ID cannot serialize non-string/integer value');
  },
};

export const BUILTIN_SCALARS: Record<string, GraphQLScalarType> = {
  Int: GraphQLInt,
  Float: GraphQLFloat,
  String: GraphQLString,
  Boolean: GraphQLBoolean,
  ID: GraphQLID,
};
