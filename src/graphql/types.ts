/**
 * 类型系统的内部表示（不是 AST）：
 * 命名类型（标量/枚举/对象）与可空性包装（NAMED / LIST + nonNull）。
 */

export interface NamedTypeRef {
  kind: 'NAMED';
  name: string;
  nonNull: boolean;
}

export interface ListTypeRef {
  kind: 'LIST';
  ofType: TypeRef;
  nonNull: boolean;
}

export type TypeRef = NamedTypeRef | ListTypeRef;

export interface ArgDef {
  name: string;
  type: TypeRef;
  defaultValue?: unknown;
  /** 诊断日志中该参数需要脱敏（如口令、邮箱） */
  sensitive?: boolean;
}

export interface FieldDef {
  name: string;
  type: TypeRef;
  args: Map<string, ArgDef>;
  /** 诊断日志中该字段返回值需要脱敏 */
  sensitive?: boolean;
}

export interface ScalarType {
  kind: 'SCALAR';
  name: string;
  /** 输出强制：返回值不满足时抛错（执行期错误，而非 500） */
  assertOutput: (value: unknown) => void;
}

export interface EnumType {
  kind: 'ENUM';
  name: string;
  values: ReadonlySet<string>;
}

export interface ObjectType {
  kind: 'OBJECT';
  name: string;
  fields: Map<string, FieldDef>;
}

export type NamedType = ScalarType | EnumType | ObjectType;

export function isObjectType(t: NamedType | undefined): t is ObjectType {
  return t?.kind === 'OBJECT';
}

/** "ID!" / "[String!]!" -> TypeRef */
export function parseTypeRef(text: string): TypeRef {
  let i = 0;

  const parseInner = (): TypeRef => {
    let ref: TypeRef;
    if (text[i] === '[') {
      i += 1;
      const inner = parseInner();
      if (text[i] !== ']') {
        throw new Error(`Malformed list type in "${text}" at offset ${i}`);
      }
      i += 1;
      ref = { kind: 'LIST', ofType: inner, nonNull: false };
    } else {
      const start = i;
      while (i < text.length && /[A-Za-z0-9_]/.test(text[i]!)) i += 1;
      const name = text.slice(start, i);
      if (name.length === 0) {
        throw new Error(`Malformed type reference "${text}"`);
      }
      ref = { kind: 'NAMED', name, nonNull: false };
    }
    if (text[i] === '!') {
      i += 1;
      return { ...ref, nonNull: true };
    }
    return ref;
  };

  const result = parseInner();
  if (i !== text.length) {
    throw new Error(`Trailing characters in type reference "${text}"`);
  }
  return result;
}

export function typeRefToString(ref: TypeRef): string {
  const inner = ref.kind === 'LIST' ? `[${typeRefToString(ref.ofType)}]` : ref.name;
  return ref.nonNull ? `${inner}!` : inner;
}

export function withNullability(ref: TypeRef, nonNull: boolean): TypeRef {
  return { ...ref, nonNull };
}
