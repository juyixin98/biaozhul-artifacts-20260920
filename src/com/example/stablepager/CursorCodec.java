package com.example.stablepager;

import java.util.Base64;

/**
 * Opaque continuation cursor.
 *
 * <p>Wire format ({@code .} separator, no padding):
 * <pre>
 *   v1.<urlsafe-base64 payload>.<urlsafe-base64 tag>
 * </pre>
 * where {@code tag = HMAC-SHA256(secret, version + "." + payloadBytes)}. The
 * payload is a small JSON object binding the cursor to:
 * <ul>
 *   <li><b>the query</b> — canonical fingerprint (filters/sort/order/limit),</li>
 *   <li><b>the snapshot</b> — snapshot id + version,</li>
 *   <li><b>the last sort tuple</b> — sort value + unique id of the last row
 *       returned, which is where the next page resumes.</li>
 * </ul>
 * The HMAC makes every field tamper-evident: flipping a byte invalidates the tag
 * and is rejected with {@code CURSOR_INVALID}. Tokens produced with another
 * secret (e.g. after a server restart with a fresh secret) fail identically.
 */
public final class CursorCodec {

    public static final String VERSION_PREFIX = "v1.";

    private static final Base64.Encoder ENCODER = Base64.getUrlEncoder().withoutPadding();
    private static final Base64.Decoder DECODER = Base64.getUrlDecoder();

    private final javax.crypto.Mac mac; // guarded by synchronized blocks (Mac is not thread-safe)

    public CursorCodec(String secret) {
        try {
            javax.crypto.spec.SecretKeySpec key =
                    new javax.crypto.spec.SecretKeySpec(secret.getBytes(java.nio.charset.StandardCharsets.UTF_8), "HmacSHA256");
            this.mac = javax.crypto.Mac.getInstance("HmacSHA256");
            this.mac.init(key);
        } catch (Exception e) {
            throw new IllegalStateException("failed to initialize HMAC-SHA256", e);
        }
    }

    /** Encodes a payload map into a signed, url-safe cursor token. */
    public synchronized String encode(java.util.Map<String, Object> payload) {
        byte[] payloadBytes = Json.stringify(payload).getBytes(java.nio.charset.StandardCharsets.UTF_8);
        mac.reset();
        mac.update(VERSION_PREFIX.getBytes(java.nio.charset.StandardCharsets.UTF_8));
        byte[] tag = mac.doFinal(payloadBytes);
        return VERSION_PREFIX + ENCODER.encodeToString(payloadBytes) + "." + ENCODER.encodeToString(tag);
    }

    /**
     * Verifies the signature and decodes the payload. Any structural problem
     * (bad shape, bad base64, bad/foreign HMAC, wrong version) is reported as
     * {@code CURSOR_INVALID}.
     */
    @SuppressWarnings("unchecked")
    public java.util.Map<String, Object> decode(String token) {
        if (token == null || !token.startsWith(VERSION_PREFIX)) {
            throw invalid("cursor must start with " + VERSION_PREFIX);
        }
        String body = token.substring(VERSION_PREFIX.length());
        int sep = body.indexOf('.');
        if (sep <= 0 || sep == body.length() - 1) {
            throw invalid("malformed cursor");
        }
        String payloadB64 = body.substring(0, sep);
        String tagB64 = body.substring(sep + 1);

        byte[] payloadBytes;
        byte[] providedTag;
        try {
            payloadBytes = DECODER.decode(payloadB64);
            providedTag = DECODER.decode(tagB64);
        } catch (IllegalArgumentException e) {
            throw invalid("cursor is not valid base64");
        }

        byte[] expectedTag;
        synchronized (this) {
            mac.reset();
            mac.update(VERSION_PREFIX.getBytes(java.nio.charset.StandardCharsets.UTF_8));
            expectedTag = mac.doFinal(payloadBytes);
        }
        if (!constantTimeEquals(expectedTag, providedTag)) {
            throw invalid("cursor signature does not match");
        }

        Object parsed;
        try {
            parsed = Json.parse(new String(payloadBytes, java.nio.charset.StandardCharsets.UTF_8));
        } catch (ApiException e) {
            throw invalid("cursor payload is not valid JSON");
        }
        if (!(parsed instanceof java.util.Map<?, ?>)) {
            throw invalid("cursor payload must be a JSON object");
        }
        java.util.Map<String, Object> payload = (java.util.Map<String, Object>) parsed;
        requireString(payload, "f");
        requireString(payload, "s");
        if (!payload.containsKey("k") || !payload.containsKey("id")) {
            throw invalid("cursor payload is missing the last sort tuple");
        }
        return payload;
    }

    private static void requireString(java.util.Map<String, Object> payload, String key) {
        Object v = payload.get(key);
        if (!(v instanceof String) || ((String) v).isEmpty()) {
            throw invalid("cursor payload field '" + key + "' is missing");
        }
    }

    private static ApiException invalid(String detail) {
        return new ApiException(400, "CURSOR_INVALID", "invalid cursor: " + detail);
    }

    private static boolean constantTimeEquals(byte[] a, byte[] b) {
        if (a.length != b.length) {
            return false;
        }
        int diff = 0;
        for (int i = 0; i < a.length; i++) {
            diff |= a[i] ^ b[i];
        }
        return diff == 0;
    }
}
