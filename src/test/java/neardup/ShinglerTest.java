package neardup;

import neardup.core.Shingler;

import java.util.List;
import java.util.Set;

import static neardup.TestRunner.approx;
import static neardup.TestRunner.check;

final class ShinglerTest {

    static void run() {
        TestRunner.group("shingler");

        Shingler sh = new Shingler(); // k=3

        // Lowercasing + punctuation splitting; 4 consecutive word tokens ->
        // two 3-shingles.
        Set<Long> s1 = sh.shingle("The quick brown fox.");
        check(s1.size() == 2, "4 words produce 2 shingles", "size=" + s1.size());

        Set<Long> s2 = sh.shingle("the QUICK brown FOX!");
        check(s1.equals(s2), "case-insensitive normalization");

        // Shingles never cross sentence boundaries.
        Set<Long> s3 = sh.shingle("the quick brown. fox jumps high.");
        check(s3.size() == 2, "no cross-sentence windows");
        check(!s3.equals(s1), "boundary changes the windows");

        // Lines are also boundaries.
        Set<Long> s4 = sh.shingle("a b c\na b c");
        check(s4.size() == 1, "duplicate shingles across lines dedupe to one set",
                "size=" + s4.size());

        // Segments shorter than k contribute nothing.
        check(sh.shingle("a b").isEmpty(), "2 tokens -> empty shingle set");
        check(sh.shingle("a b c d").size() == 2, "4 tokens -> 2 shingles");

        // CJK: single-character tokens; 3 CJK chars in a sentence -> 1 shingle.
        Set<Long> z1 = sh.shingle("今天气。");
        check(z1.size() == 1, "3 CJK chars -> 1 shingle", "size=" + z1.size());
        check(sh.shingle("你好").isEmpty(), "2 CJK chars -> empty shingle set");
        check(sh.shingle("今天天气").size() == 2, "4 CJK chars -> 2 shingles");

        // Reordering identical sentences keeps the shingle SET identical.
        String p = "a b c. d e f.";
        String q = "d e f. a b c.";
        check(sh.shingle(p).equals(sh.shingle(q)),
                "sentence reorder preserves shingle set");

        // Determinism
        check(sh.shingle("The quick brown fox.").equals(sh.shingle("The quick brown fox.")),
                "deterministic output");

        // Empty / null
        check(sh.shingle("").isEmpty(), "empty text -> empty set");
        check(sh.shingle(null).isEmpty(), "null text -> empty set");

        // k=1: every token is a shingle.
        Shingler sh1 = new Shingler(1);
        check(sh1.shingle("a b c").size() == 3, "k=1 -> one shingle per token");
    }
}
