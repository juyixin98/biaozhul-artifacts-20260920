package sessions.op;

import java.util.ArrayList;
import java.util.Comparator;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

import sessions.agg.AggregateFunction;
import sessions.model.Event;
import sessions.model.ResultKind;
import sessions.model.Window;
import sessions.model.WindowUpdate;
import sessions.time.TimerService;

/**
 * 事件时间会话窗口算子（多 key）。
 *
 * <h3>语义</h3>
 * <ul>
 *   <li><b>会话间隔 gap</b>：同一 key 的相邻事件间隔 {@code <= gap} 即属于同一会话；
 *       间隔恰好等于 gap 也合并（"边界间隔相等"在同一会话内）。</li>
 *   <li><b>水位线 W</b>：时间 {@code t >= W} 视为准时；{@code t < W} 为迟到。</li>
 *   <li><b>允许迟到 L</b>：仅当 {@code t >= W - L}（饱和减法）的事件才被接受；
 *       更早的事件直接丢弃并计入 droppedLateEvents。</li>
 *   <li><b>封窗</b>：窗口 [s,e] 在水位线 {@code W >= e + gap}（饱和加法）时封存，
 *       发出 ADD(最终聚合)。封存后窗口状态仍保留 L 的时间。</li>
 *   <li><b>迟到桥接</b>：被接受的迟到事件若同时触及活动窗口与已封存窗口，
 *       二者（可能多个，含传递链）合并为一个新窗口：对每个被并入的已封存窗口
 *       先发 RETRACT，新窗口若在当前水位线下立即满足封窗条件则立即发 ADD
 *       （该 ADD 之后仍可再次被撤回），否则作为活动窗口等待封窗定时器。</li>
 *   <li><b>状态清理</b>：已封存窗口在 {@code W > e + gap + L} 时被清除，
 *       清除本身不产生结果记录。</li>
 * </ul>
 *
 * <h3>可注入性</h3>
 * 时间与调度全部来自 {@link TimerService}：封窗与清除是事件时间定时器，
 * 因而测试无需任何线程睡眠即可确定地推进。
 */
public final class SessionWindowOperator {

    private static final class WindowState {
        final String key;
        Window window;
        long aggregate;
        boolean sealed;

        WindowState(String key, Window window, long aggregate, boolean sealed) {
            this.key = key;
            this.window = window;
            this.aggregate = aggregate;
            this.sealed = sealed;
        }

        long sealAt(long gap) {
            return saturatingAdd(window.end(), gap);
        }
    }

    private final long gap;
    private final long allowedLateness;
    private final AggregateFunction<?> agg;
    private final TimerService timerService;

    private final Map<String, List<WindowState>> activeByKey = new LinkedHashMap<>();
    private final Map<String, List<WindowState>> sealedByKey = new LinkedHashMap<>();
    private final List<WindowUpdate> changelog = new ArrayList<>();

    private long updateSequence = 0;
    private long receivedEvents = 0;
    private long droppedLateEvents = 0;

    public SessionWindowOperator(long gap, long allowedLateness,
                                 AggregateFunction<?> agg, TimerService timerService) {
        if (gap < 0) {
            throw new IllegalArgumentException("gap must be >= 0");
        }
        if (allowedLateness < 0) {
            throw new IllegalArgumentException("allowedLateness must be >= 0");
        }
        this.gap = gap;
        this.allowedLateness = allowedLateness;
        this.agg = agg;
        this.timerService = timerService;
    }

    // ---------------------------------------------------------------------
    // 对外 API
    // ---------------------------------------------------------------------

    /**
     * 处理一个事件：先经门控（迟到超界则丢弃），再做窗口合并，
     * 最后让本轮新注册且已到点的定时器触发。
     */
    public void processElement(Event event) {
        receivedEvents++;
        if (event.timestamp() < saturatingSub(
                timerService.currentWatermark(), allowedLateness)) {
            droppedLateEvents++;
            return;
        }
        mergeIntoWindow(event);
        // 注意：此处不做"同值推进水位线"。mergeIntoWindow 对合并后立即满足
        // 封窗条件的窗口会直接封存（含注册其清除定时器）；重跑到点定时器会错误地
        // 再次触发严格清除定时器，导致刚封存的状态被立刻清除。
    }

    /** 推进水位线（触发到点的封窗/清除定时器）。 */
    public void advanceWatermark(long newWatermark) {
        timerService.advanceWatermark(newWatermark);
    }

    /**
     * 有界输入收尾：把水位线推进正无穷，封存全部活动窗口并清除全部已封存窗口。
     * 收尾后所有窗口状态为空。
     */
    public void finish() {
        timerService.advanceWatermark(Long.MAX_VALUE);
    }

    public List<WindowUpdate> changelog() {
        return List.copyOf(changelog);
    }

    public long receivedEvents() {
        return receivedEvents;
    }

    public long droppedLateEvents() {
        return droppedLateEvents;
    }

    /** 某 key 当前活动（未封存）窗口数，用于状态清理检查。 */
    public int activeWindowCount(String key) {
        return activeByKey.getOrDefault(key, List.of()).size();
    }

    /** 某 key 当前已封存但尚未清除的窗口数。 */
    public int sealedWindowCount(String key) {
        return sealedByKey.getOrDefault(key, List.of()).size();
    }

    public int totalActiveWindowCount() {
        return activeByKey.values().stream().mapToInt(List::size).sum();
    }

    public int totalRetainedWindowCount() {
        return totalActiveWindowCount()
                + sealedByKey.values().stream().mapToInt(List::size).sum();
    }

    public long currentWatermark() {
        return timerService.currentWatermark();
    }

    // ---------------------------------------------------------------------
    // 核心合并逻辑
    // ---------------------------------------------------------------------

    private void mergeIntoWindow(Event event) {
        String key = event.key();
        long t = event.timestamp();
        List<WindowState> actives = activeByKey.computeIfAbsent(key, k -> new ArrayList<>());
        List<WindowState> sealeds = sealedByKey.computeIfAbsent(key, k -> new ArrayList<>());

        // 1) 从事件本身出发做可达闭包：事件触及的窗口全部并入；
        //    每并入一个窗口，事件的"触及半径"就通过该窗口延伸
        //    （等价于：与事件或已并入窗口相接的窗口都要并入）。
        List<WindowState> picked = new ArrayList<>();
        long newStart = t;
        long newEnd = t;
        long newAgg = addValue(agg.emptyResult(), event.value());

        boolean grew = true;
        while (grew) {
            grew = false;
            // 触判定：候选窗口与"事件 t 或当前生长区间 [newStart,newEnd]"相接即并入。
            // 事件与窗口：t 落在 [start-gap, end+gap]；窗口与生长区间：间隙 <= gap。
            Window growing = new Window(newStart, newEnd);
            List<WindowState> candidates = new ArrayList<>(actives.size() + sealeds.size());
            candidates.addAll(actives);
            candidates.addAll(sealeds);
            for (WindowState s : candidates) {
                if (picked.contains(s)) {
                    continue;
                }
                boolean touchedByEvent = s.window.connects(t, gap);
                boolean touchedByGrowing = growing.connects(s.window, gap);
                if (touchedByEvent || touchedByGrowing) {
                    picked.add(s);
                    newStart = Math.min(newStart, s.window.start());
                    newEnd = Math.max(newEnd, s.window.end());
                    newAgg = mergeValues(newAgg, s.aggregate);
                    grew = true;
                }
            }
        }

        // 2) 对被并入的已封存窗口，按窗口起点顺序先发 RETRACT。
        picked.stream()
                .filter(s -> s.sealed)
                .sorted(Comparator.comparingLong(s -> s.window.start()))
                .forEach(s -> emit(ResultKind.RETRACT, key, s.window, s.aggregate));

        // 3) 摘除旧状态，构造合并后的新窗口。
        actives.removeAll(picked);
        sealeds.removeAll(picked);

        WindowState merged = new WindowState(key, new Window(newStart, newEnd), newAgg, false);
        long sealTime = merged.sealAt(gap);
        if (timerService.currentWatermark() >= sealTime) {
            sealState(merged); // 立即封存：迟到合并产生的大窗口可能已过封窗点
        } else {
            actives.add(merged);
            timerService.registerTimer(sealTime, () -> {
                if (actives.contains(merged)) {
                    sealState(merged);
                }
            });
        }
    }

    private void sealState(WindowState state) {
        if (state.sealed) {
            return;
        }
        String key = state.key;
        List<WindowState> actives = activeByKey.get(key);
        List<WindowState> sealeds = sealedByKey.get(key);
        state.sealed = true;
        if (actives != null) {
            actives.remove(state);
        }
        sealeds.add(state);
        emit(ResultKind.ADD, key, state.window, state.aggregate);

        // 清除使用严格 W > purgeAt：在 W == purgeAt 这一刻，
        // 事件 t = purgeAt 仍满足门控（t >= W-L），状态必须还在。
        long purgeAt = saturatingAdd(state.sealAt(gap), allowedLateness);
        timerService.registerTimerAfter(purgeAt, () -> {
            // 若该窗口之后被迟到事件并入，state 身份已不在 sealed 列表中：空操作。
            sealeds.remove(state);
        });
    }

    private void emit(ResultKind kind, String key, Window window, long aggregate) {
        changelog.add(new WindowUpdate(updateSequence++, kind, key, window, aggregate,
                timerService.currentWatermark()));
    }

    @SuppressWarnings("unchecked")
    private long addValue(long result, long value) {
        return ((AggregateFunction<Object>) agg).add(result, value);
    }

    @SuppressWarnings("unchecked")
    private long mergeValues(long a, long b) {
        return ((AggregateFunction<Object>) agg).merge(a, b);
    }

    static long saturatingAdd(long a, long b) {
        long r = a + b;
        if (b > 0 && r < a) {
            return Long.MAX_VALUE;
        }
        if (b < 0 && r > a) {
            return Long.MIN_VALUE;
        }
        return r;
    }

    static long saturatingSub(long a, long b) {
        if (b == 0 || a == Long.MIN_VALUE) {
            return a; // MIN_VALUE(-inf) - L 仍为 -inf
        }
        long r = a - b;
        return r > a ? Long.MIN_VALUE : r; // 下溢
    }
}
