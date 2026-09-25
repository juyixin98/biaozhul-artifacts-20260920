package boolsearch;

import boolsearch.eval.Evaluator;
import boolsearch.query.Node;
import boolsearch.query.ParseException;
import boolsearch.query.Parser;
import boolsearch.server.SearchServer;

import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.SortedSet;

/**
 * 入口：
 *   serve [--port N] [--docs N] [--seed N]   启动 JSON 服务（默认 8080 端口，预载 200 篇合成文档）
 *   query "<表达式>" [--docs N] [--seed N] [--no-optimize]   本地求值一次查询
 *   demo                                     演示若干查询（含错误位置展示）
 */
public final class Main {
    public static void main(String[] args) throws Exception {
        if (args.length == 0) {
            usage();
            return;
        }
        switch (args[0]) {
            case "serve" -> serve(args);
            case "query" -> query(args);
            case "demo" -> demo();
            default -> usage();
        }
    }

    private static void usage() {
        System.out.println("""
                用法:
                  serve [--port N] [--docs N] [--seed N]
                  query "<表达式>" [--docs N] [--seed N] [--no-optimize]
                  demo
                """);
    }

    private static InvertedIndex loadCorpus(int docs, long seed) {
        InvertedIndex index = new InvertedIndex();
        Corpus.generate(docs, seed).forEach(index::addDocument);
        return index;
    }

    private static int intArg(String[] args, String name, int def) {
        for (int i = 0; i < args.length - 1; i++) {
            if (args[i].equals(name)) return Integer.parseInt(args[i + 1]);
        }
        return def;
    }

    private static long longArg(String[] args, String name, long def) {
        for (int i = 0; i < args.length - 1; i++) {
            if (args[i].equals(name)) return Long.parseLong(args[i + 1]);
        }
        return def;
    }

    private static boolean hasFlag(String[] args, String flag) {
        for (String a : args) if (a.equals(flag)) return true;
        return false;
    }

    private static void serve(String[] args) throws Exception {
        int port = intArg(args, "--port", 8080);
        int docs = intArg(args, "--docs", 200);
        long seed = longArg(args, "--seed", 42L);
        SearchServer s = new SearchServer(port);
        Corpus.generate(docs, seed).forEach(s::addDoc);
        s.start();
        System.out.printf("服务已启动: http://localhost:%d （预载 %d 篇合成文档, seed=%d）%n", s.port(), docs, seed);
        Thread.currentThread().join();
    }

    private static void query(String[] args) throws Exception {
        if (args.length < 2) { usage(); return; }
        String q = args[1];
        InvertedIndex index = loadCorpus(intArg(args, "--docs", 200), longArg(args, "--seed", 42L));
        boolean optimize = !hasFlag(args, "--no-optimize");
        runQuery(index, q, optimize);
    }

    private static void demo() throws Exception {
        InvertedIndex index = loadCorpus(50, 7L);
        String[] queries = {
                "apple AND banana",
                "apple OR cherry AND NOT grape",
                "(apple OR cherry) AND NOT (grape OR mango)",
                "NOT kiwi",
                "NOT NOT apple",
                "unknownterm",
                "apple AND",          // 解析错误：EOF 位置
                "(apple OR banana",   // 解析错误：缺少 ')'
                "apple banana",       // 解析错误：缺少运算符
        };
        for (String q : queries) {
            runQuery(index, q, true);
        }
    }

    private static void runQuery(InvertedIndex index, String q, boolean optimize) {
        System.out.println("查询: " + q);
        try {
            Node ast = Parser.parse(q);
            List<String> trace = new ArrayList<>();
            Evaluator ev = new Evaluator(index, optimize);
            ev.setTrace(trace);
            SortedSet<Integer> r = ev.eval(ast);
            System.out.println("  结果(" + r.size() + "): " + r);
            for (String t : trace) System.out.println("  计划: " + t);
        } catch (ParseException e) {
            System.out.println("  解析错误: " + e.getMessage() + " @位置 " + e.position());
            System.out.println("  " + q);
            System.out.println("  " + " ".repeat(Math.max(0, e.position())) + "^");
        }
        System.out.println();
    }
}
