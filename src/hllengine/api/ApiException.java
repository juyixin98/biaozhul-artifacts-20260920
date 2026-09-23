package hllengine.api;

/**
 * Error codes used in JSON error envelopes ({@code {"ok":false,"error":{...}}}).
 *
 * <ul>
 *   <li>{@code BAD_FORMAT} &ndash; request or serialized sketch is malformed
 *       (bad JSON, bad base64, truncated bytes, wrong magic/checksum, unknown field).</li>
 *   <li>{@code INCOMPATIBLE_CONFIG} &ndash; merge/import attempted between sketches
 *       whose precision, hash algorithm or hash seed differ.</li>
 *   <li>{@code NOT_FOUND} / {@code ALREADY_EXISTS} &ndash; catalog lifecycle.</li>
 *   <li>{@code UNSUPPORTED} / {@code BAD_REQUEST} &ndash; other request problems.</li>
 * </ul>
 */
public final class ApiException extends RuntimeException {

    public static final String BAD_FORMAT = "BAD_FORMAT";
    public static final String INCOMPATIBLE_CONFIG = "INCOMPATIBLE_CONFIG";
    public static final String NOT_FOUND = "NOT_FOUND";
    public static final String ALREADY_EXISTS = "ALREADY_EXISTS";
    public static final String BAD_REQUEST = "BAD_REQUEST";
    public static final String UNSUPPORTED = "UNSUPPORTED";

    private final String code;

    public ApiException(String code, String message) {
        super(message);
        this.code = code;
    }

    public String code() {
        return code;
    }
}
