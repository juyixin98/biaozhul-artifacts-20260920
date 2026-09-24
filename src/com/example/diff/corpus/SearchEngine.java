package com.example.diff.corpus;

import com.example.diff.Lines;

import java.util.ArrayList;
import java.util.Comparator;
import java.util.List;
import java.util.Locale;

/**
 * Local, dependency-free retrieval over {@link Document}s.
 *
 * <p>This is a deliberately simple local text-analysis library &mdash; it does
 * substring and per-term matching with term frequency, no external search
 * service. Every hit carries line and character positions ("with position").
 *
 * <p>Terms are lowercase alphanumeric runs. A query matches a document when it
 * contains ALL query terms (AND semantics); an exact phrase (all query terms
 * appearing consecutively in a line) adds a boost.
 */
public final class SearchEngine {

    public static final class Hit {
        public final String docId;
        public final String title;
        public final double score;
        /** Lines (1-based) containing at least one query term. */
        public final List<Integer> matchedLines;
        /** Char offsets of each term occurrence in the body. */
        public final List<Integer> matchedOffsets;
        /** Short snippets around each matched line. */
        public final List<String> snippets;

        Hit(String docId, String title, double score,
            List<Integer> matchedLines, List<Integer> matchedOffsets, List<String> snippets) {
            this.docId = docId;
            this.title = title;
            this.score = score;
            this.matchedLines = matchedLines;
            this.matchedOffsets = matchedOffsets;
            this.snippets = snippets;
        }
    }

    private final List<Document> docs;

    public SearchEngine(List<Document> docs) {
        this.docs = new ArrayList<>(docs);
    }

    public List<Document> documents() {
        return new ArrayList<>(docs);
    }

    public List<Hit> search(String query, int limit) {
        List<String> terms = tokenize(query == null ? "" : query.toLowerCase(Locale.ROOT));
        List<Hit> hits = new ArrayList<>();
        if (terms.isEmpty()) {
            return hits;
        }
        for (Document doc : docs) {
            Hit hit = score(doc, terms);
            if (hit != null) {
                hits.add(hit);
            }
        }
        hits.sort(Comparator.comparingDouble((Hit h) -> h.score).reversed()
                .thenComparing(h -> h.docId));
        if (limit > 0 && hits.size() > limit) {
            return new ArrayList<>(hits.subList(0, limit));
        }
        return hits;
    }

    private static Hit score(Document doc, List<String> terms) {
        String hayTitle = doc.title.toLowerCase(Locale.ROOT);
        String hay = doc.body.toLowerCase(Locale.ROOT);
        double score = 0;
        List<Integer> lines = new ArrayList<>();
        List<Integer> offsets = new ArrayList<>();
        List<String> snippets = new ArrayList<>();
        List<Lines.Line> bodyLines = Lines.split(doc.body);

        for (String term : terms) {
            int freq = 0;
            int at = hay.indexOf(term);
            while (at >= 0) {
                freq++;
                offsets.add(at);
                at = hay.indexOf(term, at + term.length());
            }
            int titleFreq = countOccurrences(hayTitle, term);
            if (freq + titleFreq == 0) {
                return null; // AND semantics
            }
            score += 1.0 + Math.log(freq + 1) + 2.0 * titleFreq;
        }

        // Phrase boost: consecutive terms within one line.
        String phrase = String.join(" ", terms);
        for (int li = 0; li < bodyLines.size(); li++) {
            String content = bodyLines.get(li).content().toLowerCase(Locale.ROOT);
            boolean any = false;
            boolean all = true;
            for (String term : terms) {
                if (content.contains(term)) {
                    any = true;
                } else {
                    all = false;
                }
            }
            if (any) {
                if (!lines.contains(li + 1)) {
                    lines.add(li + 1);
                    snippets.add(bodyLines.get(li).content());
                }
            }
            if (all && content.contains(phrase)) {
                score += 3.0;
            }
        }
        return new Hit(doc.id, doc.title, score, lines, offsets, snippets);
    }

    private static int countOccurrences(String hay, String term) {
        int n = 0;
        int at = hay.indexOf(term);
        while (at >= 0) {
            n++;
            at = hay.indexOf(term, at + term.length());
        }
        return n;
    }

    /** Lowercase alphanumeric runs; punctuation and line breaks are separators. */
    public static List<String> tokenize(String text) {
        List<String> out = new ArrayList<>();
        int start = -1;
        for (int i = 0; i <= text.length(); i++) {
            boolean alnum = i < text.length() && Character.isLetterOrDigit(text.charAt(i));
            if (alnum && start < 0) {
                start = i;
            } else if (!alnum && start >= 0) {
                out.add(text.substring(start, i));
                start = -1;
            }
        }
        return out;
    }
}
