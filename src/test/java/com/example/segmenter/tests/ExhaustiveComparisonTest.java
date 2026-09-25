package com.example.segmenter.tests;

import com.example.segmenter.api.Segmenter;
import com.example.segmenter.data.CorpusLoader;
import com.example.segmenter.model.Dictionary;
import com.example.segmenter.model.Segmentation;

import java.nio.file.Path;
import java.nio.file.Paths;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

import static com.example.segmenter.tests.TestFramework.check;
import static com.example.segmenter.tests.TestFramework.suite;

/**
 * 验收核心：对小句穷举全部切分（2^(n-1)），与动态规划的 1-best 及完整 N-best
 * 列表逐条对照。覆盖重叠词、未知字符、空串、字典版本切换。
 */
public final class ExhaustiveComparisonTest {

    private ExhaustiveComparisonTest() {
    }

    public static void run() {
        suite("exhaustive comparison (DP vs brute force)");

        // 含重叠结构的小型显式词典：
        //  研究 / 研究生 / 生 / 生命 / 命 / 学生会 / 学生 / 学 / 生气
        Map<String, Long> freq = new LinkedHashMap<>();
        long[] values = {3, 2, 5, 4, 7, 1, 6, 8, 2};
        String[] words = {"研究", "研究生", "生", "生命", "命", "学生会", "学生", "学", "生气"};
        for (int i = 0; i < words.length; i++) {
            freq.put(words[i], values[i]);
        }
        Dictionary dict = Dictionary.fromFrequencies("overlap", freq);
        double unk = 9.0;
        Segmenter segmenter = new Segmenter(dict, unk);
        BruteForce brute = new BruteForce(dict, unk);

        List<String> sentences = Arrays.asList(
                "",
                "生",
                "研究",
                "研究生命",
                "学生研究生命",
                "学生会生气",
                "研究X生命",       // 含未知字符
                "ひらがな",        // 全部未知
                "学生，生命",      // 标点未知
                "研究生生命",
                "研究学生会生气"
        );

        for (String text : sentences) {
            List<Segmentation> expected = brute.enumerate(text);
            List<Segmentation> actual =
                    segmenter.segmentNBest(text, Segmenter.MAX_N_BEST);

            check("穷举句[" + label(text) + "]路径数一致 ("
                            + expected.size() + " 条)",
                    expected.size() == actual.size());

            int compareCount = Math.min(expected.size(), actual.size());
            boolean sameOrder = true;
            boolean sameCost = true;
            for (int i = 0; i < compareCount; i++) {
                if (!expected.get(i).words().equals(actual.get(i).words())) {
                    sameOrder = false;
                }
                if (!sameUnknown(expected.get(i), actual.get(i))) {
                    sameOrder = false;
                }
                if (Math.abs(expected.get(i).totalCost() - actual.get(i).totalCost()) > 1e-9) {
                    sameCost = false;
                }
            }
            check("穷举句[" + label(text) + "]排序与词面逐条一致", sameOrder);
            check("穷举句[" + label(text) + "]总代价逐条一致", sameCost);
        }

        // 词典版本切换：v1 / v2 对“研究生命的起源”给出不同的 1-best，
        // 且两版都分别与各自的穷举结果第 1 名一致。
        suite("dictionary version switch vs brute force");
        Path dataDir = Paths.get("data", "corpora");
        Dictionary v1 = CorpusLoader.load(dataDir.resolve("dict_v1.corpus"));
        Dictionary v2 = CorpusLoader.load(dataDir.resolve("dict_v2.corpus"));

        check("两个版本名不同", !v1.version().equals(v2.version()));

        String text = "研究生命的起源";
        Segmenter seg1 = new Segmenter(v1);
        Segmenter seg2 = new Segmenter(v2);
        List<String> best1 = seg1.segment(text).words();
        List<String> best2 = seg2.segment(text).words();

        System.out.println("  v1 best: " + best1 + " cost=" + seg1.segment(text).totalCost());
        System.out.println("  v2 best: " + best2 + " cost=" + seg2.segment(text).totalCost());

        check("版本切换导致切分结果不同", !best1.equals(best2));

        List<Segmentation> brute1 = new BruteForce(v1, Segmenter.DEFAULT_UNKNOWN_CHAR_COST)
                .enumerate(text);
        List<Segmentation> brute2 = new BruteForce(v2, Segmenter.DEFAULT_UNKNOWN_CHAR_COST)
                .enumerate(text);
        check("v1 1-best 与穷举最优一致",
                brute1.get(0).words().equals(best1)
                        && sameUnknown(brute1.get(0), seg1.segment(text)));
        check("v2 1-best 与穷举最优一致",
                brute2.get(0).words().equals(best2)
                        && sameUnknown(brute2.get(0), seg2.segment(text)));

        // 同一文本在两版本上各自做完整 N-best 对照
        check("v1 完整 N-best 与穷举一致",
                sameAsBrute(seg1, new BruteForce(v1, Segmenter.DEFAULT_UNKNOWN_CHAR_COST), text));
        check("v2 完整 N-best 与穷举一致",
                sameAsBrute(seg2, new BruteForce(v2, Segmenter.DEFAULT_UNKNOWN_CHAR_COST), text));

        // 未知字符 + 版本切换组合
        String mixed = "研究生X命";
        check("v1 混合未知句 N-best 与穷举一致",
                sameAsBrute(seg1, new BruteForce(v1, Segmenter.DEFAULT_UNKNOWN_CHAR_COST), mixed));
        check("v2 混合未知句 N-best 与穷举一致",
                sameAsBrute(seg2, new BruteForce(v2, Segmenter.DEFAULT_UNKNOWN_CHAR_COST), mixed));
    }

    private static boolean sameAsBrute(Segmenter seg, BruteForce brute, String text) {
        List<Segmentation> expected = brute.enumerate(text);
        List<Segmentation> actual = seg.segmentNBest(text, Segmenter.MAX_N_BEST);
        if (expected.size() != actual.size()) return false;
        for (int i = 0; i < expected.size(); i++) {
            if (!expected.get(i).words().equals(actual.get(i).words())) return false;
            if (!sameUnknown(expected.get(i), actual.get(i))) return false;
            if (Math.abs(expected.get(i).totalCost() - actual.get(i).totalCost()) > 1e-9) {
                return false;
            }
        }
        return true;
    }

    private static boolean sameUnknown(Segmentation a, Segmentation b) {
        List<com.example.segmenter.model.Token> ta = a.tokens();
        List<com.example.segmenter.model.Token> tb = b.tokens();
        if (ta.size() != tb.size()) return false;
        for (int i = 0; i < ta.size(); i++) {
            if (ta.get(i).unknown() != tb.get(i).unknown()) return false;
        }
        return true;
    }

    private static String label(String text) {
        if (text.isEmpty()) return "空串";
        return text;
    }

    // 避免未使用导入警告（List 在断言消息等处使用）
    @SuppressWarnings("unused")
    private static List<Object> keep() {
        return new ArrayList<>();
    }
}
