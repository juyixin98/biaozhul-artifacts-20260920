package tvl.lexer;

/** 词法单元类型。 */
public enum TokenType {
    INTEGER,
    STRING,
    IDENT,
    // 关键字
    AND, OR, NOT, IS, NULL, TRUE, FALSE, UNKNOWN,
    // 比较/算术运算符
    EQ, NE, LT, LE, GT, GE, PLUS, MINUS, STAR, SLASH,
    LPAREN, RPAREN,
    EOF
}
