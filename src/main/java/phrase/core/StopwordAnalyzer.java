package phrase.core;

import java.util.ArrayList;
import java.util.List;
import java.util.Set;

/**
 * 停用词分析器：在委托分析器（本项目为 {@link StandardAnalyzer}）结果上删除停用词，
 * <b>但保留其余词项的原始位置编号</b>。
 *
 * <p>例如文本 "the cat sat" 经标准分析得到 the@1, cat@2, sat@3，
 * 经本分析器得到 cat@2, sat@3 —— 停用词 the 不出现，位置 1 形成空位（gap）。
 * 这样短语匹配中的 slop 才能正确表达“允许跨过几个词（含被删掉的停用词）”。
 */
public final class StopwordAnalyzer implements Analyzer {

    private final Analyzer delegate;
    private final Set<String> stopwords;

    public StopwordAnalyzer(Analyzer delegate, Set<String> stopwords) {
        this.delegate = delegate;
        this.stopwords = Set.copyOf(stopwords);
    }

    @Override
    public String name() {
        return "stopword";
    }

    public Set<String> stopwords() {
        return stopwords;
    }

    @Override
    public List<Token> analyze(String text) {
        List<Token> base = delegate.analyze(text);
        List<Token> out = new ArrayList<>();
        for (Token t : base) {
            if (!stopwords.contains(t.term())) {
                out.add(t);
            }
        }
        return out;
    }
}
