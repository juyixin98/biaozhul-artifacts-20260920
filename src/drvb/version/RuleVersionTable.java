package drvb.version;

import drvb.time.Clock;

import java.util.ArrayList;
import java.util.Collections;
import java.util.HashSet;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.TreeMap;

/**
 * 规则版本 + 时间区间绑定表（线程安全，内存实现，无外部系统）。
 *
 * <h3>区间语义</h3>
 * 绑定按事件时间（不是处理时间）构成左闭右开区间：
 * 绑定 b[i] 在 [b[i].effectiveFrom, b[i+1].effectiveFrom) 内生效，最后一条向右无限延伸。
 * 事件按<b>事件时间</b>解析版本，因此乱序 / 晚到事件自然命中对应历史版本。
 *
 * <h3>拒绝静默套用最新规则</h3>
 * 事件时间早于第一条绑定（未发布任何覆盖该时间的版本）时解析失败，
 * 抛出 {@link VersionException}（{@code MISSING_RULE_VERSION}），绝不回退为最新规则。
 *
 * <h3>热更新与回滚</h3>
 * 新绑定的 effectiveFrom 必须严格大于当前最大边界（保证不重开历史区间）；
 * 回滚 = 新增一条指向<b>已存在且未回收</b>版本的绑定，旧绑定全部保留，可审计。
 *
 * <h3>历史版本回收</h3>
 * 版本被回收后成为"墓碑"：绑定记录保留（审计需要），但规则体不再可用，
 * 解析到墓碑的事件以 {@code RECLAIMED_RULE_VERSION} 被拒绝。
 */
public final class RuleVersionTable {

    private final Clock clock;

    /** 版本 id -&gt; 不可变版本；被回收后移至 {@link #tombstones}。 */
    private final Map<String, RuleVersion> versions = new LinkedHashMap<>();
    private final Set<String> tombstones = new HashSet<>();

    /** 版本 id -&gt; 被回收的处理时间。 */
    private final Map<String, Long> reclaimedAt = new LinkedHashMap<>();

    /** 关键：按 effectiveFrom 排序的绑定（左闭右开区间）。 */
    private final TreeMap<Long, Binding> bindings = new TreeMap<>();
    private long nextSeq = 1;

    public RuleVersionTable(Clock clock) {
        this.clock = clock;
    }

    /**
     * 发布新版本并在事件时间 {@code effectiveFrom} 生效。
     *
     * @return 新建的绑定
     * @throws VersionException {@code BAD_EFFECTIVE_FROM}（边界不严格递增）、
     *                          {@code VERSION_EXISTS}（id 重复）
     */
    public synchronized Binding publish(RuleVersion version, long effectiveFrom, String note) {
        if (version == null) {
            throw new IllegalArgumentException("version 不能为空");
        }
        if (versions.containsKey(version.id()) || tombstones.contains(version.id())) {
            throw new VersionException("VERSION_EXISTS", "规则版本 id 已存在: " + version.id());
        }
        checkBoundary(effectiveFrom);
        versions.put(version.id(), version);
        Binding b = new Binding(nextSeq++, effectiveFrom, version.id(),
                clock.nowMillis(), "PUBLISH", note);
        bindings.put(effectiveFrom, b);
        return b;
    }

    /**
     * 回滚：从 {@code effectiveFrom} 起让已存在版本 {@code existingVersionId} 重新生效。
     * 不删除、不修改任何既有绑定，而是追加新绑定。
     *
     * @throws VersionException {@code VERSION_NOT_FOUND}（版本不存在或已回收）、
     *                          {@code ALREADY_CURRENT}（与当前版本相同）、
     *                          {@code BAD_EFFECTIVE_FROM}
     */
    public synchronized Binding rollback(String existingVersionId, long effectiveFrom, String note) {
        RuleVersion v = versions.get(existingVersionId);
        if (v == null) {
            throw new VersionException("VERSION_NOT_FOUND",
                    "回滚目标版本不存在或已被回收: " + existingVersionId);
        }
        checkBoundary(effectiveFrom);
        String current = bindings.isEmpty() ? null : bindings.lastEntry().getValue().ruleVersionId();
        if (existingVersionId.equals(current)) {
            throw new VersionException("ALREADY_CURRENT",
                    "版本 " + existingVersionId + " 已是当前生效版本，回滚无意义");
        }
        Binding b = new Binding(nextSeq++, effectiveFrom, existingVersionId,
                clock.nowMillis(), "ROLLBACK", note);
        bindings.put(effectiveFrom, b);
        return b;
    }

    private void checkBoundary(long effectiveFrom) {
        if (!bindings.isEmpty() && effectiveFrom <= bindings.lastKey()) {
            throw new VersionException("BAD_EFFECTIVE_FROM",
                    "新绑定的 effectiveFrom 必须严格大于当前最大边界 " + bindings.lastKey()
                            + "，实际: " + effectiveFrom + "（历史区间不可变）");
        }
    }

    /** 按事件时间解析版本与命中的绑定。 */
    public synchronized Resolved resolve(long eventTime) {
        Map.Entry<Long, Binding> e = bindings.floorEntry(eventTime);
        if (e == null) {
            long first = bindings.isEmpty() ? Long.MIN_VALUE : bindings.firstKey();
            throw new VersionException("MISSING_RULE_VERSION",
                    "事件时间 " + eventTime + " 早于任何已发布规则版本的生效边界 "
                            + first + "；拒绝静默套用最新规则");
        }
        Binding b = e.getValue();
        if (tombstones.contains(b.ruleVersionId())) {
            throw new VersionException("RECLAIMED_RULE_VERSION",
                    "事件时间 " + eventTime + " 对应的历史规则版本 " + b.ruleVersionId()
                            + " 已被回收（绑定序号 " + b.seq() + "），无法对该晚到事件求值");
        }
        return new Resolved(versions.get(b.ruleVersionId()), b);
    }

    /**
     * 把版本标记为墓碑（底层操作，<b>不检查</b>回收前提）。
     *
     * <p>业务代码应使用 {@link ReclamationService#reclaim(String)}，它会强制
     * "历史版本回收前提"；本方法仅供回收服务本身及需要直接构造墓碑状态的测试使用。
     */
    public synchronized void markReclaimed(String versionId) {
        RuleVersion v = versions.remove(versionId);
        if (v == null) {
            throw new VersionException("VERSION_NOT_FOUND", "版本不存在或已回收: " + versionId);
        }
        tombstones.add(versionId);
        reclaimedAt.put(versionId, clock.nowMillis());
    }

    public synchronized RuleVersion getVersion(String id) {
        return versions.get(id);
    }

    public synchronized boolean isReclaimed(String id) {
        return tombstones.contains(id);
    }

    public synchronized boolean existsAlive(String id) {
        return versions.containsKey(id);
    }

    public synchronized int bindingCount() {
        return bindings.size();
    }

    /** 当前（最大事件时间边界）生效绑定。 */
    public synchronized Binding currentBinding() {
        return bindings.isEmpty() ? null : bindings.lastEntry().getValue();
    }

    public synchronized List<Binding> bindingsView() {
        return new ArrayList<>(bindings.values());
    }

    /** 版本在所有绑定中出现过的区间列表 [from, toExclusive)，toExclusive=null 表示无限。 */
    public synchronized List<Interval> intervalsOf(String versionId) {
        List<Binding> all = bindingsView();
        List<Interval> out = new ArrayList<>();
        for (int i = 0; i < all.size(); i++) {
            Binding b = all.get(i);
            if (!b.ruleVersionId().equals(versionId)) {
                continue;
            }
            Long to = (i + 1 < all.size()) ? all.get(i + 1).effectiveFrom() : null;
            out.add(new Interval(b.effectiveFrom(), to));
        }
        return out;
    }

    /** 不可变快照，供 GC 判定与状态接口使用（一致性取锁一次）。 */
    public synchronized Snapshot snapshot() {
        Map<String, RuleVersion> vs = Collections.unmodifiableMap(new LinkedHashMap<>(versions));
        List<Binding> bs = Collections.unmodifiableList(bindingsView());
        Set<String> ts = Collections.unmodifiableSet(new HashSet<>(tombstones));
        Map<String, Long> ra = Collections.unmodifiableMap(new LinkedHashMap<>(reclaimedAt));
        return new Snapshot(vs, bs, ts, ra);
    }

    /** 解析结果：版本 + 命中绑定。 */
    public static final class Resolved {
        private final RuleVersion version;
        private final Binding binding;

        Resolved(RuleVersion version, Binding binding) {
            this.version = version;
            this.binding = binding;
        }

        public RuleVersion version() {
            return version;
        }

        public Binding binding() {
            return binding;
        }
    }

    /** 左闭右开区间；{@code toExclusive} 为 null 表示向右无限延伸（当前版本）。 */
    public static final class Interval {
        private final long fromInclusive;
        private final Long toExclusive;

        public Interval(long fromInclusive, Long toExclusive) {
            this.fromInclusive = fromInclusive;
            this.toExclusive = toExclusive;
        }

        public long fromInclusive() {
            return fromInclusive;
        }

        public Long toExclusive() {
            return toExclusive;
        }
    }

    /** 一致性只读快照。 */
    public static final class Snapshot {
        private final Map<String, RuleVersion> aliveVersions;
        private final List<Binding> bindings;
        private final Set<String> tombstones;
        private final Map<String, Long> reclaimedAt;

        Snapshot(Map<String, RuleVersion> aliveVersions, List<Binding> bindings,
                 Set<String> tombstones, Map<String, Long> reclaimedAt) {
            this.aliveVersions = aliveVersions;
            this.bindings = bindings;
            this.tombstones = tombstones;
            this.reclaimedAt = reclaimedAt;
        }

        public Map<String, RuleVersion> aliveVersions() {
            return aliveVersions;
        }

        public List<Binding> bindings() {
            return bindings;
        }

        public Set<String> tombstones() {
            return tombstones;
        }

        public Map<String, Long> reclaimedAt() {
            return reclaimedAt;
        }
    }
}
