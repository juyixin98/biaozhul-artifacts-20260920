package tvl.core;

/**
 * 词法/语法/语义（类型检查）阶段的错误。携带错误位置。
 * 错误码见 code，便于自动化测试与调用方分类处理。
 */
public class CompileException extends RuntimeException {
    private static final long serialVersionUID = 1L;

    private final String code;
    private final transient Pos pos;
    private final transient int length;

    public CompileException(String code, String message, Pos pos) {
        this(code, message, pos, 1);
    }

    public CompileException(String code, String message, Pos pos, int length) {
        super(message);
        this.code = code;
        this.pos = pos;
        this.length = length;
    }

    public String code() {
        return code;
    }

    public Pos pos() {
        return pos;
    }

    /** 出错 token/操作符的字符长度，用于在错误信息中高亮。 */
    public int length() {
        return length;
    }
}
