package phj.join;

import phj.core.JoinType;
import phj.core.Key;
import phj.core.Relation;
import phj.core.Row;
import phj.core.Value;

import java.util.ArrayList;
import java.util.List;

/**
 * 嵌套循环连接参考实现（朴素 O(N*M)，只用于正确性对照）。
 *
 * 语义：
 *  - 连接键任意一列为 NULL -> 永不匹配；
 *  - INNER：仅输出匹配组合；
 *  - LEFT：左行无任何匹配时输出 左行 + 右列全 NULL 补位；
 *  - 输出列顺序固定为 左列在前、右列在后；
 *  - 重复键产生笛卡尔积。
 */
public final class NestedLoopJoin {

    private NestedLoopJoin() {}

    public static List<Row> join(Relation left, Relation right,
                                 int[] leftKeyIdx, int[] rightKeyIdx,
                                 JoinType joinType) {
        List<Row> out = new ArrayList<>();
        int rw = right.width();
        for (Row lrow : left.rows()) {
            Key lk = Key.ofRow(lrow, leftKeyIdx);
            boolean matched = false;
            for (Row rrow : right.rows()) {
                Key rk = Key.ofRow(rrow, rightKeyIdx);
                if (!lk.isNull() && !rk.isNull() && lk.equals(rk)) {
                    out.add(concat(lrow, rrow));
                    matched = true;
                }
            }
            if (joinType == JoinType.LEFT && !matched) {
                out.add(concat(lrow, nullRow(rw)));
            }
        }
        return out;
    }

    public static Row concat(Row l, Row r) {
        List<Value> vals = new ArrayList<>(l.width() + (r == null ? 0 : r.width()));
        vals.addAll(l.values());
        if (r != null) vals.addAll(r.values());
        return new Row(vals);
    }

    public static Row nullRow(int width) {
        List<Value> vals = new ArrayList<>(width);
        for (int i = 0; i < width; i++) vals.add(Value.NULL);
        return new Row(vals);
    }
}
