package com.bm25pager;

import com.bm25pager.api.ApiServer;
import com.bm25pager.corpus.SyntheticCorpus;
import com.bm25pager.index.IndexManager;
import com.bm25pager.index.SnapshotExpiredException;
import com.bm25pager.json.Json;
import com.bm25pager.model.Document;
import com.bm25pager.search.Bm25;
import com.bm25pager.search.Hit;
import com.bm25pager.search.InvalidCursorException;
import com.bm25pager.search.SearchResult;
import com.bm25pager.search.SearchService;
import com.bm25pager.text.Tokenizer;

import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
import java.time.Duration;
import java.util.ArrayList;
import java.util.HashSet;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * 零依赖自动化测试（不使用 JUnit，直接用 main 运行）。
 *
 * 覆盖验收点：
 *   1. 固定分词规则
 *   2. BM25 手算数值核对（N=3 的小型封闭语料）
 *   3. 空文档 / 仅标点文档
 *   4. 重复词与词频饱和（单调、边际递减、长度归一化）
 *   5. 同分结果按 docId 升序
 *   6. 连续分页不漏不重（页大小 1、7、100 多组）
 *   7. 游标绑定快照：更新索引后旧游标仍翻旧页面，新旧两版本互不干扰
 *   8. 快照过期错误：compact 驱逐旧版本后，旧游标翻页得到 SNAPSHOT_EXPIRED
 *   9. 损坏游标得到 INVALID_CURSOR
 *  10. 真实 HTTP 端到端（JDK HttpClient 打正在监听的服务）
 *
 * 退出码：全部通过 0，任一失败 1。
 */
public final class Tests {

    private static int passed = 0;
    private static int failed = 0;
    private static final List<String> failures = new ArrayList<>();

    public static void main(String[] args) throws Exception {
        testTokenizer();
        testBm25HandComputed();
        testEmptyDocuments();
        testRepeatedTermSaturation();
        testTieBreakerDocId();
        testPaginationNoGapsNoDupes();
        testEmptyQueryAndUnknownTerm();
        testSnapshotIsolation();
        testSnapshotExpiry();
        testInvalidCursor();
        testSyntheticCorpusPagination();
        runHttpTests();

        System.out.println();
        System.out.println("================================================");
        System.out.println(" Test summary: " + passed + " passed, " + failed + " failed");
        System.out.println("================================================");
        for (String f : failures) {
            System.out.println("FAIL: " + f);
        }
        if (failed > 0) {
            System.exit(1);
        }
    }

    // ---------------------------------------------------------------------
    // 1. 分词
    // ---------------------------------------------------------------------

    static void testTokenizer() {
        check("tokenizer/simple",
                List.of("hello", "world"),
                Tokenizer.tokenize("Hello, WORLD!"));

        check("tokenizer/cjk-are-separators",
                List.of("search", "engine", "v2"),
                Tokenizer.tokenize("搜索 search engine 索引 v2"));

        check("tokenizer/empty", List.of(), Tokenizer.tokenize(""));
        check("tokenizer/null", List.of(), Tokenizer.tokenize(null));
        check("tokenizer/punctuation-only", List.of(), Tokenizer.tokenize("... !!! \t\n"));
        // 字母与数字同属 token 字符，只有非字母数字才切分
        check("tokenizer/digits-kept",
                List.of("token10", "is", "a1"),
                Tokenizer.tokenize("token10 is A1"));
        check("tokenizer/locale-stable",
                List.of("title"),
                Tokenizer.tokenize("TITLE"));
    }

    // ---------------------------------------------------------------------
    // 2. BM25 手算数值（N=3，封闭小语料）
    //
    //    d1 = "a b c"  (len 3)   d2 = "a a b" (len 3)   d3 = "c d" (len 2)
    //    avgdl = 8/3
    //    Lucene 风格 idf(a) = ln(1 + (3-2+0.5)/(2+0.5)) = ln(1 + 1.5/2.5) = ln(1.6)
    //    d1 对 "a"：tf=1
    //      norm = 1-0.75+0.75*3/(8/3) = 0.25+0.84375 = 1.09375
    //      denom = 1 + 1.2*1.09375 = 2.3125
    //      score = ln(1.6) * 2.2 / 2.3125
    // ---------------------------------------------------------------------

    static void testBm25HandComputed() {
        double idfA = Bm25.idf(3, 2);
        checkClose("bm25/idf-a", Math.log(1.6), idfA, 1e-12);

        double idfC = Bm25.idf(3, 2);
        checkClose("bm25/idf-c-same-df", idfA, idfC, 1e-12);

        double idfB = Bm25.idf(3, 2); // b 也在 d1,d2 中
        double idfD = Bm25.idf(3, 1); // d 只在 d3 中，idf 应更大
        check("bm25/rarer-term-higher-idf", true, idfD > idfB);

        double avgdl = 8.0 / 3.0;
        double expectedD1A = Math.log(1.6) * 2.2
                / (1.0 + Bm25.K1 * (1.0 - Bm25.B + Bm25.B * 3.0 / avgdl));
        double actualD1A = Bm25.termScore(1, 3, avgdl, idfA);
        checkClose("bm25/d1-a-hand-computed", expectedD1A, actualD1A, 1e-12);

        // d2 对 "a"：tf=2，同长度 => 必须严格高于 d1
        double d2A = Bm25.termScore(2, 3, avgdl, idfA);
        check("bm25/tf2-beats-tf1", true, d2A > actualD1A);

        // 空文档长度 0 / avgdl 边界：tf 为 0 时必须返回 0，不产生 NaN
        checkClose("bm25/zero-tf", 0.0, Bm25.termScore(0, 3, avgdl, idfA), 0.0);
        checkClose("bm25/empty-avgdl", 0.0, Bm25.termScore(1, 0, 0.0, idfA), 0.0);

        // 端到端：d2 对查询 "a" 应排第一
        IndexManager mgr = smallManager();
        SearchService svc = new SearchService(mgr);
        SearchResult r = svc.search("a", 10);
        check("bm25/e2e-ids", List.of("d2", "d1"), ids(r));
        check("bm25/e2e-finite", true,
                r.hits().stream().allMatch(h -> Double.isFinite(h.score()) && h.score() > 0));
    }

    // ---------------------------------------------------------------------
    // 3. 空文档
    // ---------------------------------------------------------------------

    static void testEmptyDocuments() {
        IndexManager mgr = new IndexManager(3);
        mgr.loadInitial(List.of(
                new Document("e1", "", Map.of()),
                new Document("e2", "   ...\n!!!\t  ", Map.of()),
                new Document("n1", "alpha beta alpha", Map.of())));

        SearchService svc = new SearchService(mgr);
        SearchResult r = svc.search("alpha", 10);
        check("empty/only-n1-matches", List.of("n1"), ids(r));

        // 空文档之间不会因为词项命中而出现 NaN/异常；未知词结果为空
        SearchResult none = svc.search("zzz", 10);
        check("empty/unknown-term-hits", 0, none.totalHits());
        check("empty/unknown-term-no-cursor", true, none.nextCursor() == null);

        // n1 长度为 3，两篇空文档长度为 0：avgdl = 3/3 = 1.0，不产生除零
        checkClose("empty/avgdl-with-empty-docs", 1.0,
                mgr.currentSnapshot().avgDocLength(), 1e-12);
    }

    // ---------------------------------------------------------------------
    // 4. 重复词：词频饱和 + 长度归一化
    // ---------------------------------------------------------------------

    static void testRepeatedTermSaturation() {
        IndexManager mgr = smallManager();
        // 加入一篇全是 x 重复的文档
        mgr.upsert(new Document("x1", "x ".repeat(50).trim(), Map.of()));
        mgr.upsert(new Document("x2", "x ".repeat(5).trim() + " filler ".repeat(45), Map.of()));
        mgr.commit();

        SearchService svc = new SearchService(mgr);
        SearchResult r = svc.search("x", 10);
        List<Hit> hits = r.hits();

        // 两篇都命中；x1（短、tf 高）必须排在前面；分数均有限
        check("repeat/both-match", List.of("x1", "x2"), ids(r));

        double s50 = hits.get(0).score();
        double s5 = hits.get(1).score();
        check("repeat/higher-tf-wins", true, s50 > s5);
        check("repeat/finite", true, Double.isFinite(s50) && Double.isFinite(s5));

        // 饱和性：在同长度控制下，50 次重复的分数不得达到 5 次重复的 10 倍。
        // 用两篇同长度（4 个 filler 凑长）直接调打分函数核对。
        IndexManager m2 = new IndexManager(2);
        m2.loadInitial(List.of(
                new Document("f5", "repeat repeat repeat repeat repeat "
                        + "f f f f f f f f f f f f f f f", Map.of()),   // len 20, tf=5
                new Document("f10", "repeat ".repeat(10).trim() + " "
                        + "f ".repeat(10).trim(), Map.of())));          // len 20, tf=10
        SearchResult rr = new SearchService(m2).search("repeat", 10);
        double score5 = byId(rr, "f5").score();
        double score10 = byId(rr, "f10").score();
        check("repeat/saturation-ratio-under-2x", true,
                score10 < score5 * 2.0,
                "score10=" + score10 + " score5=" + score5);
    }

    // ---------------------------------------------------------------------
    // 5. 同分按 docId
    // ---------------------------------------------------------------------

    static void testTieBreakerDocId() {
        IndexManager mgr = new IndexManager(2);
        mgr.loadInitial(List.of(
                new Document("zebra", "banana apple", Map.of()),
                new Document("alpha", "banana apple", Map.of()),
                new Document("middle", "banana apple", Map.of())));
        SearchResult r = new SearchService(mgr).search("banana", 10);

        // 三篇词频/长度完全一致 => idf 相同、tf 相同、长度相同 => 完全同分
        List<Hit> hits = r.hits();
        check("tie/all-three", 3, hits.size());
        check("tie/same-score", true,
                Math.abs(hits.get(0).score() - hits.get(1).score()) < 1e-15
                        && Math.abs(hits.get(1).score() - hits.get(2).score()) < 1e-15);
        check("tie/docid-ascending", List.of("alpha", "middle", "zebra"), ids(r));
    }

    // ---------------------------------------------------------------------
    // 6. 连续分页不漏不重
    // ---------------------------------------------------------------------

    static void testPaginationNoGapsNoDupes() {
        IndexManager mgr = new IndexManager(5);
        mgr.loadInitial(SyntheticCorpus.create());
        SearchService svc = new SearchService(mgr);

        for (int pageSize : new int[]{1, 2, 7, 13, 100}) {
            // 用所有文档都可能命中的查询（data 在 60 篇批量文档中；pageSize 1 制造 60+ 页）
            assertPaginationStable(svc, "data", pageSize, "data ps=" + pageSize);
        }
        // 多词查询
        assertPaginationStable(svc, "ranking data", 5, "multiword ps=5");
        // 查询词重复也不影响结果
        assertPaginationStable(svc, "data DATA Data", 5, "duplicate-query-terms ps=5");
    }

    private static void assertPaginationStable(SearchService svc, String query,
                                               int pageSize, String label) {
        SearchResult page = svc.search(query, pageSize);
        List<String> fullOrder = new ArrayList<>();
        long boundVersion = page.version();
        int pageIndex = 0;

        while (true) {
            check(label + "/version-stable", boundVersion, page.version());
            check(label + "/page-index-" + pageIndex, pageIndex, page.page());

            List<String> pageIds = ids(page);
            // 页内不重复
            check(label + "/no-dup-within-page-" + pageIndex, true,
                    pageIds.size() == new HashSet<>(pageIds).size());
            fullOrder.addAll(pageIds);

            if (!page.hasMore()) {
                check(label + "/last-page-size", true, page.nextCursor() == null);
                break;
            }
            String next = page.nextCursor();
            check(label + "/has-cursor", true, next != null && !next.isEmpty());
            page = svc.searchAfter(next);
            pageIndex++;
        }

        // 总量一致、全局不重不漏
        check(label + "/total-count", fullOrder.size(), page.totalHits());
        check(label + "/no-dup-globally", fullOrder.size(), new HashSet<>(fullOrder).size());

        // 与一次性取全量的顺序完全一致
        SearchResult all = svc.search(query, 100);
        // totalHits 可能 >100，逐页拿到的全量顺序应等于 rank 顺序；用 pageSize=100 多次补齐
        List<String> baseline = collectAll(svc, query, 100);
        check(label + "/order-matches-baseline", baseline, fullOrder);
    }

    private static List<String> collectAll(SearchService svc, String query, int ps) {
        SearchResult p = svc.search(query, ps);
        List<String> out = new ArrayList<>(ids(p));
        while (p.hasMore()) {
            p = svc.searchAfter(p.nextCursor());
            out.addAll(ids(p));
        }
        return out;
    }

    // ---------------------------------------------------------------------
    // 7. 空查询 / 未知词
    // ---------------------------------------------------------------------

    static void testEmptyQueryAndUnknownTerm() {
        IndexManager mgr = new IndexManager(2);
        mgr.loadInitial(SyntheticCorpus.create());
        SearchService svc = new SearchService(mgr);

        SearchResult a = svc.search("", 10);
        check("empty-query/total-zero", 0, a.totalHits());
        check("empty-query/no-cursor", true, a.nextCursor() == null);

        SearchResult b = svc.search("   ", 10);
        check("blank-query/total-zero", 0, b.totalHits());

        SearchResult c = svc.search("definitelynotaword", 10);
        check("unknown-term/total-zero", 0, c.totalHits());
    }

    // ---------------------------------------------------------------------
    // 8. 快照隔离：更新不影响旧游标
    // ---------------------------------------------------------------------

    static void testSnapshotIsolation() {
        IndexManager mgr = new IndexManager(5);
        mgr.loadInitial(SyntheticCorpus.create());
        SearchService svc = new SearchService(mgr);

        // v1 上取前两页（ps=10）
        SearchResult p1v1 = svc.search("data", 10);
        check("isolation/v1", 1L, p1v1.version());
        SearchResult p2v1 = svc.searchAfter(p1v1.nextCursor());
        check("isolation/p2-v1", 1L, p2v1.version());

        List<String> v1First10 = ids(p1v1);
        List<String> v1Next10 = ids(p2v1);

        // 制造 v2：插入一篇 data 词频很高的文档，再删除一篇老文档
        mgr.upsert(new Document("zz-new", "data ".repeat(20) + "ranking ranking", Map.of()));
        mgr.delete("common-0001");
        long v2 = mgr.commit();
        check("isolation/v2-number", 2L, v2);

        // 旧游标继续 -> 仍在 v1 上，页面内容字节级一致
        SearchResult p2v1Again = svc.searchAfter(p1v1.nextCursor());
        check("isolation/old-cursor-stays-v1", 1L, p2v1Again.version());
        check("isolation/old-page-unchanged", v1Next10, ids(p2v1Again));

        // 继续翻第 3 页依然是 v1 的数据（zz-new 不在，common-0001 也还在排名中）
        SearchResult p3v1 = svc.searchAfter(p2v1.nextCursor());
        check("isolation/p3-still-v1", 1L, p3v1.version());
        List<String> v1All = new ArrayList<>(v1First10);
        v1All.addAll(v1Next10);
        v1All.addAll(ids(p3v1));
        check("isolation/deleted-doc-still-in-v1", true,
                v1All.contains("common-0001"));
        check("isolation/new-doc-absent-from-v1", true, !v1All.contains("zz-new"));

        // 全新首页查询 -> v2：新文档出现且排第一，被删文档消失
        SearchResult firstV2 = svc.search("data", 10);
        check("isolation/new-query-on-v2", 2L, firstV2.version());
        check("isolation/new-doc-ranks-first-v2", "zz-new", firstV2.hits().get(0).docId());
        check("isolation/deleted-absent-v2", true,
                firstV2.hits().stream().noneMatch(h -> h.docId().equals("common-0001")));

        // v1 游标与 v2 游标可交错使用，互不干扰
        SearchResult interleaveV1 = svc.searchAfter(p2v1.nextCursor());
        SearchResult interleaveV2 = svc.searchAfter(firstV2.nextCursor());
        check("isolation/interleave-v1", 1L, interleaveV1.version());
        check("isolation/interleave-v2", 2L, interleaveV2.version());
        check("isolation/interleave-v1-content", ids(p3v1), ids(interleaveV1));
    }

    // ---------------------------------------------------------------------
    // 9. 快照过期
    // ---------------------------------------------------------------------

    static void testSnapshotExpiry() {
        IndexManager mgr = new IndexManager(5);
        mgr.loadInitial(SyntheticCorpus.create());
        SearchService svc = new SearchService(mgr);

        SearchResult first = svc.search("data", 5);
        long cursorVersion = first.version();
        String cursor = first.nextCursor();

        // 再产生 5 个新版本，把 v1 推出保留窗口
        for (int i = 0; i < 5; i++) {
            mgr.upsert(new Document("tmp-" + i, "data update " + i, Map.of()));
            mgr.commit();
        }
        check("expiry/v1-evicted", true,
                !mgr.retainedVersions().contains(cursorVersion));

        SnapshotExpiredException caught = null;
        try {
            svc.searchAfter(cursor);
        } catch (SnapshotExpiredException see) {
            caught = see;
        }
        check("expiry/throws", true, caught != null);
        if (caught != null) {
            check("expiry/requested-version", 1L, caught.requestedVersion());
            check("expiry/current-version", 6L, caught.currentVersion());
        }

        // compact 显式驱逐同样生效
        mgr.compact(1);
        check("expiry/compact-keeps-one", List.of(6L), mgr.retainedVersions());
    }

    // ---------------------------------------------------------------------
    // 10. 损坏游标
    // ---------------------------------------------------------------------

    static void testInvalidCursor() {
        IndexManager mgr = new IndexManager(2);
        mgr.loadInitial(SyntheticCorpus.create());
        SearchService svc = new SearchService(mgr);

        expectInvalidCursor(svc, null);
        expectInvalidCursor(svc, "");
        expectInvalidCursor(svc, "not-base64!!!");
        expectInvalidCursor(svc, java.util.Base64.getUrlEncoder().withoutPadding()
                .encodeToString("{\"oops\":1}".getBytes(StandardCharsets.UTF_8)));
        expectInvalidCursor(svc, java.util.Base64.getUrlEncoder().withoutPadding()
                .encodeToString("[1,2,3]".getBytes(StandardCharsets.UTF_8)));

        // 字段类型错误
        String badTypes = java.util.Base64.getUrlEncoder().withoutPadding()
                .encodeToString(("{\"v\":\"x\",\"t\":[],\"ps\":5,\"page\":0,"
                        + "\"lastScore\":0,\"lastId\":\"\"}").getBytes(StandardCharsets.UTF_8));
        expectInvalidCursor(svc, badTypes);
    }

    private static void expectInvalidCursor(SearchService svc, String token) {
        boolean threw = false;
        try {
            svc.searchAfter(token);
        } catch (InvalidCursorException expected) {
            threw = true;
        }
        check("invalid-cursor/rejected:" + String.valueOf(token), true, threw);
    }

    // ---------------------------------------------------------------------
    // 11. 合成语料上的专项断言
    // ---------------------------------------------------------------------

    static void testSyntheticCorpusPagination() {
        IndexManager mgr = new IndexManager(3);
        mgr.loadInitial(SyntheticCorpus.create());
        SearchService svc = new SearchService(mgr);

        // tie-001 / tie-002 对 banana 完全同分；tie-003 词频更高排前
        SearchResult r = svc.search("banana", 10);
        check("corpus/banana-order", List.of("tie-003", "tie-001", "tie-002"), ids(r));
        checkClose("corpus/tie-score-equal",
                byId(r, "tie-001").score(), byId(r, "tie-002").score(), 1e-15);
        check("corpus/tie003-beats-tie", true,
                byId(r, "tie-003").score() > byId(r, "tie-001").score());

        // 恰好一页时不应有 nextCursor
        SearchResult onePage = svc.search("banana", 10);
        check("corpus/single-page-no-cursor", false, onePage.hasMore());

        // pageSize 裁剪：非正值夹到下限 1，超大值夹到上限 100
        SearchResult clamped = svc.search("data", 0);
        check("corpus/pagesize-clamped-min", 1, clamped.pageSize());
        SearchResult capped = svc.search("data", 99999);
        check("corpus/pagesize-capped-max", 100, capped.pageSize());

        // 数字分词
        SearchResult num = svc.search("v2", 10);
        check("corpus/numeric-token", List.of("numeric-1"), ids(num));

        // CJK 不作为词项（"检索" 分词为空），但英文词正常
        SearchResult cjk = svc.search("engine", 10);
        check("corpus/cjk-english-part", List.of("cjk-1"), ids(cjk));

        // 空文档永远不命中
        SearchResult any = svc.search("data", 100);
        check("corpus/empty-not-ranked", true,
                any.hits().stream().noneMatch(h -> h.docId().startsWith("empty-")));
    }

    // ---------------------------------------------------------------------
    // 12. HTTP 端到端
    // ---------------------------------------------------------------------

    static void runHttpTests() throws Exception {
        IndexManager mgr = new IndexManager(3);
        mgr.loadInitial(SyntheticCorpus.create());
        ApiServer server = new ApiServer(mgr, 0);
        server.start();
        int port = server.port();
        String base = "http://127.0.0.1:" + port;

        HttpClient client = HttpClient.newBuilder()
                .connectTimeout(Duration.ofSeconds(5))
                .build();

        try {
            // health
            HttpResponse<String> health = httpGet(client, base + "/health");
            check("http/health-status", 200, health.statusCode());
            Map<String, Object> healthBody = Json.parseObject(health.body());
            check("http/health-ok", "ok", healthBody.get("status"));

            // 首页检索
            HttpResponse<String> s1 = httpPost(client, base + "/search",
                    Map.of("query", "data", "pageSize", 7));
            check("http/search-status", 200, s1.statusCode());
            Map<String, Object> s1Body = Json.parseObject(s1.body());
            check("http/search-version", 1L, ((Number) s1Body.get("version")).longValue());
            check("http/search-pagesize", 7, ((Number) s1Body.get("pageSize")).intValue());
            check("http/search-hits-is-list", true, s1Body.get("hits") instanceof List<?>);
            List<?> hits1 = (List<?>) s1Body.get("hits");
            check("http/search-page-full", 7, hits1.size());

            // 连续翻页直到取完，与全量比对
            List<String> seen = new ArrayList<>();
            for (Object h : hits1) {
                seen.add((String) ((Map<?, ?>) h).get("docId"));
            }
            String cursor = (String) s1Body.get("nextCursor");
            int pages = 1;
            while (cursor != null) {
                HttpResponse<String> sp = httpPost(client, base + "/search/continue",
                        Map.of("cursor", cursor));
                check("http/continue-status", 200, sp.statusCode());
                Map<String, Object> pb = Json.parseObject(sp.body());
                check("http/continue-version", 1L,
                        ((Number) pb.get("version")).longValue());
                for (Object h : (List<?>) pb.get("hits")) {
                    seen.add((String) ((Map<?, ?>) h).get("docId"));
                }
                cursor = (String) pb.get("nextCursor");
                pages++;
            }
            check("http/pages-walked", true, pages >= 8, "pages=" + pages);
            Map<String, Object> allBody = Json.parseObject(
                    httpPost(client, base + "/search", Map.of("query", "data", "pageSize", 100)).body());
            check("http/total-matches", ((Number) allBody.get("totalHits")).intValue(), seen.size());
            check("http/no-duplicates", seen.size(), new HashSet<>(seen).size());

            // 同分 tie 顺序通过 HTTP 也成立
            Map<String, Object> banana = Json.parseObject(
                    httpPost(client, base + "/search", Map.of("query", "banana")).body());
            List<?> bananaHits = (List<?>) banana.get("hits");
            check("http/banana-1", "tie-003", ((Map<?, ?>) bananaHits.get(0)).get("docId"));
            check("http/banana-2", "tie-001", ((Map<?, ?>) bananaHits.get(1)).get("docId"));
            check("http/banana-3", "tie-002", ((Map<?, ?>) bananaHits.get(2)).get("docId"));

            // 快照隔离（HTTP 层）：先拿 v1 游标，再 upsert 产生 v2，旧游标仍是 v1
            HttpResponse<String> v1page = httpPost(client, base + "/search",
                    Map.of("query", "data", "pageSize", 5));
            String v1cursor = (String) Json.parseObject(v1page.body()).get("nextCursor");

            HttpResponse<String> up = httpPost(client, base + "/documents/upsert",
                    mapOf("docId", "http-new", "content", "data ".repeat(15) + "ranking",
                            "metadata", Map.of("title", "HTTP new")));
            check("http/upsert-status", 200, up.statusCode());
            check("http/upsert-version", 2L,
                    ((Number) Json.parseObject(up.body()).get("version")).longValue());

            Map<String, Object> oldViaCursor = Json.parseObject(
                    httpPost(client, base + "/search/continue", Map.of("cursor", v1cursor)).body());
            check("http/old-cursor-version", 1L,
                    ((Number) oldViaCursor.get("version")).longValue());

            Map<String, Object> newFirst = Json.parseObject(
                    httpPost(client, base + "/search", Map.of("query", "data")).body());
            check("http/new-query-version", 2L,
                    ((Number) newFirst.get("version")).longValue());
            check("http/new-doc-first", "http-new",
                    ((Map<?, ?>) ((List<?>) newFirst.get("hits")).get(0)).get("docId"));

            // 删除
            HttpResponse<String> del = httpDelete(client, base + "/documents/tie-002");
            check("http/delete-status", 200, del.statusCode());
            check("http/delete-found", true,
                    (Boolean) Json.parseObject(del.body()).get("found"));
            Map<String, Object> afterDel = Json.parseObject(
                    httpPost(client, base + "/search", Map.of("query", "banana")).body());
            List<?> afterDelHits = (List<?>) afterDel.get("hits");
            check("http/delete-took-effect", 2, afterDelHits.size());

            // 快照过期：compact 到 1 份，旧 v1 游标 -> 410 SNAPSHOT_EXPIRED
            HttpResponse<String> compact = httpPost(client, base + "/admin/compact?keep=1",
                    new LinkedHashMap<>());
            check("http/compact-status", 200, compact.statusCode());

            HttpResponse<String> expired = httpPost(client, base + "/search/continue",
                    Map.of("cursor", v1cursor));
            check("http/expired-status", 410, expired.statusCode());
            Map<String, Object> expiredBody = Json.parseObject(expired.body());
            check("http/expired-code", "SNAPSHOT_EXPIRED", expiredBody.get("error"));
            check("http/expired-current-version", 3L,
                    ((Number) expiredBody.get("currentVersion")).longValue());

            // 损坏游标 -> 400 INVALID_CURSOR
            HttpResponse<String> bad = httpPost(client, base + "/search/continue",
                    Map.of("cursor", "%%%not-base64"));
            check("http/bad-cursor-status", 400, bad.statusCode());
            check("http/bad-cursor-code", "INVALID_CURSOR",
                    Json.parseObject(bad.body()).get("error"));

            // 空查询：HTTP 层允许空串（与缺字段的 400 区分），返回空结果集
            HttpResponse<String> emptyQuery = httpPost(client, base + "/search",
                    mapOf("query", ""));
            check("http/empty-query-status", 200, emptyQuery.statusCode());
            check("http/empty-query-total", 0,
                    ((Number) Json.parseObject(emptyQuery.body()).get("totalHits")).intValue());

            // 坏 JSON / 缺字段 / 404
            HttpResponse<String> badJson = httpPostRaw(client, base + "/search", "{not json");
            check("http/bad-json-status", 400, badJson.statusCode());
            HttpResponse<String> missingField = httpPost(client, base + "/search", Map.of());
            check("http/missing-field-status", 400, missingField.statusCode());
            HttpResponse<String> notFound = httpGet(client, base + "/nope");
            check("http/404-status", 404, notFound.statusCode());

            // status 端点
            Map<String, Object> status = Json.parseObject(
                    httpGet(client, base + "/admin/status").body());
            check("http/status-doc-count", true,
                    ((Number) status.get("docCount")).intValue() > 0);
        } finally {
            server.stop();
        }
    }

    // ---------------------------------------------------------------------
    // HTTP 小工具
    // ---------------------------------------------------------------------

    private static HttpResponse<String> httpGet(HttpClient client, String url) throws Exception {
        HttpRequest req = HttpRequest.newBuilder(URI.create(url)).GET().build();
        return client.send(req, HttpResponse.BodyHandlers.ofString(StandardCharsets.UTF_8));
    }

    private static HttpResponse<String> httpPost(HttpClient client, String url, Object body)
            throws Exception {
        return httpPostRaw(client, url, Json.write(body));
    }

    private static HttpResponse<String> httpPostRaw(HttpClient client, String url, String raw)
            throws Exception {
        HttpRequest req = HttpRequest.newBuilder(URI.create(url))
                .header("Content-Type", "application/json")
                .POST(HttpRequest.BodyPublishers.ofString(raw, StandardCharsets.UTF_8))
                .build();
        return client.send(req, HttpResponse.BodyHandlers.ofString(StandardCharsets.UTF_8));
    }

    private static HttpResponse<String> httpDelete(HttpClient client, String url) throws Exception {
        HttpRequest req = HttpRequest.newBuilder(URI.create(url)).DELETE().build();
        return client.send(req, HttpResponse.BodyHandlers.ofString(StandardCharsets.UTF_8));
    }

    private static Map<String, Object> mapOf(Object... kv) {
        Map<String, Object> m = new LinkedHashMap<>();
        for (int i = 0; i < kv.length; i += 2) {
            m.put((String) kv[i], kv[i + 1]);
        }
        return m;
    }

    // ---------------------------------------------------------------------
    // 测试辅助
    // ---------------------------------------------------------------------

    private static IndexManager smallManager() {
        IndexManager mgr = new IndexManager(3);
        mgr.loadInitial(List.of(
                new Document("d1", "a b c", Map.of()),
                new Document("d2", "a a b", Map.of()),
                new Document("d3", "c d", Map.of())));
        return mgr;
    }

    private static List<String> ids(SearchResult r) {
        List<String> out = new ArrayList<>(r.hits().size());
        for (Hit h : r.hits()) {
            out.add(h.docId());
        }
        return out;
    }

    private static Hit byId(SearchResult r, String docId) {
        for (Hit h : r.hits()) {
            if (h.docId().equals(docId)) {
                return h;
            }
        }
        throw new IllegalStateException("docId not found: " + docId);
    }

    private static void check(String name, Object expected, Object actual) {
        if (expected.equals(actual)) {
            passed++;
        } else {
            failed++;
            failures.add(name + " — expected=<" + expected + "> actual=<" + actual + ">");
            System.out.println("FAIL " + name + " — expected=<" + expected + "> actual=<" + actual + ">");
        }
    }

    private static void check(String name, Object expected, Object actual, String detail) {
        if (expected.equals(actual)) {
            passed++;
        } else {
            failed++;
            failures.add(name + " — " + detail);
            System.out.println("FAIL " + name + " — " + detail);
        }
    }

    private static void checkClose(String name, double expected, double actual, double eps) {
        if (Double.isFinite(actual) && Math.abs(expected - actual) <= eps) {
            passed++;
        } else {
            failed++;
            failures.add(name + " — expected=<" + expected + "> actual=<" + actual + "> eps=" + eps);
            System.out.println("FAIL " + name + " — expected=<" + expected + "> actual=<" + actual + ">");
        }
    }
}
