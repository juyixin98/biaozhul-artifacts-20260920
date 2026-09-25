package sessions.agg;

/** 求和聚合：结果为窗口内事件 value 之和。 */
public final class SumAggregate implements AggregateFunction<long[]> {

    @Override
    public long emptyResult() {
        return 0L;
    }

    @Override
    public long add(long result, long value) {
        return result + value;
    }

    @Override
    public long[] add(long[] acc, long value) {
        long[] next = acc == null ? new long[1] : acc;
        next[0] += value;
        return next;
    }

    @Override
    public long result(long[] acc) {
        return acc == null ? 0L : acc[0];
    }

    @Override
    public long merge(long a, long b) {
        return a + b;
    }
}
