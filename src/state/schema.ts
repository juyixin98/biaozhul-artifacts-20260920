/**
 * 业务 GraphQL schema 与 resolver 映射（状态适配层的上半部分）。
 * 所有数据来自 SQLite（合成夹具），resolver 通过 context.db 访问。
 *
 * 夹具内置的失败点（用于非空冒泡核验）：
 *  - Tag 标量序列化遇到 "__THROW__" 抛错 -> 驱动列表元素非空冒泡
 *  - posts 中 id="p4" 的 title 解析器抛 RESOLVER 错误（title 可空）
 *  - users 中 u3 的 pet.name! 为 "__THROW__"（pet 可空，冒泡止于 pet）
 *  - mutation.bump 支持 delayMs，用于核验顶层串行
 */

import {
  BUILTIN_SCALARS,
  GraphQLError,
  type GraphQLSchema,
  type ResolverFn,
  list,
  named,
  nonNull,
  type GraphQLObjectType,
  type GraphQLScalarType,
  type FieldDef,
  type InputArgDef,
  type TypeRef,
} from '../graphql/index.js';
import {
  mapComment,
  mapPost,
  mapUser,
  type CommentRow,
  type PostRow,
  type UserRow,
} from './db.js';

const sleep = (ms: number): Promise<void> =>
  new Promise((resolve) => setTimeout(resolve, ms));

/** Tag：普通字符串标量，但序列化保留标记 "__THROW__" 时抛错（合成解析器失败）。 */
const TagScalar: GraphQLScalarType = {
  kind: 'SCALAR',
  name: 'Tag',
  description: 'String-like scalar; the fixture marker "__THROW__" fails serialization.',
  parseValue: (value) => {
    if (typeof value !== 'string') throw new Error('Tag must be a string');
    return value;
  },
  serialize: (value) => {
    if (typeof value !== 'string') throw new Error('Tag must serialize from string');
    if (value === '__THROW__') {
      throw new Error('Synthetic tag resolver failure (fixture marker "__THROW__")');
    }
    return value;
  },
};

/* --------------------------------- 行加载工具 --------------------------------- */

function getUserRow(db: import('better-sqlite3').Database, id: string): UserRow | undefined {
  return db.prepare('SELECT * FROM users WHERE id = ?').get(id) as UserRow | undefined;
}

function getPostRow(db: import('better-sqlite3').Database, id: string): PostRow | undefined {
  return db.prepare('SELECT * FROM posts WHERE id = ?').get(id) as PostRow | undefined;
}

/* ---------------------------------- resolver --------------------------------- */

const userResolver: ResolverFn = (_source, args, ctx) => {
  const row = getUserRow(ctx.db, String(args.id));
  return row ? mapUser(row) : null;
};

const usersResolver: ResolverFn = (_source, args, ctx) => {
  const limit = args.limit === undefined ? null : Number(args.limit);
  const rows = ctx.db.prepare('SELECT * FROM users ORDER BY id').all() as UserRow[];
  return rows.slice(0, limit ?? rows.length).map(mapUser);
};

const postsResolver: ResolverFn = (_source, _args, ctx) => {
  const rows = ctx.db.prepare('SELECT * FROM posts ORDER BY id').all() as PostRow[];
  return rows.map(mapPost);
};

const postResolver: ResolverFn = (_source, args, ctx) => {
  const row = getPostRow(ctx.db, String(args.id));
  return row ? mapPost(row) : null;
};

const strictTagsResolver: ResolverFn = (): string[] => [
  'alpha', 'beta', '__THROW__', 'delta',
];

const looseTagsResolver: ResolverFn = (): string[] => [
  'alpha', '__THROW__', 'gamma',
];

const nestedTagsResolver: ResolverFn = (): string[][] => [
  ['alpha', 'beta'],
  ['gamma', '__THROW__'],
  ['delta'],
];

/**
 * 并发探针：读取 counter 当前值，可延迟返回。
 * 同一 query 内两个别名字段并发执行时，会读到相同值（中间无 mutation）。
 */
const snapshotCounterResolver: ResolverFn = async (_source, args, ctx) => {
  const delay = Number(args.delayMs ?? 0);
  if (delay > 0) await sleep(delay);
  const row = ctx.db.prepare('SELECT value FROM counters WHERE id = ?').get('c1') as
    | { value: number }
    | undefined;
  return row?.value ?? 0;
};

const bumpResolver: ResolverFn = async (_source, args, ctx) => {
  const delay = Number(args.delayMs ?? 0);
  if (delay > 0) await sleep(delay);
  const id = String(args.id ?? 'c1');
  ctx.db
    .prepare('UPDATE counters SET value = value + 1 WHERE id = ?')
    .run(id);
  const row = ctx.db.prepare('SELECT * FROM counters WHERE id = ?').get(id) as
    | { id: string; value: number }
    | undefined;
  if (!row) {
    throw new GraphQLError(`Counter "${id}" not found`, { category: 'RESOLVER' });
  }
  return row;
};

/* --------------------------------- 字段构造糖 --------------------------------- */

function field(
  name: string,
  type: TypeRef,
  resolve?: ResolverFn,
  extra: { args?: Record<string, InputArgDef>; description?: string } = {},
): FieldDef {
  return {
    name,
    type,
    resolve,
    args: new Map(Object.entries(extra.args ?? {})),
    ...(extra.description ? { description: extra.description } : {}),
  };
}

function arg(name: string, type: TypeRef, defaultValue?: unknown): InputArgDef {
  return defaultValue === undefined ? { name, type } : { name, type, defaultValue };
}

/* ---------------------------------- 组装类型 ---------------------------------- */

export function buildSchema(): GraphQLSchema {
  const types = new Map<string, ReturnType<GraphQLSchema['getType']>>();

  const userType: GraphQLObjectType = {
    kind: 'OBJECT',
    name: 'User',
    fields: new Map<string, FieldDef>(),
  };
  const postType: GraphQLObjectType = {
    kind: 'OBJECT',
    name: 'Post',
    fields: new Map<string, FieldDef>(),
  };
  const commentType: GraphQLObjectType = {
    kind: 'OBJECT',
    name: 'Comment',
    fields: new Map<string, FieldDef>(),
  };
  const petType: GraphQLObjectType = {
    kind: 'OBJECT',
    name: 'Pet',
    fields: new Map<string, FieldDef>(),
  };
  const counterType: GraphQLObjectType = {
    kind: 'OBJECT',
    name: 'Counter',
    fields: new Map<string, FieldDef>(),
  };

  const queryType: GraphQLObjectType = {
    kind: 'OBJECT',
    name: 'Query',
    fields: new Map<string, FieldDef>(),
  };
  const mutationType: GraphQLObjectType = {
    kind: 'OBJECT',
    name: 'Mutation',
    fields: new Map<string, FieldDef>(),
  };

  userType.fields = new Map<string, FieldDef>([
    ['id', field('id', nonNull(named('ID')))],
    ['name', field('name', nonNull(named('String')))],
    ['email', field('email', nonNull(named('String')))],
    ['tags', field('tags', nonNull(list(nonNull(named('String')))))],
    ['scores', field('scores', list(named('Int')))],
    [
      'bestFriend',
      field('bestFriend', named('User'), (_s, _a, ctx) => {
        const friendId = (_s as { bestFriendId: string | null }).bestFriendId;
        if (!friendId) return null;
        const row = getUserRow(ctx.db, friendId);
        return row ? mapUser(row) : null;
      }),
    ],
    [
      'posts',
      field('posts', nonNull(list(nonNull(named('Post')))), (source, _a, ctx) => {
        const rows = ctx.db
          .prepare('SELECT * FROM posts WHERE author_id = ? ORDER BY id')
          .all((source as { id: string }).id) as PostRow[];
        return rows.map(mapPost);
      }),
    ],
    [
      'pet',
      field('pet', named('Pet'), (source) => {
        // 合成夹具：u2 有正常宠物；u3 的宠物名命中失败标记；u1 无宠物(null)。
        const pets: Record<string, { name: string } | null> = {
          u1: null,
          u2: { name: 'Rex' },
          u3: { name: '__THROW__' },
        };
        return pets[(source as { id: string }).id] ?? null;
      }),
    ],
  ]);

  postType.fields = new Map<string, FieldDef>([
    ['id', field('id', nonNull(named('ID')))],
    [
      'author',
      field('author', nonNull(named('User')), (source, _a, ctx) => {
        const row = getUserRow(ctx.db, (source as { authorId: string }).authorId);
        if (!row) {
          throw new GraphQLError('Author missing for post (fixture)', {
            category: 'RESOLVER',
          });
        }
        return mapUser(row);
      }),
    ],
    [
      'title',
      field('title', named('String'), (source) => {
        if ((source as { failTitle?: boolean }).failTitle) {
          throw new GraphQLError('Synthetic failure resolving post title (fixture)', {
            category: 'RESOLVER',
          });
        }
        return (source as { title: string | null }).title;
      }),
    ],
    ['tags', field('tags', nonNull(list(nonNull(named('String')))))],
    [
      'comments',
      field('comments', nonNull(list(nonNull(named('Comment')))), (source, _a, ctx) => {
        const rows = ctx.db
          .prepare('SELECT * FROM comments WHERE post_id = ? ORDER BY id')
          .all((source as { id: string }).id) as CommentRow[];
        return rows.map(mapComment);
      }),
    ],
  ]);

  commentType.fields = new Map<string, FieldDef>([
    ['id', field('id', nonNull(named('ID')))],
    ['body', field('body', nonNull(named('String')))],
    [
      'author',
      field('author', nonNull(named('User')), (source, _a, ctx) => {
        const row = getUserRow(ctx.db, (source as { authorId: string }).authorId);
        if (!row) throw new GraphQLError('Comment author missing', { category: 'RESOLVER' });
        return mapUser(row);
      }),
    ],
    [
      'post',
      field('post', nonNull(named('Post')), (source, _a, ctx) => {
        const row = getPostRow(ctx.db, (source as { postId: string }).postId);
        if (!row) throw new GraphQLError('Comment post missing', { category: 'RESOLVER' });
        return mapPost(row);
      }),
    ],
  ]);

  petType.fields = new Map<string, FieldDef>([
    ['name', field('name', nonNull(named('Tag')))],
  ]);

  counterType.fields = new Map<string, FieldDef>([
    ['id', field('id', nonNull(named('ID')))],
    ['value', field('value', nonNull(named('Int')))],
  ]);

  queryType.fields = new Map<string, FieldDef>([
    ['user', field('user', named('User'), userResolver, {
      args: { id: arg('id', nonNull(named('ID'))) },
    })],
    ['users', field('users', nonNull(list(nonNull(named('User')))), usersResolver, {
      args: { limit: arg('limit', named('Int')) },
    })],
    ['posts', field('posts', nonNull(list(nonNull(named('Post')))), postsResolver)],
    ['post', field('post', named('Post'), postResolver, {
      args: { id: arg('id', nonNull(named('ID'))) },
    })],
    [
      'strictTags',
      field('strictTags', nonNull(list(nonNull(named('Tag')))), strictTagsResolver),
    ],
    [
      'looseTags',
      field('looseTags', list(nonNull(named('Tag'))), looseTagsResolver),
    ],
    [
      'nestedTags',
      field('nestedTags', nonNull(list(nonNull(list(nonNull(named('Tag')))))), nestedTagsResolver),
    ],
    [
      'nestedLoose',
      field('nestedLoose', list(nonNull(list(nonNull(named('Tag'))))), nestedTagsResolver),
    ],
    [
      'snapshotCounter',
      field('snapshotCounter', nonNull(named('Int')), snapshotCounterResolver, {
        args: { delayMs: arg('delayMs', named('Int'), 0) },
      }),
    ],
  ]);

  mutationType.fields = new Map<string, FieldDef>([
    ['bump', field('bump', nonNull(named('Counter')), bumpResolver, {
      args: {
        id: arg('id', named('ID'), 'c1'),
        delayMs: arg('delayMs', named('Int'), 0),
      },
    })],
  ]);

  for (const t of [
    TagScalar,
    userType, postType, commentType, petType, counterType,
    queryType, mutationType,
  ]) {
    types.set(t.name, t);
  }

  return {
    queryType,
    mutationType,
    getType: (name) => types.get(name) ?? BUILTIN_SCALARS[name],
  };
}
