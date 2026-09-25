package com.example.ac.server;

import com.example.ac.Compiled;
import com.example.ac.EmptyPatternPolicy;
import com.example.ac.Match;
import com.example.ac.Pattern;
import com.example.ac.corpus.CorpusProfile;
import com.example.ac.corpus.CorpusRunner;
import com.example.ac.corpus.SyntheticCorpus;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/** 把领域对象转成 JSON 友好的 Map，集中在此以便服务端与测试复用。 */
final class JsonViews {

    private JsonViews() {
    }

    static Map<String, Object> matchView(Match m) {
        Map<String, Object> j = new LinkedHashMap<>();
        j.put("patternId", m.patternId());
        j.put("start", m.start());
        j.put("end", m.end());
        j.put("charStart", m.charStart());
        j.put("charEnd", m.charEnd());
        j.put("patternIndex", m.patternIndex());
        j.put("matched", m.matched());
        return j;
    }

    static List<Map<String, Object>> matchViews(List<Match> matches) {
        List<Map<String, Object>> out = new ArrayList<>(matches.size());
        for (Match m : matches) {
            out.add(matchView(m));
        }
        return out;
    }

    static Map<String, Object> compiledView(Compiled c) {
        Map<String, Object> j = new LinkedHashMap<>();
        j.put("patternCount", c.patterns().size());
        j.put("nonEmptyCount", c.nonEmpty().size());
        j.put("emptyPatternIndices", c.emptyIndices());
        j.put("skippedIndices", c.skippedIndices());
        j.put("emptyPatternPolicy", c.policy().name());
        return j;
    }

    static Map<String, Object> runView(CorpusRunner.RunResult r) {
        Map<String, Object> j = new LinkedHashMap<>();
        j.put("chunkUnit", r.chunkUnit());
        j.put("chunkSize", r.chunkSize());
        j.put("chunkCount", r.chunkCount());
        List<Map<String, Object>> chunks = new ArrayList<>();
        for (CorpusRunner.ChunkStat cs : r.chunks()) {
            Map<String, Object> cj = new LinkedHashMap<>();
            cj.put("chunkIndex", cs.chunkIndex());
            cj.put("inputSize", cs.inputSize());
            cj.put("emittedCount", cs.emittedCount());
            cj.put("crossChunkCompleted", cs.crossChunkCompleted());
            chunks.add(cj);
        }
        j.put("chunks", chunks);
        j.put("matchCount", r.matches().size());
        j.put("totalCrossChunk", r.totalCrossChunk());
        j.put("elapsedNanos", r.elapsedNanos());
        return j;
    }

    static Map<String, Object> corpusView(CorpusProfile p, boolean includeText) {
        Map<String, Object> j = new LinkedHashMap<>();
        j.put("profile", p.name());
        j.put("codePointLength", p.text().codePointCount(0, p.text().length()));
        j.put("utf16Length", p.text().length());
        j.put("utf8Length", p.text().getBytes(java.nio.charset.StandardCharsets.UTF_8).length);
        j.put("patternCount", p.patterns().size());
        List<Map<String, Object>> pats = new ArrayList<>();
        for (int i = 0; i < p.patterns().size(); i++) {
            Map<String, Object> pj = new LinkedHashMap<>();
            pj.put("index", i);
            pj.put("id", p.patternIds().get(i));
            pj.put("literal", p.patterns().get(i));
            pats.add(pj);
        }
        j.put("patterns", pats);
        if (includeText) {
            j.put("text", p.text());
        }
        return j;
    }
}
