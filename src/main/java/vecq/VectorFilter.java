package vecq;

/**
 * 向量化过滤执行器：对“输入选择向量的一个批次”求三值状态，再压实（compact）出 TRUE 行。
 *
 * 处理模型：
 *   输入 SV（有序、可稀疏、可重复）
 *     -> 按 batchSize 切批
 *     -> 每批求出 byte[] 状态（0=FALSE,1=TRUE,2=UNKNOWN，见 {@link Tri}）
 *     -> 顺序压实 TRUE 的行下标到输出 SV
 *
 * 压实保持顺序，输入有序且分批顺序处理，所以输出仍然有序；重复下标只要状态为 TRUE
 * 就会被原样重复输出。
 */
public final class VectorFilter {

    private final Table table;

    public VectorFilter(Table table) {
        this.table = table;
    }

    /**
     * 对输入选择向量执行过滤。
     * @return 过滤后的选择向量；同时通过 stats 回批数等统计
     */
    public SelectionVector apply(FilterExpr f, SelectionVector input, int batchSize, ExecStats stats) {
        if (f instanceof FilterExpr.Union u) {
            // 顶层 union：每个分支独立过滤，多重集并集（重复下标按次数累加）
            SelectionVector merged = null;
            for (FilterExpr branch : u.branches()) {
                SelectionVector part = apply(branch, input, batchSize, stats);
                merged = (merged == null) ? part : SelectionVector.union(merged, part);
            }
            return merged == null ? SelectionVector.empty(table.rowCount()) : merged;
        }

        int[] rows = input.toArray();
        int n = rows.length;
        int[] out = new int[Math.max(4, n)];
        int outLen = 0;
        int batches = 0;

        for (int start = 0; start < n; start += batchSize) {
            batches++;
            int len = Math.min(batchSize, n - start);
            byte[] state = evalBatch(f, rows, start, len);
            for (int i = 0; i < len; i++) {
                if (state[i] == Tri.TRUE) {
                    if (outLen == out.length) out = java.util.Arrays.copyOf(out, out.length * 2);
                    out[outLen++] = rows[start + i];
                }
            }
        }
        stats.filterBatches += batches;
        return SelectionVector.ofSorted(table.rowCount(), out, outLen);
    }

    /** 求一个批次的三值状态。 */
    public byte[] evalBatch(FilterExpr f, int[] rows, int off, int len) {
        byte[] state = new byte[len];
        switch (f) {
            case FilterExpr.Compare cmp -> evalCompare(cmp, rows, off, len, state);
            case FilterExpr.IsNull isn -> {
                Column c = table.column(isn.column());
                boolean negate = isn.negate();
                for (int i = 0; i < len; i++) {
                    boolean isNull = c.isNull(rows[off + i]);
                    state[i] = (isNull != negate) ? Tri.TRUE : Tri.FALSE;
                }
            }
            case FilterExpr.Not n -> {
                byte[] child = evalBatch(n.child(), rows, off, len);
                for (int i = 0; i < len; i++) state[i] = Tri.not(child[i]);
            }
            case FilterExpr.And and -> {
                java.util.Arrays.fill(state, Tri.TRUE);
                for (FilterExpr child : and.children()) {
                    byte[] cs = evalBatch(child, rows, off, len);
                    for (int i = 0; i < len; i++) state[i] = Tri.and(state[i], cs[i]);
                }
            }
            case FilterExpr.Or or -> {
                java.util.Arrays.fill(state, Tri.FALSE);
                for (FilterExpr child : or.children()) {
                    byte[] cs = evalBatch(child, rows, off, len);
                    for (int i = 0; i < len; i++) state[i] = Tri.or(state[i], cs[i]);
                }
            }
            case FilterExpr.Union u ->
                throw new InvalidQueryException("union 不能在批次求值中出现（只允许位于根节点）");
        }
        return state;
    }

    // ---------------- 比较 ----------------

    private void evalCompare(FilterExpr.Compare cmp, int[] rows, int off, int len, byte[] state) {
        Column c = table.column(cmp.column());
        String op = normalize(cmp.op());
        Object literal = cmp.value();

        // 任何与 NULL 字面量的比较（= / !=）结果恒为 UNKNOWN —— 即使该行本身也是 NULL
        if (literal == null) {
            java.util.Arrays.fill(state, Tri.UNKNOWN);
            return;
        }

        if (c instanceof IntColumn ic) {
            int lit = Column.toInt(literal);
            int[] data = ic.raw();
            switch (op) {
                case "=" -> {
                    for (int i = 0; i < len; i++) {
                        int r = rows[off + i];
                        state[i] = ic.isNull(r) ? Tri.UNKNOWN : tri(data[r] == lit);
                    }
                }
                case "!=" -> {
                    for (int i = 0; i < len; i++) {
                        int r = rows[off + i];
                        state[i] = ic.isNull(r) ? Tri.UNKNOWN : tri(data[r] != lit);
                    }
                }
                case "<" -> {
                    for (int i = 0; i < len; i++) {
                        int r = rows[off + i];
                        state[i] = ic.isNull(r) ? Tri.UNKNOWN : tri(data[r] < lit);
                    }
                }
                case "<=" -> {
                    for (int i = 0; i < len; i++) {
                        int r = rows[off + i];
                        state[i] = ic.isNull(r) ? Tri.UNKNOWN : tri(data[r] <= lit);
                    }
                }
                case ">" -> {
                    for (int i = 0; i < len; i++) {
                        int r = rows[off + i];
                        state[i] = ic.isNull(r) ? Tri.UNKNOWN : tri(data[r] > lit);
                    }
                }
                case ">=" -> {
                    for (int i = 0; i < len; i++) {
                        int r = rows[off + i];
                        state[i] = ic.isNull(r) ? Tri.UNKNOWN : tri(data[r] >= lit);
                    }
                }
                default -> throw new InvalidQueryException("整型列不支持算子 " + op);
            }
        } else if (c instanceof StringColumn sc) {
            String lit = (String) literal;
            if (op.equals("=")) {
                for (int i = 0; i < len; i++) {
                    int r = rows[off + i];
                    state[i] = sc.isNull(r) ? Tri.UNKNOWN : tri(lit.equals(sc.getString(r)));
                }
            } else if (op.equals("!=")) {
                for (int i = 0; i < len; i++) {
                    int r = rows[off + i];
                    state[i] = sc.isNull(r) ? Tri.UNKNOWN : tri(!lit.equals(sc.getString(r)));
                }
            } else {
                throw new InvalidQueryException("字符串列不支持算子 " + op);
            }
        } else {
            throw new InvalidQueryException("不支持的列类型: " + c.typeName());
        }
    }

    static String normalize(String op) {
        return switch (op) {
            case "==" -> "=";
            case "<>" -> "!=";
            default -> op;
        };
    }

    private static byte tri(boolean b) {
        return b ? Tri.TRUE : Tri.FALSE;
    }
}
