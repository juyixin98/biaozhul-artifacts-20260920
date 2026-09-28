/**
 * 仓储层：执行内核与 SQLite 之间的唯一边界。
 * 上层（解析器）只依赖此处的接口，便于替换为内存实现做单元测试。
 */
import type { DatabaseSync, SQLInputValue, StatementSync } from 'node:sqlite';
import type { CommentRow, PostRow, UserRow } from './fixtures.js';

/** node:sqlite 的行类型较宽泛，这里集中做一次边界转换（仓储是唯一边界） */
function one<T>(stmt: StatementSync, ...params: SQLInputValue[]): T | undefined {
  return stmt.get(...params) as T | undefined;
}

function many<T>(stmt: StatementSync, ...params: SQLInputValue[]): T[] {
  return stmt.all(...params) as unknown as T[];
}

export interface Repository {
  userById(id: string): UserRow | undefined;
  userByHandle(handle: string): UserRow | undefined;
  usersByIds(ids: readonly string[]): UserRow[];
  friendsOf(userId: string): UserRow[];
  postById(id: string): PostRow | undefined;
  postsByAuthor(authorId: string, includeDrafts: boolean): PostRow[];
  postsByStatus(status: string): PostRow[];
  /** 相关帖子：共享标签或同一作者，确定性顺序，可注入空元素（故障夹具） */
  relatedPosts(postId: string): Array<PostRow | null>;
  commentsByPost(postId: string, includeNullBodyFault: boolean): Array<Omit<CommentRow, 'body'> & { body: string | null }>;
  /** 故意对故障夹具帖子返回 null（列表本身非空的冒泡夹具） */
  archivedComments(postId: string): CommentRow[] | null;
  setNickname(userId: string, nickname: string): UserRow;
  createPost(input: {
    id: string;
    authorId: string;
    title: string;
    body: string;
    status: string;
  }): PostRow;
  appendMutationLog(label: string): number;
}

export function createRepository(db: DatabaseSync): Repository {
  const userByIdStmt = db.prepare('SELECT * FROM users WHERE id = ?');
  const userByHandleStmt = db.prepare('SELECT * FROM users WHERE handle = ?');
  const usersByIdsStmt = db.prepare('SELECT * FROM users WHERE id = ?');
  const friendIdsStmt = db.prepare('SELECT friend_id AS fid FROM friendships WHERE user_id = ? ORDER BY friend_id');
  const postByIdStmt = db.prepare('SELECT * FROM posts WHERE id = ?');
  const postsByAuthorStmt = db.prepare(
    'SELECT * FROM posts WHERE author_id = ? ORDER BY created_at, id',
  );
  const postsByStatusStmt = db.prepare(
    'SELECT * FROM posts WHERE status = ? ORDER BY created_at, id',
  );
  const commentsByPostStmt = db.prepare(
    'SELECT * FROM comments WHERE post_id = ? ORDER BY created_at, id',
  );
  const tagsOfStmt = db.prepare('SELECT tags FROM posts WHERE id = ?');
  const postsByTagStmt = db.prepare(
    'SELECT * FROM posts WHERE tags LIKE ? AND id <> ? ORDER BY created_at, id',
  );
  const postsByAuthorListStmt = db.prepare(
    "SELECT * FROM posts WHERE author_id = ? AND id <> ? ORDER BY created_at, id",
  );
  const insertLogStmt = db.prepare('INSERT INTO mutation_log (label, created_at) VALUES (?, ?)');
  const lastLogStmt = db.prepare('SELECT seq FROM mutation_log ORDER BY seq DESC LIMIT 1');
  const setNicknameStmt = db.prepare('UPDATE users SET nickname = ? WHERE id = ?');
  const insertPostStmt = db.prepare(`
    INSERT INTO posts (id, author_id, title, body, status, tags, created_at)
    VALUES (?, ?, ?, ?, ?, '[]', ?)
  `);

  const userById = (id: string): UserRow | undefined => one<UserRow>(userByIdStmt, id);

  return {
    userById,

    userByHandle(handle: string): UserRow | undefined {
      return one<UserRow>(userByHandleStmt, handle);
    },

    usersByIds(ids: readonly string[]): UserRow[] {
      const found = new Map<string, UserRow>();
      for (const id of ids) {
        const row = one<UserRow>(usersByIdsStmt, id);
        if (row) found.set(id, row);
      }
      // 保持入参顺序
      return ids.map((id) => found.get(id)).filter((u): u is UserRow => u !== undefined);
    },

    friendsOf(userId: string): UserRow[] {
      const rows = many<{ fid: string }>(friendIdsStmt, userId);
      return rows
        .map((r) => userById(r.fid))
        .filter((u): u is UserRow => u !== undefined);
    },

    postById(id: string): PostRow | undefined {
      return one<PostRow>(postByIdStmt, id);
    },

    postsByAuthor(authorId: string, includeDrafts: boolean): PostRow[] {
      const rows = many<PostRow>(postsByAuthorStmt, authorId);
      return includeDrafts ? rows : rows.filter((p) => p.status !== 'DRAFT');
    },

    postsByStatus(status: string): PostRow[] {
      return many<PostRow>(postsByStatusStmt, status);
    },

    relatedPosts(postId: string): Array<PostRow | null> {
      const tagRow = one<{ tags: string }>(tagsOfStmt, postId);
      if (!tagRow) return [];
      const tags: string[] = JSON.parse(tagRow.tags) as string[];
      const collected = new Map<string, PostRow>();
      for (const tag of tags) {
        const like = `%${JSON.stringify(tag)}%`;
        for (const row of many<PostRow>(postsByTagStmt, like, postId)) {
          collected.set(row.id, row);
        }
      }
      const self = one<PostRow>(postByIdStmt, postId);
      if (self) {
        for (const row of many<PostRow>(postsByAuthorListStmt, self.author_id, postId)) {
          collected.set(row.id, row);
        }
      }
      const result = [...collected.values()].sort((a, b) => a.id.localeCompare(b.id));
      // 故障夹具：在非空元素列表中注入一个洞（元素类型 Post!）
      if (postId === 'p-fp-element') {
        return result.length > 0 ? [result[0]!, null, ...result.slice(1)] : [null];
      }
      return result;
    },

    commentsByPost(
      postId: string,
      includeNullBodyFault: boolean,
    ): Array<Omit<CommentRow, 'body'> & { body: string | null }> {
      const rows = many<CommentRow>(commentsByPostStmt, postId);
      return rows.map((row) => {
        if (includeNullBodyFault && row.id === 'c-fp-nullbody') {
          const { body: _body, ...rest } = row;
          void _body;
          return { ...rest, body: null };
        }
        return row;
      });
    },

    archivedComments(postId: string): CommentRow[] | null {
      // 故障夹具：列表本身声明为 [Comment!]!，解析器却给出 null
      if (postId === 'p-fp-listnull') return null;
      return many<CommentRow>(commentsByPostStmt, postId);
    },

    setNickname(userId: string, nickname: string): UserRow {
      const existing = userById(userId);
      if (!existing) throw new Error(`No user with id ${userId}`);
      setNicknameStmt.run(nickname, userId);
      return { ...existing, nickname };
    },

    createPost(input: {
      id: string;
      authorId: string;
      title: string;
      body: string;
      status: string;
    }): PostRow {
      const now = new Date().toISOString();
      insertPostStmt.run(
        input.id,
        input.authorId,
        input.title,
        input.body,
        input.status,
        now,
      );
      const created = one<PostRow>(postByIdStmt, input.id);
      if (!created) throw new Error(`Failed to load newly created post ${input.id}`);
      return created;
    },

    appendMutationLog(label: string): number {
      insertLogStmt.run(label, new Date().toISOString());
      const row = one<{ seq: number }>(lastLogStmt);
      if (!row) throw new Error('mutation_log did not return a sequence');
      return row.seq;
    },
  };
}
