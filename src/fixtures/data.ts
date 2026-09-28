/**
 * 合成夹具的【原始事实数据】。
 * 这是测试参考答案的独立事实来源：测试用的期望成本由这些数组
 * 直接、独立地计算（见 test/helpers/reference.ts），
 * 不经过 src/core 的估计器或执行器。
 *
 * 刻意让真实基数高于 schema 声明上界，用于复现
 * “静态估计低于实际夹具 → 运行时预算超限取消”。
 */
export interface UserRow { id: number; name: string; age: number; role: string }
export interface PostRow { id: number; user_id: number; title: string }
export interface TagRow { id: number; post_id: number; name: string; weight: number }

/** 12 个用户；schema 中 users 声明上界为 10。 */
export const FIXTURE_USERS: UserRow[] = Array.from({ length: 12 }, (_, i) => ({
  id: i + 1,
  name: `user-${i + 1}`,
  age: 20 + i,
  role: i % 3 === 0 ? 'admin' : 'member',
}));

/** 每个用户 6 篇帖子；posts 声明上界为 5。共 72 篇。 */
export const FIXTURE_POSTS: PostRow[] = FIXTURE_USERS.flatMap((u) =>
  Array.from({ length: 6 }, (_, j) => ({
    id: (u.id - 1) * 6 + j + 1,
    user_id: u.id,
    title: `post-${u.id}-${j + 1}`,
  })),
);

/** 每篇帖子 5 个标签；tags 声明上界为 4。共 360 个。 */
export const FIXTURE_TAGS: TagRow[] = FIXTURE_POSTS.flatMap((p) =>
  Array.from({ length: 5 }, (_, k) => ({
    id: (p.id - 1) * 5 + k + 1,
    post_id: p.id,
    name: `tag-${p.id}-${k + 1}`,
    weight: (k % 3) + 2,
  })),
);

/** 单个用户查询夹具（按 id 命中） */
export function userById(id: number): UserRow | undefined {
  return FIXTURE_USERS.find((u) => u.id === id);
}
