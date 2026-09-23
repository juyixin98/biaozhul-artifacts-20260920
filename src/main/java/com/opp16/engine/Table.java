package com.opp16.engine;

import com.opp16.engine.json.Json;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * In-memory table: ordered list of equal-length columns addressed by name.
 * Row count is taken from the columns; a table with zero columns has zero rows.
 */
public final class Table {

    private final List<Column> columns = new ArrayList<>();
    private final Map<String, Integer> index = new LinkedHashMap<>();

    public void add(Column column) {
        if (index.containsKey(column.name)) {
            throw new EngineException("duplicate column name: " + column.name);
        }
        if (!columns.isEmpty() && column.size() != rowCount()) {
            throw new EngineException("column '" + column.name + "' has " + column.size()
                    + " rows but table has " + rowCount());
        }
        index.put(column.name, columns.size());
        columns.add(column);
    }

    public int rowCount() { return columns.isEmpty() ? 0 : columns.get(0).size(); }

    public int columnCount() { return columns.size(); }

    public Column column(int i) { return columns.get(i); }

    public List<Column> columns() { return columns; }

    public boolean hasColumn(String name) { return index.containsKey(name); }

    public Column column(String name) {
        Integer i = index.get(name);
        if (i == null) throw new EngineException("unknown column: " + name);
        return columns.get(i);
    }

    /** Serialize the whole relation, including NULL bitmaps as encoded values. */
    public Json.Value toJson() {
        Json.Obj o = new Json.Obj();
        o.put("rowCount", rowCount());
        Json.Arr cols = new Json.Arr();
        for (Column c : columns) cols.add(c.toJson());
        o.put("columns", cols);
        return o;
    }

    public static Table fromJson(Json.Obj obj) {
        Json.Arr cols = obj.get("columns").asArray();
        Table t = new Table();
        for (Json.Value cv : cols.list) {
            Json.Obj co = cv.asObject();
            t.add(Column.fromJson(co.getString("name"), co.getString("type"),
                    co.get("values").asArray()));
        }
        return t;
    }
}
