package com.example.segmenter.tests;

import com.example.segmenter.api.Segmenter;
import com.example.segmenter.model.Dictionary;
import com.example.segmenter.model.Segmentation;

import java.util.HashSet;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;

import static com.example.segmenter.tests.TestFramework.check;
import static com.example.segmenter.tests.TestFramework.assertEquals;
import static com.example.segmenter.tests.TestFramework.suite;

/** N 最佳路径性质：排序、去重、数量上限、n=1 与 segment() 一致、子前缀一致性。 */
public final class NBestTest {

    private NBestTest() {
    }

    public static void run() {
        suite("n-best paths");

        Map<String, Long> freq = new LinkedHashMap<>();
        freq.put("研究", 5L);
        freq.put("研究生", 3L);
        freq.put("生", 7L);
        freq.put("生命", 4L);
        freq.put("命", 2L);
        freq.put("的", 9L);
        Dictionary dict = Dictionary.fromFrequencies("nb", freq);
        Segmenter seg = new Segmenter(dict, 8.0);
        BruteForce brute = new BruteForce(dict, 8.0);
        String text = "研究生生命的";

        for (int n : new int[]{1, 2, 3, 5, 10}) {
            List<Segmentation> got = seg.segmentNBest(text, n);
            List<Segmentation> want = brute.enumerate(text);
            int expectSize = Math.min(n, want.size());
            assertEquals("n=" + n + " 返回数量正确", expectSize, got.size());

            boolean sorted = true;
            for (int i = 1; i < got.size(); i++) {
                if (got.get(i - 1).totalCost() > got.get(i).totalCost() + 1e-12) {
                    sorted = false;
                }
            }
            check("n=" + n + " 总代价非降序", sorted);

            // 与穷举的前 n 条逐条相同
            boolean prefixMatch = true;
            for (int i = 0; i < expectSize; i++) {
                if (!got.get(i).words().equals(want.get(i).words())) {
                    prefixMatch = false;
                }
            }
            check("n=" + n + " 与穷举前 n 条一致", prefixMatch);
        }

        // 路径互不相同（词面或未知标记至少一处不同）
        List<Segmentation> all = seg.segmentNBest(text, Segmenter.MAX_N_BEST);
        Set<String> signatures = new HashSet<>();
        for (Segmentation s : all) {
            StringBuilder sig = new StringBuilder();
            s.tokens().forEach(t -> sig.append(t.unknown() ? 'u' : 'w').append(t.word()).append('|'));
            signatures.add(sig.toString());
        }
        assertEquals("N 最佳路径互不相同", all.size(), signatures.size());

        // rank 连续从 1 开始
        boolean ranks = true;
        for (int i = 0; i < all.size(); i++) {
            if (all.get(i).rank() != i + 1) ranks = false;
        }
        check("rank 从 1 连续编号", ranks);

        // 1-best 两种调用方式一致
        check("segment() 与 segmentNBest(text,1) 一致",
                seg.segment(text).words().equals(seg.segmentNBest(text, 1).get(0).words()));

        // n 大于候选总数时返回全部（不报错）
        List<Segmentation> few = seg.segmentNBest("的", 10);
        // "的"：词典词 / 未知字 两条
        assertEquals("候选不足时返回全部（2 条）", 2, few.size());

        // 不枚举指数候选的间接保证：N 固定时返回数也固定
        check("最多返回 64 条",
                seg.segmentNBest("研究生生命的研究生生命", Segmenter.MAX_N_BEST).size()
                        <= Segmenter.MAX_N_BEST);
    }
}
