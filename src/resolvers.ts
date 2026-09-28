/**
 * 解析器装配（状态适配的一部分）：
 * 把仓储的行数据映射为 GraphQL 字段取值，并植入确定性的故障夹具逻辑。
 * 解析器不感知执行内核；内核只通过 schema.resolvers 调用它们。
 */
import type { Repository } from './data/repository.js';
import type { CommentRow, PostRow, UserRow } from './data/fixtures.js';
import type { SchemaDefinition } from './graphql/schema.js';

export interface AppContext {
  repo: Repository;
  /** 每次请求生成的标识，解析器可用于错误信息 */
  requestId: string;
}

const sleep = (ms: number): Promise<void> =>
  new Promise((resolve) => setTimeout(resolve, Math.max(0, ms)));

/** 仅用于演示：解析器显式抛出的错误（会被内核归为 RESOLVER_FAILURE） */
class ResolverFault extends Error {
  constructor(message: string) {
    super(message);
    this.name = 'ResolverFault';
  }
}

function asUser(source: unknown): UserRow {
  return source as UserRow;
}

function asPost(source: unknown): PostRow {
  return source as PostRow;
}

function asComment(source: unknown): CommentRow {
  return source as CommentRow;
}

export function buildResolvers(): SchemaDefinition<AppContext>['resolvers'] {
  return {
    Query: {
      user: (_src, args, ctx) => {
        if (typeof args.id === 'string') return ctx.repo.userById(args.id);
        if (typeof args.handle === 'string') return ctx.repo.userByHandle(args.handle);
        return undefined;
      },
      users: (_src, args, ctx) => {
        const ids = Array.isArray(args.ids) ? (args.ids as string[]) : [];
        return ctx.repo.usersByIds(ids);
      },
      post: (_src, args, ctx) => ctx.repo.postById(String(args.id)),
      posts: (_src, args, ctx) => {
        const status = String(args.status ?? 'PUBLIC');
        const limit = Number(args.limit ?? 20);
        return ctx.repo.postsByStatus(status).slice(0, limit);
      },
      feed: (_src, args, ctx) => {
        const limit = Number(args.limit ?? 10);
        return ctx.repo.postsByStatus('PUBLIC').slice(0, limit);
      },
      echoDelay: async (_src, args) => {
        const ms = Number(args.ms);
        await sleep(ms);
        return Number(args.value);
      },
    },

    Mutation: {
      recordPulse: async (_src, args, ctx) => {
        const delayMs = Number(args.delayMs ?? 0);
        if (delayMs > 0) await sleep(delayMs);
        return ctx.repo.appendMutationLog(String(args.label));
      },
      setNickname: (_src, args, ctx) =>
        ctx.repo.setNickname(String(args.userId), String(args.nickname)),
      createPost: (_src, args, ctx) => {
        // 本地合成 ID，避免与夹具的固定 ID 冲突
        const id = `p-new-${Date.now().toString(36)}-${Math.floor(Math.random() * 1e6).toString(36)}`;
        return ctx.repo.createPost({
          id,
          authorId: String(args.authorId),
          title: String(args.title),
          body: String(args.body),
          status: String(args.status ?? 'DRAFT'),
        });
      },
      boom: (_src, args) => {
        throw new ResolverFault(String(args.message ?? 'boom'));
      },
    },

    User: {
      id: (src) => asUser(src).id,
      handle: (src) => asUser(src).handle,
      displayName: (src) => asUser(src).display_name,
      nickname: (src) => asUser(src).nickname,
      email: (src) => asUser(src).email,
      emailVerified: (src) => asUser(src).email_verified === 1,
      createdAt: (src) => asUser(src).created_at,
      friends: (src, _args, ctx) => ctx.repo.friendsOf(asUser(src).id),
      posts: (src, args, ctx) =>
        ctx.repo.postsByAuthor(asUser(src).id, args.includeDrafts === true),
    },

    Post: {
      id: (src) => asPost(src).id,
      title: (src) => {
        const post = asPost(src);
        if (post.id === 'p-fp-throw') {
          throw new ResolverFault('forced resolver failure on title of p-fp-throw');
        }
        return post.title;
      },
      body: (src) => asPost(src).body,
      status: (src) => asPost(src).status,
      tags: (src) => JSON.parse(asPost(src).tags) as string[],
      createdAt: (src) => asPost(src).created_at,
      author: (_src, _args, ctx) => ctx.repo.userById(asPost(_src).author_id),
      comments: (src, args, ctx) =>
        ctx.repo.commentsByPost(asPost(src).id, args.includeFault === true),
      related: (src, _args, ctx) => ctx.repo.relatedPosts(asPost(src).id),
      archivedComments: (src, _args, ctx) => ctx.repo.archivedComments(asPost(src).id),
      faultyNote: (src) => {
        const post = asPost(src);
        if (post.id === 'p-fp-throw') {
          throw new ResolverFault('forced resolver failure on faultyNote');
        }
        return null;
      },
      badCount: () => {
        // 返回错误类型：Int 字段解析器给字符串，触发 COERCION_FAILURE 而非 500
        return 'not-a-number' as unknown;
      },
    },

    Comment: {
      id: (src) => asComment(src).id,
      body: (src) => asComment(src).body,
      author: (src, _args, ctx) => ctx.repo.userById(asComment(src).author_id),
      post: (src, _args, ctx) => ctx.repo.postById(asComment(src).post_id),
      createdAt: (src) => asComment(src).created_at,
    },
  };
}
