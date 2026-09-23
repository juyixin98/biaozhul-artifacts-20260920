package tvl.evaluator;

import tvl.expr.ExpressionException;

/**
 * 表达式求值期错误：整数溢出、除零。
 * 携带触发节点的源码偏移位置。
 */
public class EvalException extends ExpressionException {
    public EvalException(String message, int position) {
        super(message, position);
    }
}
