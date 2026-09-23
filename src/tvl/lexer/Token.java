package tvl.lexer;

/**
 * 词法单元。
 *
 * @param type   类型
 * @param text   原始文本（字符串 token 为转义还原后的值）
 * @param intVal 整数字面量值（仅 INTEGER）
 * @param pos    从 0 开始的字符偏移
 * @param line   从 1 开始的行号
 * @param column 从 1 开始的列号
 */
public record Token(TokenType type, String text, long intVal, int pos, int line, int column) {

    public Token(TokenType type, String text, int pos, int line, int column) {
        this(type, text, 0L, pos, line, column);
    }

    @Override
    public String toString() {
        return type + "(" + text + ")@" + pos;
    }
}
