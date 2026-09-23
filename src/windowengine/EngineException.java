package windowengine;

/**
 * 引擎统一异常。所有可预期的错误（请求格式、类型、溢出等）都用它抛出，
 * 携带稳定的错误码字符串，便于 JSON 入口返回 {"ok":false,"error":{...}}。
 */
public class EngineException extends RuntimeException {

    private final String code;

    public EngineException(ErrorCode code, String message) {
        super(message);
        this.code = code.code();
    }

    public EngineException(ErrorCode code, String message, Throwable cause) {
        super(message, cause);
        this.code = code.code();
    }

    public String code() {
        return code;
    }
}
