package phraseindex;

import java.util.List;

public final class TokenizerTest {

    public static void register(Suite s) {
        s.test("null and empty input tokenize to nothing", () -> {
            Suite.assertEquals(List.of(), Tokenizer.tokenize(null), "null");
            Suite.assertEquals(List.of(), Tokenizer.tokenize(""), "empty");
        });

        s.test("whitespace-only input (incl. runs of spaces) tokenizes to nothing", () -> {
            Suite.assertEquals(List.of(), Tokenizer.tokenize("   "), "spaces");
            Suite.assertEquals(List.of(), Tokenizer.tokenize(" \t\n  \r "), "mixed whitespace");
        });

        s.test("leading, trailing and consecutive spaces collapse", () -> {
            Suite.assertEquals(List.of("a", "b", "c"),
                    Tokenizer.tokenize("  a   b  c "), "collapse runs");
        });

        s.test("lowercasing and whitespace kinds", () -> {
            Suite.assertEquals(List.of("hello", "world", "java"),
                    Tokenizer.tokenize("Hello\tWORLD\nJava"), "lower + tab/newline");
        });

        s.test("repeated words are kept at every position", () -> {
            Suite.assertEquals(List.of("go", "go", "go"),
                    Tokenizer.tokenize("go go go"), "repeats preserved");
            Suite.assertEquals(List.of("ha", "ha"),
                    Tokenizer.tokenize("ha   ha"), "repeats with double spaces");
        });

        s.test("single token with surrounding spaces", () -> {
            Suite.assertEquals(List.of("x"), Tokenizer.tokenize("   x   "), "single");
        });
    }
}
