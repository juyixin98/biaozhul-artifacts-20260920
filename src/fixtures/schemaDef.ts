/**
 * 类型化模型契约。字段倍率与列表声明上界是成本契约的一部分：
 *
 *   users  声明上界 10（夹具实际 12）
 *   posts  声明上界 5 （夹具实际 6）
 *   tags   声明上界 4 （夹具实际 5）
 */
import { buildSchema } from '../schema/schema.js';

export function appSchema() {
  return buildSchema({
    queryType: 'Query',
    types: [
      {
        name: 'Query',
        fields: [
          { name: 'users', type: 'User', list: true, declaredUpperBound: 10, multiplier: 1 },
          { name: 'userById', type: 'User', list: false, declaredUpperBound: 0, multiplier: 2 },
        ],
      },
      {
        name: 'User',
        fields: [
          { name: 'id', type: 'ID', list: false, declaredUpperBound: 0, multiplier: 1 },
          { name: 'name', type: 'STRING', list: false, declaredUpperBound: 0, multiplier: 1 },
          { name: 'age', type: 'INT', list: false, declaredUpperBound: 0, multiplier: 1 },
          { name: 'role', type: 'STRING', list: false, declaredUpperBound: 0, multiplier: 4 },
          { name: 'posts', type: 'Post', list: true, declaredUpperBound: 5, multiplier: 1 },
        ],
      },
      {
        name: 'Post',
        fields: [
          { name: 'id', type: 'ID', list: false, declaredUpperBound: 0, multiplier: 1 },
          { name: 'title', type: 'STRING', list: false, declaredUpperBound: 0, multiplier: 2 },
          { name: 'tags', type: 'Tag', list: true, declaredUpperBound: 4, multiplier: 1 },
        ],
      },
      {
        name: 'Tag',
        fields: [
          { name: 'name', type: 'STRING', list: false, declaredUpperBound: 0, multiplier: 1 },
          { name: 'weight', type: 'INT', list: false, declaredUpperBound: 0, multiplier: 3 },
        ],
      },
    ],
    fragments: {
      UserCore: {
        name: 'UserCore',
        onType: 'User',
        fields: [
          { kind: 'field', name: 'id', alias: 'id', args: {}, selections: [] },
          { kind: 'field', name: 'name', alias: 'name', args: {}, selections: [] },
          { kind: 'field', name: 'role', alias: 'role', args: {}, selections: [] },
        ],
      },
      // 互为循环的片段声明：只有查询实际展开它们时才构成 STATE_CONFLICT。
      LoopA: {
        name: 'LoopA',
        onType: 'User',
        fields: [
          { kind: 'field', name: 'id', alias: 'id', args: {}, selections: [] },
          { kind: 'fragmentSpread', name: 'LoopB' },
        ],
      },
      LoopB: {
        name: 'LoopB',
        onType: 'User',
        fields: [
          { kind: 'field', name: 'name', alias: 'name', args: {}, selections: [] },
          { kind: 'fragmentSpread', name: 'LoopA' },
        ],
      },
    },
  });
}
