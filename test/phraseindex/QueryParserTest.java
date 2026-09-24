package phraseindex;

import java.util.List;
import java.util.Set;

public final class QueryParserTest {

    public static void register(Suite s) {
        s.test("bare word is a single-term phrase, normalized lowercase", () -> {
            Query q = QueryParser.parse("HELLO");
            Suite.assertEquals(new Query.Phrase(List.of("hello")), q, "bare word");
        });

        s.test("quoted repeated-word phrase keeps every term", () -> {
            Query q = QueryParser.parse("\"go go go\"");
            Suite.assertEquals(new Query.Phrase(List.of("go", "go", "go")), q, "repeated phrase");
        });

        s.test("extra whitespace inside quotes collapses away", () -> {
            Query q = QueryParser.parse("\"a    b\"");
            Suite.assertEquals(new Query.Phrase(List.of("a", "b")), q, "inner spaces");
        });

        s.test("empty quoted phrase parses to empty term list", () -> {
            Query q = QueryParser.parse("\"\"");
            Suite.assertEquals(new Query.Phrase(List.of()), q, "empty phrase");
        });

        s.test("adjacent atoms bind with implicit AND; explicit AND equivalent", () -> {
            Query implicit = QueryParser.parse("cat dog");
            Query explicit = QueryParser.parse("cat AND dog");
            Suite.assertEquals(explicit, implicit, "implicit AND");
        });

        s.test("OR has lower precedence than AND", () -> {
            Query q = QueryParser.parse("a OR b AND c");
            Query expected = new Query.Or(
                    new Query.Phrase(List.of("a")),
                    new Query.And(
                            new Query.Phrase(List.of("b")),
                            new Query.Phrase(List.of("c"))));
            Suite.assertEquals(expected, q, "precedence");
        });

        s.test("parentheses override precedence", () -> {
            Query q = QueryParser.parse("(a OR b) AND c");
            Query expected = new Query.And(
                    new Query.Or(
                            new Query.Phrase(List.of("a")),
                            new Query.Phrase(List.of("b"))),
                    new Query.Phrase(List.of("c")));
            Suite.assertEquals(expected, q, "grouping");
        });

        s.test("NOT binds tighter than AND and is case-insensitive", () -> {
            Query q = QueryParser.parse("not a and b");
            Query expected = new Query.And(
                    new Query.Not(new Query.Phrase(List.of("a"))),
                    new Query.Phrase(List.of("b")));
            Suite.assertEquals(expected, q, "not precedence");
        });

        s.test("quoted keyword is content, not an operator", () -> {
            Query q = QueryParser.parse("\"and\"");
            Suite.assertEquals(new Query.Phrase(List.of("and")), q, "quoted keyword");
        });

        s.test("unbalanced quote rejected", () -> {
            try {
                QueryParser.parse("\"unterminated");
                Suite.fail("expected QueryParseException");
            } catch (QueryParseException expected) {
                // expected
            }
        });

        s.test("missing close paren rejected", () -> {
            try {
                QueryParser.parse("(a OR b");
                Suite.fail("expected QueryParseException");
            } catch (QueryParseException expected) {
                // expected
            }
        });

        s.test("empty / whitespace-only query rejected", () -> {
            for (String bad : Set.of("", "   ", "\t\n")) {
                try {
                    QueryParser.parse(bad);
                    Suite.fail("expected exception for [" + bad + "]");
                } catch (QueryParseException expected) {
                    // expected
                }
            }
        });

        s.test("dangling operator rejected", () -> {
            try {
                QueryParser.parse("a AND");
                Suite.fail("expected QueryParseException");
            } catch (QueryParseException expected) {
                // expected
            }
        });
    }
}
