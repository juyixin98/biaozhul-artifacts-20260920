package dev.dedup.hll;

import java.math.BigDecimal;
import java.nio.charset.StandardCharsets;

/**
 * 把 JSON 请求中的值映射为确定性的规范化字节，再交给固定哈希。
 *
 * 带类型前缀，保证不同 JSON 类型不会碰撞（例如字符串 "1" 与数字 1）：
 * <pre>
 *   null      -> [0x00]
 *   false     -> [0x01]
 *   true      -> [0x02]
 *   整数 Long -> [0x03] + 十进制 ASCII（负号开头）
 *   浮点 Doub -> [0x04] + BigDecimal stripTrailingZeros().toPlainString()
 *   字符串    -> [0x05] + UTF-8 字节
 * </pre>
 *
 * 对象/数组不允许作为去重元素（报错），避免定义模糊的序列化问题。
 */
public final class ValueCanonicalizer {

    private static final byte TAG_NULL = 0x00;
    private static final byte TAG_FALSE = 0x01;
    private static final byte TAG_TRUE = 0x02;
    private static final byte TAG_INT = 0x03;
    private static final byte TAG_FLOAT = 0x04;
    private static final byte TAG_STRING = 0x05;

    private ValueCanonicalizer() {
    }

    public static byte[] encode(Object value) {
        if (value == null) {
            return new byte[] {TAG_NULL};
        }
        if (value instanceof Boolean) {
            return new byte[] {((Boolean) value) ? TAG_TRUE : TAG_FALSE};
        }
        if (value instanceof Integer || value instanceof Long) {
            return prefix(TAG_INT, value.toString().getBytes(StandardCharsets.US_ASCII));
        }
        if (value instanceof Number) {
            double d = ((Number) value).doubleValue();
            if (Double.isNaN(d) || Double.isInfinite(d)) {
                throw new IllegalArgumentException("NaN/Infinity 不能作为去重元素");
            }
            // stripTrailingZeros + toPlainString：1.0 与 1.00、1E2 与 100.0 规范化一致，且无指数写法
            String canon = new BigDecimal(value.toString()).stripTrailingZeros().toPlainString();
            return prefix(TAG_FLOAT, canon.getBytes(StandardCharsets.US_ASCII));
        }
        if (value instanceof String) {
            return prefix(TAG_STRING, ((String) value).getBytes(StandardCharsets.UTF_8));
        }
        throw new IllegalArgumentException(
                "不支持作为去重元素的值类型: " + value.getClass().getSimpleName()
                        + "（仅支持字符串、整数、有限浮点数、布尔、null）");
    }

    /** 返回规范化规则的人类可读说明（写入响应与文档）。 */
    public static String describe() {
        return "类型标签前缀: null=0x00,false=0x01,true=0x02,int=0x03,float=0x04,string=0x05；"
                + "整数按十进制文本；浮点 BigDecimal.stripTrailingZeros().toPlainString()；字符串按 UTF-8";
    }

    private static byte[] prefix(byte tag, byte[] body) {
        byte[] out = new byte[body.length + 1];
        out[0] = tag;
        System.arraycopy(body, 0, out, 1, body.length);
        return out;
    }
}
