package invidx.corpus;

import org.junit.jupiter.api.Test;

import java.util.List;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertTrue;

class CorpusGeneratorTest {

    @Test
    void deterministicAcrossInstances() {
        List<String> a = new CorpusGenerator(7).generate(50);
        List<String> b = new CorpusGenerator(7).generate(50);
        assertEquals(a, b);
    }

    @Test
    void everyFifthDocumentSharesTopicTerm() {
        List<String> docs = new CorpusGenerator(1).generate(100);
        long hits = docs.stream()
                .filter(t -> t.toLowerCase().contains(CorpusGenerator.guaranteedTopicTerm(0)))
                .count();
        assertTrue(hits >= 5, "expected a non-trivial posting list, got " + hits);
    }
}
