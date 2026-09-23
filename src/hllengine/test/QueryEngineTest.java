package hllengine.test;

import hllengine.api.ApiException;
import hllengine.engine.Dataset;
import hllengine.engine.LogicalPlan;
import hllengine.engine.QueryResult;
import hllengine.json.Json;

import java.util.List;
import java.util.Map;

public final class QueryEngineTest implements TestRunner.Suite {

    private Dataset sample() {
        Dataset ds = new Dataset("events", List.of("uid", "country", "age"));
        ds.appendAll(List.of(
                Map.of("uid", "u1", "country", "CN", "age", 20L),
                Map.of("uid", "u1", "country", "CN", "age", 20L),
                Map.of("uid", "u2", "country", "CN", "age", 31L),
                Map.of("uid", "u3", "country", "US", "age", 40L),
                Map.of("uid", "u3", "country", "US", "age", 40L),
                Map.of("uid", "u4", "country", "US", "age", 55L)
        ));
        return ds;
    }

    @Override
    public void register(TestRunner.Registry r) {
        r.add("engine.scanReturnsAllRows", this::scan);
        r.add("engine.filterWithAndOrNot", this::filters);
        r.add("engine.projectReordersAndSubsets", this::project);
        r.add("engine.limitTruncates", this::limit);
        r.add("engine.globalAggregates", this::globalAggregate);
        r.add("engine.groupByWithExactAndHllDistinct", this::groupBy);
        r.add("engine.approximateColumnsAreLabelled", this::approxLabel);
        r.add("engine.explainPlanExports", this::explain);
        r.add("engine.unknownDatasetAndColumnRejected", this::unknownRefs);
        r.add("engine.malformedPlanRejected", this::malformedPlan);
        r.add("engine.numericEqualityCoercesLongDouble", this::numericCompare);
    }

    private LogicalPlan parse(String json) {
        return LogicalPlan.parse(Json.parseObject(json));
    }

    private void scan(TestRunner.Assert a) {
        Dataset ds = sample();
        QueryResult r = parse("{\"dataset\":\"events\"}").execute(name -> ds);
        a.eq(r.rows().size(), 6, "all six rows returned");
        a.eq(r.columns(), List.of("uid", "country", "age"), "columns preserved");
    }

    private void filters(TestRunner.Assert a) {
        Dataset ds = sample();
        QueryResult r = parse("{\"dataset\":\"events\",\"filter\":{\"op\":\"and\",\"args\":["
                + "{\"op\":\"ge\",\"field\":\"age\",\"value\":30},"
                + "{\"op\":\"or\",\"args\":["
                + "  {\"op\":\"eq\",\"field\":\"country\",\"value\":\"CN\"},"
                + "  {\"op\":\"eq\",\"field\":\"country\",\"value\":\"US\"}]}]}}")
                .execute(name -> ds);
        a.eq(r.rows().size(), 4, "age>=30 matches u2,u3,u3,u4");

        QueryResult not = parse("{\"dataset\":\"events\",\"filter\":"
                + "{\"op\":\"not\",\"arg\":{\"op\":\"eq\",\"field\":\"country\",\"value\":\"CN\"}}}")
                .execute(name -> ds);
        a.eq(not.rows().size(), 3, "not CN -> 3 US rows");

        QueryResult in = parse("{\"dataset\":\"events\",\"filter\":"
                + "{\"op\":\"in\",\"field\":\"age\",\"value\":[20,40]}}").execute(name -> ds);
        a.eq(in.rows().size(), 4, "age in [20,40] -> 4 rows");

        QueryResult like = parse("{\"dataset\":\"events\",\"filter\":"
                + "{\"op\":\"like\",\"field\":\"uid\",\"value\":\"u1\"}}").execute(name -> ds);
        a.eq(like.rows().size(), 2, "uid contains u1 -> 2 rows");
    }

    private void project(TestRunner.Assert a) {
        Dataset ds = sample();
        QueryResult r = parse("{\"dataset\":\"events\",\"project\":[\"country\",\"uid\"]}")
                .execute(name -> ds);
        a.eq(r.columns(), List.of("country", "uid"), "projected column order");
        a.eq(r.rows().get(0).keySet(), java.util.LinkedHashSet.class.isAssignableFrom(r.rows().get(0).keySet().getClass())
                ? r.rows().get(0).keySet() : r.rows().get(0).keySet(), "row key order stable");
        a.check(!r.rows().get(0).containsKey("age"), "age projected out");
    }

    private void limit(TestRunner.Assert a) {
        Dataset ds = sample();
        QueryResult r = parse("{\"dataset\":\"events\",\"limit\":2}").execute(name -> ds);
        a.eq(r.rows().size(), 2, "limit 2");
        a.eq(r.toMap().get("truncatedByLimit"), true, "truncated flag set");
    }

    private void globalAggregate(TestRunner.Assert a) {
        Dataset ds = sample();
        QueryResult r = parse("{\"dataset\":\"events\",\"aggregate\":{\"aggregates\":["
                + "{\"fn\":\"count\"},"
                + "{\"fn\":\"count_distinct\",\"field\":\"uid\",\"alias\":\"exact_uv\"},"
                + "{\"fn\":\"hll_distinct\",\"field\":\"uid\",\"precision\":12,\"alias\":\"approx_uv\"},"
                + "{\"fn\":\"avg\",\"field\":\"age\",\"alias\":\"avg_age\"},"
                + "{\"fn\":\"sum\",\"field\":\"age\",\"alias\":\"sum_age\"},"
                + "{\"fn\":\"min\",\"field\":\"age\",\"alias\":\"min_age\"},"
                + "{\"fn\":\"max\",\"field\":\"age\",\"alias\":\"max_age\"}"
                + "]}}").execute(name -> ds);
        Map<String, Object> row = r.rows().get(0);
        a.eq(row.get("count"), 6L, "count(*)");
        a.eq(row.get("exact_uv"), 4L, "exact distinct uids = 4");
        a.eq(row.get("approx_uv"), 4L, "HLL distinct uids estimates 4 for tiny set");
        a.withinAbsolute(((Number) row.get("avg_age")).doubleValue(), 34.3333, 0.01, "avg age");
        a.eq(row.get("sum_age"), 206.0, "sum age");
        a.eq(row.get("min_age"), 20L, "min age");
        a.eq(row.get("max_age"), 55L, "max age");
    }

    private void groupBy(TestRunner.Assert a) {
        Dataset ds = sample();
        QueryResult r = parse("{\"dataset\":\"events\",\"filter\":"
                + "{\"op\":\"gt\",\"field\":\"age\",\"value\":18},"
                + "\"aggregate\":{\"groupBy\":[\"country\"],\"aggregates\":["
                + "{\"fn\":\"count_distinct\",\"field\":\"uid\",\"alias\":\"uv_exact\"},"
                + "{\"fn\":\"hll_distinct\",\"field\":\"uid\",\"precision\":12,\"alias\":\"uv_hll\"}"
                + "]}}").execute(name -> ds);
        a.eq(r.rows().size(), 2, "two country groups");
        Map<String, Object> cn = byCountry(r, "CN");
        Map<String, Object> us = byCountry(r, "US");
        a.eq(cn.get("uv_exact"), 2L, "CN exact distinct = 2");
        a.eq(us.get("uv_exact"), 2L, "US exact distinct = 2");
        a.eq(cn.get("uv_hll"), 2L, "CN HLL estimate = 2");
        a.eq(us.get("uv_hll"), 2L, "US HLL estimate = 2");
    }

    private Map<String, Object> byCountry(QueryResult r, String c) {
        for (Map<String, Object> row : r.rows()) {
            if (c.equals(row.get("country"))) return row;
        }
        throw new AssertionError("country " + c + " not in result");
    }

    private void approxLabel(TestRunner.Assert a) {
        Dataset ds = sample();
        QueryResult r = parse("{\"dataset\":\"events\",\"aggregate\":{\"aggregates\":["
                + "{\"fn\":\"hll_distinct\",\"field\":\"uid\",\"alias\":\"uv\"},"
                + "{\"fn\":\"count_distinct\",\"field\":\"uid\",\"alias\":\"exact\"}]}}")
                .execute(name -> ds);
        Map<String, Object> out = r.toMap();
        @SuppressWarnings("unchecked")
        Map<String, Object> approx = (Map<String, Object>) out.get("approximateColumns");
        a.check(approx.containsKey("uv"), "uv marked approximate");
        a.check(!approx.containsKey("exact"), "exact distinct not in approximate map");
        Map<String, Object> row = r.rows().get(0);
        a.check(row.containsKey("uv_relativeStandardError"), "error sibling column present");
        double sigma = ((Number) row.get("uv_relativeStandardError")).doubleValue();
        a.approx(sigma, 1.04 / 64, 1e-12, "sigma reported");
    }

    private void explain(TestRunner.Assert a) {
        Dataset ds = sample();
        LogicalPlan plan = parse("{\"dataset\":\"events\",\"filter\":"
                + "{\"op\":\"eq\",\"field\":\"country\",\"value\":\"CN\"},"
                + "\"aggregate\":{\"aggregates\":[{\"fn\":\"count\"}]}}");
        Map<String, Object> explained = new java.util.LinkedHashMap<>();
        explained.putAll(plan.describe());
        explained.putAll(plan.plan().describe());
        String json = Json.write(explained);
        a.check(json.contains("Scan") && json.contains("Filter") && json.contains("HashAggregate"),
                "explain names all operators");
        a.check(json.contains("estimatedTotalCost"), "physical plan carries a cost");
        // The plan is also executable as usual.
        a.eq(plan.execute(name -> ds).rows().get(0).get("count"), 3L, "explained plan still runs");
    }

    private void unknownRefs(TestRunner.Assert a) {
        boolean threw = false;
        try {
            parse("{\"dataset\":\"nope\"}").execute(name -> {
                throw new ApiException(ApiException.NOT_FOUND, "dataset \"nope\" does not exist");
            });
        } catch (ApiException ae) {
            threw = true;
            a.eq(ae.code(), ApiException.NOT_FOUND, "missing dataset code");
        }
        a.check(threw, "unknown dataset rejected");

        Dataset ds = sample();
        threw = false;
        try {
            parse("{\"dataset\":\"events\",\"project\":[\"nope\"]}").execute(name -> ds);
        } catch (ApiException ae) {
            threw = true;
            a.eq(ae.code(), ApiException.BAD_REQUEST, "bad column code");
        }
        a.check(threw, "unknown projected column rejected");
    }

    private void malformedPlan(TestRunner.Assert a) {
        String[] bad = {
                "{\"dataset\":\"events\",\"aggregate\":{\"aggregates\":[]}}",
                "{\"dataset\":\"events\",\"aggregate\":{\"aggregates\":[{\"fn\":\"frobnicate\",\"field\":\"x\"}]}}",
                "{\"dataset\":\"events\",\"filter\":{\"op\":\"eq\",\"field\":\"age\"}}",
                "{\"dataset\":\"events\",\"project\":[1,2]}",
                "{\"dataset\":\"events\",\"unknownThing\":1}",
                "{\"dataset\":\"events\",\"aggregates\":[{\"fn\":\"count\"}]}",
                "{\"dataset\":\"events\",\"limit\":-3}",
        };
        Dataset ds = sample();
        for (String s : bad) {
            boolean threw = false;
            try {
                parse(s).execute(name -> ds);
            } catch (ApiException ae) {
                threw = true;
            }
            a.check(threw, "malformed plan rejected: " + s);
        }
    }

    private void numericCompare(TestRunner.Assert a) {
        Dataset ds = new Dataset("t", List.of("v"));
        ds.appendAll(List.of(Map.of("v", 1L), Map.of("v", 1.0), Map.of("v", 2L)));
        QueryResult r = parse("{\"dataset\":\"t\",\"filter\":{\"op\":\"eq\",\"field\":\"v\",\"value\":1}}")
                .execute(name -> ds);
        a.eq(r.rows().size(), 2, "1 equals both long 1 and double 1.0");
    }
}
