package joinplanner.test;

import java.util.ArrayList;
import java.util.List;

import joinplanner.core.JoinPlanner;
import joinplanner.core.OrderEnumerator;
import joinplanner.core.ProblemParser;
import joinplanner.core.ValidatedProblem;
import joinplanner.json.JsonParser;
import joinplanner.model.EdgeSpec;
import joinplanner.model.ProblemSpec;
import joinplanner.model.TableSpec;

/** Shared builders for tests. */
final class TestBase {

    private TestBase() {
    }

    static ValidatedProblem parse(String json) {
        return new ProblemParser().parse(JsonParser.parse(json));
    }

    static double dpCost(ValidatedProblem p) {
        return new JoinPlanner(p).plan().totalCost();
    }

    static double bruteForceBushyCost(ValidatedProblem p) {
        JoinPlanner planner = new JoinPlanner(p);
        planner.plan();
        double[] r = new OrderEnumerator(p, planner).bestBushyBruteForce();
        return joinplanner.core.Stats.fromLog(r[0]);
    }

    static double bestLegalLeftDeepCost(ValidatedProblem p) {
        JoinPlanner planner = new JoinPlanner(p);
        planner.plan();
        return new OrderEnumerator(p, planner).enumerateLeftDeep().stream()
                .filter(OrderEnumerator.LeftDeepOrder::legal)
                .mapToDouble(OrderEnumerator.LeftDeepOrder::cost)
                .min().orElseThrow();
    }

    /** Programmatic builder: tables named t0.. with given cardinalities. */
    static ProblemSpecBuilder builder(double... cards) {
        return new ProblemSpecBuilder(cards);
    }

    static final class ProblemSpecBuilder {
        final List<TableSpec> tables = new ArrayList<>();
        final List<EdgeSpec> edges = new ArrayList<>();
        Double defaultSel;
        boolean cross;

        ProblemSpecBuilder(double... cards) {
            for (int i = 0; i < cards.length; i++) {
                tables.add(new TableSpec("t" + i, cards[i], false, null));
            }
        }

        ProblemSpecBuilder unique(int... idx) {
            for (int i : idx) {
                tables.set(i, new TableSpec(tables.get(i).name(), tables.get(i).rows(),
                        true, null));
            }
            return this;
        }

        /** Edge with explicit selectivity. */
        ProblemSpecBuilder edge(int l, int r, double sel) {
            edges.add(new EdgeSpec("t" + l, "c", "t" + r, "c", sel, null, null));
            return this;
        }

        boolean hasEdge(int l, int r) {
            String a = "t" + Math.min(l, r);
            String b = "t" + Math.max(l, r);
            return edges.stream().anyMatch(e ->
                    (e.leftTable().equals(a) && e.rightTable().equals(b))
                            || (e.leftTable().equals(b) && e.rightTable().equals(a)));
        }

        /** Edge with NDV hints. */
        ProblemSpecBuilder edgeNdv(int l, int r, Long ndvL, Long ndvR) {
            edges.add(new EdgeSpec("t" + l, "c", "t" + r, "c", null, ndvL, ndvR));
            return this;
        }

        ProblemSpecBuilder defaultSel(double s) {
            this.defaultSel = s;
            return this;
        }

        ProblemSpecBuilder allowCross() {
            this.cross = true;
            return this;
        }

        ValidatedProblem build() {
            ProblemSpec spec = new ProblemSpec(List.copyOf(tables), List.copyOf(edges),
                    defaultSel, cross);
            return new ProblemParser().parse(toJson(spec));
        }

        private Object toJson(ProblemSpec spec) {
            StringBuilder sb = new StringBuilder("{\"tables\":[");
            for (int i = 0; i < spec.tables().size(); i++) {
                if (i > 0) {
                    sb.append(',');
                }
                TableSpec t = spec.tables().get(i);
                sb.append("{\"name\":\"").append(t.name()).append("\",\"rows\":")
                        .append(fmt(t.rows())).append("}");
            }
            sb.append("],\"edges\":[");
            for (int i = 0; i < spec.edges().size(); i++) {
                if (i > 0) {
                    sb.append(',');
                }
                EdgeSpec e = spec.edges().get(i);
                sb.append("{\"leftTable\":\"").append(e.leftTable())
                        .append("\",\"leftColumn\":\"c\",\"rightTable\":\"")
                        .append(e.rightTable()).append("\",\"rightColumn\":\"c\"");
                if (e.selectivity() != null) {
                    sb.append(",\"selectivity\":").append(fmt(e.selectivity()));
                }
                if (e.ndvLeft() != null) {
                    sb.append(",\"ndvLeft\":").append(e.ndvLeft());
                }
                if (e.ndvRight() != null) {
                    sb.append(",\"ndvRight\":").append(e.ndvRight());
                }
                sb.append('}');
            }
            sb.append(']');
            if (defaultSel != null) {
                sb.append(",\"defaultSelectivity\":").append(fmt(defaultSel));
            }
            if (cross) {
                sb.append(",\"allowCrossProducts\":true");
            }
            sb.append('}');
            return JsonParser.parse(sb.toString());
        }

        private static String fmt(double d) {
            if (d == Math.rint(d) && d < 1e18) {
                return Long.toString((long) d);
            }
            return Double.toString(d);
        }
    }
}
