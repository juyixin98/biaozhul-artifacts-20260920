package vecq;

/**
 * 单个聚合的批量累加器。直接按选择下标更新，重复下标重复累加。
 *
 * 结果约定：
 *  - count(*)：选择向量行数（含重复、含 NULL）；
 *  - count(col)：非 NULL 个数；
 *  - sum/avg/min/max：忽略 NULL；没有任何非 NULL 输入时结果为 null；
 *  - avg 返回 double（sum 为 long，avg = (double) sum / count）。
 *
 * 向量化引擎与逐行参照引擎共用同一个累加器实现，
 * 保证聚合结果按构造顺序逐位一致，差分比较不会出现伪差异。
 */
abstract class Accumulator {

    final String name;

    Accumulator(String name) {
        this.name = name;
    }

    /** rows[off .. off+len) 为本批（可能含重复下标）。 */
    abstract void update(int[] rows, int off, int len);

    abstract Object finish();

    static Accumulator create(AggSpec spec, Table table) {
        String outName = spec.outputName();
        String fn = spec.func().toLowerCase();
        if (fn.equals("count") && spec.isCountStar()) {
            return new CountStar(outName);
        }
        Column c = table.column(spec.column());
        return switch (fn) {
            case "count" -> new CountCol(outName, c);
            case "sum" -> new Sum(outName, (IntColumn) c);
            case "avg" -> new Avg(outName, (IntColumn) c);
            case "min" -> c instanceof IntColumn ic
                    ? new MinInt(outName, ic)
                    : new MinStr(outName, (StringColumn) c);
            case "max" -> c instanceof IntColumn ic
                    ? new MaxInt(outName, ic)
                    : new MaxStr(outName, (StringColumn) c);
            default -> throw new InvalidQueryException("不支持的聚合: " + fn);
        };
    }

    // ---- count(*) ----
    static final class CountStar extends Accumulator {
        private long count;
        CountStar(String n) { super(n); }
        @Override void update(int[] rows, int off, int len) { count += len; }
        @Override Object finish() { return count; }
    }

    // ---- count(col) ----
    static final class CountCol extends Accumulator {
        private final Column col;
        private long count;
        CountCol(String n, Column c) { super(n); this.col = c; }
        @Override void update(int[] rows, int off, int len) {
            for (int i = 0; i < len; i++) if (!col.isNull(rows[off + i])) count++;
        }
        @Override Object finish() { return count; }
    }

    // ---- sum(int) ----
    static final class Sum extends Accumulator {
        private final IntColumn col;
        private long sum;
        private boolean seen;
        Sum(String n, IntColumn c) { super(n); this.col = c; }
        @Override void update(int[] rows, int off, int len) {
            int[] data = col.raw();
            for (int i = 0; i < len; i++) {
                int r = rows[off + i];
                if (!col.isNull(r)) { sum += data[r]; seen = true; }
            }
        }
        @Override Object finish() {
            // 没有任何非 NULL 输入时返回 null
            return seen ? sum : null;
        }
    }

    // ---- avg(int) ----
    static final class Avg extends Accumulator {
        private final IntColumn col;
        private long sum;
        private long count;
        Avg(String n, IntColumn c) { super(n); this.col = c; }
        @Override void update(int[] rows, int off, int len) {
            int[] data = col.raw();
            for (int i = 0; i < len; i++) {
                int r = rows[off + i];
                if (!col.isNull(r)) { sum += data[r]; count++; }
            }
        }
        @Override Object finish() { return count == 0 ? null : (double) sum / count; }
    }

    // ---- min/max int ----
    static final class MinInt extends Accumulator {
        private final IntColumn col;
        private int min = Integer.MAX_VALUE;
        private boolean seen;
        MinInt(String n, IntColumn c) { super(n); this.col = c; }
        @Override void update(int[] rows, int off, int len) {
            int[] data = col.raw();
            for (int i = 0; i < len; i++) {
                int r = rows[off + i];
                if (!col.isNull(r)) { seen = true; if (data[r] < min) min = data[r]; }
            }
        }
        @Override Object finish() { return seen ? min : null; }
    }

    static final class MaxInt extends Accumulator {
        private final IntColumn col;
        private int max = Integer.MIN_VALUE;
        private boolean seen;
        MaxInt(String n, IntColumn c) { super(n); this.col = c; }
        @Override void update(int[] rows, int off, int len) {
            int[] data = col.raw();
            for (int i = 0; i < len; i++) {
                int r = rows[off + i];
                if (!col.isNull(r)) { seen = true; if (data[r] > max) max = data[r]; }
            }
        }
        @Override Object finish() { return seen ? max : null; }
    }

    // ---- min/max string ----
    static final class MinStr extends Accumulator {
        private final StringColumn col;
        private String min;
        MinStr(String n, StringColumn c) { super(n); this.col = c; }
        @Override void update(int[] rows, int off, int len) {
            for (int i = 0; i < len; i++) {
                int r = rows[off + i];
                if (!col.isNull(r)) {
                    String v = col.getString(r);
                    if (min == null || v.compareTo(min) < 0) min = v;
                }
            }
        }
        @Override Object finish() { return min; }
    }

    static final class MaxStr extends Accumulator {
        private final StringColumn col;
        private String max;
        MaxStr(String n, StringColumn c) { super(n); this.col = c; }
        @Override void update(int[] rows, int off, int len) {
            for (int i = 0; i < len; i++) {
                int r = rows[off + i];
                if (!col.isNull(r)) {
                    String v = col.getString(r);
                    if (max == null || v.compareTo(max) > 0) max = v;
                }
            }
        }
        @Override Object finish() { return max; }
    }
}
