package bitemporal.api;

import com.fasterxml.jackson.annotation.JsonInclude;

/** 统一响应信封：成功带 data，失败带 error；以 exitCode 区分进程退出码。 */
@JsonInclude(JsonInclude.Include.NON_NULL)
public record ApiResponse(boolean success, Object data, ErrorBody error) {

    public static ApiResponse ok(Object data) {
        return new ApiResponse(true, data, null);
    }

    public static ApiResponse fail(String code, String message, String recordId) {
        return new ApiResponse(false, null, new ErrorBody(code, message, recordId));
    }

    @JsonInclude(JsonInclude.Include.NON_NULL)
    public record ErrorBody(String code, String message, String recordId) {
    }
}
