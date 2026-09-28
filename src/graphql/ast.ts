/**
 * GraphQL 文档的精简 AST 定义。
 * 只覆盖受限子集：query / mutation、字段、别名、片段（定义/展开）、
 * 变量定义、字面量实参与变量实参、指令（仅识别并在校验阶段处理 skip/include）。
 */

export type OperationType = 'query' | 'mutation';

export interface Location {
  /** 从 1 开始的行号 */
  line: number;
  /** 从 1 开始的列号 */
  column: number;
  /** 从 0 开始的文档偏移 */
  offset: number;
}

export interface NameNode {
  kind: 'Name';
  value: string;
  loc: Location;
}

export interface VariableNode {
  kind: 'Variable';
  name: NameNode;
  loc: Location;
}

export interface IntValueNode {
  kind: 'IntValue';
  value: number;
  loc: Location;
}

export interface StringValueNode {
  kind: 'StringValue';
  value: string;
  loc: Location;
}

export interface BooleanValueNode {
  kind: 'BooleanValue';
  value: boolean;
  loc: Location;
}

export interface NullValueNode {
  kind: 'NullValue';
  loc: Location;
}

export interface EnumValueNode {
  kind: 'EnumValue';
  value: string;
  loc: Location;
}

export interface ListValueNode {
  kind: 'ListValue';
  values: ValueNode[];
  loc: Location;
}

export interface ObjectValueNode {
  kind: 'ObjectValue';
  fields: ObjectFieldNode[];
  loc: Location;
}

export interface ObjectFieldNode {
  kind: 'ObjectField';
  name: NameNode;
  value: ValueNode;
}

export type ValueNode =
  | VariableNode
  | IntValueNode
  | StringValueNode
  | BooleanValueNode
  | NullValueNode
  | EnumValueNode
  | ListValueNode
  | ObjectValueNode;

export interface ArgumentNode {
  kind: 'Argument';
  name: NameNode;
  value: ValueNode;
}

export interface DirectiveNode {
  kind: 'Directive';
  name: NameNode;
  arguments: ArgumentNode[];
}

export interface VariableDefinitionNode {
  kind: 'VariableDefinition';
  variable: VariableNode;
  /** 词法表示，如 String / [ID!]! */
  type: TypeNode;
  defaultValue: ValueNode | null;
  directives: DirectiveNode[];
}

export type NonNullTypeNode = {
  kind: 'NonNullType';
  type: NamedTypeNode | ListTypeNode;
  loc: Location;
};

export type ListTypeNode = {
  kind: 'ListType';
  type: TypeNode;
  loc: Location;
};

export type NamedTypeNode = {
  kind: 'NamedType';
  name: NameNode;
  loc: Location;
};

export type TypeNode = NamedTypeNode | ListTypeNode | NonNullTypeNode;

export interface FieldNode {
  kind: 'Field';
  /** 原始字段名 */
  name: NameNode;
  /** 响应键：存在别名时为别名，否则为字段名 */
  alias: NameNode | null;
  arguments: ArgumentNode[];
  directives: DirectiveNode[];
  selectionSet: SelectionSetNode | null;
}

export interface FragmentSpreadNode {
  kind: 'FragmentSpread';
  name: NameNode;
  directives: DirectiveNode[];
}

export interface InlineFragmentNode {
  kind: 'InlineFragment';
  typeCondition: NameNode | null;
  directives: DirectiveNode[];
  selectionSet: SelectionSetNode;
}

export type SelectionNode = FieldNode | FragmentSpreadNode | InlineFragmentNode;

export interface SelectionSetNode {
  kind: 'SelectionSet';
  selections: SelectionNode[];
}

export interface OperationDefinitionNode {
  kind: 'OperationDefinition';
  operation: OperationType;
  name: NameNode | null;
  variableDefinitions: VariableDefinitionNode[];
  directives: DirectiveNode[];
  selectionSet: SelectionSetNode;
  loc: Location;
}

export interface FragmentDefinitionNode {
  kind: 'FragmentDefinition';
  name: NameNode;
  typeCondition: NameNode;
  directives: DirectiveNode[];
  selectionSet: SelectionSetNode;
}

export type DefinitionNode = OperationDefinitionNode | FragmentDefinitionNode;

export interface DocumentNode {
  kind: 'Document';
  definitions: DefinitionNode[];
}
