package tvl.parser;

import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

import tvl.core.DataType;
import tvl.core.Pos;

/**
 * 表达式 AST 基类。resolvedType 由 {@code TypeChecker} 填充：
 * 对于 NULL 字面量，会尽量结合上下文推成具体类型（仍可能保持 NULL）。
 */
public abstract class Expr {

    /** 节点起始位置（1 起始行列）。 */
    public abstract Pos pos();

    /** 节点在源码中的字符长度。 */
    public abstract int length();

    /** 类型检查后推导出的类型。 */
    public DataType resolvedType;

    /** 导出为可 JSON 序列化的结构（含节点类型与位置）。 */
    public abstract Map<String, Object> toJson();

    protected Map<String, Object> node(String kind, Pos pos, int length) {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("node", kind);
        m.put("pos", pos.toJson());
        m.put("length", length);
        if (resolvedType != null) {
            m.put("resolvedType", resolvedType.name());
        }
        return m;
    }
}
