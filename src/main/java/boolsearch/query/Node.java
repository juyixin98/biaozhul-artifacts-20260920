package boolsearch.query;

import java.util.List;

/** 查询 AST。AND/OR 节点在解析时按同优先级扁平化。 */
public sealed interface Node {
    record Term(String term) implements Node {}
    record And(List<Node> children) implements Node {}
    record Or(List<Node> children) implements Node {}
    record Not(Node child) implements Node {}
}
