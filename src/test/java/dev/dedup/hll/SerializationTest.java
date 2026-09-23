package dev.dedup.hll;

import java.util.Base64;
import java.util.LinkedHashMap;
import java.util.Map;

/** 配置兼容性、二进制/JSON 序列化往返与坏格式拒绝测试。 */
public class SerializationTest extends TestCase {

    @Override
    public String name() {
        return "兼容性检查 + 二进制/JSON 序列化往返 + 坏格式";
    }

    @Override
    public void run() {
        testConfigCompatibility();
        testBinaryRoundTrip();
        testBinaryCorruption();
        testJsonRoundTrip();
        testJsonCorruption();
    }

    private void testConfigCompatibility() {
        HllConfig a = new HllConfig(12);
        HllConfig b = new HllConfig(12);
        HllConfig c = new HllConfig(11);
        HllConfig d = new HllConfig(12, "OTHER_HASH");
        check(a.isCompatibleWith(b), "同 p 同 hashId 兼容");
        check(a.incompatibilityReason(c) != null && a.incompatibilityReason(c).contains("精度"),
                "不同 p 必须报精度不兼容");
        check(a.incompatibilityReason(d) != null && a.incompatibilityReason(d).contains("哈希"),
                "不同 hashId 必须报哈希不兼容");
        fails(IllegalArgumentException.class, () -> new HllConfig(3), "p=3 越界拒绝");
        fails(IllegalArgumentException.class, () -> new HllConfig(19), "p=19 越界拒绝");

        // mergeWith 必须在不兼容时拒绝且不修改任何一方
        HllSketch s12 = new HllSketch(new HllConfig(12));
        HllSketch s11 = new HllSketch(new HllConfig(11));
        s12.addString("x");
        s11.addString("y");
        byte[] before12 = s12.registersSnapshot();
        fails(IncompatibleSketchException.class, () -> s12.mergeWith(s11),
                "不同精度草图合并必须抛 IncompatibleSketchException");
        check(java.util.Arrays.equals(before12, s12.registersSnapshot()),
                "拒绝合并不得修改目标草图");

        // 未知 hashId 的草图（手工构造）与标准草图也不能合并
        HllSketch alien = new HllSketch(new HllConfig(12, "UNKNOWN-VENDOR-HASH"));
        fails(IncompatibleSketchException.class, () -> s12.mergeWith(alien),
                "不同哈希标识合并必须拒绝");
    }

    private HllSketch populated(int p) {
        HllSketch s = new HllSketch(new HllConfig(p));
        for (int i = 0; i < 3000; i++) {
            s.addString("user-" + i);
        }
        return s;
    }

    private void testBinaryRoundTrip() {
        for (int p : new int[] {4, 12, 18}) {
            HllSketch s = populated(p);
            byte[] bin = BinarySketchCodec.encode(s);
            HllSketch back = BinarySketchCodec.decode(bin);
            check(s.equals(back), "p=" + p + " 二进制往返逐寄存器一致");
            eqLong(back.estimate(), s.estimate(), "p=" + p + " 往返后估计一致");
            // 头部布局断言
            check(bin[0] == 'H' && bin[1] == 'L' && bin[2] == 'C' && bin[3] == 'D',
                    "p=" + p + " 魔数 HLCD");
            check((bin[4] & 0xff) == 1, "p=" + p + " 版本=1");
            check((bin[5] & 0xff) == p, "p=" + p + " 头部精度字节");
            check(bin.length == 12 + Murmur3Hash128.HASH_ID.getBytes().length + (1 << p),
                    "p=" + p + " 总长度=12+hashId+m");
        }
    }

    private void testBinaryCorruption() {
        byte[] good = BinarySketchCodec.encode(populated(12));

        // 1. 截断
        byte[] truncated = java.util.Arrays.copyOf(good, good.length - 1);
        expectBadFormat(() -> BinarySketchCodec.decode(truncated), "截断 1 字节");
        expectBadFormat(() -> BinarySketchCodec.decode(new byte[11]), "短于头部");
        expectBadFormat(() -> BinarySketchCodec.decode(null), "null");

        // 2. 魔数错误
        byte[] badMagic = good.clone();
        badMagic[0] = 'X';
        expectBadFormat(() -> BinarySketchCodec.decode(badMagic), "魔数被改");

        // 3. 版本错误
        byte[] badVer = good.clone();
        badVer[4] = 2;
        expectBadFormat(() -> BinarySketchCodec.decode(badVer), "版本=2");
        badVer[4] = 0;
        expectBadFormat(() -> BinarySketchCodec.decode(badVer), "版本=0");

        // 4. 精度越界（同时修复长度无关——精度本身先被拒）
        byte[] badP = good.clone();
        badP[5] = 3;
        expectBadFormat(() -> BinarySketchCodec.decode(badP), "精度 p=3");
        badP[5] = 19;
        expectBadFormat(() -> BinarySketchCodec.decode(badP), "精度 p=19");

        // 5. 未知标志位
        byte[] badFlags = good.clone();
        badFlags[7] = 1;
        expectBadFormat(() -> BinarySketchCodec.decode(badFlags), "标志位非零（CRC 也会失败，但标志位先校验）");

        // 6. hashId 长度被改（0）
        byte[] zeroHashLen = good.clone();
        zeroHashLen[6] = 0;
        expectBadFormat(() -> BinarySketchCodec.decode(zeroHashLen), "hashId 长度=0");

        // 7. 寄存器区/哈希区单比特翻转 -> CRC 必须捕获
        for (int pos : new int[] {12, 12 + 5, good.length - 1, good.length / 2}) {
            byte[] flip = good.clone();
            flip[pos] ^= 0x01;
            expectBadFormat(() -> BinarySketchCodec.decode(flip), "偏移 " + pos + " 单比特翻转");
        }

        // 8. CRC 字段自身被改
        byte[] badCrc = good.clone();
        badCrc[8] ^= (byte) 0xFF;
        expectBadFormat(() -> BinarySketchCodec.decode(badCrc), "CRC 字段被改");

        // 9. 寄存器越界值：手工构造合法长度但某寄存器 rank 超过 65-p
        //    p=12 -> maxRank=53。需要同时保持 CRC 正确，因此构造合法草图后改寄存器并重算 CRC
        byte[] crafted = craftWithIllegalRegister(12, (byte) 54);
        expectBadFormat(() -> BinarySketchCodec.decode(crafted), "寄存器 rank=54 超过上界 53（CRC 有效）");
    }

    private byte[] craftWithIllegalRegister(int p, byte badRank) {
        HllSketch s = new HllSketch(new HllConfig(p));
        s.addString("seed");
        byte[] bin = BinarySketchCodec.encode(s);
        int hashLen = Murmur3Hash128.HASH_ID.getBytes().length;
        bin[12 + hashLen] = badRank;
        // 重算 CRC（offset 8..11）覆盖 hashId+regs
        java.util.zip.CRC32 crc = new java.util.zip.CRC32();
        crc.update(bin, 12, hashLen + (1 << p));
        long v = crc.getValue();
        bin[8] = (byte) (v >>> 24);
        bin[9] = (byte) (v >>> 16);
        bin[10] = (byte) (v >>> 8);
        bin[11] = (byte) v;
        return bin;
    }

    private void testJsonRoundTrip() {
        HllSketch s = populated(12);
        Map<String, Object> j = SketchJsonCodec.toJson(s);
        eq(j.get("sketchFormat"), "HLL-JSON", "JSON 格式名");
        eq(j.get("version"), 1, "JSON 版本");
        eq(j.get("precision"), 12, "JSON 精度");
        eq(j.get("registerCount"), 4096, "JSON 寄存器数");
        HllSketch back = SketchJsonCodec.fromJson(j);
        check(s.equals(back), "JSON 草图往返一致");
        eqLong(back.estimate(), s.estimate(), "JSON 往返后估计一致");
    }

    private void testJsonCorruption() {
        Map<String, Object> base = SketchJsonCodec.toJson(populated(12));

        expectBadFormat(() -> SketchJsonCodec.fromJson(null), "null JSON 草图");

        Map<String, Object> m1 = new LinkedHashMap<>(base);
        m1.put("sketchFormat", "WRONG");
        expectBadFormat(() -> SketchJsonCodec.fromJson(m1), "sketchFormat 错误");

        Map<String, Object> m2 = new LinkedHashMap<>(base);
        m2.put("version", 9);
        expectBadFormat(() -> SketchJsonCodec.fromJson(m2), "version=9");

        Map<String, Object> m3 = new LinkedHashMap<>(base);
        m3.put("precision", 19);
        expectBadFormat(() -> SketchJsonCodec.fromJson(m3), "precision=19");

        Map<String, Object> m4 = new LinkedHashMap<>(base);
        m4.put("registerCount", 100);
        expectBadFormat(() -> SketchJsonCodec.fromJson(m4), "registerCount 与 precision 矛盾");

        Map<String, Object> m5 = new LinkedHashMap<>(base);
        m5.put("registersBase64", "!!!not-base64!!!");
        expectBadFormat(() -> SketchJsonCodec.fromJson(m5), "非法 Base64");

        // Base64 合法但字节数不对
        Map<String, Object> m6 = new LinkedHashMap<>(base);
        m6.put("registersBase64", Base64.getEncoder().encodeToString(new byte[10]));
        expectBadFormat(() -> SketchJsonCodec.fromJson(m6), "寄存器字节数=10 ≠ 4096");

        // 寄存器越界（长度恰好 4096，构造 rank=54）
        byte[] regs = new byte[4096];
        regs[0] = 54;
        Map<String, Object> m7 = new LinkedHashMap<>(base);
        m7.put("registersBase64", Base64.getEncoder().encodeToString(regs));
        expectBadFormat(() -> SketchJsonCodec.fromJson(m7), "JSON 寄存器 rank=54 越界");

        Map<String, Object> m8 = new LinkedHashMap<>(base);
        m8.remove("hashId");
        expectBadFormat(() -> SketchJsonCodec.fromJson(m8), "缺 hashId");
    }

    private void expectBadFormat(Runnable r, String what) {
        fails(SketchFormatException.class, r, "坏格式必须拒绝: " + what);
    }
}
