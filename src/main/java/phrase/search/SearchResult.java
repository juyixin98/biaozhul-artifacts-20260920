package phrase.search;

import java.util.List;

/** 一次检索的完整结果。 */
public record SearchResult(String analyzer, int slop, String field,
                           List<String> queryTerms, List<DocMatch> hits,
                           int totalHits, boolean truncated) {}
