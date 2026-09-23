package dedup.core;

import dedup.json.Json;

/** 去重器的可观察计数器（{@code /stats} 与快照都会暴露）。 */
public class Stats {
    public long seen;            // 处理过的事件总数（含重启后继续累计）
    public long accepted;        // 首次接受
    public long suppressed;      // 承诺范围内抑制的重复
    public long unguarded;       // 无法保证去重而透传
    public long payloadMismatch; // 同 ID 不同载荷的次数
    public long timeEvicted;     // 因水位线推进而释放的墓碑数
    public long forcedEvicted;   // 因硬上限被迫淘汰的墓碑数
    public long activeTombstonesPeak; // 墓碑数历史峰值

    public Json.Obj toJson() {
        Json.Obj o = new Json.Obj();
        o.put("seen", Json.Num.of(seen));
        o.put("accepted", Json.Num.of(accepted));
        o.put("suppressed", Json.Num.of(suppressed));
        o.put("unguarded", Json.Num.of(unguarded));
        o.put("payloadMismatch", Json.Num.of(payloadMismatch));
        o.put("timeEvicted", Json.Num.of(timeEvicted));
        o.put("forcedEvicted", Json.Num.of(forcedEvicted));
        o.put("activeTombstonesPeak", Json.Num.of(activeTombstonesPeak));
        return o;
    }
}
