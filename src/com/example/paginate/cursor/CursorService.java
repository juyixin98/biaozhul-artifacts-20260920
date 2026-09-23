package com.example.paginate.cursor;

import com.example.paginate.json.Json;
import com.example.paginate.web.ApiException;

import javax.crypto.Mac;
import javax.crypto.spec.SecretKeySpec;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.security.SecureRandom;
import java.util.Base64;
import java.util.LinkedHashMap;
import java.util.Map;

/**
 * 游标签发与校验。
 *
 * 游标格式：base64url(负载JSON) "." base64url(HMAC-SHA256(负载, 服务端密钥))
 * - 负载绑定：查询指纹、快照 ID、最后排序元组 (排序键值, 唯一ID)
 * - MAC 防篡改：改动任何字节都会在校验时以 403 cursor_invalid 拒绝
 * - 密钥每次启动随机生成（也可用环境变量固定），重启后旧游标全部失效
 */
public final class CursorService {

    private static final Base64.Encoder ENCODER = Base64.getUrlEncoder().withoutPadding();
    private static final Base64.Decoder DECODER = Base64.getUrlDecoder();

    private final byte[] secret;

    public CursorService(String secret) {
        this.secret = secret.getBytes(StandardCharsets.UTF_8);
    }

    public static String randomSecret() {
        byte[] bytes = new byte[32];
        new SecureRandom().nextBytes(bytes);
        return ENCODER.encodeToString(bytes);
    }

    public String encode(CursorPayload payload) {
        Map<String, Object> body = new LinkedHashMap<>();
        body.put("v", CursorPayload.VERSION);
        body.put("q", payload.queryHash());
        body.put("s", payload.snapshotId());
        body.put("k", payload.lastSortValue());
        body.put("i", payload.lastId());
        String bodyB64 = ENCODER.encodeToString(
                Json.stringify(body).getBytes(StandardCharsets.UTF_8));
        return bodyB64 + "." + sign(bodyB64);
    }

    /**
     * 校验并解析游标。
     *
     * @param expectedQueryHash 当前请求的查询指纹；不一致说明游标被拿去配了别的查询
     * @throws ApiException 403 游标被篡改/伪造；400 结构非法或查询不匹配
     */
    public CursorPayload decode(String token, String expectedQueryHash) {
        if (token == null || token.isEmpty()) {
            throw ApiException.badRequest("cursor_invalid", "游标为空");
        }
        int dot = token.indexOf('.');
        if (dot <= 0 || dot != token.lastIndexOf('.') || dot == token.length() - 1) {
            throw ApiException.forbidden("cursor_invalid", "游标格式非法");
        }
        String bodyB64 = token.substring(0, dot);
        String macB64 = token.substring(dot + 1);

        // 先验 MAC：任何伪造/篡改都在这里被挡住
        String expectedMac = sign(bodyB64);
        if (!constantTimeEquals(expectedMac, macB64)) {
            throw ApiException.forbidden("cursor_invalid", "游标签名校验失败（游标被篡改或密钥已轮换）");
        }

        byte[] jsonBytes;
        try {
            jsonBytes = DECODER.decode(bodyB64);
        } catch (IllegalArgumentException e) {
            throw ApiException.forbidden("cursor_invalid", "游标负载无法解码");
        }

        Map<String, Object> body;
        try {
            body = Json.parseObject(new String(jsonBytes, StandardCharsets.UTF_8));
        } catch (RuntimeException e) {
            throw ApiException.forbidden("cursor_invalid", "游标负载不是合法 JSON");
        }

        Long version = Json.getLong(body, "v");
        if (version == null || version != CursorPayload.VERSION) {
            throw ApiException.badRequest("cursor_version_unsupported",
                    "游标版本不受支持: " + version);
        }
        String queryHash = Json.getString(body, "q");
        String snapshotId = Json.getString(body, "s");
        String lastSortValue = Json.getString(body, "k");
        Long lastId = Json.getLong(body, "i");
        if (queryHash == null || snapshotId == null || lastSortValue == null || lastId == null) {
            throw ApiException.badRequest("cursor_invalid", "游标缺少必要字段");
        }
        if (!queryHash.equals(expectedQueryHash)) {
            throw ApiException.badRequest("cursor_query_mismatch",
                    "游标属于另一个查询（排序/筛选/页大小与当前请求不一致），不能复用");
        }
        return new CursorPayload(queryHash, snapshotId, lastSortValue, lastId);
    }

    private String sign(String bodyB64) {
        try {
            Mac mac = Mac.getInstance("HmacSHA256");
            mac.init(new SecretKeySpec(secret, "HmacSHA256"));
            byte[] raw = mac.doFinal(bodyB64.getBytes(StandardCharsets.UTF_8));
            return ENCODER.encodeToString(raw);
        } catch (Exception e) {
            throw new IllegalStateException("HMAC 不可用", e);
        }
    }

    private static boolean constantTimeEquals(String a, String b) {
        return MessageDigest.isEqual(
                a.getBytes(StandardCharsets.UTF_8), b.getBytes(StandardCharsets.UTF_8));
    }
}
