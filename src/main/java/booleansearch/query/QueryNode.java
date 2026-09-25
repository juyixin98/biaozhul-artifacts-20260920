package booleansearch.query;

/**
 * 查询抽象语法树节点。
 *
 * <p>文法（OR 优先级最低，AND 次之，NOT 最高）：
 * <pre>
 * expr    := orExpr
 * orExpr  := andExpr ( OR andExpr )*
 * andExpr := notExpr ( AND notExpr )*
 * notExpr := NOT notExpr | atom
 * atom    := TERM | '(' expr ')'
 * </pre>
 * 每个节点记录其在原始查询串中的字符位置，便于错误定位。
 */
public sealed interface QueryNode
        permits QueryNode.Term, QueryNode.Not, QueryNode.And, QueryNode.Or {

    /** 节点起始位置（0 基）。 */
    int position();

    /** 词项叶子。 */
    record Term(String term, int position) implements QueryNode {
    }

    /** 逻辑非；position 指向 NOT 关键字。 */
    record Not(QueryNode child, int position) implements QueryNode {
    }

    /** 逻辑与；position 指向首个操作数（保留左端位置）。 */
    record And(java.util.List<QueryNode> children, int position) implements QueryNode {
    }

    /** 逻辑或；position 指向首个操作数。 */
    record Or(java.util.List<QueryNode> children, int position) implements QueryNode {
    }
}
