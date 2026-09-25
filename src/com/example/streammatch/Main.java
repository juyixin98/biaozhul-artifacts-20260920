package com.example.streammatch;

import com.example.streammatch.json.Json;
import com.example.streammatch.server.JsonHttpService;
import com.sun.net.httpserver.HttpServer;

import java.util.ArrayList;
import java.util.List;
import java.util.Map;

/**
 * 命令行入口。
 *
 * <pre>
 *   java com.example.streammatch.Main                # 内置演示 + 自检
 *   java com.example.streammatch.Main demo           # 同上
 *   java com.example.streammatch.Main server [port]  # 启动 JSON HTTP 服务（默认 8080）
 * </pre>
 */
public final class Main {

    private Main() {
    }

    public static void main(String[] args) throws Exception {
        String cmd = args.length == 0 ? "demo" : args[0];
        switch (cmd) {
            case "demo" -> runDemo();
            case "server" -> {
                int port = args.length > 1 ? Integer.parseInt(args[1]) : 8080;
                HttpServer server = JsonHttpService.startServer(port);
                System.out.println("JSON 服务已启动: http://localhost:" + port);
                System.out.println("  GET  /health");
                System.out.println("  GET  /corpora");
                System.out.println("  POST /corpus     样例: curl -X POST 'http://localhost:"
                        + port + "/corpus?name=overlap' -d '{}'");
                System.out.println("  POST /match");
                System.out.println("  POST /match/stream");
                System.out.println("按 Ctrl+C 停止。");
                Thread.currentThread().join();
            }
            default -> {
                System.err.println("未知命令: " + cmd + "（可用: demo, server）");
                System.exit(2);
            }
        }
    }

    /** 命令行演示：一次性匹配、分块匹配、跨字节块匹配均与朴素结果核对。 */
    static void runDemo() {
        int failures = 0;

        // 1) 经典重叠：ushers + he/she/his/hers，含重复 "he"
        List<String> patterns = List.of("he", "she", "his", "hers", "he");
        String text = "ushers";
        failures += check("经典重叠 ushers", text, patterns, EmptyPatternPolicy.SKIP, 1, 1);

        // 2) 共享前缀
        List<String> pref = List.of("pr3fix", "pr3fix/0000/000042", "pr3", "x");
        failures += check("共享前缀", "pr3fix/0000/000042 pr3fix", pref,
                EmptyPatternPolicy.SKIP, 1, 1);

        // 3) Unicode：星面层 emoji 跨越码点/字节边界
        List<String> uni = List.of("😀", "日本語", "a");
        failures += check("Unicode emoji+CJK", "x😀日本語ay", uni,
                EmptyPatternPolicy.SKIP, 1, 2); // 2 字节切分

        // 4) 空模式三种策略
        failures += checkEmptyPolicies("空模式策略", "abc", List.of("a", ""));

        // 5) 合成语料 + 多种分块
        for (String name : CorpusGenerator.names()) {
            CorpusGenerator.Corpus c = CorpusGenerator.generate(name, 7);
            failures += check("语料 " + name + " (码点块 1)", c.text(), c.patterns(),
                    EmptyPatternPolicy.SKIP, 1, 1);
            failures += check("语料 " + name + " (码点块 3)", c.text(), c.patterns(),
                    EmptyPatternPolicy.SKIP, 3, 1);
            failures += check("语料 " + name + " (字节块 1)", c.text(), c.patterns(),
                    EmptyPatternPolicy.SKIP, 1, 1, true);
            failures += check("语料 " + name + " (字节块 4)", c.text(), c.patterns(),
                    EmptyPatternPolicy.SKIP, 4, 4, true);
        }

        // 6) 打印一个最经典样例的 JSON 响应片段
        Map<String, Object> sample = JsonHttpService.handleMatch(Map.of(
                "text", "ushers",
                "patterns", patterns,
                "compareWithNaive", true));
        System.out.println("\n样例 /match 响应:");
        System.out.println(Json.writePretty(sample));

        if (failures == 0) {
            System.out.println("演示自检全部通过 ✔");
        } else {
            System.out.println(failures + " 项自检未通过 [FAIL]");
            System.exit(1);
        }
    }

    /**
     * 对同一文本执行：一次性 AC、码点分块 AC、（可选）UTF-8 字节分块 AC，
     * 全部与朴素匹配比较。
     */
    private static int check(String label, String text, List<String> patterns,
                             EmptyPatternPolicy policy, int chunkCp, int chunkB) {
        return check(label, text, patterns, policy, chunkCp, chunkB, false);
    }

    private static int check(String label, String text, List<String> patterns,
                             EmptyPatternPolicy policy, int chunkCp, int chunkB,
                             boolean byteMode) {
        AhoCorasick ac = new AhoCorasick(patterns, policy);
        List<Match> naive = NaiveMatcher.match(text, patterns, policy);

        List<Match> oneShot = ac.matchAll(text);
        List<Match> streamed = streamByCodePoints(ac, text, chunkCp);
        boolean ok = eq(oneShot, naive) && eq(streamed, naive);

        if (byteMode) {
            AhoCorasick ac2 = new AhoCorasick(patterns, policy);
            List<Match> byteStreamed = streamByBytes(ac2, text, chunkB);
            if (!eq(byteStreamed, naive)) {
                ok = false;
            }
        }

        System.out.printf("[%s] %-32s 命中=%-7d 朴素=%-7d 码点块=%-3d%s%n",
                ok ? "PASS" : "FAIL", label, oneShot.size(), naive.size(), chunkCp,
                byteMode ? " 字节块=" + chunkB : "");
        return ok ? 0 : 1;
    }

    private static int checkEmptyPolicies(String label, String text, List<String> patterns) {
        int fails = 0;
        for (EmptyPatternPolicy p : EmptyPatternPolicy.values()) {
            AhoCorasick ac = new AhoCorasick(patterns, p);
            List<Match> naive = NaiveMatcher.match(text, patterns, p);
            List<Match> streamed = streamByCodePoints(ac, text, 1);
            boolean ok = eq(streamed, naive);
            // 空流边界：空文本应有 n+1 概念下的 1 个空命中位置（BEFORE/AFTER）
            List<Match> emptyStream = new AhoCorasick(patterns, p).matchAll("");
            List<Match> emptyNaive = NaiveMatcher.match("", patterns, p);
            boolean emptyOk = eq(emptyStream, emptyNaive);
            System.out.printf("[%s] %-32s policy=%-6s 非空流命中=%-4d 空流命中=%d%n",
                    (ok && emptyOk) ? "PASS" : "FAIL", label, p, streamed.size(),
                    emptyStream.size());
            if (!ok || !emptyOk) {
                fails++;
            }
        }
        return fails;
    }

    static List<Match> streamByCodePoints(AhoCorasick ac, String text, int chunk) {
        StreamMatcher sm = new StreamMatcher(ac);
        List<Match> all = new ArrayList<>();
        int total = text.codePointCount(0, text.length());
        int jStart = 0;
        for (int s = 0; s < total; s += chunk) {
            int e = Math.min(total, s + chunk);
            int jEnd = text.offsetByCodePoints(jStart, e - s);
            sm.feedChars(text.substring(jStart, jEnd));
            all.addAll(sm.drainMatches());
            jStart = jEnd;
        }
        all.addAll(sm.finish());
        return all;
    }

    static List<Match> streamByBytes(AhoCorasick ac, String text, int chunk) {
        StreamMatcher sm = new StreamMatcher(ac);
        List<Match> all = new ArrayList<>();
        byte[] bytes = text.getBytes(java.nio.charset.StandardCharsets.UTF_8);
        for (int off = 0; off < bytes.length; off += chunk) {
            sm.feedBytes(bytes, off, Math.min(chunk, bytes.length - off), true);
            all.addAll(sm.drainMatches());
        }
        all.addAll(sm.finish());
        return all;
    }

    private static boolean eq(List<Match> a, List<Match> b) {
        return JsonHttpService.canonicalEquals(a, b);
    }
}
