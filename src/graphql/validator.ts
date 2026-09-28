/**
 * 执行前校验。任何一项失败都拒绝执行（不产生部分数据），
 * 返回结构化错误列表，错误类别见 errors.ts。
 *
 * 覆盖：
 * - 片段：重名、目标不存在、类型条件必须是对象、展开目标存在、片段循环（含间接环）
 * - 操作：匿名操作唯一性、重名、变量定义重名与输入类型
 * - 字段：存在性、叶/对象选择集规则、未知/必填参数、字面量类型
 * - 变量：使用必先定义、skip/include 的 if 必须是 Boolean、变量类型与位置兼容
 * - 指令：仅允许 @skip/@include，参数必须为 if: Boolean，且不可重复
 * - 合并：同响应键字段按 GraphQL FieldsInSetCanMerge 规则判定兼容
 */
import type {
  DocumentNode,
  FieldNode,
  FragmentDefinitionNode,
  OperationDefinitionNode,
  SelectionSetNode,
  TypeNode,
  ValueNode,
  VariableDefinitionNode,
} from './ast.js';
import { collectFields, responseKey } from './collect.js';
import { GraphQLError } from './errors.js';
import type { SchemaTypeView } from './schema.js';
import { isObjectType } from './types.js';
import type { FieldDef, ObjectType, TypeRef } from './types.js';
import { typeNodeToRef } from './coercion.js';
import { typeRefToString } from './types.js';

const ALLOWED_DIRECTIVES = new Set(['skip', 'include']);

export function validateDocument(
  schema: SchemaTypeView,
  document: DocumentNode,
): GraphQLError[] {
  const errors: GraphQLError[] = [];

  const fragments = new Map<string, FragmentDefinitionNode>();
  const operations: OperationDefinitionNode[] = [];
  for (const def of document.definitions) {
    if (def.kind === 'FragmentDefinition') fragments.set(def.name.value, def);
    else operations.push(def);
  }

  validateOperationList(operations, errors);
  validateFragmentsShape(schema, fragments, errors);
  for (const operation of operations) validateFragmentTargets(operation.selectionSet, fragments, errors);
  for (const fragment of fragments.values()) {
    validateFragmentTargets(fragment.selectionSet, fragments, errors);
  }
  detectFragmentCycles(fragments, errors);

  for (const operation of operations) {
    const variableDefs = validateVariableDefinitions(schema, operation, errors);
    const rootType = operation.operation === 'query' ? schema.queryType : schema.mutationType;
    validateSelectionSet(
      schema,
      rootType,
      operation.selectionSet,
      fragments,
      variableDefs,
      new Set(),
      errors,
    );
    findValidationConflicts(schema, rootType, operation.selectionSet, fragments, errors);
  }

  // 片段内的字段/参数同样要校验（即便未被使用——与 GraphQL 规范一致）
  for (const fragment of fragments.values()) {
    const type = schema.types.get(fragment.typeCondition.value);
    if (!type || !isObjectType(type)) continue; // 形状阶段已报错
    validateSelectionSet(schema, type, fragment.selectionSet, fragments, new Map(), new Set(), errors);
    findValidationConflicts(schema, type, fragment.selectionSet, fragments, errors);
  }

  return errors;
}

// ---- 操作列表 ----

function validateOperationList(
  operations: OperationDefinitionNode[],
  errors: GraphQLError[],
): void {
  if (operations.length === 0) {
    errors.push(new GraphQLError('Document must contain at least one operation', 'VALIDATION'));
  }
  const names = new Map<string, number>();
  let anonymous = 0;
  for (const op of operations) {
    if (op.name === null) {
      anonymous += 1;
    } else {
      names.set(op.name.value, (names.get(op.name.value) ?? 0) + 1);
    }
    for (const directive of op.directives) {
      errors.push(
        new GraphQLError(
          `Directive "@${directive.name.value}" is not allowed on operations in this subset`,
          'DIRECTIVE',
          { locations: directive.name.loc },
        ),
      );
    }
  }
  if (anonymous > 0 && operations.length > 1) {
    errors.push(
      new GraphQLError(
        'This anonymous operation must be the only defined operation',
        'AMBIGUOUS_OPERATION',
      ),
    );
  }
  for (const [name, count] of names) {
    if (count > 1) {
      errors.push(new GraphQLError(`Operation name "${name}" is defined multiple times`, 'VALIDATION'));
    }
  }
}

// ---- 片段 ----

function validateFragmentsShape(
  schema: SchemaTypeView,
  fragments: Map<string, FragmentDefinitionNode>,
  errors: GraphQLError[],
): void {
  const seen = new Set<string>();
  for (const fragment of fragments.values()) {
    if (seen.has(fragment.name.value)) {
      errors.push(
        new GraphQLError(`Fragment "${fragment.name.value}" is defined more than once`, 'VALIDATION', {
          locations: fragment.name.loc,
        }),
      );
    }
    seen.add(fragment.name.value);
    for (const directive of fragment.directives) {
      if (directive.name.value !== 'skip' && directive.name.value !== 'include') {
        errors.push(
          new GraphQLError(
            `Directive "@${directive.name.value}" is not supported; only @skip and @include are available`,
            'DIRECTIVE',
            { locations: directive.name.loc },
          ),
        );
      }
    }
    const type = schema.types.get(fragment.typeCondition.value);
    if (!type) {
      errors.push(
        new GraphQLError(
          `Fragment "${fragment.name.value}" conditions on unknown type "${fragment.typeCondition.value}"`,
          'VALIDATION',
          { locations: fragment.typeCondition.loc },
        ),
      );
    } else if (!isObjectType(type)) {
      errors.push(
        new GraphQLError(
          `Fragment "${fragment.name.value}" must condition on an object type, "${fragment.typeCondition.value}" is not one`,
          'VALIDATION',
          { locations: fragment.typeCondition.loc },
        ),
      );
    }
  }
}

function validateFragmentTargets(
  set: SelectionSetNode,
  fragments: Map<string, FragmentDefinitionNode>,
  errors: GraphQLError[],
): void {
  const check = (selectionSet: SelectionSetNode): void => {
    for (const selection of selectionSet.selections) {
      if (selection.kind === 'FragmentSpread') {
        if (!fragments.has(selection.name.value)) {
          errors.push(
            new GraphQLError(
              `Unknown fragment "${selection.name.value}"`,
              'FRAGMENT_NOT_FOUND',
              { locations: selection.name.loc },
            ),
          );
        }
      } else if (selection.kind === 'InlineFragment') {
        check(selection.selectionSet);
      } else if (selection.selectionSet) {
        check(selection.selectionSet);
      }
    }
  };
  check(set);
}

/** 经典三色 DFS：在片段展开图上找后向边并报告环路径 */
function detectFragmentCycles(
  fragments: Map<string, FragmentDefinitionNode>,
  errors: GraphQLError[],
): void {
  const color = new Map<string, 0 | 1 | 2>(); // 0 白 1 灰 2 黑
  const stack: string[] = [];

  const spreads = (name: string): string[] => {
    const fragment = fragments.get(name);
    if (!fragment) return [];
    const names: string[] = [];
    const walk = (set: SelectionSetNode): void => {
      for (const selection of set.selections) {
        if (selection.kind === 'FragmentSpread') names.push(selection.name.value);
        else if (selection.kind === 'InlineFragment') walk(selection.selectionSet);
        else if (selection.selectionSet) walk(selection.selectionSet);
      }
    };
    walk(fragment.selectionSet);
    return names;
  };

  const visit = (name: string): void => {
    color.set(name, 1);
    stack.push(name);
    for (const next of spreads(name)) {
      if (!fragments.has(next)) continue; // 未定义已在别处报错
      const nextColor = color.get(next) ?? 0;
      if (nextColor === 1) {
        const cycleStart = stack.indexOf(next);
        const cycle = [...stack.slice(cycleStart), next];
        errors.push(
          new GraphQLError(
            `Cannot spread fragment "${next}" within itself: ${cycle.join(' -> ')}`,
            'FRAGMENT_CYCLE',
            { locations: fragments.get(next)!.name.loc },
          ),
        );
      } else if (nextColor === 0) {
        visit(next);
      }
    }
    stack.pop();
    color.set(name, 2);
  };

  for (const name of fragments.keys()) {
    if ((color.get(name) ?? 0) === 0) visit(name);
  }
}

// ---- 变量定义 ----

function validateVariableDefinitions(
  schema: SchemaTypeView,
  operation: OperationDefinitionNode,
  errors: GraphQLError[],
): Map<string, VariableDefinitionNode> {
  const defs = new Map<string, VariableDefinitionNode>();
  for (const def of operation.variableDefinitions) {
    const varName = def.variable.name.value;
    if (defs.has(varName)) {
      errors.push(
        new GraphQLError(`Variable "$${varName}" is defined more than once`, 'VARIABLE_TYPE', {
          locations: def.variable.loc,
        }),
      );
      continue;
    }
    validateTypeIsInput(schema, def.type, errors);
    if (def.defaultValue) {
      validateLiteralNode(schema, def.defaultValue, typeNodeToRef(def.type), errors);
    }
    defs.set(varName, def);
  }
  return defs;
}

function validateTypeIsInput(
  schema: SchemaTypeView,
  node: TypeNode,
  errors: GraphQLError[],
): void {
  if (node.kind === 'NonNullType') {
    validateTypeIsInput(schema, node.type, errors);
    return;
  }
  if (node.kind === 'ListType') {
    validateTypeIsInput(schema, node.type, errors);
    return;
  }
  const type = schema.types.get(node.name.value);
  if (!type) {
    errors.push(
      new GraphQLError(`Unknown type "${node.name.value}" in variable definition`, 'VALIDATION', {
        locations: node.name.loc,
      }),
    );
  } else if (type.kind === 'OBJECT') {
    errors.push(
      new GraphQLError(
        `Variable type "${node.name.value}" must be an input type (scalar/enum/list)`,
        'VARIABLE_TYPE',
        { locations: node.name.loc },
      ),
    );
  }
}

// ---- 选择集递归校验 ----

function validateSelectionSet(
  schema: SchemaTypeView,
  parentType: ObjectType,
  selectionSet: SelectionSetNode,
  fragments: Map<string, FragmentDefinitionNode>,
  variableDefs: Map<string, VariableDefinitionNode>,
  visitedFragments: Set<string>,
  errors: GraphQLError[],
): void {
  if (selectionSet.selections.length === 0) {
    errors.push(
      new GraphQLError(
        `Selection set on "${parentType.name}" must have at least one field`,
        'VALIDATION',
      ),
    );
  }

  for (const selection of selectionSet.selections) {
    if (selection.kind === 'Field') {
      validateField(schema, parentType, selection, variableDefs, fragments, errors);
      continue;
    }
    validateDirectiveList(selection.directives, errors);

    if (selection.kind === 'FragmentSpread') {
      const fragment = fragments.get(selection.name.value);
      if (!fragment) continue;
      if (visitedFragments.has(fragment.name.value)) continue; // 环已由专门阶段报告
      const nextVisited = new Set(visitedFragments).add(fragment.name.value);
      if (fragment.typeCondition.value === parentType.name) {
        validateSelectionSet(
          schema,
          parentType,
          fragment.selectionSet,
          fragments,
          variableDefs,
          nextVisited,
          errors,
        );
      }
      continue;
    }

    // InlineFragment
    if (selection.typeCondition && selection.typeCondition.value !== parentType.name) {
      // 受限子集无抽象类型，类型条件不匹配的分支在该父类型下不适用
      const target = schema.types.get(selection.typeCondition.value);
      if (!target || !isObjectType(target)) {
        errors.push(
          new GraphQLError(
            `Inline fragment conditions on unknown type "${selection.typeCondition.value}"`,
            'VALIDATION',
            { locations: selection.typeCondition.loc },
          ),
        );
      }
      continue;
    }
    validateSelectionSet(
      schema,
      parentType,
      selection.selectionSet,
      fragments,
      variableDefs,
      new Set(visitedFragments),
      errors,
    );
  }
}

function validateField(
  schema: SchemaTypeView,
  parentType: ObjectType,
  field: FieldNode,
  variableDefs: Map<string, VariableDefinitionNode>,
  fragments: Map<string, FragmentDefinitionNode>,
  errors: GraphQLError[],
): void {
  validateDirectiveList(field.directives, errors);

  const fieldDef = parentType.fields.get(field.name.value);
  if (!fieldDef) {
    errors.push(
      new GraphQLError(
        `Cannot query field "${field.name.value}" on type "${parentType.name}"`,
        'FIELD_NOT_FOUND',
        { locations: field.name.loc },
      ),
    );
    return;
  }

  validateArguments(schema, field, fieldDef, variableDefs, errors);

  const baseType = namedTargetType(schema, fieldDef.type);
  const isLeaf = baseType.kind === 'SCALAR' || baseType.kind === 'ENUM';

  if (field.selectionSet) {
    if (isLeaf) {
      errors.push(
        new GraphQLError(
          `Field "${field.name.value}" of leaf type "${typeRefToString(fieldDef.type)}" must not have a selection set`,
          'VALIDATION',
          { locations: field.name.loc },
        ),
      );
      return;
    }
    validateSelectionSet(
      schema,
      baseType,
      field.selectionSet,
      fragments,
      variableDefs,
      new Set(),
      errors,
    );
  } else if (!isLeaf) {
    errors.push(
      new GraphQLError(
        `Field "${field.name.value}" of type "${typeRefToString(fieldDef.type)}" must have a selection set`,
        'VALIDATION',
        { locations: field.name.loc },
      ),
    );
  }
}

function namedTargetType(schema: SchemaTypeView, ref: TypeRef) {
  let current: TypeRef = ref;
  while (current.kind === 'LIST') current = current.ofType;
  const named = schema.types.get(current.name);
  if (!named) {
    throw new GraphQLError(`Unknown type "${current.name}" referenced by schema`, 'INTERNAL');
  }
  return named;
}

// ---- 参数 ----

function validateArguments(
  schema: SchemaTypeView,
  field: FieldNode,
  fieldDef: FieldDef,
  variableDefs: Map<string, VariableDefinitionNode>,
  errors: GraphQLError[],
): void {
  const provided = new Set<string>();
  for (const argument of field.arguments) {
    const argDef = fieldDef.args.get(argument.name.value);
    if (!argDef) {
      errors.push(
        new GraphQLError(
          `Unknown argument "${argument.name.value}" on field "${field.name.value}"`,
          'ARGUMENT',
          { locations: argument.name.loc },
        ),
      );
      continue;
    }
    provided.add(argument.name.value);

    if (argument.value.kind === 'Variable') {
      const varName = argument.value.name.value;
      const varDef = variableDefs.get(varName);
      if (!varDef) {
        errors.push(
          new GraphQLError(
            `Variable "$${varName}" is not defined by operation`,
            'VARIABLE_TYPE',
            { locations: argument.value.loc },
          ),
        );
      } else {
        const varRef = typeNodeToRef(varDef.type);
        if (!typesCompatibleForVariable(argDef.type, varRef, varDef.defaultValue !== null)) {
          errors.push(
            new GraphQLError(
              `Variable "$${varName}" of type "${typeRefToString(varRef)}" is not compatible with argument "${argDef.name}: ${typeRefToString(argDef.type)}"`,
              'VARIABLE_TYPE',
              { locations: argument.value.loc },
            ),
          );
        }
        if (argument.name.value === 'if' && (varRef.kind !== 'NAMED' || varRef.name !== 'Boolean')) {
          errors.push(
            new GraphQLError(
              `Variable "$${varName}" used in "if" must be of type Boolean`,
              'VARIABLE_TYPE',
              { locations: argument.value.loc },
            ),
          );
        }
      }
    } else {
      validateLiteralNode(schema, argument.value, argDef.type, errors);
      if (argument.value.kind === 'NullValue' && argDef.type.nonNull) {
        errors.push(
          new GraphQLError(
            `Argument "${argDef.name}" of non-null type "${typeRefToString(argDef.type)}" cannot be null`,
            'ARGUMENT',
            { locations: argument.name.loc },
          ),
        );
      }
    }
  }

  for (const argDef of fieldDef.args.values()) {
    if (!provided.has(argDef.name) && argDef.defaultValue === undefined && argDef.type.nonNull) {
      errors.push(
        new GraphQLError(
          `Field "${field.name.value}" argument "${argDef.name}" of required type "${typeRefToString(argDef.type)}" was not provided`,
          'ARGUMENT',
          { locations: field.name.loc },
        ),
      );
    }
  }
}

/**
 * 变量类型与参数位置的兼容性（规范 "AreTypesCompatible" 的简化版）：
 * 位置非空、变量可空时，仅当参数有默认值才兼容；
 * 列表递归；命名类型名必须相同（含枚举/标量区分）。
 */
function typesCompatibleForVariable(location: TypeRef, variable: TypeRef, hasDefault: boolean): boolean {
  if (location.kind === 'NAMED' && variable.kind === 'NAMED') {
    if (location.name !== variable.name) return false;
    if (location.nonNull && !variable.nonNull && !hasDefault) return false;
    return true;
  }
  if (location.kind === 'LIST' && variable.kind === 'LIST') {
    if (location.nonNull && !variable.nonNull && !hasDefault) return false;
    return typesCompatibleForVariable(location.ofType, variable.ofType, hasDefault);
  }
  return false;
}

function validateLiteralNode(
  schema: SchemaTypeView,
  node: ValueNode,
  ref: TypeRef,
  errors: GraphQLError[],
): void {
  if (node.kind === 'NullValue') return; // 非空检查由调用方处理
  if (node.kind === 'Variable') return;

  if (ref.kind === 'LIST') {
    if (node.kind !== 'ListValue') {
      errors.push(
        new GraphQLError(`Expected list value for type "${typeRefToString(ref)}"`, 'ARGUMENT'),
      );
      return;
    }
    for (const item of node.values) validateLiteralNode(schema, item, ref.ofType, errors);
    return;
  }

  const type = schema.types.get(ref.name);
  switch (node.kind) {
    case 'IntValue':
      if (ref.name !== 'Int' && ref.name !== 'ID') {
        errors.push(
          new GraphQLError(`Int cannot be used for "${ref.name}"`, 'ARGUMENT'),
        );
      }
      break;
    case 'StringValue':
      if (type?.kind === 'ENUM' || (ref.name !== 'String' && ref.name !== 'ID' && ref.name !== 'DateTime')) {
        errors.push(
          new GraphQLError(`String cannot be used for "${ref.name}"`, 'ARGUMENT'),
        );
      }
      break;
    case 'BooleanValue':
      if (ref.name !== 'Boolean') {
        errors.push(new GraphQLError(`Boolean cannot be used for "${ref.name}"`, 'ARGUMENT'));
      }
      break;
    case 'EnumValue':
      if (type?.kind !== 'ENUM' || !type.values.has(node.value)) {
        errors.push(
          new GraphQLError(
            `Enum value "${node.value}" is not valid for "${ref.name}"`,
            'ARGUMENT',
          ),
        );
      }
      break;
    case 'ListValue':
      errors.push(new GraphQLError(`List value used for scalar type "${ref.name}"`, 'ARGUMENT'));
      break;
    case 'ObjectValue':
      errors.push(
        new GraphQLError('Input object values are not supported in this subset', 'ARGUMENT'),
      );
      break;
  }
}

// ---- 指令 ----

function validateDirectiveList(
  directives: ReadonlyArray<{ name: { value: string; loc: import('./ast.js').Location }; arguments: import('./ast.js').ArgumentNode[] }>,
  errors: GraphQLError[],
): void {
  const seen = new Set<string>();
  for (const directive of directives) {
    if (!ALLOWED_DIRECTIVES.has(directive.name.value)) {
      errors.push(
        new GraphQLError(
          `Directive "@${directive.name.value}" is not supported; only @skip and @include are available`,
          'DIRECTIVE',
          { locations: directive.name.loc },
        ),
      );
      continue;
    }
    if (seen.has(directive.name.value)) {
      errors.push(
        new GraphQLError(`Directive "@${directive.name.value}" may only be used once`, 'DIRECTIVE', {
          locations: directive.name.loc,
        }),
      );
    }
    seen.add(directive.name.value);
    const ifArg = directive.arguments.find((a) => a.name.value === 'if');
    if (!ifArg) {
      errors.push(
        new GraphQLError(`Directive "@${directive.name.value}" requires "if" argument`, 'DIRECTIVE', {
          locations: directive.name.loc,
        }),
      );
      continue;
    }
    if (ifArg.value.kind !== 'BooleanValue' && ifArg.value.kind !== 'Variable') {
      errors.push(
        new GraphQLError(
          `Argument "if" of "@${directive.name.value}" must be a Boolean`,
          'DIRECTIVE',
          { locations: ifArg.value.loc },
        ),
      );
    }
  }
}

// ---- 合并冲突（FieldsInSetCanMerge 简化实现） ----

interface FieldEntry {
  node: FieldNode;
  def: FieldDef;
  parentType: ObjectType;
}

function findValidationConflicts(
  schema: SchemaTypeView,
  parentType: ObjectType,
  selectionSet: SelectionSetNode,
  fragments: Map<string, FragmentDefinitionNode>,
  errors: GraphQLError[],
): void {
  const collected = collectFields(parentType, selectionSet, {
    fragments,
    variables: 'static',
  });

  for (const group of collected.values()) {
    const first = group.nodes[0]!;
    const firstDef = parentType.fields.get(first.name.value);
    if (!firstDef) continue;

    for (let i = 1; i < group.nodes.length; i += 1) {
      const other = group.nodes[i]!;
      const otherDef = parentType.fields.get(other.name.value);
      if (!otherDef) continue;

      if (first.name.value !== other.name.value) {
        errors.push(
          new GraphQLError(
            `Fields "${responseKey(first)}" conflict because "${first.name.value}" and "${other.name.value}" are different fields under the same response key`,
            'FIELD_CONFLICT',
            { locations: [first.name.loc, other.name.loc] },
          ),
        );
        continue;
      }

      const argConflict = compareArguments(first, other);
      if (argConflict) {
        errors.push(
          new GraphQLError(
            `Fields "${responseKey(first)}" conflict because they use differing arguments: ${argConflict}`,
            'FIELD_CONFLICT',
            { locations: [first.name.loc, other.name.loc] },
          ),
        );
        continue;
      }

      if (Boolean(first.selectionSet) !== Boolean(other.selectionSet)) {
        errors.push(
          new GraphQLError(
            `Fields "${responseKey(first)}" conflict because one is a leaf and the other has a selection set`,
            'FIELD_CONFLICT',
            { locations: [first.name.loc, other.name.loc] },
          ),
        );
      }
    }

    // 同名字段类型必然相同；对合并后的子选择集继续递归
    if (group.mergedSelectionSet) {
      const target = namedTargetType(schema, firstDef.type);
      if (isObjectType(target)) {
        findValidationConflicts(schema, target, group.mergedSelectionSet, fragments, errors);
      }
    }
  }
}

function compareArguments(a: FieldNode, b: FieldNode): string | null {
  const mapA = new Map(a.arguments.map((arg) => [arg.name.value, arg.value]));
  const mapB = new Map(b.arguments.map((arg) => [arg.name.value, arg.value]));
  const names = new Set([...mapA.keys(), ...mapB.keys()]);
  for (const name of names) {
    const valueA = mapA.get(name);
    const valueB = mapB.get(name);
    if (!valueA || !valueB) return `argument "${name}" is missing in one field`;
    if (!valueLiteralsEqual(valueA, valueB)) {
      return `argument "${name}" has different values`;
    }
  }
  return null;
}

/** 结构相等：变量按名称比较，字面量按值比较（规范对实参的比较方式） */
function valueLiteralsEqual(a: ValueNode, b: ValueNode): boolean {
  if (a.kind !== b.kind) return false;
  if (a.kind === 'Variable' && b.kind === 'Variable') {
    return a.name.value === b.name.value;
  }
  if (a.kind === 'ListValue' && b.kind === 'ListValue') {
    return (
      a.values.length === b.values.length &&
      a.values.every((item, i) => valueLiteralsEqual(item, b.values[i]!))
    );
  }
  if (a.kind === 'IntValue' && b.kind === 'IntValue') return a.value === b.value;
  if (a.kind === 'StringValue' && b.kind === 'StringValue') return a.value === b.value;
  if (a.kind === 'BooleanValue' && b.kind === 'BooleanValue') return a.value === b.value;
  if (a.kind === 'EnumValue' && b.kind === 'EnumValue') return a.value === b.value;
  if (a.kind === 'NullValue' && b.kind === 'NullValue') return true;
  return false;
}
