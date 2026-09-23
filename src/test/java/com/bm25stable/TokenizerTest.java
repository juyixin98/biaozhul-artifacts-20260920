package com.bm25stable;

import com.bm25stable.TestFramework.TestCase;

import java.util.List;

import static com.bm25stable.TestFramework.assertEquals;
import static com.bm25stable.TestFramework.assertTrue;

/** 分词规则测试：小写化、[a-z0-9]+ 切分、空文本。 */
public final class TokenizerTest {

    @TestCase
    static void emptyAndNullTextYieldNoTokens() {
        assertEquals(List.of(), Tokenizer.tokenize(""), "empty string");
        assertEquals(List.of(), Tokenizer.tokenize(null), "null string");
        assertEquals(List.of(), Tokenizer.tokenize("   "), "whitespace only");
    }

    @TestCase
    static void punctuationOnlyYieldsNoTokens() {
        assertEquals(List.of(), Tokenizer.tokenize("!!! ??? ..."), "punctuation only");
    }

    @TestCase
    static void lowercasesAndSplitsOnNonAlphanumeric() {
        assertEquals(List.of("hello", "world"), Tokenizer.tokenize("Hello, World!"), "basic split");
        assertEquals(List.of("apple", "apple", "apple"),
                Tokenizer.tokenize("Apple APPLE aPpLe"), "case folding");
        assertEquals(List.of("java", "21", "release", "2023"),
                Tokenizer.tokenize("Java 21 release 2023"), "digits are token chars");
        assertEquals(List.of("a", "b", "c"), Tokenizer.tokenize("a--b__c"), "dashes and underscores split");
    }

    @TestCase
    static void repeatedTokensArePreserved() {
        assertEquals(List.of("x", "x", "x"), Tokenizer.tokenize("x x x"), "repeats preserved");
        assertTrue(Tokenizer.tokenize("one two two").size() == 3, "duplicate token count");
    }
}
