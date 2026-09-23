package tvl.json;

/** JSON 文本本身格式不合法（解析期）。 */
public class JsonParseException extends RuntimeException {
    private static final long serialVersionUID = 1L;
    public final int line;
    public final int column;

    public JsonParseException(String message, int line, int column) {
        super(message + "（位于 JSON 第 " + line + " 行第 " + column + " 列）");
        this.line = line;
        this.column = column;
    }
}
