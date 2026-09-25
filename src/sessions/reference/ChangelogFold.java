package sessions.reference;

import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.TreeMap;

import sessions.model.ResultKind;
import sessions.model.WindowUpdate;

/**
 * 把流式算子的 changelog（RETRACT/ADD 序列）折叠为某一时刻的"结果表"。
 *
 * <p>折叠规则：按 sequence 顺序应用；ADD 放入 (key, window) 槽位，
 * RETRACT 清除该槽位。封窗后迟到导致的"撤回 + 新增"因此表现为
 * 旧槽位消失、新槽位出现，可直接与离线参考实现比对。
 */
public final class ChangelogFold {

    /** 结果表中的一行：窗口与聚合值（按 key 分组后以窗口起点排序）。 */
    public record Row(String key, long start, long end, long aggregate) {
    }

    private ChangelogFold() {
    }

    public static List<Row> fold(List<WindowUpdate> updates) {
        record Slot(long start, long end, long aggregate) {
        }
        Map<String, TreeMap<Long, Slot>> table = new TreeMap<>();

        for (WindowUpdate u : updates) {
            TreeMap<Long, Slot> perKey =
                    table.computeIfAbsent(u.key(), k -> new TreeMap<>());
            long start = u.window().start();
            if (u.kind() == ResultKind.ADD) {
                Slot old = perKey.get(start);
                if (old != null) {
                    throw new IllegalStateException(
                            "ADD over existing slot without retract for key=" + u.key()
                                    + " window=" + u.window() + " (existing end=" + old.end() + ")");
                }
                perKey.put(start, new Slot(start, u.window().end(), u.aggregate()));
            } else {
                Slot old = perKey.remove(start);
                if (old == null || old.end() != u.window().end()
                        || old.aggregate() != u.aggregate()) {
                    throw new IllegalStateException(
                            "RETRACT does not match emitted ADD: " + u
                                    + (old == null ? " (no slot)" : " (slot=" + old + ")"));
                }
            }
        }

        List<Row> rows = new ArrayList<>();
        for (Map.Entry<String, TreeMap<Long, Slot>> e : table.entrySet()) {
            for (Slot s : e.getValue().values()) {
                rows.add(new Row(e.getKey(), s.start(), s.end(), s.aggregate()));
            }
        }
        return rows;
    }
}
