package tvl.lexer;

import tvl.expr.ExpressionException;

/** 词法错误（无法识别的字符、未闭合字符串、整数字面量溢出等）。 */
public class LexException extends ExpressionException {
    public LexException(String message, int position) {
        super(message, position);
    }
}
