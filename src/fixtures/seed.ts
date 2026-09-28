/**
 * 本地合成种子数据。
 *
 * 这是一份固定、可手算的夹具，独立于被测内核——测试里的期望值都按
 * 这里的常量手工计算，而不是让核心自己生成答案。
 *
 * 数据形态（服务于规定的验证场景）：
 * - org 1 下有 SHALLOW_TOTAL 个成员，每个成员 DEEP_PER_USER 个任务，
 *   每个任务 COMMENTS_PER_TASK 条评论，每条评论 REPLY_DEPTH 层回复，
 *   构成“短而深”的列表链（每层元素少、嵌套深，深层被扣停）；
 * - org 1 的 orgTasks 实际有 ORG_TASKS_ACTUAL 条，超过查询常用的
 *   声明上界，用于“估计低于实际夹具”的场景；
 * - 所有 edge 带 position，保证取消时保留的前缀元素确定可断言。
 */
import { DatabaseSync } from 'node:sqlite';

export const SEED = {
  orgId: 1,
  membersTotal: 2,
  tasksPerUser: 2,
  commentsPerTask: 1,
  replyDepth: 3,
  orgTasksActual: 6,
  orgTasksDeclaredBound: 2,
} as const;

type Row = Record<string, string | number | null>;

interface Edge {
  relation: string;
  parentId: number;
  position: number;
  childId: number;
}

export interface SeededData {
  org: Row;
  users: Row[];
  tasks: Row[];
  comments: Row[];
  edges: Edge[];
}

/** 纯函数构造夹具行，便于测试在不碰数据库时手算预期。 */
export function buildSeed(): SeededData {
  const org: Row = {
    id: SEED.orgId,
    name: 'ACME',
    plan: 'enterprise',
    heavyReport: 'quarterly-report-blob',
  };

  const users: Row[] = [];
  const tasks: Row[] = [];
  const comments: Row[] = [];
  const edges: Edge[] = [];

  for (let u = 1; u <= SEED.membersTotal; u += 1) {
    const userId = u;
    users.push({
      id: userId,
      name: `user${u}`,
      active: u % 2 === 0 ? 1 : 0,
      role: u === 1 ? 'admin' : 'member',
      orgId: SEED.orgId,
    });
    edges.push({ relation: 'members', parentId: SEED.orgId, position: u - 1, childId: userId });

    for (let t = 1; t <= SEED.tasksPerUser; t += 1) {
      const taskId = (u - 1) * SEED.tasksPerUser + t;
      tasks.push({
        id: taskId,
        title: `task-${u}-${t}`,
        body: `body-${u}-${t}`,
        done: 0,
        orgId: SEED.orgId,
      });
      edges.push({ relation: 'tasks', parentId: userId, position: t - 1, childId: taskId });
      edges.push({ relation: 'assignee', parentId: taskId, position: 0, childId: userId });
      edges.push({ relation: 'orgTasks', parentId: SEED.orgId, position: taskId - 1, childId: taskId });

      for (let c = 1; c <= SEED.commentsPerTask; c += 1) {
        const commentId =
          1000 + (taskId - 1) * (SEED.commentsPerTask + SEED.replyDepth) + c;
        comments.push({ id: commentId, text: `c-${taskId}-${c}` });
        edges.push({
          relation: 'taskComments',
          parentId: taskId,
          position: c - 1,
          childId: commentId,
        });

        // 短而深的回复链：comment -> reply -> reply ...
        let parent = commentId;
        for (let r = 1; r <= SEED.replyDepth; r += 1) {
          const replyId = commentId + r;
          comments.push({ id: replyId, text: `r-${taskId}-${r}` });
          edges.push({ relation: 'replies', parentId: parent, position: 0, childId: replyId });
          parent = replyId;
        }
      }
    }
  }

  // 额外两个任务只挂在 orgTasks 关系上（不属于任何成员的 tasks），
  // 使 orgTasks 实际基数 6 > 查询常用声明上界 2（估计低于实际夹具）。
  for (let extra = 1; extra <= SEED.orgTasksActual - SEED.membersTotal * SEED.tasksPerUser; extra += 1) {
    const taskId = SEED.membersTotal * SEED.tasksPerUser + extra;
    tasks.push({
      id: taskId,
      title: `task-org-${extra}`,
      body: `body-org-${extra}`,
      done: 1,
      orgId: SEED.orgId,
    });
    edges.push({
      relation: 'orgTasks',
      parentId: SEED.orgId,
      position: taskId - 1,
      childId: taskId,
    });
  }

  return { org, users, tasks, comments, edges };
}

/** 在内存 SQLite 上建表并灌入夹具，返回数据库句柄。 */
export function seedDatabase(db: DatabaseSync): DatabaseSync {
  db.exec(`
    CREATE TABLE Org (
      id INTEGER PRIMARY KEY,
      name TEXT NOT NULL,
      plan TEXT NOT NULL,
      "heavyReport" TEXT NOT NULL
    );
    CREATE TABLE User (
      id INTEGER PRIMARY KEY,
      name TEXT NOT NULL,
      active INTEGER NOT NULL,
      role TEXT NOT NULL,
      "orgId" INTEGER NOT NULL
    );
    CREATE TABLE Task (
      id INTEGER PRIMARY KEY,
      title TEXT NOT NULL,
      body TEXT NOT NULL,
      done INTEGER NOT NULL,
      "orgId" INTEGER NOT NULL
    );
    CREATE TABLE Comment (
      id INTEGER PRIMARY KEY,
      text TEXT NOT NULL
    );
    CREATE TABLE edge (
      relation TEXT NOT NULL,
      parent_id INTEGER NOT NULL,
      position INTEGER NOT NULL,
      child_id INTEGER NOT NULL
    );
  `);

  const data = buildSeed();
  insert(db, 'Org', ['id', 'name', 'plan', 'heavyReport'], data.org);
  for (const r of data.users) insert(db, 'User', ['id', 'name', 'active', 'role', 'orgId'], r);
  for (const r of data.tasks) insert(db, 'Task', ['id', 'title', 'body', 'done', 'orgId'], r);
  for (const r of data.comments) insert(db, 'Comment', ['id', 'text'], r);

  const edgeStmt = db.prepare(
    'INSERT INTO edge (relation, parent_id, position, child_id) VALUES (?, ?, ?, ?)',
  );
  for (const e of data.edges) {
    edgeStmt.run(e.relation, e.parentId, e.position, e.childId);
  }
  return db;
}

function insert(
  db: DatabaseSync,
  table: string,
  columns: string[],
  row: Row,
): void {
  const placeholders = columns.map(() => '?').join(', ');
  const quoted = columns.map((c) => `"${c}"`).join(', ');
  const stmt = db.prepare(
    `INSERT INTO "${table}" (${quoted}) VALUES (${placeholders})`,
  );
  stmt.run(...columns.map((c) => row[c] ?? null));
}
