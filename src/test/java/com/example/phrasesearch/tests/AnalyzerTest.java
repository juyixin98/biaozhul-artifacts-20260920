package com.example.phrasesearch.tests;

import com.example.phrasesearch.analyze.StandardAnalyzer;
import com.example.phrasesearch.analyze.StopGapAnalyzer;
import com.example.phrasesearch.model.AnalyzedField;
import com.example.phrasesearch.model.Token;

import java.util.List;

/**
 * 分析器测试——锁定切词规则，以及“停用词是否保留位置”的两种明确语义。
 */
final class AnalyzerTest {

    private AnalyzerTest() {
    }

    static void run() {
        standardKeepsStopwordsAndPositions();
        lowerCaseAndDigitsAndOffsets();
        stopGapRemovesStopwordsButKeepsPositionHoles();
        stopGapPositionCountIncludesTrailingStopword();
        consecutiveDelimitersDoNotConsumePositions();
    }

    private static void standardKeepsStopwordsAndPositions() {
        AnalyzedField af = new StandardAnalyzer().analyze("body", "The quick brown fox");
        Assert.equals("standard: token count（停用词保留）", 4, af.tokens().size());
        Assert.equals("standard: positionCount", 4, af.positionCount());
        Assert.equals("standard: 停用词小写保留",
                List.of("the", "quick", "brown", "fox"),
                af.tokens().stream().map(Token::term).toList());
        Assert.equals("standard: 位置连续 0..3",
                List.of(0, 1, 2, 3),
                af.tokens().stream().map(Token::position).toList());
    }

    private static void lowerCaseAndDigitsAndOffsets() {
        AnalyzedField af = new StandardAnalyzer().analyze("t", "  Hello, World-42! ");
        Assert.equals("切词: Hello World 42",
                List.of("hello", "world", "42"),
                af.tokens().stream().map(Token::term).toList());
        Token hello = af.tokens().get(0);
        Assert.equals("Hello 偏移", 2, hello.startOffset());
        Assert.equals("Hello 偏移 end", 7, hello.endOffset());
        Token num = af.tokens().get(2);
        Assert.equals("42 偏移 start", 15, num.startOffset());
        Assert.equals("42 偏移 end", 17, num.endOffset());
    }

    private static void stopGapRemovesStopwordsButKeepsPositionHoles() {
        StopGapAnalyzer a = new StopGapAnalyzer(StopGapAnalyzer.DEFAULT_STOPWORDS);
        AnalyzedField af = a.analyze("body", "the quick brown fox");

        Assert.equals("stop_gap: 停用词被删除",
                List.of("quick", "brown", "fox"),
                af.tokens().stream().map(Token::term).toList());
        // 关键断言：位置没有收紧，被删的 the 留下洞
        Assert.equals("stop_gap: 保留词的位置是 1,2,3（不是 0,1,2）",
                List.of(1, 2, 3),
                af.tokens().stream().map(Token::position).toList());
        Assert.equals("stop_gap: positionCount 仍为 4", 4, af.positionCount());
    }

    private static void stopGapPositionCountIncludesTrailingStopword() {
        StopGapAnalyzer a = new StopGapAnalyzer(StopGapAnalyzer.DEFAULT_STOPWORDS);
        // 字段以停用词结尾：位置槽必须计入，否则跨字段拼接时后一字段基址会错位
        AnalyzedField af = a.analyze("t", "quick the");
        Assert.equals("尾部停用词: 保留 quick", List.of("quick"),
                af.tokens().stream().map(Token::term).toList());
        Assert.equals("尾部停用词: positionCount=2（含尾部洞）", 2, af.positionCount());
    }

    private static void consecutiveDelimitersDoNotConsumePositions() {
        AnalyzedField af = new StandardAnalyzer().analyze("t", "a...b,,,c");
        Assert.equals("标点不产生空位置", List.of(0, 1, 2),
                af.tokens().stream().map(Token::position).toList());
    }
}
