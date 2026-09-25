package phrase.test;

import phrase.core.Analyzer;
import phrase.core.Analyzers;
import phrase.core.StandardAnalyzer;
import phrase.core.StopwordAnalyzer;
import phrase.core.Token;

import java.util.List;

/** 分词与停用词位置保留测试。 */
public final class AnalyzerTest {

    public static void register(TestRunner runner) {
        runner.add("tokenizer/simple-lowercase-and-positions", AnalyzerTest::simple);
        runner.add("tokenizer/punctuation-does-not-consume-position", AnalyzerTest::punctuation);
        runner.add("tokenizer/digits-and-empty-input", AnalyzerTest::digitsAndEmpty);
        runner.add("tokenizer/unicode-letters", AnalyzerTest::unicode);
        runner.add("analyzer/stopwords-removed-but-positions-kept",
                AnalyzerTest::stopwordPositionsKept);
        runner.add("analyzer/stopword-list-contents", AnalyzerTest::stopwordList);
        runner.add("analyzer/unknown-analyzer-rejected", AnalyzerTest::unknown);
    }

    private static List<Token> std(String text) {
        return new StandardAnalyzer().analyze(text);
    }

    private static void simple() {
        List<Token> ts = std("Hello World");
        Asserts.assertEquals(2, ts.size(), "two tokens");
        Asserts.assertEquals("hello", ts.get(0).term(), "term0");
        Asserts.assertEquals(1, ts.get(0).position(), "pos0");
        Asserts.assertEquals("world", ts.get(1).term(), "term1");
        Asserts.assertEquals(2, ts.get(1).position(), "pos1");
    }

    private static void punctuation() {
        // 标点不产生词项，也不占位置：a,b -> a@1 b@2
        List<Token> ts = std("a, b... c!");
        Asserts.assertEquals(List.of("a", "b", "c"),
                ts.stream().map(Token::term).toList(), "terms through punctuation");
        Asserts.assertEquals(List.of(1, 2, 3),
                ts.stream().map(Token::position).toList(), "positions stay dense");
    }

    private static void digitsAndEmpty() {
        Asserts.assertEquals(0, std("").size(), "empty string -> 0 tokens");
        Asserts.assertEquals(0, std("--- !!!").size(), "punctuation only -> 0 tokens");
        List<Token> ts = std("node42 v2");
        Asserts.assertEquals(List.of("node42", "v2"),
                ts.stream().map(Token::term).toList(), "alphanumeric tokens");
    }

    private static void unicode() {
        List<Token> ts = std("短语 检索 系统");
        Asserts.assertEquals(3, ts.size(), "CJK runs split by spaces");
        Asserts.assertEquals("短语", ts.get(0).term(), "cjk term preserved");
        Asserts.assertEquals(1, ts.get(0).position(), "cjk pos");
    }

    private static void stopwordPositionsKept() {
        Analyzer a = Analyzers.stopword();
        Asserts.assertEquals("stopword", a.name(), "analyzer name");
        // "the cat sat in the mat": the@1 cat@2 sat@3 in@4 the@5 mat@6
        // 删除 the/in 后：cat@2 sat@3 mat@6 —— 位置 1/4/5 留空
        List<Token> ts = a.analyze("the cat sat in the mat");
        Asserts.assertEquals(List.of("cat", "sat", "mat"),
                ts.stream().map(Token::term).toList(), "stopwords removed");
        Asserts.assertEquals(List.of(2, 3, 6),
                ts.stream().map(Token::position).toList(),
                "remaining tokens KEEP original positions (gaps at 1,4,5)");

        // 开头、结尾都是停用词的情况
        List<Token> edge = a.analyze("the dog and the cat");
        Asserts.assertEquals(List.of("dog", "cat"),
                edge.stream().map(Token::term).toList(), "edge stopwords removed");
        Asserts.assertEquals(List.of(2, 5),
                edge.stream().map(Token::position).toList(), "edge gaps preserved");
    }

    private static void stopwordList() {
        StopwordAnalyzer a = (StopwordAnalyzer) Analyzers.stopword();
        Asserts.assertTrue(a.stopwords().contains("the"), "the is a stopword");
        Asserts.assertTrue(a.stopwords().contains("a"), "a is a stopword");
        Asserts.assertFalse(a.stopwords().contains("cat"), "cat is not a stopword");
    }

    private static void unknown() {
        Asserts.assertThrows(IllegalArgumentException.class,
                () -> Analyzers.byName("bogus"), "unknown analyzer must throw");
    }
}
