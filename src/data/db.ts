/**
 * SQLite 连接与迁移。
 * 使用 Node 22 内置的 node:sqlite，零原生编译依赖；
 * 数据库文件本地存放（data/app.db），测试使用 :memory:。
 */
import { mkdirSync } from 'node:fs';
import path from 'node:path';
import { DatabaseSync } from 'node:sqlite';

let warned = false;

export function openDatabase(filename: string): DatabaseSync {
  if (filename !== ':memory:') {
    mkdirSync(path.dirname(filename), { recursive: true });
  }
  const db = new DatabaseSync(filename);
  db.exec('PRAGMA journal_mode = WAL;');
  db.exec('PRAGMA foreign_keys = ON;');
  migrateDatabase(db);
  if (!warned && filename !== ':memory:') {
    warned = true;
  }
  return db;
}

/** 供测试在既有内存连接上执行同一套幂等迁移 */
export function migrateDatabase(db: DatabaseSync): void {
  migrate(db);
}

function migrate(db: DatabaseSync): void {
  db.exec(`
    CREATE TABLE IF NOT EXISTS users (
      id            TEXT PRIMARY KEY,
      handle        TEXT NOT NULL UNIQUE,
      display_name  TEXT NOT NULL,
      email         TEXT NOT NULL,
      email_verified INTEGER NOT NULL CHECK (email_verified IN (0, 1)),
      nickname      TEXT,
      created_at    TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS friendships (
      user_id    TEXT NOT NULL REFERENCES users(id),
      friend_id  TEXT NOT NULL REFERENCES users(id),
      PRIMARY KEY (user_id, friend_id)
    );

    CREATE TABLE IF NOT EXISTS posts (
      id          TEXT PRIMARY KEY,
      author_id   TEXT NOT NULL REFERENCES users(id),
      title       TEXT NOT NULL,
      body        TEXT NOT NULL,
      status      TEXT NOT NULL CHECK (status IN ('DRAFT', 'PUBLIC', 'ARCHIVED')),
      tags        TEXT NOT NULL DEFAULT '[]',
      created_at  TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS comments (
      id          TEXT PRIMARY KEY,
      post_id     TEXT NOT NULL REFERENCES posts(id),
      author_id   TEXT NOT NULL REFERENCES users(id),
      body        TEXT NOT NULL,
      created_at  TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS mutation_log (
      seq         INTEGER PRIMARY KEY AUTOINCREMENT,
      label       TEXT NOT NULL,
      created_at  TEXT NOT NULL
    );
  `);
}
