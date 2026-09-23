package vecq;

import java.util.LinkedHashMap;
import java.util.Map;

/** 已注册的表目录（表名 -> 表）。HTTP 服务模式下在启动时加载。 */
public final class Catalog {

    private final Map<String, Table> tables = new LinkedHashMap<>();

    public void register(Table t) {
        if (tables.put(t.name(), t) != null) {
            // 允许同名覆盖，保留插入顺序
        }
    }

    public Table require(String name) {
        Table t = tables.get(name);
        if (t == null) {
            throw new InvalidQueryException("表目录中不存在表 \"" + name
                    + "\"；已注册表: " + tables.keySet());
        }
        return t;
    }

    public boolean contains(String name) {
        return tables.containsKey(name);
    }

    public Map<String, Table> tables() {
        return tables;
    }
}
