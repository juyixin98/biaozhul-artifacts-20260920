/**
 * 字段收集：把选择集（含片段展开/内联片段）按响应键(alias ?? name)分组合并。
 * 片段类型条件必须与当前对象类型匹配（本系统不含 interface/union）。
 */

import type {
  FieldNode,
  FragmentDefinitionNode,
  SelectionSetNode,
} from './ast.js';
import { GraphQLError } from './error.js';

export function responseKey(field: FieldNode): string {
  return field.alias ? field.alias.value : field.name.value;
}

export type FragmentMap = Record<string, FragmentDefinitionNode>;

/**
 * 递归收集字段。调用方必须先完成片段循环校验，否则会死循环。
 */
export function collectFields(
  fragments: FragmentMap,
  selectionSet: SelectionSetNode,
  parentTypeName: string,
): Map<string, FieldNode[]> {
  const groups = new Map<string, FieldNode[]>();
  visit(selectionSet);
  return groups;

  function visit(set: SelectionSetNode): void {
    for (const selection of set.selections) {
      if (selection.kind === 'Field') {
        const key = responseKey(selection);
        const list = groups.get(key);
        if (list) {
          list.push(selection);
        } else {
          groups.set(key, [selection]);
        }
      } else if (selection.kind === 'FragmentSpread') {
        const fragment = fragments[selection.name.value];
        if (fragment && fragment.typeCondition.name.value === parentTypeName) {
          visit(fragment.selectionSet);
        }
      } else {
        const condition = selection.typeCondition;
        if (!condition || condition.name.value === parentTypeName) {
          visit(selection.selectionSet);
        }
      }
    }
  }
}

/**
 * 片段循环检测：对片段 spread 图做 WHITE/GRAY/BLACK 深度优先染色。
 * 命中 GRAY 回边时给出完整展开链。
 */
export function detectFragmentCycles(
  fragments: FragmentMap,
): GraphQLError[] {
  const colors = new Map<string, 'WHITE' | 'GRAY' | 'BLACK'>();
  const errors: GraphQLError[] = [];

  for (const name of Object.keys(fragments)) colors.set(name, 'WHITE');

  const visit = (name: string, stack: string[]): void => {
    colors.set(name, 'GRAY');
    const nextStack = [...stack, name];
    for (const spreadName of collectSpreadTargets(fragments[name].selectionSet)) {
      const color = colors.get(spreadName);
      if (color === 'GRAY') {
        const cycleStart = nextStack.indexOf(spreadName);
        const chain = [...nextStack.slice(cycleStart), spreadName];
        errors.push(
          new GraphQLError(
            `Cannot spread fragment "${spreadName}" within itself via ${chain.join(' -> ')}.`,
            {
              category: 'VALIDATION',
              locations: [{ line: fragments[name].loc.line, column: fragments[name].loc.column }],
            },
          ),
        );
      } else if (color === 'WHITE') {
        visit(spreadName, nextStack);
      }
    }
    colors.set(name, 'BLACK');
  };

  for (const name of Object.keys(fragments)) {
    if (colors.get(name) === 'WHITE') visit(name, []);
  }
  return errors;
}

function collectSpreadTargets(set: SelectionSetNode): string[] {
  const names: string[] = [];
  for (const selection of set.selections) {
    if (selection.kind === 'FragmentSpread') {
      names.push(selection.name.value);
    } else if (selection.kind === 'InlineFragment') {
      names.push(...collectSpreadTargets(selection.selectionSet));
    } else if (selection.selectionSet) {
      names.push(...collectSpreadTargets(selection.selectionSet));
    }
  }
  return names;
}
