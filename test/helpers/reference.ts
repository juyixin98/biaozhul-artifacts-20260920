/**
 * 独立参考答案助手。
 *
 * 这些期望值【不】来自 src/core 的估计器/执行器/解析器：
 *  - 基数直接读原始夹具数组的长度与过滤结果；
 *  - 倍率在本文件内按 schema 声明独立硬编码；
 *  - 期望数据形状由夹具内容直接构造。
 * 若被测核心与本助手同时出错且错误一致，只可能是同一处声明被抄错，
 * 因此数字在测试中也以字面量形式显式断言。
 */
import { FIXTURE_POSTS, FIXTURE_TAGS, FIXTURE_USERS } from '../../src/fixtures/data.js';

// 独立硬编码的字段倍率（与 src/fixtures/schemaDef.ts 中的声明对应）
export const REF_MULT = {
  users: 1,
  userById: 2,
  user: { id: 1, name: 1, age: 1, role: 4, posts: 1 },
  post: { id: 1, title: 2, tags: 1 },
  tag: { name: 1, weight: 3 },
} as const;

// 独立硬编码的声明上界
export const REF_BOUND = { users: 10, posts: 5, tags: 4 } as const;

export const REF_ACTUAL = { users: FIXTURE_USERS.length, postsPerUser: 6, tagsPerPost: 5 } as const;

const postsOf = (userId: number) => FIXTURE_POSTS.filter((p) => p.user_id === userId);
const tagsOf = (postId: number) => FIXTURE_TAGS.filter((t) => t.post_id === postId);

// ---- 场景 S1：users{id name posts{id title tags{name weight}}} ----
export function s1ActualCost(): number {
  const tagCost = REF_MULT.tag.name + REF_MULT.tag.weight; // 4
  const postCost = REF_MULT.post.id + REF_MULT.post.title + REF_MULT.post.tags + REF_ACTUAL.tagsPerPost * tagCost;
  const userCost = REF_MULT.user.id + REF_MULT.user.name + REF_MULT.user.posts + REF_ACTUAL.postsPerUser * postCost;
  return REF_MULT.users + REF_ACTUAL.users * userCost;
}
export const S1_ESTIMATED = (() => {
  const tagCost = REF_MULT.tag.name + REF_MULT.tag.weight;
  const postCost = REF_MULT.post.id + REF_MULT.post.title + REF_MULT.post.tags + REF_BOUND.tags * tagCost;
  const userCost = REF_MULT.user.id + REF_MULT.user.name + REF_MULT.user.posts + REF_BOUND.posts * postCost;
  return REF_MULT.users + REF_BOUND.users * userCost;
})();

// ---- 场景 S2：users{id role role} ----
export const S2_USER_COST = REF_MULT.user.id + 2 * REF_MULT.user.role; // 9
export const S2_ESTIMATED = REF_MULT.users + REF_BOUND.users * S2_USER_COST; // 91
export const S2_ACTUAL = REF_MULT.users + REF_ACTUAL.users * S2_USER_COST; // 109

// ---- 场景 S4：users{id posts{id tags{name weight}}} ----
export function s4ActualCost(): number {
  const tagCost = REF_MULT.tag.name + REF_MULT.tag.weight; // 4
  const postCost = REF_MULT.post.id + REF_MULT.post.tags + REF_ACTUAL.tagsPerPost * tagCost; // 22
  const userCost = REF_MULT.user.id + REF_MULT.user.posts + REF_ACTUAL.postsPerUser * postCost; // 134
  return REF_MULT.users + REF_ACTUAL.users * userCost; // 1609
}
export const S4_ESTIMATED = (() => {
  const tagCost = REF_MULT.tag.name + REF_MULT.tag.weight;
  const postCost = REF_MULT.post.id + REF_MULT.post.tags + REF_BOUND.tags * tagCost; // 18
  const userCost = REF_MULT.user.id + REF_MULT.user.posts + REF_BOUND.posts * postCost; // 92
  return REF_MULT.users + REF_BOUND.users * userCost; // 921
})();

// ---- 场景 S5：users{id name posts{id title}} ----
export const S5_ESTIMATED = (() => {
  const postCost = REF_MULT.post.id + REF_MULT.post.title; // 3
  const userCost = REF_MULT.user.id + REF_MULT.user.name + REF_MULT.user.posts + REF_BOUND.posts * postCost; // 18
  return REF_MULT.users + REF_BOUND.users * userCost; // 181
})();

// ---- 双展开片段 ...UserCore ...UserCore ----
export const USERCORE_COST = REF_MULT.user.id + REF_MULT.user.name + REF_MULT.user.role; // 6
export const FRAG2_ESTIMATED = REF_MULT.users + REF_BOUND.users * (2 * USERCORE_COST); // 121
export const FRAG2_ACTUAL = REF_MULT.users + REF_ACTUAL.users * (2 * USERCORE_COST); // 145

/** 独立构造 S1 第一行数据期望值，不经过执行器 */
export function expectedFirstUserS1() {
  const u = FIXTURE_USERS[0]!;
  return {
    id: u.id,
    name: u.name,
    posts: postsOf(u.id).map((p) => ({
      id: p.id,
      title: p.title,
      tags: tagsOf(p.id).map((t) => ({ name: t.name, weight: t.weight })),
    })),
  };
}

export function expectedCounts() {
  return {
    users: FIXTURE_USERS.length,
    postsOfFirstUser: postsOf(FIXTURE_USERS[0]!.id).length,
    tagsOfFirstPost: tagsOf(FIXTURE_POSTS[0]!.id).length,
  };
}
