package phraseindex;

import java.util.List;

/** Boolean query AST. Precedence: OR &lt; AND &lt; NOT &lt; atom (phrase / parenthesized group). */
public sealed interface Query
        permits Query.Phrase, Query.And, Query.Or, Query.Not {

    /** Exact phrase; terms are already normalized via {@link Tokenizer}. */
    record Phrase(List<String> terms) implements Query {
    }

    record And(Query left, Query right) implements Query {
    }

    record Or(Query left, Query right) implements Query {
    }

    record Not(Query child) implements Query {
    }
}
