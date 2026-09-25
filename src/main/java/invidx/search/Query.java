package invidx.search;

import invidx.analyzer.Tokenizer;

import java.util.Arrays;
import java.util.List;

/**
 * Supported boolean retrieval queries.
 *
 * <ul>
 *   <li>{@link Term} — documents containing one term</li>
 *   <li>{@link Boolean} — AND / OR over several terms, evaluated against a
 *       single document revision</li>
 * </ul>
 */
public sealed interface Query permits Query.Term, Query.Boolean {

    static Query term(String term) {
        return new Term(normalize(term));
    }

    /** Parse {@code "a b c"} / {@code "AND:a,b"} / {@code "OR:a,b"}. */
    static Query parse(String spec) {
        String s = spec.strip();
        Operator op = null;
        String body = s;
        if (s.regionMatches(true, 0, "AND:", 0, 4)) {
            op = Operator.AND;
            body = s.substring(4);
        } else if (s.regionMatches(true, 0, "OR:", 0, 3)) {
            op = Operator.OR;
            body = s.substring(3);
        }
        List<String> rawTerms;
        if (op == null) {
            rawTerms = Tokenizer.tokenize(body);
        } else {
            rawTerms = Arrays.stream(body.split("[,\n]"))
                    .map(String::strip)
                    .filter(t -> !t.isEmpty())
                    .flatMap(t -> Tokenizer.tokenize(t).stream())
                    .toList();
        }
        List<String> terms = rawTerms.stream().distinct().toList();
        if (terms.isEmpty()) {
            throw new IllegalArgumentException("empty query: " + spec);
        }
        if (terms.size() == 1 && op == null) {
            return new Term(terms.get(0));
        }
        return new Boolean(op == null ? Operator.AND : op, terms);
    }

    private static String normalize(String term) {
        List<String> toks = Tokenizer.tokenize(term);
        if (toks.size() != 1) {
            throw new IllegalArgumentException("not a single term: " + term);
        }
        return toks.get(0);
    }

    enum Operator {AND, OR}

    record Term(String term) implements Query {
    }

    record Boolean(Operator op, List<String> terms) implements Query {
        public Boolean {
            if (terms.isEmpty()) {
                throw new IllegalArgumentException("boolean query needs terms");
            }
            terms = List.copyOf(terms);
        }
    }
}
