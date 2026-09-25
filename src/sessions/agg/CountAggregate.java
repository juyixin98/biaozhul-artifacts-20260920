package sessions.agg;

/** 计数聚合：结果为窗口内事件条数。事件 value 被忽略。 */
public final class CountAggregate implements AggregateFunction<long[]> {

    @Override
    public long emptyResult() {
        return 0L;
    }

    @Override
    public long add(long result, long value) {
        return result + 1;
    }

    @Override
    public long[] add(long[] acc, long value) {
        long[] next = acc == null ? new long[1] : acc;
        next[0]++;
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
