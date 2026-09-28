/**
 * 本地合成 schema（类型契约）。
 *
 * 类型与关系一览：
 *   Org(name×1, plan×2, heavyReport×5)
 *     ├─ members       → User
 *     ├─ orgTasks      → Task
 *     └─ archivedTasks → Task
 *   User(name×1, active bool×1, role×1)
 *     └─ tasks → Task
 *   Task(title×1, body×3, done bool×1, orgId int×1)
 *     ├─ taskComments → Comment
 *     └─ assignee     → User
 *   Comment(text×2)
 *     └─ replies → Comment（自引用，构造“短而深”的链）
 *
 * 片段：
 * - orgSummary / taskCore：普通可复用片段；
 * - loopA / loopB：inline 片段，互相引用形成 A→B→A 循环，
 *   用于验证循环片段在静态展开阶段被拒绝。
 */
import type { Schema } from '../contracts/schema.js';

export const FIXTURE_SCHEMA: Schema = {
  types: {
    Org: {
      fields: {
        name: { type: 'string', multiplier: 1 },
        plan: { type: 'string', multiplier: 2 },
        heavyReport: { type: 'string', multiplier: 5 },
      },
      relations: {
        members: { to: 'User' },
        orgTasks: { to: 'Task' },
        archivedTasks: { to: 'Task' },
      },
    },
    User: {
      fields: {
        name: { type: 'string', multiplier: 1 },
        active: { type: 'bool', multiplier: 1 },
        role: { type: 'string', multiplier: 1 },
        orgId: { type: 'int', multiplier: 1 },
      },
      relations: {
        tasks: { to: 'Task' },
      },
    },
    Task: {
      fields: {
        title: { type: 'string', multiplier: 1 },
        body: { type: 'string', multiplier: 3 },
        done: { type: 'bool', multiplier: 1 },
        orgId: { type: 'int', multiplier: 1 },
      },
      relations: {
        taskComments: { to: 'Comment' },
        assignee: { to: 'User' },
      },
    },
    Comment: {
      fields: {
        text: { type: 'string', multiplier: 2 },
      },
      relations: {
        replies: { to: 'Comment' },
      },
    },
  },
  fragments: {
    orgSummary: {
      name: 'orgSummary',
      fields: [
        { kind: 'field', alias: 'orgName', field: 'name' },
        { kind: 'field', alias: 'orgPlan', field: 'plan' },
      ],
    },
    taskCore: {
      name: 'taskCore',
      fields: [
        { kind: 'field', alias: 'title', field: 'title' },
        { kind: 'field', alias: 'body', field: 'body' },
      ],
    },
    loopA: {
      name: 'loopA',
      inline: true,
      fields: [
        { kind: 'field', alias: 'text', field: 'text' },
        { kind: 'spread', fragment: 'loopB' },
      ],
    },
    loopB: {
      name: 'loopB',
      inline: true,
      fields: [{ kind: 'spread', fragment: 'loopA' }],
    },
  },
};
