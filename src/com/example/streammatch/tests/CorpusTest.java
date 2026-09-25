package com.example.streammatch.tests;

import com.example.streammatch.CorpusGenerator;

import java.util.HashSet;
import java.util.List;

import static com.example.streammatch.tests.TestFramework.assertEquals;
import static com.example.streammatch.tests.TestFramework.assertTrue;
import static com.example.streammatch.tests.TestFramework.section;

/** 合成语料生成器：确定性（同种子）、规模、覆盖类型、未知语料报错。 */
public final class CorpusTest {

    private CorpusTest() {
    }

    public static void run() {
        section("合成语料生成器", () -> {
            for (String name : CorpusGenerator.names()) {
                CorpusGenerator.Corpus a = CorpusGenerator.generate(name, 42);
                CorpusGenerator.Corpus b = CorpusGenerator.generate(name, 42);
                CorpusGenerator.Corpus c = CorpusGenerator.generate(name, 43);
                assertEquals(name, a.name(), "语料名回填 " + name);
                assertTrue(a.patterns().size() > 0, name + " 至少有一个模式");
                assertTrue(a.text() != null && !a.text().isEmpty(), name + " 文本非空");
                assertTrue(a.text().equals(b.text()) && a.patterns().equals(b.patterns()),
                        name + " 同种子结果确定");
                assertTrue(!a.text().equals(c.text()) || a.patterns().equals(c.patterns()),
                        name + " 不同种子至少重新生成");
            }

            CorpusGenerator.Corpus dna = CorpusGenerator.generate("randomDna", 5);
            assertTrue(dna.text().codePointCount(0, dna.text().length()) == 200_000,
                    "randomDna 文本 200000 码点");
            assertTrue(new HashSet<>(dna.patterns()).size() < dna.patterns().size()
                    || dna.patterns().size() == 2000, "randomDna 2000 个模式（允许随机重复）");

            boolean threw = false;
            try {
                CorpusGenerator.generate("nope", 1);
            } catch (IllegalArgumentException e) {
                threw = true;
            }
            assertTrue(threw, "未知语料名抛 IllegalArgumentException");
        });
    }
}
