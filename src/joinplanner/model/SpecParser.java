package joinplanner.model;

import joinplanner.json.BadInputException;
import joinplanner.json.J;

import java.util.ArrayList;
import java.util.HashMap;
import java.util.HashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;

/** Parses and validates a {@code /plan} (or {@code /enumerate}) request body into a {@link Spec}. */
public final class SpecParser {

    public static final int MAX_TABLES = 8;
    public static final double DEFAULT_CAP = 1.0e15;

    private SpecParser() {}

    public static Spec parse(Map<String, Object> body) {
        List<Object> tableNodes = J.getArr(body, "tables");
        if (tableNodes == null || tableNodes.isEmpty()) {
            throw new BadInputException("'tables' is required and must contain at least one table");
        }
        if (tableNodes.size() > MAX_TABLES) {
            throw new BadInputException(
                    "At most " + MAX_TABLES + " tables are supported (got " + tableNodes.size() + ")");
        }

        List<Table> tables = new ArrayList<>();
        Set<String> names = new HashSet<>();
        for (int i = 0; i < tableNodes.size(); i++) {
            Map<String, Object> tn = J.obj(tableNodes.get(i), "tables[" + i + "]");
            String name = J.getStr(tn, "name");
            if (name == null || name.isBlank()) {
                throw new BadInputException("tables[" + i + "].name is required");
            }
            if (!names.add(name)) {
                throw new BadInputException("Duplicate table name: '" + name + "'");
            }
            Object rowsNode = J.get(tn, "rows");
            if (rowsNode == null) {
                throw new BadInputException("tables[" + i + "].rows is required");
            }
            double rowsD = J.nonNegNum(rowsNode, "tables[" + i + "].rows");
            if (rowsD > 1e18) {
                throw new BadInputException("tables[" + i + "].rows must be <= 1e18");
            }
            tables.add(new Table(name, (long) rowsD));
        }

        Map<String, Integer> indexByName = new HashMap<>();
        for (int i = 0; i < tables.size(); i++) {
            indexByName.put(tables.get(i).name(), i);
        }

        List<Edge> edges = new ArrayList<>();
        List<Object> edgeNodes = J.getArr(body, "edges");
        if (edgeNodes != null) {
            for (int i = 0; i < edgeNodes.size(); i++) {
                Map<String, Object> en = J.obj(edgeNodes.get(i), "edges[" + i + "]");
                String left = J.getStr(en, "left");
                String right = J.getStr(en, "right");
                if (left == null || right == null) {
                    throw new BadInputException("edges[" + i + "] needs 'left' and 'right' table names");
                }
                Integer li = indexByName.get(left);
                Integer ri = indexByName.get(right);
                if (li == null) {
                    throw new BadInputException("edges[" + i + "].left references unknown table '" + left + "'");
                }
                if (ri == null) {
                    throw new BadInputException("edges[" + i + "].right references unknown table '" + right + "'");
                }
                if (li.equals(ri)) {
                    throw new BadInputException(
                            "edges[" + i + "] is a self-join on '" + left + "'; self-joins are not supported");
                }

                Double sel = J.getNum(en, "selectivity");
                if (sel != null) {
                    if (sel < 0 || sel > 1) {
                        throw new BadInputException("edges[" + i + "].selectivity must be within [0,1]");
                    }
                }
                Long lndv = readNdv(en, "leftNdv", i);
                Long rndv = readNdv(en, "rightNdv", i);
                boolean leftUnique = Boolean.TRUE.equals(J.getBool(en, "leftUnique"));
                boolean rightUnique = Boolean.TRUE.equals(J.getBool(en, "rightUnique"));
                String on = J.getStr(en, "on");

                Edge edge = new Edge(left, right, sel, lndv, rndv, leftUnique, rightUnique, on);
                edge.leftIndex = li;
                edge.rightIndex = ri;
                edges.add(edge);
            }
        }

        Map<String, Object> options = J.getObj(body, "options");
        boolean leftDeepOnly = false;
        CostModel costModel = CostModel.TOTAL_INTERMEDIATE_ROWS;
        double defaultSel = 0.1;
        double cap = DEFAULT_CAP;
        if (options != null) {
            Boolean ld = J.getBool(options, "leftDeepOnly");
            if (ld != null) {
                leftDeepOnly = ld;
            }
            String cm = J.getStr(options, "costModel");
            if (cm != null) {
                costModel = CostModel.parse(cm);
            }
            Double ds = J.getNum(options, "defaultSelectivity");
            if (ds != null) {
                if (ds <= 0 || ds > 1) {
                    throw new BadInputException("options.defaultSelectivity must be within (0,1]");
                }
                defaultSel = ds;
            }
            Double ec = J.getNum(options, "estimateCap");
            if (ec != null) {
                if (ec < 1) {
                    throw new BadInputException("options.estimateCap must be >= 1");
                }
                cap = ec;
            }
        }

        return new Spec(tables, edges, leftDeepOnly, costModel, defaultSel, cap);
    }

    private static Long readNdv(Map<String, Object> en, String key, int edgeIndex) {
        Double v = J.getNum(en, key);
        if (v == null) {
            return null;
        }
        if (v < 1 || v != Math.rint(v) || v > 1e18) {
            throw new BadInputException(
                    "edges[" + edgeIndex + "]." + key + " must be an integer within [1, 1e18]");
        }
        return v.longValue();
    }
}
