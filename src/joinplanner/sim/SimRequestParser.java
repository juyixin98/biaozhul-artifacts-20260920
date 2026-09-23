package joinplanner.sim;

import java.util.ArrayList;
import java.util.List;
import java.util.Map;

import joinplanner.core.BadRequestException;
import joinplanner.json.J;

/** Parses /api/simulate requests. */
public final class SimRequestParser {

    public SimConfig parse(Object rootNode) {
        Map<String, Object> root = J.obj(rootNode, "$");
        Object problem = root.get("problem");
        if (problem == null) {
            // allow the planning fields to be inline
            problem = root;
        }

        List<ColumnGen> cols = new ArrayList<>();
        Object columnsNode = root.get("columns");
        if (columnsNode != null) {
            List<Object> arr = J.arr(columnsNode, "$.columns");
            for (int i = 0; i < arr.size(); i++) {
                String path = "$.columns[" + i + "]";
                Map<String, Object> cj = J.obj(arr.get(i), path);
                String table = J.str(cj, "table", path);
                String column = J.str(cj, "column", path);
                long ndv = cj.containsKey("ndv") ? J.lng(cj.get("ndv"), path + ".ndv") : -1L;
                double zipf = cj.containsKey("zipf")
                        ? J.dbl(cj.get("zipf"), path + ".zipf") : 1.0;
                if (zipf < 0) {
                    throw new BadRequestException(path + ".zipf: must be >= 0");
                }
                cols.add(new ColumnGen(table, column, ndv, zipf));
            }
        }

        long seed = root.containsKey("seed") ? J.lng(root.get("seed"), "$.seed") : 42L;
        long maxRows = root.containsKey("maxRows")
                ? J.lng(root.get("maxRows"), "$.maxRows")
                : SimConfig.DEFAULT_MAX_ROWS;
        if (maxRows <= 0) {
            throw new BadRequestException("maxRows: must be positive");
        }
        return new SimConfig(problem, List.copyOf(cols), seed, maxRows);
    }
}
