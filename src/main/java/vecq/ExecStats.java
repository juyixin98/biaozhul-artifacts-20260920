package vecq;

/** 执行统计（区分“过滤批数”和过滤后下游算子的批数），便于在测试中验证批边界行为。 */
public final class ExecStats {

    public int filterBatches;
    public int downstreamBatches;

    public void reset() {
        filterBatches = 0;
        downstreamBatches = 0;
    }
}
