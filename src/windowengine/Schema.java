package windowengine;

import java.util.ArrayList;
import java.util.Collections;
import java.util.List;

/**
 * 列模式。列名不允许重复、不区分大小写但保留原始写法（输出按原始写法）。
 * 列类型仅用于声明校验，不强制非 NULL 值类型转换；NULL 在任何列中都合法。
 */
public final class Schema {

    private final List<String> names;
    private final List<Value.Type> types;

    public Schema(List<String> names, List<Value.Type> types) {
        if (names.size() != types.size()) {
            throw new EngineException(ErrorCode.INVALID_REQUEST,
                    "列名数量与列类型数量不一致");
        }
        this.names = new ArrayList<>(names);
        this.types = new ArrayList<>(types);
    }

    public int size() {
        return names.size();
    }

    public List<String> names() {
        return Collections.unmodifiableList(names);
    }

    public List<Value.Type> types() {
        return Collections.unmodifiableList(types);
    }

    public String name(int index) {
        return names.get(index);
    }

    public Value.Type type(int index) {
        return types.get(index);
    }

    public int indexOf(String columnName) {
        int found = -1;
        for (int i = 0; i < names.size(); i++) {
            if (names.get(i).equalsIgnoreCase(columnName)) {
                if (found != -1) {
                    throw new EngineException(ErrorCode.AMBIGUOUS_COLUMN,
                            "列名存在歧义（大小写不敏感匹配到多列）: " + columnName);
                }
                found = i;
            }
        }
        return found;
    }

    /** 严格按列名解析；找不到抛 COLUMN_NOT_FOUND。 */
    public int requireIndex(String columnName) {
        int idx = indexOf(columnName);
        if (idx < 0) {
            throw new EngineException(ErrorCode.COLUMN_NOT_FOUND,
                    "列不存在: " + columnName);
        }
        return idx;
    }

    public boolean contains(String columnName) {
        return indexOf(columnName) >= 0;
    }

    /** 追加一个窗口输出列，返回新 Schema（不可变风格，不修改自身）。 */
    public Schema withColumn(String name, Value.Type type) {
        if (contains(name)) {
            throw new EngineException(ErrorCode.DUPLICATE_COLUMN,
                    "窗口输出列与已有列重名: " + name);
        }
        List<String> newNames = new ArrayList<>(names);
        List<Value.Type> newTypes = new ArrayList<>(types);
        newNames.add(name);
        newTypes.add(type);
        return new Schema(newNames, newTypes);
    }
}
