/**
 * SQLite 适配：连接、DDL、合成夹具播种。
 * 数据库文件可配置（默认 data/app.db），测试用 :memory:。
 */

import Database, { type Database as DatabaseType } from 'better-sqlite3';
import { mkdirSync } from 'node:fs';
import { dirname } from 'node:path';

export interface SeedData {
  users: Array<{
    id: string;
    name: string;
    email: string;
    tags: string[];
    scores: Array<number | null>;
    bestFriendId: string | null;
  }>;
  posts: Array<{
    id: string;
    authorId: string;
    title: string | null;
    tags: string[];
    failTitle?: boolean;
  }>;
  comments: Array<{ id: string; postId: string; authorId: string; body: string }>;
  counters: Array<{ id: string; value: number }>;
  strictTags: string[];
  looseTags: string[];
}

const SCHEMA_DDL = `
CREATE TABLE IF NOT EXISTS users (
  id           TEXT PRIMARY KEY,
  name         TEXT NOT NULL,
  email        TEXT NOT NULL,
  tags         TEXT NOT NULL,
  scores       TEXT NOT NULL,
  best_friend_id TEXT
);

CREATE TABLE IF NOT EXISTS posts (
  id         TEXT PRIMARY KEY,
  author_id  TEXT NOT NULL REFERENCES users(id),
  title      TEXT,
  tags       TEXT NOT NULL,
  fail_title INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS comments (
  id        TEXT PRIMARY KEY,
  post_id   TEXT NOT NULL REFERENCES posts(id),
  author_id TEXT NOT NULL REFERENCES users(id),
  body      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS counters (
  id    TEXT PRIMARY KEY,
  value INTEGER NOT NULL
);
`;

export function openDatabase(filename: string): DatabaseType {
  if (filename !== ':memory:') {
    mkdirSync(dirname(filename), { recursive: true });
  }
  const db = new Database(filename);
  db.pragma('journal_mode = WAL');
  db.pragma('foreign_keys = ON');
  db.exec(SCHEMA_DDL);
  return db;
}

export function seedDatabase(db: DatabaseType, data: SeedData): void {
  const insertUser = db.prepare(
    `INSERT INTO users (id, name, email, tags, scores, best_friend_id)
     VALUES (@id, @name, @email, @tags, @scores, @bestFriendId)`,
  );
  const insertPost = db.prepare(
    `INSERT INTO posts (id, author_id, title, tags, fail_title)
     VALUES (@id, @authorId, @title, @tags, @failTitle)`,
  );
  const insertComment = db.prepare(
    `INSERT INTO comments (id, post_id, author_id, body)
     VALUES (@id, @postId, @authorId, @body)`,
  );
  const insertCounter = db.prepare(
    `INSERT INTO counters (id, value) VALUES (@id, @value)`,
  );

  const tx = db.transaction(() => {
    db.exec('DELETE FROM comments; DELETE FROM posts; DELETE FROM users; DELETE FROM counters;');
    for (const user of data.users) {
      insertUser.run({
        id: user.id,
        name: user.name,
        email: user.email,
        tags: JSON.stringify(user.tags),
        scores: JSON.stringify(user.scores),
        bestFriendId: user.bestFriendId,
      });
    }
    for (const post of data.posts) {
      insertPost.run({
        id: post.id,
        authorId: post.authorId,
        title: post.title,
        tags: JSON.stringify(post.tags),
        failTitle: post.failTitle ? 1 : 0,
      });
    }
    for (const comment of data.comments) insertComment.run(comment);
    for (const counter of data.counters) insertCounter.run(counter);
  });
  tx();
}

export function isSeeded(db: DatabaseType): boolean {
  const row = db.prepare('SELECT COUNT(*) AS n FROM users').get() as { n: number };
  return row.n > 0;
}

export interface UserRow {
  id: string;
  name: string;
  email: string;
  tags: string;
  scores: string;
  best_friend_id: string | null;
}

export interface PostRow {
  id: string;
  author_id: string;
  title: string | null;
  tags: string;
  fail_title: number;
}

export interface CommentRow {
  id: string;
  post_id: string;
  author_id: string;
  body: string;
}

export function mapUser(row: UserRow) {
  return {
    id: row.id,
    name: row.name,
    email: row.email,
    tags: JSON.parse(row.tags) as string[],
    scores: JSON.parse(row.scores) as Array<number | null>,
    bestFriendId: row.best_friend_id,
  };
}

export function mapPost(row: PostRow) {
  return {
    id: row.id,
    authorId: row.author_id,
    title: row.title,
    tags: JSON.parse(row.tags) as string[],
    failTitle: row.fail_title === 1,
  };
}

export function mapComment(row: CommentRow) {
  return {
    id: row.id,
    postId: row.post_id,
    authorId: row.author_id,
    body: row.body,
  };
}
