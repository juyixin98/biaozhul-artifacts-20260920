package dev.dedup.hll;

import java.util.Base64;
import java.util.LinkedHashMap;
import java.util.Map;

/**
 * 草图的可移植 JSON 表示（export/load 使用）：
 * <pre>
 * {
 *   "sketchFormat": "HLL-JSON",
 *   "version": 1,
 *   "hashId": "MURMUR3_X64_128_SEED9747B28C_H1",
 *   "precision": 12,
 *   "registerCount": 4096,
 *   "registersBase64": "..."   // m 个寄存器字节，标准 Base64（无换行）
 * }
 * </pre>
 * 反序列化执行与二进制格式相同严格的字段/值域校验。
 */
public final class SketchJsonCodec {

    public static final String FORMAT_NAME = "HLL-JSON";
    public static final int VERSION = 1;

    private SketchJsonCodec() {
    }

    public static Map<String, Object> toJson(HllSketch sketch) {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("sketchFormat", FORMAT_NAME);
        m.put("version", VERSION);
        m.put("hashId", sketch.config().hashId());
        m.put("precision", sketch.config().precision());
        m.put("registerCount", sketch.registerCount());
        m.put("registersBase64",
                Base64.getEncoder().withoutPadding().encodeToString(sketch.registersSnapshot()));
        return m;
    }

    public static HllSketch fromJson(Map<String, Object> obj) {
        if (obj == null) {
            throw new SketchFormatException("草图 JSON 为 null");
        }
        Object fmt = obj.get("sketchFormat");
        if (!FORMAT_NAME.equals(fmt)) {
            throw new SketchFormatException("sketchFormat 字段错误: 期望 \"" + FORMAT_NAME + "\"，得到 " + fmt);
        }
        int ver = requireInt(obj, "version");
        if (ver != VERSION) {
            throw new SketchFormatException("不支持的草图 JSON 版本: " + ver + "（仅支持 v" + VERSION + "）");
        }
        Object hashIdObj = obj.get("hashId");
        if (!(hashIdObj instanceof String) || ((String) hashIdObj).isEmpty()) {
            throw new SketchFormatException("hashId 缺失或不是非空字符串");
        }
        int p = requireInt(obj, "precision");
        if (p < HllConfig.MIN_PRECISION || p > HllConfig.MAX_PRECISION) {
            throw new SketchFormatException(
                    "precision 越界: " + p + "，允许 [" + HllConfig.MIN_PRECISION + "," + HllConfig.MAX_PRECISION + "]");
        }
        int m = 1 << p;
        Object declaredM = obj.get("registerCount");
        if (declaredM instanceof Number && ((Number) declaredM).intValue() != m) {
            throw new SketchFormatException(
                    "registerCount(" + declaredM + ") 与 precision 推导值(" + m + ")不一致");
        }
        Object b64 = obj.get("registersBase64");
        if (!(b64 instanceof String)) {
            throw new SketchFormatException("registersBase64 缺失或不是字符串");
        }
        byte[] regs;
        try {
            regs = Base64.getDecoder().decode((String) b64);
        } catch (IllegalArgumentException iae) {
            throw new SketchFormatException("registersBase64 不是合法 Base64: " + iae.getMessage());
        }
        if (regs.length != m) {
            throw new SketchFormatException("寄存器字节数 " + regs.length + " 与精度要求 " + m + " 不一致");
        }
        int maxRank = 65 - p;
        for (int i = 0; i < m; i++) {
            int r = regs[i] & 0xff;
            if (r > maxRank) {
                throw new SketchFormatException(
                        "寄存器 #" + i + " 值越界: " + r + "（p=" + p + " 时最大 rank 为 " + maxRank + "）");
            }
        }
        return new HllSketch(new HllConfig(p, (String) hashIdObj), regs);
    }

    private static int requireInt(Map<String, Object> obj, String key) {
        Object v = obj.get(key);
        if (!(v instanceof Number)) {
            throw new SketchFormatException(key + " 缺失或不是数字");
        }
        return ((Number) v).intValue();
    }
}
