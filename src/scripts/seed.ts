/**
 * 本地播种脚本：npm run seed [-- --reset]
 * --reset：删除数据文件中的业务表后重建（仅本地开发用途）。
 */
import { rmSync } from 'node:fs';
import { loadConfig } from '../config.js';
import { openDatabase } from '../data/db.js';
import { seedDatabase } from '../data/fixtures.js';

const reset = process.argv.includes('--reset');
const config = loadConfig();

if (reset && config.dbPath !== ':memory:') {
  for (const suffix of ['', '-wal', '-shm']) {
    rmSync(config.dbPath + suffix, { force: true });
  }
}

const db = openDatabase(config.dbPath);
const counts = seedDatabase(db);
process.stdout.write(
  `seed complete: users=${counts.users} posts=${counts.posts} comments=${counts.comments}\n`,
);
db.close();
