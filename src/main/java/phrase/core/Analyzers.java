package phrase.core;

import java.util.Set;

/** 分析器工厂与内置停用词表。 */
public final class Analyzers {

    /** 固定的内置停用词表（纯本地，无外部资源）。 */
    public static final Set<String> DEFAULT_STOPWORDS =
            Set.of("a", "an", "the", "of", "is", "and", "in", "to", "on", "for");

    private Analyzers() {}

    public static Analyzer standard() {
        return new StandardAnalyzer();
    }

    public static Analyzer stopword() {
        return new StopwordAnalyzer(new StandardAnalyzer(), DEFAULT_STOPWORDS);
    }

    /** 按名称获取分析器，未知名称抛 IllegalArgumentException（API 层映射为 400）。 */
    public static Analyzer byName(String name) {
        if (name == null || name.equals("standard")) {
            return standard();
        }
        if (name.equals("stopword")) {
            return stopword();
        }
        throw new IllegalArgumentException("unknown analyzer: " + name
                + " (supported: standard, stopword)");
    }

    public static Set<String> stopwordSet() {
        return DEFAULT_STOPWORDS;
    }
}
