package phrase.core;

import java.util.List;

/**
 * 分析器：把一段文本切成带位置的词项序列。
 * 位置从 1 开始，在同一次分析内连续递增。
 */
public interface Analyzer {

    /** 分析器名称，用于 JSON API 的 "analyzer" 字段。 */
    String name();

    /** 对文本分词，返回按位置升序排列的词项。 */
    List<Token> analyze(String text);
}
