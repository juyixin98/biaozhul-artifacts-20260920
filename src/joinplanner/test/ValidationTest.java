package joinplanner.test;

import joinplanner.core.BadRequestException;
import joinplanner.core.DisconnectedGraphException;
import joinplanner.core.JoinPlanner;
import joinplanner.core.ProblemParser;
import joinplanner.json.JsonParser;

/** Input validation, disconnected graphs and other abnormal situations. */
public final class ValidationTest {

    public static void run(TestRunner r) {
        r.suite("Bad requests -> 400");

        expectBad(r, "{}", "missing tables");
        expectBad(r, "{\"tables\":[]}", "empty tables");
        expectBad(r, "{\"tables\":[{\"name\":\"a\",\"rows\":1}]"
                + repeatNineTables(), "9 tables rejected");
        expectBad(r, "{\"tables\":[{\"name\":\"a\",\"rows\":-3}]}", "negative rows");
        expectBad(r, "{\"tables\":[{\"name\":\"a\",\"rows\":1},"
                + "{\"name\":\"a\",\"rows\":2}]}", "duplicate names");
        expectBad(r, "{\"tables\":[{\"name\":\"a\"},{\"name\":\"b\"}]}", "missing rows");
        expectBad(r, "{\"tables\":[{\"name\":\"a\",\"rows\":1},{\"name\":\"b\",\"rows\":1}],"
                + "\"edges\":[{\"leftTable\":\"a\",\"leftColumn\":\"x\","
                + "\"rightTable\":\"a\",\"rightColumn\":\"y\"}]}", "self join");
        expectBad(r, "{\"tables\":[{\"name\":\"a\",\"rows\":1},{\"name\":\"b\",\"rows\":1}],"
                + "\"edges\":[{\"leftTable\":\"a\",\"leftColumn\":\"x\","
                + "\"rightTable\":\"z\",\"rightColumn\":\"y\"}]}", "unknown table");
        expectBad(r, "{\"tables\":[{\"name\":\"a\",\"rows\":1},{\"name\":\"b\",\"rows\":1}],"
                + "\"edges\":[{\"leftTable\":\"a\",\"leftColumn\":\"x\","
                + "\"rightTable\":\"b\",\"rightColumn\":\"y\",\"selectivity\":0}]}",
                "selectivity 0");
        expectBad(r, "{\"tables\":[{\"name\":\"a\",\"rows\":1},{\"name\":\"b\",\"rows\":1}],"
                + "\"edges\":[{\"leftTable\":\"a\",\"leftColumn\":\"x\","
                + "\"rightTable\":\"b\",\"rightColumn\":\"y\",\"selectivity\":2}]}",
                "selectivity > 1");
        expectBad(r, "{\"tables\":[{\"name\":\"a\",\"rows\":1},{\"name\":\"b\",\"rows\":1},"
                + "{\"name\":\"c\",\"rows\":1}],"
                + "\"edges\":[{\"leftTable\":\"a\",\"leftColumn\":\"x\","
                + "\"rightTable\":\"b\",\"rightColumn\":\"y\"},"
                + "{\"leftTable\":\"a\",\"leftColumn\":\"p\","
                + "\"rightTable\":\"b\",\"rightColumn\":\"q\"}]}", "duplicate edge pair");
        expectBad(r, "{not json", "malformed JSON");

        r.suite("Disconnected graph -> 422 / components / cross-product option");
        {
            String json = "{\"tables\":["
                    + "{\"name\":\"a\",\"rows\":100},{\"name\":\"b\",\"rows\":200},"
                    + "{\"name\":\"c\",\"rows\":50},{\"name\":\"d\",\"rows\":60}],"
                    + "\"edges\":[{\"leftTable\":\"a\",\"leftColumn\":\"x\","
                    + "\"rightTable\":\"b\",\"rightColumn\":\"y\"},"
                    + "{\"leftTable\":\"c\",\"leftColumn\":\"x\","
                    + "\"rightTable\":\"d\",\"rightColumn\":\"y\"}]}";
            var p = TestBase.parse(json);
            boolean threw = false;
            try {
                new JoinPlanner(p).plan();
            } catch (DisconnectedGraphException e) {
                threw = true;
                r.checkEq("two components reported", e.components().size(), 2);
            }
            r.check("disconnected rejected without flag", threw);

            String crossJson = "{\"tables\":["
                    + "{\"name\":\"a\",\"rows\":100},{\"name\":\"b\",\"rows\":200},"
                    + "{\"name\":\"c\",\"rows\":50},{\"name\":\"d\",\"rows\":60}],"
                    + "\"allowCrossProducts\":true,"
                    + "\"edges\":[{\"leftTable\":\"a\",\"leftColumn\":\"x\","
                    + "\"rightTable\":\"b\",\"rightColumn\":\"y\"},"
                    + "{\"leftTable\":\"c\",\"leftColumn\":\"x\","
                    + "\"rightTable\":\"d\",\"rightColumn\":\"y\"}]}";
            var p3 = TestBase.parse(crossJson);
            var dp = new JoinPlanner(p3).plan();
            r.checkEq("cross product plan covers all tables",
                    dp.joinOrderNames().size(), 4);
            // No NDV supplied => keys assumed unique:
            // a-b join: 100*200*(1/max(100,200)) = 100
            // c-d join: 50*60*(1/max(50,60))     = 50
            // cross product of the two:          = 5000 (selectivity 1)
            r.checkClose("cross product final cardinality",
                    dp.root().outputRows(), 100.0 * 50.0, 1e-9);
        }

        r.suite("Selectivity resolution & missing-statistic warnings");
        {
            String json = "{\"tables\":["
                    + "{\"name\":\"a\",\"rows\":1000},"
                    + "{\"name\":\"b\",\"rows\":200}],"
                    + "\"edges\":[{\"leftTable\":\"a\",\"leftColumn\":\"x\","
                    + "\"rightTable\":\"b\",\"rightColumn\":\"y\",\"ndvLeft\":500}]}";
            var p = TestBase.parse(json);
            var e = p.resolvedEdges().get(0);
            // ndvLeft=500 given, right assumed unique ndv=200 => sel 1/500
            r.checkClose("one-sided NDV => other assumed unique", e.selectivity(), 1.0 / 500, 1e-12);
            r.check("assumption warning emitted",
                    p.warnings().stream().anyMatch(w -> w.contains("assumed unique")));

            String both = "{\"tables\":[{\"name\":\"a\",\"rows\":1000},"
                    + "{\"name\":\"b\",\"rows\":200}],\"edges\":["
                    + "{\"leftTable\":\"a\",\"leftColumn\":\"x\","
                    + "\"rightTable\":\"b\",\"rightColumn\":\"y\","
                    + "\"ndvLeft\":500,\"ndvRight\":40}]}";
            var pb = TestBase.parse(both);
            r.checkClose("two-sided NDV 1/max", pb.resolvedEdges().get(0).selectivity(),
                    1.0 / 500, 1e-12);

            String def = "{\"tables\":[{\"name\":\"a\",\"rows\":1000},"
                    + "{\"name\":\"b\",\"rows\":200}],\"defaultSelectivity\":0.1,\"edges\":["
                    + "{\"leftTable\":\"a\",\"leftColumn\":\"x\","
                    + "\"rightTable\":\"b\",\"rightColumn\":\"y\"}]}";
            var pd = TestBase.parse(def);
            r.checkClose("default selectivity used",
                    pd.resolvedEdges().get(0).selectivity(), 0.1, 1e-12);

            String uniq = "{\"tables\":[{\"name\":\"a\",\"rows\":1000},"
                    + "{\"name\":\"b\",\"rows\":200}],\"edges\":["
                    + "{\"leftTable\":\"a\",\"leftColumn\":\"x\","
                    + "\"rightTable\":\"b\",\"rightColumn\":\"y\"}]}";
            var pu = TestBase.parse(uniq);
            r.checkClose("both assumed unique: 1/max(card)",
                    pu.resolvedEdges().get(0).selectivity(), 1.0 / 1000, 1e-12);
        }
    }

    private static String repeatNineTables() {
        StringBuilder sb = new StringBuilder();
        for (int i = 0; i < 8; i++) {
            sb.append(",{\"name\":\"x").append(i).append("\",\"rows\":1}");
        }
        return sb + "]";
    }

    private static void expectBad(TestRunner r, String json, String name) {
        boolean bad = false;
        try {
            new ProblemParser().parse(JsonParser.parse(json));
            new JoinPlanner(new ProblemParser().parse(JsonParser.parse(json))).plan();
        } catch (BadRequestException | joinplanner.json.JsonParseException e) {
            bad = true;
        } catch (DisconnectedGraphException e) {
            bad = true; // not expected in these cases but counts as rejected
        }
        r.check(name, bad);
    }
}
