package com.example.ac.corpus;

import com.example.ac.Compiled;
import com.example.ac.EmptyPatternPolicy;
import com.example.ac.Match;
import com.example.ac.Pattern;
import com.example.ac.StreamingMatcher;

import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.List;

/**
 * 按固定大小把语料切块后用 {@link StreamingMatcher} 跑一遍，
 * 并统计每块的命中数与“跨块完成”的命中数。
 *
 * <p>chunkUnit=CODEPOINT：按 code point 偏移切（块边界绝不会落在代理对中间）；
 * chunkUnit=UTF8_BYTE：按 UTF-8 字节切（边界可能落在一个多字节字符内部，
 * 由 StreamingMatcher 自行跨块拼装——用于验收字节流语义）。
 */
public final class CorpusRunner {

    public enum ChunkUnit {CODEPOINT, UTF8_BYTE}

    public record ChunkStat(int chunkIndex,
                            int inputSize,
                            int emittedCount,
                            int crossChunkCompleted) {
    }

    public record RunResult(String chunkUnit,
                            int chunkSize,
                            int chunkCount,
                            List<ChunkStat> chunks,
                            List<Match> matches,
                            int totalCrossChunk,
                            long elapsedNanos) {
    }

    private CorpusRunner() {
    }

    public static RunResult run(Compiled compiled, String text, ChunkUnit unit, int chunkSize) {
        if (chunkSize <= 0) {
            throw new IllegalArgumentException("chunkSize must be positive, got " + chunkSize);
        }
        StreamingMatcher matcher = new StreamingMatcher(compiled);
        List<Match> all = new ArrayList<>();
        List<ChunkStat> stats = new ArrayList<>();

        long start = System.nanoTime();
        int totalCross = 0;

        if (unit == ChunkUnit.CODEPOINT) {
            int[] cps = text.codePoints().toArray();
            int[] byteOffsets = byteOffsets(cps);
            int chunkIndex = 0;
            for (int begin = 0; begin < cps.length; begin += chunkSize) {
                int end = Math.min(begin + chunkSize, cps.length);
                String chunk = new String(cps, begin, end - begin);
                int before = all.size();
                matcher.feed(chunk, all);
                int[] boundary = {begin}; // 块起点 cp 偏移
                int cross = countCross(all, before, m -> m.start() < boundary[0]);
                totalCross += cross;
                stats.add(new ChunkStat(chunkIndex, end - begin, all.size() - before, cross));
                chunkIndex++;
            }
            int before = all.size();
            all.addAll(matcher.finish());
            if (all.size() > before) {
                // 尾部空模式命中归属到一个虚拟尾块，不计入跨块。
                stats.add(new ChunkStat(stats.size(), 0, all.size() - before, 0));
            }
        } else {
            byte[] bytes = text.getBytes(StandardCharsets.UTF_8);
            int chunkIndex = 0;
            for (int begin = 0; begin < bytes.length; begin += chunkSize) {
                int end = Math.min(begin + chunkSize, bytes.length);
                int before = all.size();
                matcher.feedBytes(bytes, begin, end - begin, all);
                int boundaryBytes = begin;
                int cross = countCross(all, before, m -> {
                    // 该命中在本块内完成（end 已到达），但起点字节在边界之前。
                    return utf8ByteStart(text, m) < boundaryBytes;
                });
                totalCross += cross;
                stats.add(new ChunkStat(chunkIndex, end - begin, all.size() - before, cross));
                chunkIndex++;
            }
            int before = all.size();
            all.addAll(matcher.finish());
            if (all.size() > before) {
                stats.add(new ChunkStat(stats.size(), 0, all.size() - before, 0));
            }
        }

        long elapsed = System.nanoTime() - start;
        return new RunResult(unit.name(), chunkSize, stats.size(), stats, all, totalCross, elapsed);
    }

    /** 便捷入口：从 profile 构建模式表。 */
    public static List<Pattern> patterns(CorpusProfile profile, String idPrefix) {
        List<Pattern> list = new ArrayList<>();
        for (int i = 0; i < profile.patterns().size(); i++) {
            String id = profile.patternIds().get(i);
            list.add(Pattern.of(idPrefix == null ? id : idPrefix + id, profile.patterns().get(i)));
        }
        return list;
    }

    private interface CrossTest {
        boolean spans(Match m);
    }

    private static int countCross(List<Match> all, int from, CrossTest test) {
        int c = 0;
        for (int i = from; i < all.size(); i++) {
            Match m = all.get(i);
            if (m.length() > 0 && test.spans(m)) {
                c++;
            }
        }
        return c;
    }

    /** 命中起点对应的 UTF-8 字节偏移。 */
    private static int utf8ByteStart(String text, Match m) {
        int[] cps = text.codePoints().toArray();
        int bytes = 0;
        for (int i = 0; i < m.start() && i < cps.length; i++) {
            bytes += utf8Len(cps[i]);
        }
        return bytes;
    }

    private static int[] byteOffsets(int[] cps) {
        int[] off = new int[cps.length + 1];
        int bytes = 0;
        for (int i = 0; i < cps.length; i++) {
            off[i] = bytes;
            bytes += utf8Len(cps[i]);
        }
        off[cps.length] = bytes;
        return off;
    }

    private static int utf8Len(int cp) {
        if (cp <= 0x7F) {
            return 1;
        }
        if (cp <= 0x7FF) {
            return 2;
        }
        if (cp <= 0xFFFF) {
            return 3;
        }
        return 4;
    }
}
