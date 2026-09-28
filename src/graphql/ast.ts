/**
 * AST 节点定义：受限 GraphQL 子集。
 * 支持 operation(query/mutation)、变量定义、别名、字段、片段展开、内联片段、指令(仅解析不执行)。
 */

export interface Location {
  line: number;
  column: number;
  offset: number;
}

export interface NameNode {
  kind: 'Name';
  value: string;
  loc: Location;
}

export type OperationTypeNode = 'query' | 'mutation' | 'subscription';

export interface DocumentNode {
  kind: 'Document';
  definitions: DefinitionNode[];
}

export type DefinitionNode = OperationDefinitionNode | FragmentDefinitionNode;

export interface OperationDefinitionNode {
  kind: 'OperationDefinition';
  operation: OperationTypeNode;
  name: NameNode | null;
  variableDefinitions: VariableDefinitionNode[];
  directives: DirectiveNode[];
  selectionSet: SelectionSetNode;
  loc: Location;
}

export interface VariableDefinitionNode {
  kind: 'VariableDefinition';
  variable: VariableNode;
  type: TypeNode;
  defaultValue: ValueNode | null;
  loc: Location;
}

export interface FragmentDefinitionNode {
  kind: 'FragmentDefinition';
  name: NameNode;
  typeCondition: NamedTypeNode;
  directives: DirectiveNode[];
  selectionSet: SelectionSetNode;
  loc: Location;
}

export interface SelectionSetNode {
  kind: 'SelectionSet';
  selections: SelectionNode[];
  loc: Location;
}

export type SelectionNode = FieldNode | FragmentSpreadNode | InlineFragmentNode;

export interface FieldNode {
  kind: 'Field';
  alias: NameNode | null;
  name: NameNode;
  arguments: ArgumentNode[];
  directives: DirectiveNode[];
  selectionSet: SelectionSetNode | null;
  loc: Location;
}

export interface FragmentSpreadNode {
  kind: 'FragmentSpread';
  name: NameNode;
  directives: DirectiveNode[];
  loc: Location;
}

export interface InlineFragmentNode {
  kind: 'InlineFragment';
  typeCondition: NamedTypeNode | null;
  directives: DirectiveNode[];
  selectionSet: SelectionSetNode;
  loc: Location;
}

export interface ArgumentNode {
  kind: 'Argument';
  name: NameNode;
  value: ValueNode;
  loc: Location;
}

export interface DirectiveNode {
  kind: 'Directive';
  name: NameNode;
  arguments: ArgumentNode[];
  loc: Location;
}

export type TypeNode = NamedTypeNode | ListTypeNode | NonNullTypeNode;

export interface NamedTypeNode {
  kind: 'NamedType';
  name: NameNode;
  loc: Location;
}

export interface ListTypeNode {
  kind: 'ListType';
  type: TypeNode;
  loc: Location;
}

export interface NonNullTypeNode {
  kind: 'NonNullType';
  type: NamedTypeNode | ListTypeNode;
  loc: Location;
}

export type ValueNode =
  | VariableNode
  | IntValueNode
  | FloatValueNode
  | StringValueNode
  | BooleanValueNode
  | NullValueNode
  | EnumValueNode
  | ListValueNode
  | ObjectValueNode;

export interface VariableNode {
  kind: 'Variable';
  name: NameNode;
  loc: Location;
}

export interface IntValueNode {
  kind: 'IntValue';
  value: string;
  loc: Location;
}

export interface FloatValueNode {
  kind: 'FloatValue';
  value: string;
  loc: Location;
}

export interface StringValueNode {
  kind: 'StringValue';
  value: string;
  block: boolean;
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
  loc: Location;
}
