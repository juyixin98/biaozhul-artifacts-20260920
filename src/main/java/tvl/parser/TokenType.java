package tvl.parser;

/**
 * 词法单元类型。
 *
 * 关键字不区分大小写（TRUE/FALSE/NULL/AND/OR/NOT/IS），由词法器识别；
 * 标识符区分大小写（与列名精确匹配）。
 */
public enum TokenType {
    // 字面量
    INTEGER,
    STRING,
    TRUE,
    FALSE,
    NULL_KW,
    IDENT,

    // 操作符
    PLUS, MINUS, STAR, SLASH,
    EQ, NE, LT, LE, GT, GE,
    LPAREN, RPAREN,

    // 关键字
    AND, OR, NOT, IS,

    EOF
}
