package tvl.parser;

import tvl.expr.ExpressionException;

/** 语法错误（含解析时发现的结构问题），携带源码偏移位置。 */
public class ParseException extends ExpressionException {
    public ParseException(String message, int position) {
        super(message, position);
    }
}
