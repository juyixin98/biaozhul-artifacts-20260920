/**
 * 片段契约解析。
 *
 * 关键语义（题目要求“片段重复不能绕过预算”）：
 * - spread 按“出现次数”展开为独立的字段子树，不记忆化、不去重；
 * - query.fragments 中的顶层 include 同理，重复名重复展开；
 * - 展开是静态的、深度受限的；A -> B -> A 这类循环在展开过程中
 *   被显式检测并报 FRAGMENT_CYCLE（INPUT_ERROR），而不是栈溢出；
 * - inline 片段只能被内联引用（测试夹具用它构造循环），顶层
 *   include 一个 inline 片段属于 INLINE_FRAGMENT_FORBIDDEN。
 *
 * 输出是“展开后”的 FieldNode[]（不含任何 spread 节点），供类型
 * 校验、成本估计与执行共用，保证三者看到完全一致的选择集。
 */
import type { FieldNode, FragmentDef } from '../contracts/ast.js';
import type { Schema } from '../contracts/schema.js';
import { fail } from '../contracts/errors.js';

/** 循环保护上界：即便没有环，异常深的展开也在此停止（输入错误）。 */
export const MAX_EXPANSION_DEPTH = 64;

export function expandFragments(
  fields: FieldNode[],
  schema: Schema,
  /** 当前展开栈：片段名 -> 它在栈中的位置，用于报告环。 */
  stack: string[] = [],
  depth = 0,
): FieldNode[] {
  if (depth > MAX_EXPANSION_DEPTH) {
    fail(
      'INPUT_ERROR',
      'FRAGMENT_CYCLE',
      `fragment expansion exceeded depth ${MAX_EXPANSION_DEPTH}; likely a cycle`,
      stack,
    );
  }
  const out: FieldNode[] = [];
  for (const node of fields) {
    if (node.kind !== 'spread') {
      if (node.kind === 'list') {
        out.push({
          ...node,
          children: expandFragments(node.children, schema, stack, depth + 1),
        });
      } else {
        out.push(node);
      }
      continue;
    }
    const frag: FragmentDef | undefined = schema.fragments[node.fragment];
    if (!frag) {
      fail('INPUT_ERROR', 'UNKNOWN_FRAGMENT', `unknown fragment: ${node.fragment}`, stack, {
        fragment: node.fragment,
      });
    }
    if (stack.includes(frag.name)) {
      const cycle = [...stack.slice(stack.indexOf(frag.name)), frag.name];
      fail(
        'INPUT_ERROR',
        'FRAGMENT_CYCLE',
        `cyclic fragment reference: ${cycle.join(' -> ')}`,
        cycle,
        { cycle },
      );
    }
    // 每次 spread 产生一份新的展开（重复出现 => 重复字段 => 重复计费）。
    const expanded = expandFragments(frag.fields, schema, [...stack, frag.name], depth + 1);
    out.push(...expanded);
  }
  return out;
}

/** 展开 query.fragments 中的顶层 include；inline 片段禁止这样引用。 */
export function expandTopLevelIncludes(
  names: string[],
  schema: Schema,
): FieldNode[] {
  const out: FieldNode[] = [];
  const seenStack: string[] = [];
  for (const name of names) {
    const frag = schema.fragments[name];
    if (!frag) {
      fail('INPUT_ERROR', 'UNKNOWN_FRAGMENT', `unknown fragment in query.fragments: ${name}`, [
        name,
      ]);
    }
    if (frag.inline) {
      fail(
        'INPUT_ERROR',
        'INLINE_FRAGMENT_FORBIDDEN',
        `fragment "${name}" is inline-only and cannot be included at top level`,
        [name],
      );
    }
    // 顶层 include 之间允许重复（重复计费），但单个 include 自身若成环仍报错。
    const expanded = expandFragments(
      frag.fields.map((f) => ({ ...f })),
      schema,
      [...seenStack, frag.name],
      1,
    );
    out.push(...expanded);
  }
  return out;
}
