package com.example.segindex;

import java.util.ArrayList;
import java.util.List;
import java.util.Random;

/** Deterministic synthetic corpus: no external data files needed. */
final class Corpus {

    static final String[] VOCABULARY = {
            "apple", "banana", "cherry", "dragon", "eagle", "forest", "grape", "harbor",
            "island", "jungle", "kite", "lemon", "mountain", "night", "ocean", "panda",
            "quiet", "river", "stone", "tiger", "umbrella", "valley", "wolf", "yellow",
            "zebra", "cloud", "storm", "ember", "frost", "meadow"
    };

    private Corpus() {
    }

    /** Builds a document of {@code wordCount} random words from the vocabulary. */
    static String randomText(Random random, int wordCount) {
        StringBuilder sb = new StringBuilder();
        for (int i = 0; i < wordCount; i++) {
            if (i > 0) {
                sb.append(' ');
            }
            sb.append(VOCABULARY[random.nextInt(VOCABULARY.length)]);
        }
        return sb.toString();
    }

    /** Ground truth: ids of live documents whose tokenized text contains the term. */
    static List<String> scanContaining(java.util.Map<String, String> liveDocs, String term) {
        String normalized = Tokenizer.normalizeTerm(term);
        List<String> matches = new ArrayList<>();
        liveDocs.forEach((id, text) -> {
            if (Tokenizer.tokenize(text).contains(normalized)) {
                matches.add(id);
            }
        });
        matches.sort(String::compareTo);
        return matches;
    }
}
