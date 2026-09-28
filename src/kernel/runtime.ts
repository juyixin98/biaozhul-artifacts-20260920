import type {
  FieldDeclaration,
  NodeDeclaration,
  NodeExecutionRecord,
  SourceResult,
} from '../types.js';

/**
 * Shared runtime view of a scheduled node. The kernel mutates `record`
 * during scheduling; field assembly and consistency logic only read it.
 */
export interface NodeRuntime {
  decl: NodeDeclaration;
  record: NodeExecutionRecord;
  result?: SourceResult;
  promise: Promise<void>;
}
