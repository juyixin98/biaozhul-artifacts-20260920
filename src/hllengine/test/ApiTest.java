package hllengine.test;

import hllengine.api.ApiException;
import hllengine.json.Json;
import hllengine.server.Engine;

import java.util.List;
import java.util.Map;

public final class ApiTest implements TestRunner.Suite {

    private Engine engine() {
        return new Engine();
    }

    private Map<String, Object> req(String json) {
        return Json.parseObject(json);
    }

    @Override
    public void register(TestRunner.Registry r) {
        r.add("api.sketchLifecycleAndEstimate", this::sketchLifecycle);
        r.add("api.shardMergeViaJsonAndCompatibilityFailure", this::shardMergeViaApi);
        r.add("api.exportImportRoundTrip", this::exportImport);
        r.add("api.datasetLoadAndHllQuery", this::datasetQuery);
        r.add("api.batchStopsAtFirstError", this::batchStop);
        r.add("api.batchContinueOnError", this::batchContinue);
        r.add("api.notFoundAndAlreadyExists", this::lifecycleErrors);
        r.add("api.malformedJsonEnvelope", this::malformedJson);
        r.add("api.unknownOpRejected", this::unknownOp);
        r.add("api.badRequestShapesRejected", this::badRequests);
    }

    private void sketchLifecycle(TestRunner.Assert a) {
        Engine e = engine();
        e.handle(req("{\"op\":\"createSketch\",\"name\":\"uv\",\"precision\":12}"));
        e.handle(req("{\"op\":\"addAll\",\"sketch\":\"uv\",\"values\":[\"a\",\"a\",\"b\",\"c\",1,1.0,true,null]}"));
        Map<String, Object> est = e.handle(req("{\"op\":\"estimate\",\"sketch\":\"uv\"}"));
        // distinct: "a","b","c", 1, 1.0(long/double differ by tag), true, null = 7
        long card = ((Number) est.get("estimatedCardinality")).longValue();
        a.eq(card, 7L, "7 typed distinct values, observed 8");
        a.eq(est.get("isEstimate"), true, "estimate flagged");
        a.eq(est.get("isExact"), false, "not exact");
        a.check(est.get("nominalRelativeStandardErrorPercent") != null, "error percent surfaced");
        a.eq(est.get("observedCount"), 8L, "observed 8 inserts");
    }

    private void shardMergeViaApi(TestRunner.Assert a) {
        Engine e = engine();
        // Three shards with overlapping keys.
        for (int s = 1; s <= 3; s++) {
            StringBuilder sb = new StringBuilder("{\"op\":\"createSketch\",\"name\":\"s").append(s)
                    .append("\",\"precision\":12}");
            e.handle(req(sb.toString()));
        }
        addRange(e, "s1", 0, 1000);
        addRange(e, "s2", 500, 1500);
        addRange(e, "s3", 1000, 2000);
        e.handle(req("{\"op\":\"createSketch\",\"name\":\"tot\",\"precision\":12}"));
        Map<String, Object> merged = e.handle(req(
                "{\"op\":\"mergeSketches\",\"target\":\"tot\",\"sources\":[\"s1\",\"s2\",\"s3\"]}"));
        double approx = ((Number) merged.get("estimatedCardinalityRaw")).doubleValue();
        a.approx(approx, 2000, 0.05, "API merge union ~2000 distinct");

        // A fourth shard with different precision must be rejected.
        e.handle(req("{\"op\":\"createSketch\",\"name\":\"s4\",\"precision\":10}"));
        addRange(e, "s4", 0, 10);
        boolean threw = false;
        try {
            e.handle(req("{\"op\":\"mergeSketches\",\"target\":\"tot\",\"sources\":[\"s4\"]}"));
        } catch (ApiException ae) {
            threw = true;
            a.eq(ae.code(), ApiException.INCOMPATIBLE_CONFIG, "API merge precision mismatch");
        }
        a.check(threw, "p=10 shard rejected against p=12 target");

        // target listed as source rejected
        threw = false;
        try {
            e.handle(req("{\"op\":\"mergeSketches\",\"target\":\"s1\",\"sources\":[\"s1\"]}"));
        } catch (ApiException ae) {
            threw = true;
            a.eq(ae.code(), ApiException.BAD_REQUEST, "self-merge code");
        }
        a.check(threw, "target-as-source rejected");
    }

    private void addRange(Engine e, String sketch, long fromInclusive, long toExclusive) {
        // addAll in chunks to keep request strings reasonable.
        int chunk = 500;
        for (long start = fromInclusive; start < toExclusive; start += chunk) {
            long end = Math.min(start + chunk, toExclusive);
            StringBuilder sb = new StringBuilder("{\"op\":\"addAll\",\"sketch\":\"").append(sketch)
                    .append("\",\"values\":[");
            for (long k = start; k < end; k++) {
                if (k > start) sb.append(',');
                sb.append('"').append("u").append(k).append('"');
            }
            sb.append("]}");
            e.handle(req(sb.toString()));
        }
    }

    private void exportImport(TestRunner.Assert a) {
        Engine e = engine();
        e.handle(req("{\"op\":\"createSketch\",\"name\":\"src\",\"precision\":11,\"seed\":3}"));
        e.handle(req("{\"op\":\"addAll\",\"sketch\":\"src\",\"values\":[\"x\",\"y\",\"z\",\"x\"]}"));
        Map<String, Object> exported = e.handle(req("{\"op\":\"exportSketch\",\"sketch\":\"src\"}"));
        String envelope = Json.write(exported);

        Engine e2 = engine();
        Map<String, Object> importReq = new java.util.LinkedHashMap<>();
        importReq.put("op", "importSketch");
        importReq.put("name", "restored");
        importReq.put("sketch", Json.parse(envelope)); // whole envelope
        e2.handle(importReq);
        Map<String, Object> est = e2.handle(req("{\"op\":\"estimate\",\"sketch\":\"restored\"}"));
        a.eq(((Number) est.get("estimatedCardinality")).longValue(), 3L, "imported sketch restores 3");
        @SuppressWarnings("unchecked")
        Map<String, Object> cfg = (Map<String, Object>) est.get("config");
        a.eq(cfg.get("precision"), 11, "imported precision");
        a.eq(cfg.get("seed"), 3, "imported seed");
    }

    private void datasetQuery(TestRunner.Assert a) {
        Engine e = engine();
        e.handle(req("{\"op\":\"createDataset\",\"name\":\"events\",\"columns\":[\"uid\",\"region\"]}"));
        e.handle(req("{\"op\":\"appendRows\",\"dataset\":\"events\",\"rows\":["
                + "{\"uid\":\"a\",\"region\":\"r1\"},"
                + "{\"uid\":\"a\",\"region\":\"r1\"},"
                + "{\"uid\":\"b\",\"region\":\"r1\"},"
                + "{\"uid\":\"c\",\"region\":\"r2\"}]}"));
        Map<String, Object> explain = e.handle(req("{\"op\":\"explain\",\"plan\":{\"dataset\":\"events\","
                + "\"filter\":{\"op\":\"eq\",\"field\":\"region\",\"value\":\"r1\"},"
                + "\"aggregate\":{\"aggregates\":["
                + "{\"fn\":\"count_distinct\",\"field\":\"uid\",\"alias\":\"exact\"},"
                + "{\"fn\":\"hll_distinct\",\"field\":\"uid\",\"alias\":\"approx\"}]}}}"));
        a.check(explain.containsKey("logicalPlan"), "explain has logical plan");
        a.check(explain.containsKey("physicalPlan"), "explain has physical plan");
        a.check(!explain.containsKey("result"), "explain does not execute");

        Map<String, Object> query = e.handle(req("{\"op\":\"query\",\"plan\":{\"dataset\":\"events\","
                + "\"aggregate\":{\"groupBy\":[\"region\"],\"aggregates\":["
                + "{\"fn\":\"count_distinct\",\"field\":\"uid\",\"alias\":\"exact_uv\"},"
                + "{\"fn\":\"hll_distinct\",\"field\":\"uid\",\"precision\":12,\"alias\":\"uv\"}]}}}"));
        @SuppressWarnings("unchecked")
        Map<String, Object> result = (Map<String, Object>) query.get("result");
        @SuppressWarnings("unchecked")
        List<Map<String, Object>> rows = (List<Map<String, Object>>) result.get("rows");
        a.eq(rows.size(), 2, "two regions");
        Map<String, Object> r1 = rows.get(0).get("region").equals("r1") ? rows.get(0) : rows.get(1);
        a.eq(r1.get("exact_uv"), 2L, "r1 exact distinct = 2");
        a.eq(r1.get("uv"), 2L, "r1 HLL = 2");
    }

    private void batchStop(TestRunner.Assert a) {
        Engine e = engine();
        Map<String, Object> out = e.handle(req("{\"op\":\"batch\",\"requests\":["
                + "{\"op\":\"createSketch\",\"name\":\"b\",\"precision\":12},"
                + "{\"op\":\"addAll\",\"sketch\":\"b\",\"values\":[1,2,3]},"
                + "{\"op\":\"estimate\",\"sketch\":\"missing\"},"
                + "{\"op\":\"addAll\",\"sketch\":\"b\",\"values\":[4]}"
                + "]}"));
        a.eq(out.get("succeeded"), false, "batch reported failure");
        a.eq(out.get("attempted"), 3, "stopped after failing index 2");
        a.eq(out.get("firstFailureAtIndex"), 2, "failure located at index 2");
        @SuppressWarnings("unchecked")
        List<Map<String, Object>> results = (List<Map<String, Object>>) out.get("results");
        a.eq(results.size(), 3, "three items reported (through failure)");
        a.eq(results.get(2).get("ok"), false, "third item failed");
        @SuppressWarnings("unchecked")
        Map<String, Object> err = (Map<String, Object>) results.get(2).get("error");
        a.eq(err.get("code"), ApiException.NOT_FOUND, "error code propagated");
        // Last request never ran: still 3 observed.
        Map<String, Object> est = e.handle(req("{\"op\":\"estimate\",\"sketch\":\"b\"}"));
        a.eq(est.get("observedCount"), 3L, "post-failure request not executed");
    }

    private void batchContinue(TestRunner.Assert a) {
        Engine e = engine();
        Map<String, Object> out = e.handle(req("{\"op\":\"batch\",\"continueOnError\":true,\"requests\":["
                + "{\"op\":\"createSketch\",\"name\":\"c\",\"precision\":12},"
                + "{\"op\":\"estimate\",\"sketch\":\"ghost\"},"
                + "{\"op\":\"addAll\",\"sketch\":\"c\",\"values\":[9]}"
                + "]}"));
        a.eq(out.get("succeeded"), false, "overall failure still reported");
        a.eq(out.get("attempted"), 3, "all 3 attempted");
        a.eq(out.get("failures"), 1, "exactly one failure recorded");
        Map<String, Object> est = e.handle(req("{\"op\":\"estimate\",\"sketch\":\"c\"}"));
        a.eq(est.get("observedCount"), 1L, "request after failure did execute");
    }

    private void lifecycleErrors(TestRunner.Assert a) {
        Engine e = engine();
        e.handle(req("{\"op\":\"createSketch\",\"name\":\"x\"}"));
        boolean threw = false;
        try {
            e.handle(req("{\"op\":\"createSketch\",\"name\":\"x\"}"));
        } catch (ApiException ae) {
            threw = true;
            a.eq(ae.code(), ApiException.ALREADY_EXISTS, "duplicate sketch");
        }
        a.check(threw, "duplicate create rejected");

        threw = false;
        try {
            e.handle(req("{\"op\":\"estimate\",\"sketch\":\"nope\"}"));
        } catch (ApiException ae) {
            threw = true;
            a.eq(ae.code(), ApiException.NOT_FOUND, "missing sketch");
        }
        a.check(threw, "missing sketch rejected");
    }

    private void malformedJson(TestRunner.Assert a) {
        boolean threw = false;
        try {
            engine().handle(req("{\"notOp\":1}"));
        } catch (ApiException ae) {
            threw = true;
            a.eq(ae.code(), ApiException.BAD_FORMAT, "missing op is a bad-format request");
        }
        a.check(threw, "request without op rejected");
    }

    private void unknownOp(TestRunner.Assert a) {
        boolean threw = false;
        try {
            engine().handle(req("{\"op\":\"frobnicate\"}"));
        } catch (ApiException ae) {
            threw = true;
            a.eq(ae.code(), ApiException.BAD_REQUEST, "unknown op code");
        }
        a.check(threw, "unknown op rejected");
    }

    private void badRequests(TestRunner.Assert a) {
        Engine e = engine();
        String[] bad = {
                "{\"op\":\"createSketch\",\"name\":\"p\",\"precision\":2}",
                "{\"op\":\"createSketch\",\"name\":\"h\",\"hashId\":\"SHA256\"}",
                "{\"op\":\"add\",\"sketch\":\"p\"}",
                "{\"op\":\"addAll\",\"sketch\":\"p\",\"values\":\"notarray\"}",
                "{\"op\":\"createDataset\",\"name\":\"d\",\"columns\":[]}",
                "{\"op\":\"appendRows\",\"dataset\":\"d\",\"rows\":[{\"unknownCol\":1}]}",
                "{\"op\":\"importSketch\",\"name\":\"z\"}",
        };
        e.handle(req("{\"op\":\"createDataset\",\"name\":\"d\",\"columns\":[\"c\"]}"));
        for (String s : bad) {
            boolean threw = false;
            try {
                e.handle(req(s));
            } catch (ApiException ae) {
                threw = true;
            }
            a.check(threw, "bad request rejected: " + s);
        }
    }
}
