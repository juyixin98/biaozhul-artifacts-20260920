package approxheavy.cms;

import approxheavy.json.Json;

import java.util.List;
import java.util.Map;

/**
 * Parsing/validation for {@link CountMinSketch} JSON. Package-visible helper
 * invoked by {@link CountMinSketch#fromJson(String)}.
 */
final class SketchJson {
    private SketchJson() {
    }

    @SuppressWarnings("unchecked")
    static CountMinSketch parseSketch(String jsonText) {
        Map<String, Object> root;
        try {
            root = Json.object(Json.parse(jsonText));
        } catch (RuntimeException e) {
            throw new IllegalArgumentException("invalid sketch JSON: " + e.getMessage(), e);
        }

        Object type = root.get("type");
        if (type != null && !"CountMinSketch".equals(type)) {
            throw new IllegalArgumentException("not a CountMinSketch document (type=" + type + ")");
        }

        int width;
        int depth;
        long seed;
        long totalCount;
        List<Object> rows;
        try {
            width = Json.integer(root, "width");
            depth = Json.integer(root, "depth");
            seed = Json.lng(root, "seed");
            totalCount = Json.optLng(root, "totalCount", 0L);
            Object cellsNode = root.get("cells");
            if (cellsNode == null) {
                throw new IllegalArgumentException("missing 'cells'");
            }
            rows = Json.list(cellsNode);
        } catch (RuntimeException e) {
            throw new IllegalArgumentException("invalid sketch fields: " + e.getMessage(), e);
        }

        if (width < 2) {
            throw new IllegalArgumentException("width must be >= 2");
        }
        if (depth < 1) {
            throw new IllegalArgumentException("depth must be >= 1");
        }
        if (rows.size() != depth) {
            throw new IllegalArgumentException(
                    "cells has " + rows.size() + " rows, expected " + depth);
        }

        CountMinSketch sketch = new CountMinSketch(width, depth, seed);
        for (int row = 0; row < depth; row++) {
            List<Object> cols = Json.list(rows.get(row));
            if (cols.size() != width) {
                throw new IllegalArgumentException(
                        "row " + row + " has " + cols.size() + " cells, expected " + width);
            }
            for (int col = 0; col < width; col++) {
                Object cell = cols.get(col);
                if (!(cell instanceof Number)) {
                    throw new IllegalArgumentException("non-numeric cell at " + row + "," + col);
                }
                long value = ((Number) cell).longValue();
                if (value < 0) {
                    throw new IllegalArgumentException("negative cell at " + row + "," + col);
                }
                sketch.cells[row][col] = value;
            }
        }

        if (totalCount > 0) {
            // The declared total must be consistent with each row's total.
            for (int row = 0; row < depth; row++) {
                long rowTotal = 0L;
                for (Object col : Json.list(rows.get(row))) {
                    rowTotal += ((Number) col).longValue();
                }
                if (rowTotal != totalCount) {
                    throw new IllegalArgumentException(
                            "totalCount " + totalCount + " inconsistent with row " + row
                                    + " total " + rowTotal);
                }
            }
            sketch.totalCount = totalCount;
        }
        return sketch;
    }
}
