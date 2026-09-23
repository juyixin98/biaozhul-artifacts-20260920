package vecq;

import java.util.ArrayList;
import java.util.List;

/**
 * 逐行解释器（参照实现 / differential oracle）。
 *
 * 与向量化引擎 {@link VectorFilter} 的实现刻意不同：
 *  - 一次只求一行的三值状态（无 byte[] 批次、无压实循环）；
 *  - 比较、IS NULL、AND/OR/NOT 全部走递归的标量求值；
 *  - 顶层 union 同样逐分支求值再做多重集合并（与向量引擎共用
 *    {@link SelectionVector#union}，保证合并算法一致、可比较）。
 *
 * 两个引擎过滤产出的选择向量必须逐下标相等，随后再用同一份 {@link ProjectAggregate}
 * 跑投影 / 聚合，结果必须逐位一致。
 */
public final class RowInterpreter {

    private final Table table;

    public RowInterpreter(Table table) {
        this.table = table;
    }

    public SelectionVector apply(FilterExpr f, SelectionVector input, ExecStats stats) {
        int[] rows = input.toArray();
        if (f instanceof FilterExpr.Union u) {
            SelectionVector merged = null;
            for (FilterExpr branch : u.branches()) {
                SelectionVector part = collect(branch, rows);
                merged = (merged == null) ? part : SelectionVector.union(merged, part);
            }
            if (stats != null) stats.filterBatches += rows.length; // 逐行：每行一个“微批”
            return merged == null ? SelectionVector.empty(table.rowCount()) : merged;
        }
        SelectionVector out = collect(f, rows);
        if (stats != null) stats.filterBatches += rows.length;
        return out;
    }

    private SelectionVector collect(FilterExpr f, int[] rows) {
        List<Integer> hit = new ArrayList<>();
        for (int row : rows) {
            if (eval(f, row) == Tri.TRUE) hit.add(row);
        }
        int[] arr = new int[hit.size()];
        for (int i = 0; i < arr.length; i++) arr[i] = hit.get(i);
        return SelectionVector.ofSorted(table.rowCount(), arr, arr.length);
    }

    /** 单行三值求值。 */
    public byte eval(FilterExpr f, int row) {
        switch (f) {
            case FilterExpr.Compare cmp -> {
                Column c = table.column(cmp.column());
                if (cmp.value() == null) return Tri.UNKNOWN; // x = NULL / x != NULL 恒 UNKNOWN
                if (c instanceof IntColumn ic) {
                    if (ic.isNull(row)) return Tri.UNKNOWN;
                    long v = ic.getInt(row);
                    long lit = Column.toInt(cmp.value());
                    return switch (VectorFilter.normalize(cmp.op())) {
                        case "=" -> tri(v == lit);
                        case "!=" -> tri(v != lit);
                        case "<" -> tri(v < lit);
                        case "<=" -> tri(v <= lit);
                        case ">" -> tri(v > lit);
                        case ">=" -> tri(v >= lit);
                        default -> throw new InvalidQueryException("整型列不支持算子 " + cmp.op());
                    };
                } else {
                    StringColumn sc = (StringColumn) c;
                    if (sc.isNull(row)) return Tri.UNKNOWN;
                    String v = sc.getString(row);
                    String lit = (String) cmp.value();
                    return switch (VectorFilter.normalize(cmp.op())) {
                        case "=" -> tri(lit.equals(v));
                        case "!=" -> tri(!lit.equals(v));
                        default -> throw new InvalidQueryException("字符串列不支持算子 " + cmp.op());
                    };
                }
            }
            case FilterExpr.IsNull isn -> {
                boolean isNull = table.column(isn.column()).isNull(row);
                return (isNull != isn.negate()) ? Tri.TRUE : Tri.FALSE;
            }
            case FilterExpr.Not n -> {
                return Tri.not(eval(n.child(), row));
            }
            case FilterExpr.And and -> {
                byte state = Tri.TRUE;
                for (FilterExpr ch : and.children()) state = Tri.and(state, eval(ch, row));
                return state;
            }
            case FilterExpr.Or or -> {
                byte state = Tri.FALSE;
                for (FilterExpr ch : or.children()) state = Tri.or(state, eval(ch, row));
                return state;
            }
            case FilterExpr.Union u ->
                throw new InvalidQueryException("union 只能位于根节点");
        }
    }

    private static byte tri(boolean b) {
        return b ? Tri.TRUE : Tri.FALSE;
    }
}
