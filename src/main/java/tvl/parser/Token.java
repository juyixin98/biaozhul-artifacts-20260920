package tvl.parser;

import tvl.core.Pos;

/** 一个词法单元：类型、文本、起止位置（start 含，end 不含）。 */
public final class Token {
    public final TokenType type;
    public final String text;
    public final Pos start;
    public final int endOffset;

    public Token(TokenType type, String text, Pos start, int endOffset) {
        this.type = type;
        this.text = text;
        this.start = start;
        this.endOffset = endOffset;
    }

    @Override
    public String toString() {
        return type + "(" + text + "@" + start + ")";
    }
}
