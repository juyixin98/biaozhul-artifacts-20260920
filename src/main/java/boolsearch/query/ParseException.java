package boolsearch.query;

/** 查询解析错误，保留出错位置（查询串中的 0 基字符偏移；EOF 时为串长）。 */
public final class ParseException extends Exception {
    private final int position;

    public ParseException(String message, int position) {
        super(message);
        this.position = position;
    }

    /** 出错位置：查询串中的 0 基字符偏移。 */
    public int position() {
        return position;
    }
}
