package com.example.phrasesearch.query;

import com.example.phrasesearch.model.Posting;

import java.util.List;

/**
 * 短语在一篇文档中的一次具体命中：短语每个词项落在哪个 posting 上。
 *
 * positions 严格递增且两两不同——重复词项也绝不能复用同一个位置。
 *
 * @param docId        命中文档内部 id
 * @param docExternalId 文档外部 id
 * @param terms        查询词项序列
 * @param postings     与 terms 一一对应的位置记录
 * @param slopUsed     实际消耗的间隙冗余 = (末位置-首位置) - (词项数-1)
 * @param crossField   是否为跨字段命中（命中跨越两个或更多字段）
 */
public record PhraseMatch(int docId,
                          String docExternalId,
                          List<String> terms,
                          List<Posting> postings,
                          int slopUsed,
                          boolean crossField) {

    public int startPosition() {
        return postings.get(0).position();
    }

    public int endPosition() {
        return postings.get(postings.size() - 1).position();
    }
}
