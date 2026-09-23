package tvl.engine;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

import tvl.analyzer.DataType;

/** 内存表：名称、列定义（有序）和数据行。 */
public final class Table {
    private final String name;
    private final Map<String, DataType> columns = new LinkedHashMap<>();
    private final List<Row> rows = new ArrayList<>();

    public Table(String name) {
        this.name = name;
    }

    public String name() {
        return name;
    }

    public void addColumn(String name, DataType type) {
        columns.put(name, type);
    }

    public DataType typeOf(String column) {
        return columns.get(column);
    }

    public Map<String, DataType> columns() {
        return columns;
    }

    public void addRow(Row row) {
        rows.add(row);
    }

    public List<Row> rows() {
        return rows;
    }

    public int rowCount() {
        return rows.size();
    }
}
