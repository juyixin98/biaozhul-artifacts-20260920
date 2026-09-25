package com.example.segmenter.tests;

import com.example.segmenter.api.Segmenter;
import com.example.segmenter.model.Dictionary;
import com.example.segmenter.model.Segmentation;

import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

import static com.example.segmenter.tests.TestFramework.check;
import static com.example.segmenter.tests.TestFramework.suite;

/**
 * “不枚举指数候选”的运行时验证：长度成倍增长时，N 最佳耗时近似线性增长
 * （而非 2^n 爆炸）。比较 n 与 2n 长度的耗时，要求比值远小于指数基线。
 */
public final class PolynomialComplexityTest {

    private PolynomialComplexityTest() {
    }

    public static void run() {
        suite("polynomial runtime (no exponential enumeration)");

        // 构造高歧义词典：任意前缀组合都成词，使切分空间确为指数级
        Map<String, Long> freq = new LinkedHashMap<>();
        String[] chars = {"甲", "乙", "丙", "丁"};
        for (String a : chars) {
            freq.put(a, 10L);
            for (String b : chars) {
                freq.put(a + b, 5L);
                for (String c : chars) {
                    freq.put(a + b + c, 2L);
                }
            }
        }
        Dictionary dict = Dictionary.fromFrequencies("ambig", freq);
        Segmenter seg = new Segmenter(dict, 20.0);

        String text100 = repeat("甲乙丙丁", 25);   // 100 码点
        String text400 = repeat("甲乙丙丁", 100);  // 400 码点
        String text1600 = repeat("甲乙丙丁", 400); // 1600 码点

        long t100 = timeNBest(seg, text100, 10);
        long t400 = timeNBest(seg, text400, 10);
        long t1600 = timeNBest(seg, text1600, 10);

        System.out.println("  100 码点: " + t100 + " ms");
        System.out.println("  400 码点: " + t400 + " ms");
        System.out.println("  1600 码点: " + t1600 + " ms");

        check("100 码点 3 秒内完成", t100 < 3000);
        check("400 码点 3 秒内完成", t400 < 3000);
        check("1600 码点 3 秒内完成", t1600 < 3000);

        // 判定基于较长串（噪声小）：长度再 x4，耗时增长远小于指数基线。
        // 指数枚举在该歧义文本上因子约为 2^(3*400)，这里允许至多 x50；
        // O(n^k) 多项式算法实际增长约一个数量级。
        long base = Math.max(t400, 5);
        check("400→1600（长度 x4）耗时 < x50（拒绝指数增长，实测见 RUNLOG）",
                t1600 < 50L * base);

        // 正确性不随长度退化：与穷举在短串上的一致性已在其它套件覆盖，
        // 这里仅检查长串能完整覆盖原文。
        Segmentation s = seg.segment(text400);
        int covered = s.words().stream().mapToInt(w -> (int) w.codePoints().count()).sum();
        check("长串路径完整覆盖原文（400 码点）", covered == 400);

        // N=64 在长串上仍然有界且有序
        List<Segmentation> nb = seg.segmentNBest(text400, 64);
        check("长串 N=64 返回不超过 64 条", nb.size() <= 64);
        boolean ordered = true;
        for (int i = 1; i < nb.size(); i++) {
            if (nb.get(i - 1).totalCost() > nb.get(i).totalCost() + 1e-9) ordered = false;
        }
        check("长串 N=64 代价有序", ordered);

        // 深链同分：所有词代价相同，每个位置都有多条候选且总代价相同，
        // 强制字典序比较器沿整条长链工作（回归递归比较可能栈溢出的问题）。
        Map<String, Long> flat = new LinkedHashMap<>();
        String[] pair = {"子", "丑"};
        for (String a : pair) {
            flat.put(a, 1L);
            for (String b : pair) {
                flat.put(a + b, 1L);
            }
        }
        Dictionary flatDict = Dictionary.fromFrequencies("flat", flat);
        // 让未知边与词典边代价也相同，制造词面相同、仅标记不同的同分候选
        Segmenter flatSeg = new Segmenter(flatDict, -Math.log(1.0 / (2 + 4)));
        String longText = repeat("子丑", 2000); // 4000 码点，深链
        List<Segmentation> flatBest = flatSeg.segmentNBest(longText, 3);
        check("深链同分不爆栈且返回结果", flatBest.size() >= 1);
        int coveredFlat = flatBest.get(0).words().stream()
                .mapToInt(w -> (int) w.codePoints().count()).sum();
        check("深链同分路径完整覆盖原文（4000 码点）", coveredFlat == 4000);
        check("深链同分名次连续",
                flatBest.get(0).rank() == 1 && flatBest.get(flatBest.size() - 1).rank() == flatBest.size());
    }

    private static long timeNBest(Segmenter seg, String text, int n) {
        // 预热
        seg.segmentNBest(text, n);
        long start = System.nanoTime();
        seg.segmentNBest(text, n);
        return (System.nanoTime() - start) / 1_000_000;
    }

    private static String repeat(String s, int times) {
        StringBuilder sb = new StringBuilder(s.length() * times);
        for (int i = 0; i < times; i++) sb.append(s);
        return sb.toString();
    }
}
