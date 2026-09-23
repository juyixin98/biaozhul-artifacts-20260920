package dedup.core;

/**
 * 去重判定结果（可观察结果对象）。
 *
 * <p>{@link #decision} 为三态：
 * <ul>
 *   <li>{@link Decision#ACCEPT} —— 首次见到，事件应当输出；</li>
 *   <li>{@link Decision#SUPPRESS} —— 在<b>承诺范围</b>内识别出的重复，事件被抑制，不输出；</li>
 *   <li>{@link Decision#UNGUARANTEED} —— 墓碑已按水位线释放（极迟重复）或被硬上限强制淘汰，
 *       系统<b>无法保证</b>去重，事件被透传输出，但 {@link #dedupGuaranteed} 置为 false。</li>
 * </ul>
 *
 * <p>该标志就是验收要求的“可观察标志”：下游与审计都能区分“承诺无重复”与“尽力而为”。
 */
public record DedupResult(
        Decision decision,
        boolean dedupGuaranteed,
        boolean payloadMismatch,
        String key,
        String id,
        long eventTime,
        long watermark,
        String reason) {

    public enum Decision { ACCEPT, SUPPRESS, UNGUARANTEED }

    /** 该判定是否向下游输出事件（ACCEPT 与 UNGUARANTEED 都输出）。 */
    public boolean emitted() {
        return decision != Decision.SUPPRESS;
    }

    public dedup.json.Json.Obj toJson() {
        var o = new dedup.json.Json.Obj();
        o.put("decision", new dedup.json.Json.Str(decision.name()));
        o.put("emitted", new dedup.json.Json.Bool(emitted()));
        o.put("dedupGuaranteed", new dedup.json.Json.Bool(dedupGuaranteed));
        o.put("payloadMismatch", new dedup.json.Json.Bool(payloadMismatch));
        o.put("key", new dedup.json.Json.Str(key));
        o.put("id", new dedup.json.Json.Str(id));
        o.put("eventTime", dedup.json.Json.Num.of(eventTime));
        o.put("watermark", watermark == Long.MIN_VALUE
                ? dedup.json.Json.Nul.INSTANCE
                : dedup.json.Json.Num.of(watermark));
        o.put("reason", new dedup.json.Json.Str(reason));
        return o;
    }
}
