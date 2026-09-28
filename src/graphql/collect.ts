/**
 * 字段收集：把（可能跨多个片段/内联片段的）选择集合按响应键合并。
 * 校验与执行共用同一份展开逻辑，保证"按什么规则判定冲突"与
 * "按什么规则执行"不发生漂移。
 *
 * 受限子集没有接口/联合，类型条件仅做对象类型相等判定。
 */
import type {
  DirectiveNode,
  FieldNode,
  FragmentDefinitionNode,
  SelectionSetNode,
} from './ast.js';
import { GraphQLError } from './errors.js';
import type { ObjectType } from './types.js';

/**
 * 指令求值。
 * - 执行期：变量已强制，if 可以是布尔变量；
 * - 静态校验期：变量取值未知，保守视为 true（两个分支都参与冲突检查）。
 */
export function shouldIncludeNode(
  directives: readonly DirectiveNode[],
  variables: Readonly<Record<string, unknown>> | 'static',
): boolean {
  let skip = false;
  let include = true;
  for (const directive of directives) {
    if (directive.name.value !== 'skip' && directive.name.value !== 'include') continue;
    const ifArg = directive.arguments.find((a) => a.name.value === 'if');
    if (!ifArg) {
      throw new GraphQLError(
        `Directive "@${directive.name.value}" requires an "if" argument`,
        'DIRECTIVE',
        { locations: directive.name.loc },
      );
    }
    const valueNode = ifArg.value;
    let value: boolean;
    if (valueNode.kind === 'BooleanValue') {
      value = valueNode.value;
    } else if (valueNode.kind === 'Variable') {
      if (variables === 'static') {
        value = true;
      } else {
        const raw = variables[valueNode.name.value];
        value = raw === undefined ? true : raw === true;
      }
    } else {
      throw new GraphQLError(
        `Argument "if" of "@${directive.name.value}" must be a Boolean`,
        'DIRECTIVE',
        { locations: directive.name.loc },
      );
    }
    if (directive.name.value === 'skip') skip = value;
    else include = value;
  }
  return !skip && include;
}

export interface CollectedField {
  /** 合并组中第一个字段：解析器调用以它为准（同名同实参已在校验期保证） */
  node: FieldNode;
  /** 合并组内所有字段节点（locations 取点用） */
  nodes: FieldNode[];
  /** 各同键字段子选择集的并集，供下一层递归 */
  mergedSelectionSet: SelectionSetNode | null;
}

export interface CollectOptions {
  fragments: ReadonlyMap<string, FragmentDefinitionNode>;
  variables: Readonly<Record<string, unknown>> | 'static';
}

function typeConditionApplies(parentType: ObjectType, typeConditionName: string): boolean {
  return parentType.name === typeConditionName;
}

/**
 * 收集响应键 -> 合并字段。同一响应键的多个字段被合并为一组，
 * 其选择集合取并集——这正是 GraphQL "字段按响应键合并" 的执行语义。
 */
export function collectFields(
  parentType: ObjectType,
  selectionSet: SelectionSetNode,
  options: CollectOptions,
): Map<string, CollectedField> {
  const groups = new Map<string, FieldNode[]>();
  const visitedFragments = new Set<string>();

  const walk = (set: SelectionSetNode, inheritedCondition: boolean): void => {
    for (const selection of set.selections) {
      if (selection.kind === 'Field') {
        const included =
          inheritedCondition && shouldIncludeNode(selection.directives, options.variables);
        if (!included) continue;
        const key = responseKey(selection);
        const group = groups.get(key);
        if (group) group.push(selection);
        else groups.set(key, [selection]);
        continue;
      }

      if (selection.kind === 'InlineFragment') {
        if (!shouldIncludeNode(selection.directives, options.variables)) continue;
        const conditionApplies =
          !selection.typeCondition ||
          typeConditionApplies(parentType, selection.typeCondition.value);
        if (!conditionApplies) continue;
        walk(selection.selectionSet, inheritedCondition);
        continue;
      }

      // FragmentSpread
      if (!shouldIncludeNode(selection.directives, options.variables)) continue;
      if (visitedFragments.has(selection.name.value)) continue;
      visitedFragments.add(selection.name.value);
      const fragment = options.fragments.get(selection.name.value);
      // 未定义片段由校验期负责报错；执行期直接跳过该展开
      if (!fragment) continue;
      if (!typeConditionApplies(parentType, fragment.typeCondition.value)) continue;
      walk(fragment.selectionSet, inheritedCondition);
    }
  };

  walk(selectionSet, true);

  const result = new Map<string, CollectedField>();
  for (const [key, nodes] of groups) {
    result.set(key, {
      node: nodes[0]!,
      nodes,
      mergedSelectionSet: mergeSelectionSets(nodes),
    });
  }
  return result;
}

export function responseKey(field: FieldNode): string {
  return field.alias ? field.alias.value : field.name.value;
}

function mergeSelectionSets(nodes: FieldNode[]): SelectionSetNode | null {
  const selections = [];
  for (const node of nodes) {
    if (node.selectionSet) selections.push(...node.selectionSet.selections);
  }
  if (selections.length === 0) return null;
  return { kind: 'SelectionSet', selections };
}
