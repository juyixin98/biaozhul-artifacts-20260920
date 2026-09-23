package phj.join;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * 执行计划 / 运行统计收集器。导出的计划是一棵树：
 * 每个节点记录算子、分区数、输入/输出行数、落盘字节、是否回退及原因。
 */
public final class StatsCollector {

    public static final class Node {
        final Map<String, Object> data = new LinkedHashMap<>();
        final List<Node> children = new ArrayList<>();

        Node(String op) {
            data.put("operator", op);
        }

        public Node child(String op) {
            Node n = new Node(op);
            children.add(n);
            return n;
        }

        public void put(String k, Object v) { data.put(k, v); }

        public void add(String k, long delta) {
            data.merge(k, delta, (a, b) -> ((Number) a).longValue() + ((Number) b).longValue());
        }

        public Map<String, Object> toMap() {
            if (!children.isEmpty()) {
                List<Map<String, Object>> kids = new ArrayList<>();
                for (Node c : children) kids.add(c.toMap());
                data.put("children", kids);
            }
            return data;
        }
    }

    private final List<String> warnings = new ArrayList<>();

    public Node root(String op) { return new Node(op); }

    public void warn(String msg) { warnings.add(msg); }

    public List<String> warnings() { return warnings; }
}
