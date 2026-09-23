package tvl.analyzer;

import tvl.expr.ExpressionException;

/**
 * 类型检查错误。类型不匹配、未知列、对不兼容类型排序等。
 * 携带触发节点的源码偏移位置。
 */
public class TypeException extends ExpressionException {
    public TypeException(String message, int position) {
        super(message, position);
    }
}
