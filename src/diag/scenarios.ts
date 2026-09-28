/**
 * 规范验证场景。每个场景给出固定 runId，便于日志重放。
 * 覆盖：短而深列表、重复别名、循环片段、估计低于实际夹具（运行时取消）。
 */
export interface Scenario {
  runId: string;
  title: string;
  query: string;
  variables?: Record<string, unknown>;
  budget: number;
  expectation: string;
}

export const SCENARIOS: Scenario[] = [
  {
    runId: 'S1-deep-list',
    title: '短而深的列表查询：users→posts→tags，预算充足，完整完成',
    query: `{
      users {
        id
        name
        posts {
          id
          title
          tags { name weight }
        }
      }
    }`,
    budget: 1_000_000,
    expectation: 'COMPLETE；实际成本按真实基数 12×6×5 计算',
  },
  {
    runId: 'S2-duplicate-alias',
    title: '重复别名：同键 role 选择两次，两处都计费，预算被第二次消耗触发取消',
    query: `{
      users {
        id
        role
        role
      }
    }`,
    // users(1) + 每个 user: id(1)+role(4)+role(4)=9 ×12 = 109
    // 取一个恰好在中途耗尽的预算：users 1 + 11 个完整用户(99) + 第12个用户 id(1) + 第一个 role(4) = 105
    budget: 105,
    expectation: 'PARTIAL；取消路径落在第二个重复 role（#dup2 语义同键）；重复未绕过预算',
  },
  {
    runId: 'S3-cyclic-fragment',
    title: '循环片段：...LoopA → ...LoopB → ...LoopA，校验期判定 STATE_CONFLICT',
    query: `{
      users {
        ...LoopA
      }
    }`,
    budget: 100_000,
    expectation: 'REJECTED STATE_CONFLICT（VALIDATE 阶段），不执行、不计成本',
  },
  {
    runId: 'S4-estimate-below-actual',
    title: '估计低于实际夹具：声明上界 10/5/4，真实 12/6/5，静态放行、运行时取消',
    query: `{
      users {
        id
        posts {
          id
          tags { name weight }
        }
      }
    }`,
    // 静态估计：tag 元素 name1+weight3=4 → tags 1+4×4=17
    //          → post 元素 id1+tags17=18 → posts 1+5×18=91
    //          → user 元素 id1+posts91=92 → users 1+10×92=921
    // 实际成本（真实基数 12/6/5）= 1609。
    // 预算设在 静态估计之上、实际成本之下，保证静态放行而运行时取消。
    budget: 1200,
    expectation: 'PARTIAL；estimateBelowActual=true；取消沿未完成元素传播并保留前缀',
  },
  {
    runId: 'S5-static-gate',
    title: '静态预算门：预算低于声明上界估计，执行前拒绝',
    query: `{
      users { id name posts { id title } }
    }`,
    // 估计 = 1 + 10×(id1+name1+posts1+5×(id1+title2)) = 1 + 10×18 = 181
    budget: 100,
    expectation: 'REJECTED RESOURCE_EXHAUSTED（STATIC_GATE 阶段）',
  },
  {
    runId: 'S6-typed-variable-bad',
    title: '变量类型错误：$uid 声明 INT 却给字符串，先做变量类型校验',
    query: `query ($uid: INT!) {
      userById(id: $uid) { id name role }
    }`,
    variables: { uid: 'not-an-int' },
    budget: 100,
    expectation: 'REJECTED INPUT_INVALID（VALIDATE 阶段，变量先校验）',
  },
  {
    runId: 'S7-alias-conflict',
    title: '别名状态冲突：同一别名绑定到不同字段',
    query: `{
      users {
        x: id
        x: name
      }
    }`,
    budget: 100,
    expectation: 'REJECTED STATE_CONFLICT',
  },
];
