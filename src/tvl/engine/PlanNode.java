package tvl.engine;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * 逻辑/物理合一的执行计划树节点，同时承载执行后统计。
 *
 * 节点种类：SCAN（全表扫描）、FILTER（谓词过滤）、PROJECT（列裁剪）、LIMIT。
 * rowsIn / rowsOut 在引擎执行时回填，可随查询结果导出。
 */
public final class PlanNode {
    private final String kind;
    private final String detail;
    private final List<PlanNode> children = new ArrayList<>();
    private long rowsIn;
    private long rowsOut;

    public PlanNode(String kind, String detail) {
        this.kind = kind;
        this.detail = detail;
    }

    public void addChild(PlanNode child) {
        children.add(child);
    }

    public String kind() {
        return kind;
    }

    public List<PlanNode> children() {
        return children;
    }

    public void setRowsIn(long n) {
        this.rowsIn = n;
    }

    public void setRowsOut(long n) {
        this.rowsOut = n;
    }

    public Map<String, Object> toJson() {
        Map<String, Object> json = new LinkedHashMap<>();
        json.put("node", kind);
        json.put("detail", detail);
        json.put("rowsIn", rowsIn);
        json.put("rowsOut", rowsOut);
        if (!children.isEmpty()) {
            List<Object> childJson = new ArrayList<>();
            for (PlanNode child : children) {
                childJson.add(child.toJson());
            }
            json.put("children", childJson);
        }
        return json;
    }
}
