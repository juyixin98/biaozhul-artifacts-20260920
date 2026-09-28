/**
 * 合成夹具：全部为确定性的本地虚构数据，无真实业务/个人信息。
 * 除常规记录外，植入固定的故障夹具记录（fp-* 前缀），
 * 供非空冒泡、解析器抛错等验收用例稳定复现。
 */
import type { DatabaseSync } from 'node:sqlite';

export interface UserRow {
  id: string;
  handle: string;
  display_name: string;
  email: string;
  email_verified: number;
  nickname: string | null;
  created_at: string;
}

export interface PostRow {
  id: string;
  author_id: string;
  title: string;
  body: string;
  status: 'DRAFT' | 'PUBLIC' | 'ARCHIVED';
  tags: string;
  created_at: string;
}

export interface CommentRow {
  id: string;
  post_id: string;
  author_id: string;
  body: string;
  created_at: string;
}

interface Fixture {
  users: UserRow[];
  friendships: Array<[string, string]>;
  posts: PostRow[];
  comments: CommentRow[];
}

const T0 = '2026-01-15T09:00:00.000Z';
const iso = (minutes: number): string =>
  new Date(new Date(T0).getTime() + minutes * 60_000).toISOString();

export function buildFixture(): Fixture {
  const people: Array<[string, string, string]> = [
    ['u-01', 'ada', 'Ada Baker'],
    ['u-02', 'lin', 'Lin Carter'],
    ['u-03', 'noa', 'Noa Diaz'],
    ['u-04', 'kai', 'Kai Evans'],
    ['u-05', 'rae', 'Rae Foster'],
    ['u-06', 'mia', 'Mia Garcia'],
  ];

  const users: UserRow[] = people.map(([id, handle, displayName], i) => ({
    id,
    handle,
    display_name: displayName,
    email: `${handle}@example.test`,
    email_verified: i % 2 === 0 ? 1 : 0,
    nickname: i === 2 ? 'noa-bean' : null,
    created_at: iso(i),
  }));

  const friendships: Array<[string, string]> = [
    ['u-01', 'u-02'],
    ['u-01', 'u-03'],
    ['u-02', 'u-04'],
    ['u-03', 'u-05'],
  ];

  const posts: PostRow[] = [
    {
      id: 'p-01',
      author_id: 'u-01',
      title: 'Getting started with local fixtures',
      body: 'All data in this service is synthetic and generated deterministically.',
      status: 'PUBLIC',
      tags: JSON.stringify(['intro', 'fixtures']),
      created_at: iso(10),
    },
    {
      id: 'p-02',
      author_id: 'u-02',
      title: 'Notes on non-null propagation',
      body: 'A null inside a non-null list bubbles differently than a null list itself.',
      status: 'PUBLIC',
      tags: JSON.stringify(['graphql', 'errors']),
      created_at: iso(20),
    },
    {
      id: 'p-03',
      author_id: 'u-03',
      title: 'Draft thoughts about fragments',
      body: 'This post starts life as a draft and is excluded from the default feed.',
      status: 'DRAFT',
      tags: JSON.stringify(['graphql']),
      created_at: iso(30),
    },
    {
      id: 'p-04',
      author_id: 'u-01',
      title: 'Archived changelog',
      body: 'Old entries kept around for archive queries.',
      status: 'ARCHIVED',
      tags: JSON.stringify(['changelog']),
      created_at: iso(40),
    },
    // ---- 故障夹具（固定 ID，解析器中按规则触发） ----
    {
      id: 'p-fp-element',
      author_id: 'u-04',
      title: 'Fault: null element inside non-null list',
      body: 'relatedPosts on this post resolves a list that contains a hole.',
      status: 'PUBLIC',
      tags: JSON.stringify(['fault', 'non-null-element']),
      created_at: iso(50),
    },
    {
      id: 'p-fp-listnull',
      author_id: 'u-05',
      title: 'Fault: non-null list resolver returns null',
      body: 'archivedComments on this post resolves null although the list is declared non-null.',
      status: 'PUBLIC',
      tags: JSON.stringify(['fault', 'non-null-list']),
      created_at: iso(60),
    },
    {
      id: 'p-fp-throw',
      author_id: 'u-06',
      title: 'Fault: resolver throws',
      body: 'Selecting title on this post throws a resolver error; body still works.',
      status: 'PUBLIC',
      tags: JSON.stringify(['fault', 'resolver-throw']),
      created_at: iso(70),
    },
  ];

  const comments: CommentRow[] = [
    {
      id: 'c-01',
      post_id: 'p-01',
      author_id: 'u-02',
      body: 'Thanks, this helped me bootstrap locally.',
      created_at: iso(15),
    },
    {
      id: 'c-02',
      post_id: 'p-01',
      author_id: 'u-03',
      body: 'The deterministic ids make tests easy to write.',
      created_at: iso(18),
    },
    {
      id: 'c-03',
      post_id: 'p-02',
      author_id: 'u-01',
      body: 'Element null versus list null is the subtle part.',
      created_at: iso(25),
    },
    {
      id: 'c-04',
      post_id: 'p-fp-element',
      author_id: 'u-01',
      body: 'Only real comment on the fault post; the list gets a hole injected.',
      created_at: iso(55),
    },
    {
      id: 'c-fp-nullbody',
      post_id: 'p-02',
      author_id: 'u-04',
      body: 'FAULT-NULL-BODY',
      created_at: iso(28),
    },
  ];

  return { users, friendships, posts, comments };
}

export function seedDatabase(db: DatabaseSync): { users: number; posts: number; comments: number } {
  const fixture = buildFixture();

  const insertUser = db.prepare(`
    INSERT OR REPLACE INTO users (id, handle, display_name, email, email_verified, nickname, created_at)
    VALUES (?, ?, ?, ?, ?, ?, ?)
  `);
  const insertFriend = db.prepare(
    'INSERT OR REPLACE INTO friendships (user_id, friend_id) VALUES (?, ?)',
  );
  const insertPost = db.prepare(`
    INSERT OR REPLACE INTO posts (id, author_id, title, body, status, tags, created_at)
    VALUES (?, ?, ?, ?, ?, ?, ?)
  `);
  const insertComment = db.prepare(`
    INSERT OR REPLACE INTO comments (id, post_id, author_id, body, created_at)
    VALUES (?, ?, ?, ?, ?)
  `);

  db.exec('BEGIN');
  try {
    for (const u of fixture.users) {
      insertUser.run(
        u.id,
        u.handle,
        u.display_name,
        u.email,
        u.email_verified,
        u.nickname,
        u.created_at,
      );
    }
    for (const [userId, friendId] of fixture.friendships) {
      insertFriend.run(userId, friendId);
    }
    for (const p of fixture.posts) {
      insertPost.run(p.id, p.author_id, p.title, p.body, p.status, p.tags, p.created_at);
    }
    for (const c of fixture.comments) {
      insertComment.run(c.id, c.post_id, c.author_id, c.body, c.created_at);
    }
    db.exec('COMMIT');
  } catch (err) {
    db.exec('ROLLBACK');
    throw err;
  }

  return {
    users: fixture.users.length,
    posts: fixture.posts.length,
    comments: fixture.comments.length,
  };
}

export function isSeeded(db: DatabaseSync): boolean {
  const row = db.prepare('SELECT COUNT(*) AS n FROM users').get() as { n: number };
  return row.n > 0;
}
