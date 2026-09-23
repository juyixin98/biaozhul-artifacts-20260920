package hllengine.engine;

import hllengine.api.ApiException;
import hllengine.json.Json;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * An in-memory table: an ordered list of column names and a list of rows
 * ({@code LinkedHashMap} per row so that projected output keeps column order).
 */
public final class Dataset {

    private final String name;
    private final List<String> columns;
    private final List<Map<String, Object>> rows;

    public Dataset(String name, List<String> columns) {
        this.name = name;
        this.columns = new ArrayList<>(columns);
        this.rows = new ArrayList<>();
    }

    public String name() {
        return name;
    }

    public List<String> columns() {
        return columns;
    }

    public List<Map<String, Object>> rows() {
        return rows;
    }

    public int size() {
        return rows.size();
    }

    /**
     * Appends a row. Unknown columns are rejected; missing columns become null.
     * Rows are copied into an ordered map so output column order is stable.
     */
    public void append(Map<String, Object> input) {
        for (String key : input.keySet()) {
            if (!columns.contains(key)) {
                throw new ApiException(ApiException.BAD_REQUEST,
                        "row for dataset \"" + name + "\" contains unknown column \"" + key
                                + "\"; declared columns are " + columns);
            }
        }
        Map<String, Object> row = new LinkedHashMap<>();
        for (String col : columns) {
            row.put(col, input.get(col));
        }
        rows.add(row);
    }

    /** Appends rows parsed from a JSON array of objects. */
    public void appendAll(List<Object> items) {
        for (int k = 0; k < items.size(); k++) {
            try {
                append(Json.asObject(items.get(k), "rows[" + k + "]"));
            } catch (ApiException ae) {
                throw ae;
            }
        }
    }

    /** Exportable snapshot of the data (rows are passed by reference, engine is single-process). */
    public Map<String, Object> exportMap() {
        Map<String, Object> out = new LinkedHashMap<>();
        out.put("name", name);
        out.put("columns", columns);
        out.put("rowCount", rows.size());
        out.put("rows", rows);
        return out;
    }
}
