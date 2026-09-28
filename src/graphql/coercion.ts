/**
 * 值强制（coercion）。
 * 1) 执行前：按变量声明的类型强制外部传入的 variables（拒绝非法类型，而非悄悄放过）；
 * 2) 执行中：把字面量实参 / 变量实参强制为解析器收到的 args；
 * 3) 完成后：标量输出强制，解析器返回了错误类型时产生执行期错误（不是 500）。
 *
 * 刻意不支持的能力：Float、自定义 input object 作为变量/参数类型
 * （受限子集，schema 中也没有使用它们的地方）。
 */
import type { TypeNode, ValueNode } from './ast.js';
import { GraphQLError } from './errors.js';
import type { NamedType, ScalarType, TypeRef } from './types.js';
import { parseTypeRef, typeRefToString } from './types.js';

export type NamedTypeLookup = (name: string) => NamedType | undefined;

function assertScalarOrEnum(
  ref: TypeRef,
  lookup: NamedTypeLookup,
): ScalarType | Extract<NamedType, { kind: 'ENUM' }> {
  if (ref.kind !== 'NAMED') {
    throw new GraphQLError('Internal error: expected named type', 'INTERNAL');
  }
  const type = lookup(ref.name);
  if (!type) {
    throw new GraphQLError(`Unknown type "${ref.name}"`, 'VALIDATION');
  }
  if (type.kind === 'OBJECT') {
    throw new GraphQLError(
      `Object type "${ref.name}" cannot be used as a variable or argument type`,
      'VALIDATION',
    );
  }
  return type;
}

/** 变量定义 AST 上的类型 -> 内部 TypeRef（两种表示保持单一解析实现） */
export function typeNodeToRef(node: TypeNode): TypeRef {
  switch (node.kind) {
    case 'NamedType':
      return parseTypeRef(node.name.value);
    case 'ListType':
      return parseTypeRef(`[${typeNodeToString(node.type)}]`);
    case 'NonNullType':
      return parseTypeRef(
        node.type.kind === 'NamedType'
          ? `${node.type.name.value}!`
          : `[${typeNodeToString(node.type.type)}]!`,
      );
  }
}

export function typeNodeToString(node: TypeNode): string {
  switch (node.kind) {
    case 'NamedType':
      return node.name.value;
    case 'ListType':
      return `[${typeNodeToString(node.type)}]`;
    case 'NonNullType':
      return `${typeNodeToString(node.type)}!`;
  }
}

function coerceInputScalar(
  value: unknown,
  typeName: string,
  lookup: NamedTypeLookup,
): unknown {
  // 内置标量不依赖 schema 查找，保证强制逻辑可独立测试
  switch (typeName) {
    case 'Int':
      if (typeof value === 'number' && Number.isSafeInteger(value)) return value;
      throw new GraphQLError(
        `Expected type "Int", got ${safePreview(value)}`,
        'VARIABLE_TYPE',
      );
    case 'String':
    case 'DateTime':
      if (typeof value === 'string') return value;
      throw new GraphQLError(
        `Expected type "${typeName}", got ${safePreview(value)}`,
        'VARIABLE_TYPE',
      );
    case 'ID':
      if (typeof value === 'string') return value;
      if (typeof value === 'number' && Number.isSafeInteger(value)) return String(value);
      throw new GraphQLError(`Expected type "ID", got ${safePreview(value)}`, 'VARIABLE_TYPE');
    case 'Boolean':
      if (typeof value === 'boolean') return value;
      throw new GraphQLError(
        `Expected type "Boolean", got ${safePreview(value)}`,
        'VARIABLE_TYPE',
      );
    default:
      break;
  }

  // 自定义标量 / 枚举：需要 schema 查找
  const type = lookup(typeName);
  if (!type) {
    throw new GraphQLError(`Unknown type "${typeName}"`, 'VALIDATION');
  }
  if (type.kind === 'OBJECT') {
    throw new GraphQLError(
      `Object type "${typeName}" cannot be used as a variable or argument type`,
      'VALIDATION',
    );
  }
  if (type.kind === 'ENUM') {
    if (typeof value !== 'string' || !type.values.has(value)) {
      throw new GraphQLError(
        `Expected value of enum "${typeName}", got ${safePreview(value)}`,
        'VARIABLE_TYPE',
      );
    }
    return value;
  }
  type.assertOutput(value);
  return value;
}

function safePreview(value: unknown): string {
  if (value === null) return 'null';
  if (Array.isArray(value)) return 'a list';
  if (typeof value === 'object') return 'an object';
  return JSON.stringify(value);
}

/**
 * 外部 variables 的强制（规范 "Coerce Variable Values"）。
 * 非空包装在最外层与列表元素处都会被检查；
 * 非数组单值传给列表类型时，按规范包装为单元素列表。
 */
export function coerceVariable(
  value: unknown,
  ref: TypeRef,
  lookup: NamedTypeLookup,
): unknown {
  return coerceVariableAt(value, ref, lookup, []);
}

function coerceVariableAt(
  value: unknown,
  ref: TypeRef,
  lookup: NamedTypeLookup,
  basePath: ReadonlyArray<number>,
): unknown {
  if (value === null) {
    if (ref.nonNull) {
      throw new GraphQLError(
        basePath.length === 0
          ? `Expected non-null type "${typeRefToString(ref)}", got null`
          : `Expected non-null list element at index ${basePath[basePath.length - 1]} of type "${typeRefToString(ref)}"`,
        'VARIABLE_TYPE',
        { path: basePath },
      );
    }
    return null;
  }

  if (ref.kind === 'NAMED') {
    return coerceInputScalar(value, ref.name, lookup);
  }

  // LIST
  const items: unknown[] = Array.isArray(value) ? value : [value];
  // 非数组单值按规范包装为单元素列表；元素按其声明类型递归强制
  return items.map((item, index) =>
    coerceVariableAt(item, ref.ofType, lookup, [...basePath, index]),
  );
}

/**
 * 字面量（含默认值）的强制，用于字段实参。
 * 变量引用从已强制完成的 variableValues 中读取——执行前变量类型已验证，
 * 这里只做取值与非空存在性检查（非空由参数校验保证）。
 */
export function coerceLiteral(
  node: ValueNode,
  ref: TypeRef,
  variables: Readonly<Record<string, unknown>>,
  lookup: NamedTypeLookup,
): unknown {
  if (node.kind === 'Variable') {
    const value = variables[node.name.value];
    return value === undefined ? null : value;
  }
  if (node.kind === 'NullValue') {
    return null;
  }

  if (ref.kind === 'LIST') {
    if (node.kind !== 'ListValue') {
      throw new GraphQLError(
        `Expected list value for type "${typeRefToString(ref)}"`,
        'ARGUMENT',
      );
    }
    return node.values.map((itemNode) =>
      coerceLiteral(itemNode, ref.ofType, variables, lookup),
    );
  }

  const type = assertScalarOrEnum(ref, lookup);
  switch (node.kind) {
    case 'IntValue':
      if (ref.name !== 'Int' && ref.name !== 'ID') {
        throw new GraphQLError(
          `Int value cannot be coerced to "${ref.name}"`,
          'ARGUMENT',
        );
      }
      if (ref.name === 'ID') return String(node.value);
      return node.value;
    case 'StringValue':
      if (type.kind === 'ENUM' || (ref.name !== 'String' && ref.name !== 'ID' && ref.name !== 'DateTime')) {
        throw new GraphQLError(
          `String value cannot be coerced to "${ref.name}"`,
          'ARGUMENT',
        );
      }
      return node.value;
    case 'BooleanValue':
      if (ref.name !== 'Boolean') {
        throw new GraphQLError(`Boolean cannot be coerced to "${ref.name}"`, 'ARGUMENT');
      }
      return node.value;
    case 'EnumValue':
      if (type.kind !== 'ENUM' || !type.values.has(node.value)) {
        throw new GraphQLError(
          `Enum value "${node.value}" is not valid for "${ref.name}"`,
          'ARGUMENT',
        );
      }
      return node.value;
    default:
      throw new GraphQLError(
        `Cannot coerce ${node.kind} to type "${ref.name}"`,
        'ARGUMENT',
      );
  }
}

/**
 * 标量输出强制：解析器返回值与声明的标量类型不一致时失败。
 * ID 在输出侧接受整数并序列化为字符串。
 */
export function coerceOutputScalar(value: unknown, typeName: string): unknown {
  switch (typeName) {
    case 'Int':
      if (typeof value !== 'number' || !Number.isSafeInteger(value)) {
        throw new GraphQLError(
          `Resolver returned non-int value for "Int": ${safePreview(value)}`,
          'COERCION_FAILURE',
        );
      }
      return value;
    case 'String':
    case 'DateTime':
      if (typeof value !== 'string') {
        throw new GraphQLError(
          `Resolver returned non-string value for "${typeName}": ${safePreview(value)}`,
          'COERCION_FAILURE',
        );
      }
      return value;
    case 'ID':
      if (typeof value === 'string') return value;
      if (typeof value === 'number' && Number.isSafeInteger(value)) return String(value);
      throw new GraphQLError(
        `Resolver returned invalid value for "ID": ${safePreview(value)}`,
        'COERCION_FAILURE',
      );
    case 'Boolean':
      if (typeof value !== 'boolean') {
        throw new GraphQLError(
          `Resolver returned non-boolean value for "Boolean": ${safePreview(value)}`,
          'COERCION_FAILURE',
        );
      }
      return value;
    default:
      return value;
  }
}
