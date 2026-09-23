package vecsearch.tests;

import vecsearch.core.Metric;
import vecsearch.json.Json;
import vecsearch.server.VecHttpServer;
import vecsearch.testutil.Assert;
import vecsearch.testutil.TestRunner;

import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.time.Duration;
import java.util.List;
import java.util.Map;

/** 通过真实 HTTP 栈端到端验证接口：状态码、JSON 结构、过滤与删除。 */
public final class HttpTest {

    private static final HttpClient HTTP = HttpClient.newBuilder()
            .connectTimeout(Duration.ofSeconds(3))
            .build();

    private record Resp(int status, Map<String, Object> body) {
    }

    @SuppressWarnings("unchecked")
    private static Resp req(VecHttpServer srv, String method, String path, String json) throws Exception {
        HttpRequest.Builder b = HttpRequest.newBuilder()
                .uri(URI.create("http://127.0.0.1:" + srv.port() + path))
                .timeout(Duration.ofSeconds(10));
        if (json == null) {
            b.method(method, HttpRequest.BodyPublishers.noBody());
        } else {
            b.header("Content-Type", "application/json")
                    .method(method, HttpRequest.BodyPublishers.ofString(json));
        }
        HttpResponse<String> resp = HTTP.send(b.build(), HttpResponse.BodyHandlers.ofString());
        Map<String, Object> parsed = resp.body().isEmpty()
                ? Map.of() : (Map<String, Object>) Json.parse(resp.body());
        return new Resp(resp.statusCode(), parsed);
    }

    public static void register(TestRunner r) {
        r.test("HTTP end-to-end: upsert/search/delete/filter + validation codes", () -> {
            VecHttpServer srv = new VecHttpServer("127.0.0.1", 0, Metric.L2, 4);
            srv.start();
            try {
                // health + root
                Assert.eq(req(srv, "GET", "/healthz", null).status(), 200);
                Assert.eq(req(srv, "GET", "/", null).status(), 200);

                // 单条 + 批量插入
                Resp one = req(srv, "POST", "/v1/vectors",
                        "{\"id\":\"v0\",\"vector\":[0,0],\"filter\":{\"g\":\"a\"}}");
                Assert.eq(one.status(), 200);
                Assert.eq(one.body().get("count"), 1.0);

                Resp batch = req(srv, "POST", "/v1/vectors",
                        "{\"vectors\":["
                                + "{\"id\":\"v1\",\"vector\":[1,0],\"filter\":{\"g\":\"b\"}},"
                                + "{\"id\":\"v2\",\"vector\":[2,0],\"filter\":{\"g\":\"a\"}},"
                                + "{\"id\":\"v3\",\"vector\":[9,0],\"filter\":{\"g\":\"b\"}}]}");
                Assert.eq(batch.status(), 200);
                Assert.eq(batch.body().get("upserted"), 3.0);

                // GET 单条 / 404
                Resp got = req(srv, "GET", "/v1/vectors/v1", null);
                Assert.eq(got.status(), 200);
                Assert.eq(((List<?>) got.body().get("vector")).get(0), 1.0);
                Assert.eq(req(srv, "GET", "/v1/vectors/nope", null).status(), 404);

                // 精确检索：[0.2,0] 最近为 v0(0.2) 然后 v1(0.8)
                Resp exact = req(srv, "POST", "/v1/search/exact",
                        "{\"vector\":[0.2,0],\"k\":2}");
                Assert.eq(exact.status(), 200);
                List<?> hits = (List<?>) exact.body().get("hits");
                Assert.eq(hits.size(), 2);
                Assert.eq(((Map<?, ?>) hits.get(0)).get("id"), "v0");
                Assert.approx(
                        ((Number) ((Map<?, ?>) hits.get(0)).get("distance")).doubleValue(),
                        0.2, 1e-9);
                Assert.eq(exact.body().get("exact"), Boolean.TRUE);
                double comps = ((Number) exact.body().get("distanceComputations")).doubleValue();
                Assert.eq(comps, 4.0);

                // 近似检索（懒构建索引）
                Resp approx = req(srv, "POST", "/v1/search",
                        "{\"vector\":[0.2,0],\"k\":2,\"nprobe\":2}");
                Assert.eq(approx.status(), 200);
                Assert.eq(approx.body().get("exact"), Boolean.FALSE);
                Assert.eq(approx.body().get("nprobe"), 2.0);

                // 过滤
                Resp filtered = req(srv, "POST", "/v1/search/exact",
                        "{\"vector\":[0,0],\"k\":10,\"filter\":{\"g\":\"a\"}}");
                for (Object h : (List<?>) filtered.body().get("hits")) {
                    Assert.eq(((Map<?, ?>) ((Map<?, ?>) h).get("filter")).get("g"), "a");
                }

                // 维度错误 400
                Assert.eq(req(srv, "POST", "/v1/vectors",
                        "{\"id\":\"bad\",\"vector\":[1,2,3]}").status(), 400);
                Assert.eq(req(srv, "POST", "/v1/search/exact",
                        "{\"vector\":[1,2,3],\"k\":1}").status(), 400);
                Assert.eq(req(srv, "POST", "/v1/search/exact",
                        "{\"vector\":[1,2],\"k\":0}").status(), 400);
                Assert.eq(req(srv, "POST", "/v1/vectors", "{not json").status(), 400);
                Assert.eq(req(srv, "GET", "/v1/nope", null).status(), 404);
                Assert.eq(req(srv, "GET", "/v1/vectors", null).status(), 404);

                // 删除后再检索不到（精确 + 近似）
                Assert.eq(req(srv, "DELETE", "/v1/vectors/v1", null).status(), 200);
                Assert.eq(req(srv, "DELETE", "/v1/vectors/v1", null).status(), 404);
                Resp after = req(srv, "POST", "/v1/search",
                        "{\"vector\":[1,0],\"k\":10,\"nprobe\":10}");
                for (Object h : (List<?>) after.body().get("hits")) {
                    Assert.isTrue(!"v1".equals(((Map<?, ?>) h).get("id")), "deleted v1 leaked");
                }

                // stats
                Resp stats = req(srv, "GET", "/v1/stats", null);
                Assert.eq(stats.status(), 200);
                Assert.eq(stats.body().get("metric"), "L2");
                Assert.eq(stats.body().get("count"), 3.0);

                // rebuild
                Resp rb = req(srv, "POST", "/v1/index/rebuild",
                        "{\"nlist\":2,\"maxIters\":10,\"seed\":42}");
                Assert.eq(rb.status(), 200);
                Assert.eq(((Map<?, ?>) rb.body().get("index")).get("nlist"), 2.0);
            } finally {
                srv.stop();
            }
        });

        r.test("HTTP COSINE server rejects zero vectors with 400", () -> {
            VecHttpServer srv = new VecHttpServer("127.0.0.1", 0, Metric.COSINE, 2);
            srv.start();
            try {
                Assert.eq(req(srv, "POST", "/v1/vectors",
                        "{\"id\":\"z\",\"vector\":[0,0,0]}").status(), 400);
                req(srv, "POST", "/v1/vectors",
                        "{\"id\":\"a\",\"vector\":[1,0,0]}");
                req(srv, "POST", "/v1/vectors",
                        "{\"id\":\"b\",\"vector\":[0,1,0]}");
                Assert.eq(req(srv, "POST", "/v1/search/exact",
                        "{\"vector\":[0,0,0],\"k\":1}").status(), 400);
                Resp ok = req(srv, "POST", "/v1/search/exact",
                        "{\"vector\":[2,0,0],\"k\":1}");
                Assert.eq(ok.status(), 200);
                Assert.eq(((Map<?, ?>) ((List<?>) ok.body().get("hits")).get(0)).get("id"), "a");
            } finally {
                srv.stop();
            }
        });
    }

    private HttpTest() {
    }
}
