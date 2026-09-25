package com.example.phrasesearch.service;

import com.example.phrasesearch.analyze.Analyzer;
import com.example.phrasesearch.analyze.StopGapAnalyzer;
import com.example.phrasesearch.index.Index;
import com.example.phrasesearch.model.GlobalToken;
import com.example.phrasesearch.model.IndexedDoc;
import com.example.phrasesearch.model.Posting;
import com.example.phrasesearch.model.Token;
import com.example.phrasesearch.query.PhraseMatch;
import com.example.phrasesearch.query.PhraseQuery;
import com.example.phrasesearch.search.Searcher;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * 应用服务层：把 HTTP/JSON 层和检索内核隔开。
 * 负责把查询原文过分析器得到词项序列、执行检索、组装结构化响应。
 */
public final class PhraseService {

    private final Index index;
    private final Searcher searcher;

    public PhraseService(Index index) {
        this.index = index;
        this.searcher = new Searcher();
    }

    public Index index() {
        return index;
    }

    /** 把查询原文用索引的分析器切成词项（分析与索引同链，保证 term 能对上倒排表）。 */
    public List<String> analyzeQuery(String rawQuery) {
        List<Token> tokens = index.analyzer().analyze("_query", rawQuery).tokens();
        return tokens.stream().map(Token::term).toList();
    }

    public SearchResult search(String rawQuery, int slop, String field) {
        List<String> terms = analyzeQuery(rawQuery);
        if (terms.isEmpty()) {
            return new SearchResult(rawQuery, List.of(), slop, field, true, List.of(), 0, 0);
        }
        PhraseQuery q = new PhraseQuery(terms, slop, field, true, rawQuery);
        List<PhraseMatch> matches = searcher.search(index, q);
        List<Searcher.DocMatches> groups = Searcher.groupByDoc(matches);
        return new SearchResult(rawQuery, terms, slop, field, false, groups,
                groups.size(), matches.size());
    }

    /** 组装 /search 响应（Map/List 结构，交给 Json 序列化）。 */
    public Map<String, Object> buildSearchResponse(SearchResult result, long elapsedMs) {
        Map<String, Object> root = new LinkedHashMap<>();
        root.put("query", result.rawQuery());
        root.put("analyzer", index.analyzer().name());
        root.put("terms", result.terms());
        root.put("slop", result.slop());
        root.put("field", result.field() == null ? "_all_cross_field" : result.field());
        root.put("matchSemantics",
                "ordered positions p0<p1<...; slopUsed=(pLast-pFirst)-(nTerms-1) <= slop; "
                        + "duplicate terms require distinct positions");
        root.put("emptyQuery", result.emptyQuery());
        root.put("totalDocsMatched", result.totalDocs());
        root.put("totalOccurrences", result.totalOccurrences());
        root.put("elapsedMs", elapsedMs);

        List<Object> docs = new ArrayList<>();
        for (Searcher.DocMatches g : result.groups()) {
            Map<String, Object> d = new LinkedHashMap<>();
            d.put("docId", g.docExternalId());
            d.put("occurrences", g.matches().size());

            List<Object> matchList = new ArrayList<>();
            for (PhraseMatch m : g.matches()) {
                Map<String, Object> match = new LinkedHashMap<>();
                match.put("slopUsed", m.slopUsed());
                match.put("crossField", m.crossField());

                List<Object> positions = new ArrayList<>();
                for (int i = 0; i < m.terms().size(); i++) {
                    Posting p = m.postings().get(i);
                    Map<String, Object> loc = new LinkedHashMap<>();
                    loc.put("term", m.terms().get(i));
                    loc.put("field", p.field());
                    loc.put("globalPosition", p.position());
                    loc.put("positionInField", p.localPosition());
                    loc.put("startOffset", p.startOffset());
                    loc.put("endOffset", p.endOffset());
                    positions.add(loc);
                }
                match.put("spanStart", m.startPosition());
                match.put("spanEnd", m.endPosition());
                match.put("positions", positions);
                matchList.add(match);
            }
            d.put("matches", matchList);
            docs.add(d);
        }
        root.put("documents", docs);
        return root;
    }

    /** /docs：列出索引文档的全局 token 流（便于核对位置，也是排障接口）。 */
    public Map<String, Object> buildDocsResponse() {
        Map<String, Object> root = new LinkedHashMap<>();
        root.put("analyzer", index.analyzer().name());
        root.put("docCount", index.size());
        List<Object> docs = new ArrayList<>();
        for (IndexedDoc idoc : index.allDocs()) {
            Map<String, Object> d = new LinkedHashMap<>();
            d.put("docId", idoc.externalId());
            d.put("fieldOrder", idoc.fieldOrder());
            List<Object> toks = new ArrayList<>();
            for (GlobalToken t : idoc.globalTokens()) {
                Map<String, Object> loc = new LinkedHashMap<>();
                loc.put("term", t.term());
                loc.put("globalPosition", t.globalPosition());
                loc.put("field", t.field());
                loc.put("positionInField", t.localPosition());
                toks.add(loc);
            }
            d.put("tokens", toks);
            docs.add(d);
        }
        root.put("documents", docs);
        return root;
    }

    /** /config：把分析器、停用词表、slop 语义等固定决策显式暴露出来。 */
    public Map<String, Object> buildConfigResponse() {
        Map<String, Object> root = new LinkedHashMap<>();
        Analyzer a = index.analyzer();
        root.put("analyzer", a.name());
        if (a instanceof StopGapAnalyzer sga) {
            root.put("stopwordMode", "removed_but_position_gap_preserved");
            root.put("stopwords", new ArrayList<>(sga.stopwords().stream().sorted().toList()));
        } else {
            root.put("stopwordMode", "kept_stopwords_occupy_positions");
            root.put("stopwords", new ArrayList<>(StopGapAnalyzer.DEFAULT_STOPWORDS
                    .stream().sorted().toList()));
            root.put("stopwordNote", "Default analyzer does not remove any token; the listed "
                    + "words are indexed normally and occupy positions like every other term.");
        }
        root.put("slopSemantics",
                "Fixed, in-order. For term positions p0<p1<...<p(n-1): "
                        + "slopUsed=(p(n-1)-p0)-(n-1); match iff slopUsed<=slop. "
                        + "slop=0 means adjacent exact phrase. Reordered/overlapping terms never match.");
        root.put("crossFieldSemantics",
                "When field is null/omitted, each document's fields are concatenated in declaration "
                        + "order into one global position stream with no extra gap at field boundaries.");
        root.put("duplicateTermSemantics",
                "Positions are strictly increasing, so repeated terms (e.g. \"that that\") are "
                        + "required to land on two distinct occurrences; one position can never be reused.");
        return root;
    }

    /** /analyze：在线查看一段文本的分析结果（词项、位置、偏移）。 */
    public Map<String, Object> buildAnalyzeResponse(String text) {
        Map<String, Object> root = new LinkedHashMap<>();
        root.put("analyzer", index.analyzer().name());
        root.put("text", text);
        var af = index.analyzer().analyze("_inline", text);
        List<Object> tokens = new ArrayList<>();
        for (Token t : af.tokens()) {
            Map<String, Object> m = new LinkedHashMap<>();
            m.put("term", t.term());
            m.put("position", t.position());
            m.put("startOffset", t.startOffset());
            m.put("endOffset", t.endOffset());
            tokens.add(m);
        }
        root.put("tokens", tokens);
        root.put("positionCount", af.positionCount());
        return root;
    }

    /** 检索结果值对象。 */
    public record SearchResult(String rawQuery,
                               List<String> terms,
                               int slop,
                               String field,
                               boolean emptyQuery,
                               List<Searcher.DocMatches> groups,
                               int totalDocs,
                               int totalOccurrences) {
    }
}
