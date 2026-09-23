package tvl.engine;

import java.util.LinkedHashMap;
import java.util.Map;

/** 内存表目录（表名 -> 表）。单机引擎的全部状态。 */
public final class Catalog {
    private final Map<String, Table> tables = new LinkedHashMap<>();

    public void put(Table table) {
        tables.put(table.name(), table);
    }

    public Table get(String name) {
        return tables.get(name);
    }

    public boolean has(String name) {
        return tables.containsKey(name);
    }

    public Map<String, Table> all() {
        return tables;
    }
}
