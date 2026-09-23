package dev.dedup.hll;

import java.nio.charset.StandardCharsets;
import java.util.Base64;

/**
 * 固定哈希算法的已知答案测试（KAT）。
 * 向量来自独立 Python 移植版 results/hash_kat_reference.py 生成的
 * results/hash-kat-vectors.json；此处硬编码 10 组 (输入, h1, h2)，
 * 证明 Java 实现与独立实现逐向量一致（同算法、同固定种子）。
 */
public class HashAlgorithmTest extends TestCase {

    // {base64 输入 或 null, utf8 输入 或 null, h1hex, h2hex}
    private static final String[][] VECTORS = {
            {"", null, "392b208a1daabbb3", "93b0608fe302957a"},
            {"YQ==", null, "5ce8d8512db25a1d", "9e6dab0f9208f004"},
            {"YWJj", null, "3743630dbfc3cedc", "cde0a23420b504bf"},
            {"aGVsbG8=", null, "8c23d6856f071a2e", "2a905546b3c1cb83"},
            {"aGVsbG8gd29ybGQ=", null, "2785cdc826220bf1", "1d8e62eeb9508d7f"},
            {"VGhlIHF1aWNrIGJyb3duIGZveCBqdW1wcyBvdmVyIHRoZSBsYXp5IGRvZw==", null,
                    "738a7f3bd2633121", "f94573727ec016e5"},
            {"AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8gISIjJCUmJygpKissLS4vMDEyMzQ1Njc4OTo7PD0+P0BB"
                    + "QkNERUZHSElKS0xNTk9QUVJTVFVWV1hZWltcXV5fYGFiY2RlZmdoaWprbG1ub3BxcnN0dXZ3eHl6e3x9fn"
                    + "+AgYKDhIWGh4iJiouMjY6PkJGSk5SVlpeYmZqbnJ2en6ChoqOkpaanqKmqq6ytrq+wsbKztLW2t7i5uru8"
                    + "vb6/wMHCw8TFxsfIycrLzM3Oz9DR0tPU1dbX2Nna29zd3t/g4eLj5OXm5+jp6uvs7e7v8PHy8/T19vf4"
                    + "+fr7/P3+/w==", null, "17fc23500f62fdad", "f47a4214bc34a447"},
            {"AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
                    + "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA==", null,
                    "c6aa562a18de2b28", "20f3837a3d32bcb9"},
            {null, "可合并近似去重", "1bf2f3cd28afd217", "3535877b0ca9da28"},
            {null, "HyperLogLog", "a2476df872716aa6", "d16071b6b033f354"},
    };

    @Override
    public String name() {
        return "MurmurHash3 KAT（与独立 Python 参考实现逐向量一致）";
    }

    @Override
    public void run() {
        check(Murmur3Hash128.FIXED_SEED == 0x9747B28C, "固定种子必须是 0x9747B28C");
        check(Murmur3Hash128.HASH_ID.equals("MURMUR3_X64_128_SEED9747B28C_H1"),
                "hashId 固定");

        for (String[] v : VECTORS) {
            byte[] input;
            String label;
            if (v[0] != null) {
                input = Base64.getDecoder().decode(v[0]);
                label = "base64:" + v[0].substring(0, Math.min(12, v[0].length())) + "...(" + input.length + "B)";
            } else {
                input = v[1].getBytes(StandardCharsets.UTF_8);
                label = "utf8:" + v[1];
            }
            long[] h = Murmur3Hash128.hash128(input);
            eq(Long.toUnsignedString(h[0], 16), v[2], "h1 " + label);
            eq(Long.toUnsignedString(h[1], 16), v[3], "h2 " + label);
        }

        // 同输入稳定、不同种子必须不同
        long[] a = Murmur3Hash128.hash128("abc".getBytes(StandardCharsets.UTF_8));
        long[] b = Murmur3Hash128.hash128("abc".getBytes(StandardCharsets.UTF_8));
        eq(a[0], b[0], "同输入哈希稳定");
        long[] otherSeed = Murmur3Hash128.hash128("abc".getBytes(StandardCharsets.UTF_8), 1);
        check(a[0] != otherSeed[0], "换种子结果应不同（证明种子确实参与）");
        check(Murmur3Hash128.hashStringToH1("abc") == a[0], "hashStringToH1 取 h1");
    }
}
