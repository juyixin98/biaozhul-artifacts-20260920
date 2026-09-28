/**
 * Schema：受限子集的类型结构声明 + 解析器注册表。
 * 结构与解析器分离：本文件只描述类型、字段、参数与可空性，
 * 具体取值逻辑由 resolvers.ts 注入（便于替换为故障/内存实现做测试）。
 */
import { GraphQLError } from './errors.js';
import {
  type ArgDef,
  type FieldDef,
  type NamedType,
  type ObjectType,
  type ScalarType,
  parseTypeRef,
  type TypeRef,
} from './types.js';

export interface FieldResolveInfo {
  fieldName: string;
  responseKey: string;
  /** 当前字段在响应中的完整路径（不含本字段） */
  path: ReadonlyArray<string | number>;
}

export type FieldResolver<TContext = unknown> = (
  source: unknown,
  args: Record<string, unknown>,
  ctx: TContext,
  info: FieldResolveInfo,
) => unknown;

export interface GraphQLSchema<TContext = unknown> {
  queryType: ObjectType;
  mutationType: ObjectType;
  types: Map<string, NamedType>;
  resolvers: Map<string, Map<string, FieldResolver<TContext>>>;
}

/**
 * 只读类型视图：校验/变量强制只依赖类型结构，不依赖解析器上下文。
 * 使用它可避免 GraphQLSchema<TContext> 在 TContext 上的逆变问题。
 */
export type SchemaTypeView = Pick<
  GraphQLSchema<unknown>,
  'types' | 'queryType' | 'mutationType'
>;

const dateTimeScalar: ScalarType = {
  kind: 'SCALAR',
  name: 'DateTime',
  assertOutput(value: unknown): void {
    if (typeof value !== 'string' || Number.isNaN(Date.parse(value))) {
      throw new GraphQLError('Expected an ISO-8601 date-time string', 'COERCION_FAILURE');
    }
  },
};

/** 内置标量的输出断言在 coercion.coerceOutputScalar 中实现；自定义标量走此处 */
const passthrough: ScalarType['assertOutput'] = () => {};

function arg(
  name: string,
  type: string,
  options: { defaultValue?: unknown; sensitive?: boolean } = {},
): ArgDef {
  return {
    name,
    type: parseTypeRef(type),
    ...(options.defaultValue !== undefined ? { defaultValue: options.defaultValue } : {}),
    ...(options.sensitive ? { sensitive: true } : {}),
  };
}

function field(
  name: string,
  type: string,
  args: ArgDef[] = [],
  options: { sensitive?: boolean } = {},
): FieldDef {
  return {
    name,
    type: parseTypeRef(type),
    args: new Map(args.map((a) => [a.name, a])),
    ...(options.sensitive ? { sensitive: true } : {}),
  };
}

export interface SchemaDefinition<TContext> {
  resolvers: Record<string, Record<string, FieldResolver<TContext>>>;
}

export interface SchemaSpec<TContext> extends SchemaDefinition<TContext> {
  objects: Record<string, FieldDef[]>;
  /** 除 Int/String/ID/Boolean/DateTime 外的自定义标量名 */
  customScalars?: string[];
  enums?: Record<string, readonly string[]>;
}

/**
 * 通用 schema 工厂：生产 schema 与单元测试的最小模型都经此构造，
 * 保证结构表示与解析器注册方式只有一种实现。
 */
export function makeSchema<TContext>(spec: SchemaSpec<TContext>): GraphQLSchema<TContext> {
  const types = new Map<string, NamedType>();

  const scalar = (name: string, assertOutput: ScalarType['assertOutput'] = passthrough): void => {
    types.set(name, { kind: 'SCALAR', name, assertOutput });
  };
  scalar('Int');
  scalar('String');
  scalar('ID');
  scalar('Boolean');
  types.set('DateTime', dateTimeScalar);
  for (const name of spec.customScalars ?? []) scalar(name);

  for (const [name, values] of Object.entries(spec.enums ?? {})) {
    types.set(name, { kind: 'ENUM', name, values: new Set(values) });
  }

  for (const [name, fields] of Object.entries(spec.objects)) {
    const objectType: ObjectType = {
      kind: 'OBJECT',
      name,
      fields: new Map(fields.map((f) => [f.name, f])),
    };
    types.set(name, objectType);
  }

  const queryType = types.get('Query');
  const mutationType = types.get('Mutation');
  if (!queryType || queryType.kind !== 'OBJECT') {
    throw new Error('Schema must define an object type named "Query"');
  }
  if (!mutationType || mutationType.kind !== 'OBJECT') {
    throw new Error('Schema must define an object type named "Mutation"');
  }

  const resolvers = new Map<string, Map<string, FieldResolver<TContext>>>();
  for (const [typeName, fieldMap] of Object.entries(spec.resolvers)) {
    resolvers.set(typeName, new Map(Object.entries(fieldMap)));
  }

  return { types, queryType, mutationType, resolvers };
}

/**
 * 单一结构真源：类型、字段、参数、可空性与解析器键名都在这里声明。
 * 执行内核只读这一份结构。
 */
export function buildSchema<TContext>(definition: SchemaDefinition<TContext>): GraphQLSchema<TContext> {
  return makeSchema<TContext>({
    objects: {
    User: [
      field('id', 'ID!'),
      field('handle', 'String!'),
      field('displayName', 'String!'),
      // 可空字段：昵称可能不存在；null 停在本字段，不向父级冒泡
      field('nickname', 'String'),
      field('email', 'String!', [], { sensitive: true }),
      field('emailVerified', 'Boolean!'),
      field('createdAt', 'DateTime!'),
      field('friends', '[User!]!'),
      field('posts', '[Post!]!', [arg('includeDrafts', 'Boolean', { defaultValue: false })]),
    ],
    Post: [
      field('id', 'ID!'),
      field('title', 'String!'),
      field('body', 'String!'),
      field('status', 'PostStatus!'),
      field('tags', '[String!]!'),
      field('createdAt', 'DateTime!'),
      field('author', 'User!'),
      field('comments', '[Comment!]!', [arg('includeFault', 'Boolean', { defaultValue: false })]),
      field('related', '[Post!]!'),
      field('archivedComments', '[Comment!]!'),
      // 两个可空故障字段：失败停在字段本地，兄弟字段仍可返回
      field('faultyNote', 'String'),
      field('badCount', 'Int'),
    ],
    Comment: [
      field('id', 'ID!'),
      // body 声明非空；故障夹具会给出 null，用于演示非空冒泡
      field('body', 'String!'),
      field('author', 'User!'),
      field('post', 'Post!'),
      field('createdAt', 'DateTime!'),
    ],
    Query: [
      field('user', 'User', [arg('id', 'ID'), arg('handle', 'String')]),
      field('users', '[User!]!', [arg('ids', '[ID!]')]),
      field('post', 'Post', [arg('id', 'ID!')]),
      field('posts', '[Post!]!', [
        arg('status', 'PostStatus', { defaultValue: 'PUBLIC' }),
        arg('limit', 'Int', { defaultValue: 20 }),
      ]),
      field('feed', '[Post!]!', [arg('limit', 'Int', { defaultValue: 10 })]),
      // 并发演示夹具：延迟 ms 毫秒后回显 value
      field('echoDelay', 'Int!', [arg('ms', 'Int!'), arg('value', 'Int!')]),
    ],
    Mutation: [
      // 顺序夹具：返回自增序号，可注入延迟，用于断言 mutation 顶层严格按序
      field('recordPulse', 'Int!', [
        arg('label', 'String!'),
        arg('delayMs', 'Int', { defaultValue: 0 }),
      ]),
      field('setNickname', 'User!', [
        arg('userId', 'ID!'),
        arg('nickname', 'String!'),
      ]),
      field('createPost', 'Post!', [
        arg('authorId', 'ID!'),
        arg('title', 'String!'),
        arg('body', 'String!'),
        arg('status', 'PostStatus', { defaultValue: 'DRAFT' }),
      ]),
      // 可空故障 mutation：解析器抛错停在本字段，后续顶层字段仍按序执行
      field('boom', 'Boolean', [arg('message', 'String', { defaultValue: 'boom' })]),
    ],
    },
    enums: {
      PostStatus: ['DRAFT', 'PUBLIC', 'ARCHIVED'],
    },
    resolvers: definition.resolvers,
  });
}

export function getObjectType(schema: GraphQLSchema, name: string): ObjectType {
  const type = schema.types.get(name);
  if (!type) {
    throw new GraphQLError(`Type "${name}" is not defined in the schema`, 'VALIDATION');
  }
  if (type.kind !== 'OBJECT') {
    throw new GraphQLError(`Type "${name}" is not an object type`, 'VALIDATION');
  }
  return type;
}

export function getResolver<TContext>(
  schema: GraphQLSchema<TContext>,
  typeName: string,
  fieldName: string,
): FieldResolver<TContext> {
  const byField = schema.resolvers.get(typeName);
  const resolver = byField?.get(fieldName);
  if (!resolver) {
    throw new GraphQLError(
      `No resolver configured for "${typeName}.${fieldName}"`,
      'INTERNAL',
    );
  }
  return resolver;
}

export { type TypeRef };
