package com.bm25stable;

/**
 * 检索相关异常，携带 HTTP 状态码与机器可读错误码，由 HTTP 层统一映射为 JSON 错误响应。
 */
public class SearchException extends RuntimeException {

    private final int status;
    private final String code;

    public SearchException(int status, String code, String message) {
        super(message);
        this.status = status;
        this.code = code;
    }

    public int status() {
        return status;
    }

    public String code() {
        return code;
    }

    /** 400：请求参数非法（查询为空、pageSize 越界等）。 */
    public static final class BadRequest extends SearchException {
        public BadRequest(String message) {
            super(400, "BAD_REQUEST", message);
        }
    }

    /** 400：游标无法解析（不是合法的 base64/JSON，或缺字段）。 */
    public static final class InvalidCursor extends SearchException {
        public InvalidCursor(String message) {
            super(400, "INVALID_CURSOR", message);
        }
    }

    /** 400：游标与当前请求不一致（换了查询词或 pageSize 却沿用旧游标）。 */
    public static final class CursorMismatch extends SearchException {
        public CursorMismatch(String message) {
            super(400, "CURSOR_MISMATCH", message);
        }
    }

    /** 410：游标绑定的快照版本已被淘汰，分页无法继续，需重新发起第一页。 */
    public static final class SnapshotExpired extends SearchException {
        public SnapshotExpired(String message) {
            super(410, "SNAPSHOT_EXPIRED", message);
        }
    }
}
