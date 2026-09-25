package booleansearch.search;

import booleansearch.query.QueryNode;

/**
 * 把 AST 渲染成规范化的全括号中缀表达式，便于响应中展示解析结果。
 */
final class AstRenderer {

    private AstRenderer() {
    }

    static String render(QueryNode node) {
        StringBuilder sb = new StringBuilder();
        renderTo(node, sb);
        return sb.toString();
    }

    private static void renderTo(QueryNode node, StringBuilder sb) {
        switch (node) {
            case QueryNode.Term t -> sb.append(t.term());
            case QueryNode.Not n -> {
                sb.append("(NOT ");
                renderTo(n.child(), sb);
                sb.append(')');
            }
            case QueryNode.And a -> renderBinary(a.children(), "AND", sb);
            case QueryNode.Or o -> renderBinary(o.children(), "OR", sb);
        }
    }

    private static void renderBinary(java.util.List<QueryNode> children, String op, StringBuilder sb) {
        sb.append('(');
        for (int i = 0; i < children.size(); i++) {
            if (i > 0) {
                sb.append(' ').append(op).append(' ');
            }
            renderTo(children.get(i), sb);
        }
        sb.append(')');
    }
}
