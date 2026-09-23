package windowengine;

/**
 * 稳定错误码枚举。code 字符串会原样出现在错误响应里，属于对外契约。
 */
public enum ErrorCode {

    INVALID_JSON("INVALID_JSON"),
    INVALID_REQUEST("INVALID_REQUEST"),
    COLUMN_NOT_FOUND("COLUMN_NOT_FOUND"),
    DUPLICATE_COLUMN("DUPLICATE_COLUMN"),
    AMBIGUOUS_COLUMN("AMBIGUOUS_COLUMN"),
    TYPE_MISMATCH("TYPE_MISMATCH"),
    INVALID_FRAME("INVALID_FRAME"),
    UNSUPPORTED("UNSUPPORTED"),
    OVERFLOW("OVERFLOW"),
    IO_ERROR("IO_ERROR");

    private final String code;

    ErrorCode(String code) {
        this.code = code;
    }

    public String code() {
        return code;
    }
}
