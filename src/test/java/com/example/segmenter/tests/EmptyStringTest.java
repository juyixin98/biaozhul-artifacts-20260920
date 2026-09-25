package com.example.segmenter.tests;

import com.example.segmenter.api.Segmenter;
import com.example.segmenter.model.Dictionary;
import com.example.segmenter.model.Segmentation;

import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

import static com.example.segmenter.tests.TestFramework.check;
import static com.example.segmenter.tests.TestFramework.assertEquals;
import static com.example.segmenter.tests.TestFramework.suite;

/** 空串与边界情况专项。 */
public final class EmptyStringTest {

    private EmptyStringTest() {
    }

    public static void run() {
        suite("empty string and boundaries");

        Map<String, Long> freq = new LinkedHashMap<>();
        freq.put("词", 1L);
        Dictionary dict = Dictionary.fromFrequencies("e", freq);
        Segmenter seg = new Segmenter(dict);

        Segmentation empty = seg.segment("");
        assertEquals("空串：1 条结果", 1, seg.segmentNBest("", 5).size());
        assertEquals("空串：词序列为空", List.of(), empty.words());
        check("空串：总代价 0", empty.totalCost() == 0.0);
        assertEquals("空串：rank=1", 1, empty.rank());
        assertEquals("空串：tokenCount=0", 0, empty.tokenCount());

        // 与穷举对照
        List<Segmentation> brute = new BruteForce(dict, Segmenter.DEFAULT_UNKNOWN_CHAR_COST)
                .enumerate("");
        assertEquals("空串穷举也只有 1 条空路径", 1, brute.size());
        check("空串穷举代价为 0", brute.get(0).totalCost() == 0.0);

        // 单字词既在词典中又是未知候选
        List<Segmentation> one = seg.segmentNBest("词", 5);
        assertEquals("单字词有 2 条路径（词典/未知）", 2, one.size());
        check("词典单字词代价 < 未知代价时词典路径胜出",
                !one.get(0).tokens().get(0).unknown());

        // 词典里没有的单字：只有未知 1 条
        List<Segmentation> onlyUnk = seg.segmentNBest("喵", 5);
        assertEquals("纯未知单字只有 1 条路径", 1, onlyUnk.size());
        check("纯未知单字标记未知", onlyUnk.get(0).tokens().get(0).unknown());
    }
}
