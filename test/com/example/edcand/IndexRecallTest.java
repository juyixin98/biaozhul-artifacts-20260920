package com.example.edcand;

import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.Random;

/**
 * 验收核心：随机小词表上，候选索引结果与“全扫描 + 精确 Levenshtein”逐项对拍。
 *
 * <p>断言：
 * <ul>
 *   <li><b>零漏报</b>：全扫描得到的每个距离 &le; k 的词项，必须出现在索引结果中；</li>
 *   <li><b>零误报</b>：索引输出的每个匹配，距离确实 &le; k（最终精算确认）；</li>
 *   <li>距离值与全扫描完全一致；</li>
 *   <li>候选数 &le; 词表规模（筛选确实在起作用）。</li>
 * </ul>
 * 覆盖：空串、组合字符（NFC/NFD）、emoji 增补平面、长公共前缀家族。
 */
public final class IndexRecallTest {

    private IndexRecallTest() {
    }

    public static void register(TestRunner r) {
        randomSmallVocabAscii(r);
        randomSmallVocabUnicode(r);
        emptyStringQueryAndTerm(r);
        combiningCharsNfcNfd(r);
        longCommonPrefix(r);
        thresholdZero(r);
        noisyCorpusVsBruteForce(r);
    }

    /** 随机 ASCII 小词表 × 随机查询 × 多个阈值，对拍全扫描。 */
    private static void randomSmallVocabAscii(TestRunner r) {
        r.add("recall: random small ASCII vocab vs brute-force (zero miss/false-positive)", t -> {
            Random rnd = new Random(20260924);
            String alpha = "abcdefghijklmnopqrstuvwxyz";
            int totalChecks = 0;
            for (int vocabRound = 0; vocabRound < 300; vocabRound++) {
                int vocabSize = 1 + rnd.nextInt(20);
                int maxLen = 6;
                List<String> vocab = new ArrayList<>();
                for (int i = 0; i < vocabSize; i++) {
                    vocab.add(randWord(rnd, alpha, rnd.nextInt(maxLen + 1)));
                }
                CandidateIndex idx = CandidateIndex.build(vocab, TextNormalization.NONE);
                for (int qRound = 0; qRound < 6; qRound++) {
                    String query = randWord(rnd, alpha, rnd.nextInt(maxLen + 2));
                    for (int k = 0; k <= 3; k++) {
                        assertAgainstBruteForce(t, idx, vocab, query, k, TextNormalization.NONE);
                        totalChecks++;
                    }
                }
            }
            System.out.printf("    (ascii rounds: %d query/threshold combos)%n", totalChecks);
        });
    }

    /** 随机 Unicode 小词表：码点池含 emoji、组合重音、CJK，保证按码点而非字节计数。 */
    private static void randomSmallVocabUnicode(TestRunner r) {
        r.add("recall: random Unicode vocab (emoji/combining/CJK) vs brute-force", t -> {
            Random rnd = new Random(777);
            int[] pool = {'a', 'e', 'i', '日', '本', '語', 0x00E9 /* é */,
                    0x0301 /* combining acute */, 0x1F600, 0x1F601, 0x2764};
            int totalChecks = 0;
            for (int vocabRound = 0; vocabRound < 200; vocabRound++) {
                int vocabSize = 1 + rnd.nextInt(12);
                List<String> vocab = new ArrayList<>();
                for (int i = 0; i < vocabSize; i++) {
                    vocab.add(randCpWord(rnd, pool, rnd.nextInt(6)));
                }
                CandidateIndex idx = CandidateIndex.build(vocab, TextNormalization.NONE);
                for (int qRound = 0; qRound < 5; qRound++) {
                    String query = randCpWord(rnd, pool, rnd.nextInt(6));
                    for (int k = 0; k <= 2; k++) {
                        assertAgainstBruteForce(t, idx, vocab, query, k, TextNormalization.NONE);
                        totalChecks++;
                    }
                }
            }
            System.out.printf("    (unicode rounds: %d query/threshold combos)%n", totalChecks);
        });
    }

    /** 空串查询与空串词项：距离等于对方长度。 */
    private static void emptyStringQueryAndTerm(TestRunner r) {
        r.add("recall: empty string query and empty term", t -> {
            List<String> vocab = List.of("", "a", "ab", "abc", "é");
            CandidateIndex idx = CandidateIndex.build(vocab, TextNormalization.NONE);
            assertAgainstBruteForce(t, idx, vocab, "", 0, TextNormalization.NONE);
            assertAgainstBruteForce(t, idx, vocab, "", 1, TextNormalization.NONE);
            assertAgainstBruteForce(t, idx, vocab, "", 3, TextNormalization.NONE);
            assertAgainstBruteForce(t, idx, vocab, "ab", 0, TextNormalization.NONE);
            assertAgainstBruteForce(t, idx, vocab, "x", 2, TextNormalization.NONE);

            SearchOutcome out = idx.search("", 1);
            boolean foundEmpty = out.matches().stream()
                    .anyMatch(m -> m.term().isEmpty() && m.distance() == 0);
            t.check(foundEmpty, "empty query must match empty term at distance 0");
        });
    }

    /** 组合字符：NFC/NFD 表示的等价与差异。 */
    private static void combiningCharsNfcNfd(TestRunner r) {
        r.add("recall: combining chars under NFC vs NFD", t -> {
            List<String> vocab = List.of("café", "café", "cafe", "cafés");
            // NFC 索引：两种表示规范化后相同，去重后只保留一个，查询 NFD 串距离应为 0
            CandidateIndex nfc = CandidateIndex.build(vocab, TextNormalization.NFC);
            SearchOutcome o = nfc.search("café", 0);
            boolean exact = o.matches().stream()
                    .anyMatch(m -> m.term().equals("café") && m.distance() == 0);
            t.check(exact, "NFD query must match NFC café at distance 0 after NFC normalization");

            // NONE 索引：NFC 与 NFD 是不同词项，码点距离为 1
            CandidateIndex none = CandidateIndex.build(vocab, TextNormalization.NONE);
            assertAgainstBruteForce(t, none, vocab, "café", 1, TextNormalization.NONE);
            SearchOutcome raw = none.search("café", 0);
            t.eq(raw.matches().size(), 1, "raw NFC form only matches itself at k=0");
        });
    }

    /** 长公共前缀：大量共享前缀的词项，候选筛选必须精确区分后缀。 */
    private static void longCommonPrefix(TestRunner r) {
        r.add("recall: long common prefix family vs brute-force", t -> {
            List<String> vocab = CorpusGenerator.longPrefixFamily();
            CandidateIndex idx = CandidateIndex.build(vocab, TextNormalization.NONE);
            for (String q : List.of("document_section_paragraph_twon",
                    "document_section_paragraph_three",
                    "document_section_paragraph_clot",
                    "document_section_paragraph_")) {
                for (int k = 0; k <= 2; k++) {
                    assertAgainstBruteForce(t, idx, vocab, q, k, TextNormalization.NONE);
                }
            }
        });
    }

    /** k=0 时只返回与查询完全相等的词项。 */
    private static void thresholdZero(TestRunner r) {
        r.add("recall: threshold 0 is exact match only", t -> {
            List<String> vocab = List.of("abc", "abcd", "abx", "abc ");
            CandidateIndex idx = CandidateIndex.build(vocab, TextNormalization.NONE);
            SearchOutcome out = idx.search("abc", 0);
            t.eq(out.matches().size(), 1, "exactly one exact match");
            t.eq(out.matches().get(0).term(), "abc", "term equals query");
            t.eq(out.matches().get(0).distance(), 0, "distance 0");
        });
    }

    /** 合成语料全量（含噪声变体）上再做一轮全扫描对拍。 */
    private static void noisyCorpusVsBruteForce(TestRunner r) {
        r.add("recall: full synthetic corpus vs brute-force (sample queries)", t -> {
            List<String> vocab = CorpusGenerator.defaultCorpus();
            CandidateIndex idx = CandidateIndex.build(vocab, TextNormalization.NFC);
            List<String> queries = List.of("peple", "café", "Japan",
                    "document_section_paragraph_two", "😀", "", "ａbc", "sistmer",
                    "word", "devolopment", "43");
            for (String q : queries) {
                for (int k = 0; k <= 2; k++) {
                    assertAgainstBruteForce(t, idx, vocab, q, k, TextNormalization.NFC);
                }
            }
        });
    }

    // ---------- 对拍核心 ----------

    /**
     * 用规范化后的词表做全扫描基准，与索引输出逐项比对。
     */
    private static void assertAgainstBruteForce(TestRunner.TestApi t,
                                                CandidateIndex idx,
                                                List<String> rawVocab,
                                                String rawQuery,
                                                int k,
                                                TextNormalization norm) {
        // 基准：规范化 + 去重（与索引构建语义一致）后的全扫描精确距离
        java.util.LinkedHashMap<String, Integer> truth = new java.util.LinkedHashMap<>();
        for (String raw : rawVocab) {
            String term = norm.apply(raw);
            int d = Levenshtein.distance(norm.apply(rawQuery), term);
            if (d <= k) {
                truth.putIfAbsent(term, d);
            }
        }

        SearchOutcome out = idx.search(rawQuery, k);

        // 零漏报
        for (Map.Entry<String, Integer> e : truth.entrySet()) {
            SearchOutcome.Match m = out.matches().stream()
                    .filter(x -> x.term().equals(e.getKey()))
                    .findFirst().orElse(null);
            if (m == null) {
                t.fail("MISSED term='" + e.getKey() + "' query='" + norm.apply(rawQuery)
                        + "' k=" + k + " trueDist=" + e.getValue());
            }
            t.eq(m.distance(), (int) e.getValue(),
                    "distance mismatch for '" + e.getKey() + "'");
        }
        // 零误报（精算确认过；这里双保险再验一次）
        for (SearchOutcome.Match m : out.matches()) {
            int d = Levenshtein.distance(norm.apply(rawQuery), m.term());
            t.check(d <= k, "FALSE POSITIVE '" + m.term() + "' dist=" + d + " k=" + k);
            t.eq(m.distance(), d, "reported distance must equal brute-force distance");
        }
        t.eq(out.matches().size(), truth.size(), "match count vs brute-force");

        // 筛选统计自洽：通过长度门的候选不超总数；精算候选数不超长度门数量
        t.check(out.candidates() <= out.totalTerms(), "candidates <= totalTerms");
        t.check(out.passedLengthGate() <= out.totalTerms(), "lengthGate <= totalTerms");
        t.check(out.candidates() <= out.passedLengthGate(), "candidates <= lengthGate");
    }

    private static String randWord(Random rnd, String alpha, int len) {
        StringBuilder sb = new StringBuilder(len);
        for (int i = 0; i < len; i++) {
            sb.append(alpha.charAt(rnd.nextInt(alpha.length())));
        }
        return sb.toString();
    }

    private static String randCpWord(Random rnd, int[] pool, int len) {
        int[] cps = new int[len];
        for (int i = 0; i < len; i++) {
            cps[i] = pool[rnd.nextInt(pool.length)];
        }
        return CodePoints.toString(cps);
    }
}
