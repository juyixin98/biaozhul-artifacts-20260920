package com.example.segmenter.tests;

import com.example.segmenter.api.Segmenter;
import com.example.segmenter.model.Dictionary;
import com.example.segmenter.model.Segmentation;
import com.example.segmenter.model.Token;

import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

import static com.example.segmenter.tests.TestFramework.check;
import static com.example.segmenter.tests.TestFramework.assertEquals;
import static com.example.segmenter.tests.TestFramework.suite;

/** 基础切分：词典命中、未知字符、空串、标点/空白、增补平面码点。 */
public final class BasicSegmentationTest {

    private BasicSegmentationTest() {
    }

    public static void run() {
        suite("basic segmentation");

        Map<String, Long> freq = new LinkedHashMap<>();
        freq.put("研究", 100L);
        freq.put("生命", 100L);
        freq.put("的", 300L);
        Dictionary dict = Dictionary.fromFrequencies("basic", freq);
        Segmenter seg = new Segmenter(dict, 7.5);

        Segmentation s1 = seg.segment("研究生命");
        assertEquals("全词典句词面", List.of("研究", "生命"), s1.words());
        check("全词典句无未知词",
                s1.tokens().stream().noneMatch(Token::unknown));
        assertEquals("词数为 2", 2, s1.tokenCount());
        double expectedCost = dict.costOf("研究") + dict.costOf("生命");
        check("总代价为边代价之和",
                Math.abs(expectedCost - s1.totalCost()) < 1e-12);

        Segmentation s2 = seg.segment("研究X生命");
        assertEquals("含未知字的词面", List.of("研究", "X", "生命"), s2.words());
        check("未知字被标记", s2.tokens().get(1).unknown());
        check("未知字代价明确为配置值",
                Math.abs(s2.tokens().get(1).cost() - 7.5) < 1e-12);
        check("非未知字未被标记",
                !s2.tokens().get(0).unknown() && !s2.tokens().get(2).unknown());

        check("全未知句逐字切出", seg.segment("ひらがな").tokenCount() == 4);
        List<Token> unkTokens = seg.segment("ひらがな").tokens();
        check("全未知句每字代价为 7.5",
                unkTokens.stream().allMatch(t -> t.unknown() && Math.abs(t.cost() - 7.5) < 1e-12));

        Segmentation empty = seg.segment("");
        check("空串词数为 0", empty.tokenCount() == 0);
        check("空串代价为 0", empty.totalCost() == 0.0);
        assertEquals("空串词面为空表", List.of(), empty.words());

        Segmentation punct = seg.segment("研究，生命。");
        assertEquals("标点按未知单字切出",
                List.of("研究", "，", "生命", "。"), punct.words());
        check("标点被标记未知",
                punct.tokens().get(1).unknown() && punct.tokens().get(3).unknown());

        Segmentation spaces = seg.segment("研究 生命");
        assertEquals("空白也是未知单字",
                List.of("研究", " ", "生命"), spaces.words());

        // 增补平面（emoji 为单个 Unicode 码点，UTF-16 下是代理对）
        Segmentation emoji = seg.segment("研究😀生命");
        assertEquals("emoji 作为一个未知码点",
                List.of("研究", "😀", "生命"), emoji.words());
        assertEquals("emoji 句 3 个词", 3, emoji.tokenCount());

        // 默认未知代价
        Segmenter defaultSeg = new Segmenter(dict);
        check("默认未知代价为 10.0",
                Math.abs(defaultSeg.unknownCharCost() - Segmenter.DEFAULT_UNKNOWN_CHAR_COST) < 1e-12);
        check("默认未知代价常量为 10.0",
                Segmenter.DEFAULT_UNKNOWN_CHAR_COST == 10.0);

        // null 与非法参数
        boolean npe = false;
        try {
            seg.segment(null);
        } catch (IllegalArgumentException e) {
            npe = true;
        }
        check("null 文本抛出 IllegalArgumentException", npe);

        boolean badN1 = false;
        try {
            seg.segmentNBest("研究", 0);
        } catch (IllegalArgumentException e) {
            badN1 = true;
        }
        check("n=0 抛出异常", badN1);

        boolean badN2 = false;
        try {
            seg.segmentNBest("研究", Segmenter.MAX_N_BEST + 1);
        } catch (IllegalArgumentException e) {
            badN2 = true;
        }
        check("n 超过上限抛出异常", badN2);
    }
}
