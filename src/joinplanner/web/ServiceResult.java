package joinplanner.web;

import java.util.Map;

/** Result of a service call: HTTP status plus JSON body. */
public record ServiceResult(int status, Map<String, Object> body) {

    public static ServiceResult ok(Map<String, Object> body) {
        return new ServiceResult(200, body);
    }

    public static ServiceResult of(int status, Map<String, Object> body) {
        return new ServiceResult(status, body);
    }
}
