/**
 * 契约解析第二阶段：类型校验。
 * 顺序约束：变量先完成类型校验，再做结构/片段校验。
 *
 * 失败归类：
 *  - 未声明片段/字段、类型不匹配、变量类型错误 → INPUT_INVALID
 *  - 重复别名指向不同字段、片段循环、变量重复   → STATE_CONFLICT
 *
 * 注意：同别名且同字段的重复选择是允许的（响应合并），
 * 但每一处出现都由估计器/执行器独立计费，重复不能绕过预算。
 */
import type {
  FieldSelection,
  ParsedQuery,
  PrimitiveKind,
  SchemaDocument,
  SelectionDef,
  VariableValues,
} from '../contract/types.js';
import { DomainError } from '../errors/DomainError.js';
import { findField, isPrimitive } from '../schema/schema.js';
import { isVarRef } from './parser.js';

export interface ValidatedVariable {
  value: unknown;
  declaredType: PrimitiveKind;
}

export interface ValidatedPlan {
  schema: SchemaDocument;
  query: ParsedQuery;
  variables: Record<string, ValidatedVariable>;
}

const PRIMITIVE_NAMES: ReadonlySet<string> = new Set(['STRING', 'INT', 'FLOAT', 'BOOL', 'ID']);

export function validateQuery(
  schema: SchemaDocument,
  query: ParsedQuery,
  rawVariables: VariableValues,
  runId: string,
): ValidatedPlan {
  // 1) 变量先完成类型校验
  const variables = coerceVariables(query, rawVariables, runId);

  // 2) 结构、字段、片段校验
  validateSelections(schema, schema.queryType, query.selections, '$', runId, new Set(), new Map(), variables);

  return { schema, query, variables };
}

function coerceVariables(
  query: ParsedQuery,
  raw: VariableValues,
  runId: string,
): Record<string, ValidatedVariable> {
  const out: Record<string, ValidatedVariable> = {};
  for (const def of query.variables) {
    if (!PRIMITIVE_NAMES.has(def.declaredType)) {
      throw new DomainError(
        'INPUT_INVALID',
        `variable $${def.name} has unknown type ${def.declaredType}`,
        runId,
        { variable: def.name, declaredType: def.declaredType },
      );
    }
    const provided = Object.prototype.hasOwnProperty.call(raw, def.name) ? raw[def.name] : def.defaultValue;
    if (provided === undefined) {
      throw new DomainError(
        'INPUT_INVALID',
        `variable $${def.name} is required but no value or default was supplied`,
        runId,
        { variable: def.name, declaredType: def.declaredType },
      );
    }
    const coerced = coerceScalar(def.declaredType as PrimitiveKind, provided, `$${def.name}`, runId);
    out[def.name] = { value: coerced, declaredType: def.declaredType as PrimitiveKind };
  }
  return out;
}

function coerceScalar(declared: PrimitiveKind, value: unknown, label: string, runId: string): unknown {
  const fail = (actual: string): never => {
    throw new DomainError(
      'INPUT_INVALID',
      `${label} expected ${declared} but got ${actual}`,
      runId,
      { variable: label, declaredType: declared, actualType: actual },
    );
  };
  switch (declared) {
    case 'STRING':
      if (typeof value !== 'string') fail(typeof value);
      return value;
    case 'INT':
      if (typeof value !== 'number' || !Number.isInteger(value)) fail(typeof value);
      return value;
    case 'FLOAT':
      if (typeof value !== 'number' || !Number.isFinite(value)) fail(typeof value);
      return value;
    case 'BOOL':
      if (typeof value !== 'boolean') fail(typeof value);
      return value;
    case 'ID':
      if (typeof value === 'string' || typeof value === 'number') return String(value);
      fail(typeof value);
  }
}

function validateSelections(
  schema: SchemaDocument,
  typeName: string,
  selections: SelectionDef[],
  path: string,
  runId: string,
  activeFragments: Set<string>,
  aliasOwners: Map<string, string>,
  variables: Record<string, ValidatedVariable>,
): void {
  for (const sel of selections) {
    if (sel.kind === 'fragmentSpread') {
      const frag = schema.fragments[sel.name];
      if (!frag) {
        throw new DomainError(
          'INPUT_INVALID',
          `unknown fragment ...${sel.name}`,
          runId,
          { fragment: sel.name, path },
          path,
        );
      }
      if (frag.onType !== typeName) {
        throw new DomainError(
          'INPUT_INVALID',
          `fragment ...${sel.name} cannot be spread on ${typeName} (declared on ${frag.onType})`,
          runId,
          { fragment: sel.name, path, expectedType: frag.onType, actualType: typeName },
          path,
        );
      }
      if (activeFragments.has(sel.name)) {
        throw new DomainError(
          'STATE_CONFLICT',
          `cyclic fragment expansion detected at ...${sel.name}`,
          runId,
          { fragment: sel.name, cycle: [...activeFragments, sel.name], path },
          path,
        );
      }
      const nextActive = new Set(activeFragments);
      nextActive.add(sel.name);
      validateSelections(schema, typeName, frag.fields, path, runId, nextActive, aliasOwners, variables);
      continue;
    }

    const field = findField(schema, typeName, sel.name);
    if (!field) {
      throw new DomainError(
        'INPUT_INVALID',
        `field ${sel.name} does not exist on type ${typeName}`,
        runId,
        { field: sel.name, type: typeName, path },
        path,
      );
    }
    const ownerKey = sel.alias;
    const existingOwner = aliasOwners.get(ownerKey);
    if (existingOwner !== undefined && existingOwner !== sel.name) {
      throw new DomainError(
        'STATE_CONFLICT',
        `alias "${sel.alias}" is bound to both ${existingOwner} and ${sel.name}`,
        runId,
        { alias: sel.alias, fields: [existingOwner, sel.name], path },
        path,
      );
    }
    aliasOwners.set(ownerKey, sel.name);

    checkArgs(sel, variables, runId, path);

    const childPath = `${path}.${sel.alias}`;
    const fieldIsObject = !isPrimitive(field.type);
    if (fieldIsObject && sel.selections.length === 0) {
      throw new DomainError(
        'INPUT_INVALID',
        `field ${typeName}.${sel.name} needs a selection set`,
        runId, { path: childPath }, childPath,
      );
    }
    if (!fieldIsObject && sel.selections.length > 0) {
      throw new DomainError(
        'INPUT_INVALID',
        `scalar field ${typeName}.${sel.name} cannot have a selection set`,
        runId, { path: childPath }, childPath,
      );
    }
    if (fieldIsObject) {
      validateSelections(schema, field.type, sel.selections, childPath, runId, new Set(activeFragments), new Map(), variables);
    }
  }
}

function checkArgs(sel: FieldSelection, variables: Record<string, ValidatedVariable>, runId: string, path: string): void {
  for (const [argName, argValue] of Object.entries(sel.args)) {
    if (isVarRef(argValue)) {
      if (!variables[argValue.name]) {
        throw new DomainError(
          'INPUT_INVALID',
          `argument ${argName} references undeclared variable $${argValue.name}`,
          runId,
          { argument: argName, variable: argValue.name, path },
          path,
        );
      }
    }
  }
}
