package com.example.ac.test;

import com.example.ac.Compiled;
import com.example.ac.EmptyPatternPolicy;
import com.example.ac.Engine;
import com.example.ac.Match;
import com.example.ac.NaiveMatcher;
import com.example.ac.Pattern;
import com.example.ac.corpus.CorpusProfile;
import com.example.ac.corpus.CorpusRunner;
import com.example.ac.corpus.SyntheticCorpus;

import java.util.List;

/** 合成语料生成 + CorpusRunner 分块统计的正确性。 */
public class CorpusTest extends TestCase {

    public CorpusTest() {
        super("synthetic-corpus");
    }

    @Override
    protected void run() {
        for (String profile : SyntheticCorpus.PROFILES) {
            CorpusProfile cp = SyntheticCorpus.generate(profile, 500, 30);
            eq(cp.patterns().size(), cp.patternIds().size(), profile + ": ids parallel to patterns");
            check(cp.text().codePointCount(0, cp.text().length()) >= 48,
                    profile + ": text has expected length");
            check(!cp.patterns().isEmpty(), profile + ": has patterns");

            List<Pattern> pats = CorpusRunner.patterns(cp, null);
            Compiled c = Engine.compile(pats, EmptyPatternPolicy.MATCH_EVERY_POSITION);

            // code point 分块与字节分块都必须与朴素全量匹配一致
            for (int size : new int[]{1, 3, 7, 13, 64, 1000}) {
                CorpusRunner.RunResult r1 = CorpusRunner.run(c, cp.text(),
                        CorpusRunner.ChunkUnit.CODEPOINT, size);
                eq(r1.matches(),
                        NaiveMatcher.match(pats, EmptyPatternPolicy.MATCH_EVERY_POSITION, cp.text()),
                        profile + " cp chunks size=" + size);

                CorpusRunner.RunResult r2 = CorpusRunner.run(c, cp.text(),
                        CorpusRunner.ChunkUnit.UTF8_BYTE, size);
                eq(r2.matches(), r1.matches(), profile + " utf8 vs cp chunks size=" + size);
            }

            // 小分块时应当真实观察到“跨块完成”的命中
            CorpusRunner.RunResult small = CorpusRunner.run(c, cp.text(),
                    CorpusRunner.ChunkUnit.CODEPOINT, 5);
            check(small.totalCrossChunk() > 0,
                    profile + ": chunk size 5 produces cross-chunk hits, got " + small.totalCrossChunk());
            CorpusRunner.RunResult oneByte = CorpusRunner.run(c, cp.text(),
                    CorpusRunner.ChunkUnit.UTF8_BYTE, 1);
            check(oneByte.totalCrossChunk() > 0 || cp.text().isEmpty(),
                    profile + ": byte-at-a-time produces cross-chunk hits");

            // 每块 emittedCount 之和 == 总命中数（尾块也计入）
            int sumEmitted = small.chunks().stream().mapToInt(CorpusRunner.ChunkStat::emittedCount).sum();
            eq(sumEmitted, small.matches().size(), profile + ": per-chunk counts sum to total");
            int sumCross = small.chunks().stream().mapToInt(CorpusRunner.ChunkStat::crossChunkCompleted).sum();
            eq(sumCross, small.totalCrossChunk(), profile + ": per-chunk cross counts sum to total");

            // 确定性：同样参数两次生成一致
            CorpusProfile again = SyntheticCorpus.generate(profile, 500, 30);
            eq(again.text(), cp.text(), profile + ": deterministic text");
            eq(again.patterns(), cp.patterns(), profile + ": deterministic patterns");
        }

        // 非法 profile
        boolean threw = false;
        try {
            SyntheticCorpus.generate("nope", 10, 10);
        } catch (IllegalArgumentException e) {
            threw = true;
        }
        check(threw, "unknown profile rejected");

        // 非法 chunkSize
        boolean threw2 = false;
        try {
            CorpusProfile cp = SyntheticCorpus.generate("dna", 50, 5);
            CorpusRunner.run(Engine.compile(CorpusRunner.patterns(cp, null),
                    EmptyPatternPolicy.MATCH_EVERY_POSITION), cp.text(),
                    CorpusRunner.ChunkUnit.CODEPOINT, 0);
        } catch (IllegalArgumentException e) {
            threw2 = true;
        }
        check(threw2, "chunkSize 0 rejected");
    }
}
