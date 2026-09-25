package phrase.search;

import java.util.List;

/** 一篇文档的检索结果。truncated=true 表示匹配数超过上限只返回了前若干条。 */
public record DocMatch(String docId, int matchCount, boolean truncated, List<Match> matches) {}
