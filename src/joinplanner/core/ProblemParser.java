package joinplanner.core;

import java.util.ArrayList;
import java.util.HashMap;
import java.util.HashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;

import joinplanner.json.J;
import joinplanner.model.EdgeSpec;
import joinplanner.model.ProblemSpec;
import joinplanner.model.ResolvedEdge;
import joinplanner.model.SelectivitySource;
import joinplanner.model.TableSpec;

/**
 * Parses a request JSON into a validated problem and resolves every edge's
 * selectivity. All validation lives here; the planner assumes a clean input.
 *
 * <p>Request shape:
 * <pre>
 * {
 *   "tables": [ {"name": "t1", "rows": 1000, "unique": false, "alias": "T1"} ],
 *   "edges":  [ {"leftTable": "o", "leftColumn": "cust_id",
 *                "rightTable": "c", "rightColumn": "id",
 *                "selectivity": 0.01, "ndvLeft": 1000, "ndvRight": 100} ],
 *   "defaultSelectivity": 0.1,
 *   "allowCrossProducts": false
 * }
 * </pre>
 */
public final class ProblemParser {

    private static final int MAX_TABLES = 8;

    public ValidatedProblem parse(Object rootNode) {
        try {
            return parseInternal(rootNode);
        } catch (IllegalArgumentException e) {
            // Every malformed-field message from the JSON accessors becomes a 400.
            throw new BadRequestException(e.getMessage());
        }
    }

    private ValidatedProblem parseInternal(Object rootNode) {
        Map<String, Object> root = J.obj(rootNode, "$");
        List<Object> tablesJson = J.fieldArr(root, "tables", "$");
        List<Object> edgesJson = root.get("edges") == null
                ? List.of() : J.arr(root.get("edges"), "$.edges");
        Double defaultSel = J.optDbl(root, "defaultSelectivity");
        boolean allowCross = J.fieldBool(root, "allowCrossProducts", false);

        if (tablesJson.isEmpty()) {
            throw new BadRequestException("tables: at least one table required");
        }
        if (tablesJson.size() > MAX_TABLES) {
            throw new BadRequestException(
                    "tables: at most " + MAX_TABLES + " tables supported, got " + tablesJson.size());
        }

        List<TableSpec> tables = new ArrayList<>();
        Map<String, Integer> indexByName = new HashMap<>();
        for (int i = 0; i < tablesJson.size(); i++) {
            String path = "$.tables[" + i + "]";
            Map<String, Object> tj = J.obj(tablesJson.get(i), path);
            String name = J.str(tj, "name", path);
            if (indexByName.containsKey(name)) {
                throw new BadRequestException(path + ": duplicate table name '" + name + "'");
            }
            double rows = J.fieldDbl(tj, "rows", path);
            if (rows < 0 || !Double.isFinite(rows)) {
                throw new BadRequestException(path + ".rows: must be a finite number >= 0");
            }
            boolean unique = J.fieldBool(tj, "unique", false);
            String alias = J.optStr(tj, "alias", null);
            indexByName.put(name, i);
            tables.add(new TableSpec(name, rows, unique, alias));
        }

        if (defaultSel != null && !(defaultSel > 0 && defaultSel <= 1)) {
            throw new BadRequestException(
                    "defaultSelectivity: must be in (0, 1], got " + defaultSel);
        }

        List<EdgeSpec> edges = new ArrayList<>();
        Set<Long> seenPairs = new HashSet<>();
        for (int i = 0; i < edgesJson.size(); i++) {
            String path = "$.edges[" + i + "]";
            Map<String, Object> ej = J.obj(edgesJson.get(i), path);
            String lt = J.str(ej, "leftTable", path);
            String lc = J.str(ej, "leftColumn", path);
            String rt = J.str(ej, "rightTable", path);
            String rc = J.str(ej, "rightColumn", path);
            Integer li = indexByName.get(lt);
            Integer ri = indexByName.get(rt);
            if (li == null) {
                throw new BadRequestException(path + ".leftTable: unknown table '" + lt + "'");
            }
            if (ri == null) {
                throw new BadRequestException(path + ".rightTable: unknown table '" + rt + "'");
            }
            if (li.equals(ri)) {
                throw new BadRequestException(path + ": self-joins are not supported (" + lt + ")");
            }
            long pairKey = pairKey(Math.min(li, ri), Math.max(li, ri));
            if (!seenPairs.add(pairKey)) {
                throw new BadRequestException(
                        path + ": multiple edges between " + lt + " and " + rt
                                + " are not supported (merge predicates into one edge)");
            }
            Double sel = J.optDbl(ej, "selectivity");
            if (sel != null && !(sel > 0 && sel <= 1)) {
                throw new BadRequestException(path + ".selectivity: must be in (0, 1]");
            }
            Long ndvL = J.optLng(ej, "ndvLeft");
            Long ndvR = J.optLng(ej, "ndvRight");
            if (ndvL != null && ndvL <= 0) {
                throw new BadRequestException(path + ".ndvLeft: must be positive");
            }
            if (ndvR != null && ndvR <= 0) {
                throw new BadRequestException(path + ".ndvRight: must be positive");
            }
            edges.add(new EdgeSpec(lt, lc, rt, rc, sel, ndvL, ndvR));
        }

        ProblemSpec spec = new ProblemSpec(
                List.copyOf(tables), List.copyOf(edges), defaultSel, allowCross);
        return resolve(spec, indexByName);
    }

    private ValidatedProblem resolve(ProblemSpec spec, Map<String, Integer> indexByName) {
        List<String> warnings = new ArrayList<>();
        List<ResolvedEdge> resolved = new ArrayList<>();
        List<List<int[]>> adjacency = new ArrayList<>(spec.n());
        for (int i = 0; i < spec.n(); i++) {
            adjacency.add(new ArrayList<>());
        }

        for (int i = 0; i < spec.edges().size(); i++) {
            EdgeSpec e = spec.edges().get(i);
            int li = indexByName.get(e.leftTable());
            int ri = indexByName.get(e.rightTable());
            double cardL = spec.tables().get(li).rows();
            double cardR = spec.tables().get(ri).rows();

            double sel;
            SelectivitySource source;
            String explanation;

            if (e.selectivity() != null) {
                sel = e.selectivity();
                source = SelectivitySource.GIVEN;
                explanation = "selectivity given explicitly";
            } else {
                double effNdvL = e.ndvLeft() != null ? e.ndvLeft() : cardL;
                double effNdvR = e.ndvRight() != null ? e.ndvRight() : cardR;
                double maxNdv = Math.max(effNdvL, effNdvR);
                if (e.ndvLeft() != null && e.ndvRight() != null) {
                    sel = 1.0 / maxNdv;
                    source = SelectivitySource.DERIVED;
                    explanation = "1/max(ndvLeft=" + e.ndvLeft()
                            + ", ndvRight=" + e.ndvRight() + ")";
                } else if (spec.defaultSelectivity() != null
                        && e.ndvLeft() == null && e.ndvRight() == null) {
                    sel = spec.defaultSelectivity();
                    source = SelectivitySource.DEFAULT_FALLBACK;
                    explanation = "defaultSelectivity=" + spec.defaultSelectivity();
                } else {
                    sel = 1.0 / maxNdv;
                    String side;
                    if (e.ndvLeft() != null) {
                        source = SelectivitySource.ASSUMED_UNIQUE_OTHER_SIDE;
                        side = "right side assumed unique (ndvRight=" + fmt(cardR) + " rows)";
                    } else if (e.ndvRight() != null) {
                        source = SelectivitySource.ASSUMED_UNIQUE_OTHER_SIDE;
                        side = "left side assumed unique (ndvLeft=" + fmt(cardL) + " rows)";
                    } else {
                        source = SelectivitySource.ASSUMED_UNIQUE;
                        side = "both sides assumed unique: 1/max(" + fmt(cardL)
                                + ", " + fmt(cardR) + ")";
                    }
                    explanation = "1/max(" + fmt(effNdvL) + ", " + fmt(effNdvR) + "); " + side;
                    warnings.add("Edge " + e.leftTable() + "." + e.leftColumn() + " = "
                            + e.rightTable() + "." + e.rightColumn()
                            + ": missing statistics — " + side);
                }
                if (sel > 1.0) {
                    sel = 1.0; // can happen when an NDV is below 1 after double conversion
                }
            }

            ResolvedEdge re = new ResolvedEdge(e, li, ri, sel, source, explanation);
            resolved.add(re);
            adjacency.get(li).add(new int[]{ri, i});
            adjacency.get(ri).add(new int[]{li, i});
        }
        return new ValidatedProblem(spec, List.copyOf(resolved), adjacency, List.copyOf(warnings));
    }

    private static long pairKey(int a, int b) {
        return ((long) a << 4) | b; // tables <= 8, 4 bits each suffices
    }

    private static String fmt(double d) {
        if (d == Math.rint(d) && d < 1e15) {
            return Long.toString((long) d);
        }
        return Double.toString(d);
    }
}
