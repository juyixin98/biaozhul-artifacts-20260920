package booleansearch.query;

/**
 * 查询解析异常，携带错误在原始查询串中的字符位置（0 基）。
 */
public class QueryParseException extends RuntimeException {

    private final int position;

    public QueryParseException(String message, int position) {
        super(message + "（位置 " + position + "）");
        this.position = position;
    }

    /** 错误位置，0 基字符偏移；-1 表示与具体位置无关。 */
    public int position() {
        return position;
    }
}
