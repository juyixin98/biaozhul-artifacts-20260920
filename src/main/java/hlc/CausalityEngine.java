package hlc;

import java.util.ArrayList;
import java.util.Collections;
import java.util.HashMap;
import java.util.LinkedHashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;

/**
 * Deterministic, single-threaded engine that interleaves events across virtual nodes and
 * checks the central HLC correctness contract:
 *
 * <ul>
 *   <li><b>Soundness (causal &rArr; timestamp):</b> if event <i>a</i> happens-before event
 *       <i>b</i> &mdash; by thread/program order or by a send&rarr;receive link, transitively
 *       &mdash; then {@code timestamp(a) < timestamp(b)}.</li>
 *   <li><b>Converse does NOT hold:</b> two events may be ordered by timestamp while being
 *       <i>concurrent</i> (neither happens-before the other). {@link Report} lists such
 *       pairs explicitly, so callers can see that timestamp order cannot be inverted into
 *       causal order.</li>
 * </ul>
 *
 * Every event pins the node's virtual physical clock to an explicit microsecond value, so
 * clock skew and physical-clock regression are part of the scenario input, not luck.
 */
public final class CausalityEngine {

    public enum Kind { LOCAL, SEND, RECV }

    /** One step of the interleaving. {@code sendRef} is set only for {@link Kind#RECV}. */
    public record EventSpec(String label, String node, Kind kind, long physicalMicros,
                            String sendRef) {

        public EventSpec {
            if (label == null || label.isBlank()) {
                throw new HLCException("event label is required");
            }
            if (node == null || node.isBlank()) {
                throw new HLCException("node is required for event " + label);
            }
            if (kind == null) {
                throw new HLCException("kind is required for event " + label);
            }
            if (physicalMicros < 0) {
                throw new HLCException("physicalMicros must be non-negative for event " + label);
            }
            if (kind == Kind.RECV && (sendRef == null || sendRef.isBlank())) {
                throw new HLCException("RECV event " + label + " must reference a SEND event");
            }
            if (kind != Kind.RECV && sendRef != null) {
                throw new HLCException("only RECV events may carry sendRef (event " + label + ")");
            }
        }
    }

    /** The timestamped result of processing one event. */
    public record Result(int step, String label, String node, Kind kind, long physicalMicros,
                         HLCTimestamp timestamp) {
    }

    /** An ordered or concurrent event pair used in the report. */
    public record Pair(String earlier, String later, HLCTimestamp earlierTs,
                       HLCTimestamp laterTs, boolean happensBefore, String reason) {
    }

    /** Outcome of running a full scenario. */
    public static final class Report {
        private final List<Result> results;
        private final List<Pair> violations;
        private final List<Pair> concurrentButOrdered;
        private final Map<String, Set<String>> predecessors;

        Report(List<Result> results, List<Pair> violations, List<Pair> concurrentButOrdered,
               Map<String, Set<String>> predecessors) {
            this.results = List.copyOf(results);
            this.violations = List.copyOf(violations);
            this.concurrentButOrdered = List.copyOf(concurrentButOrdered);
            this.predecessors = predecessors;
        }

        public List<Result> results() {
            return results;
        }

        /** Pairs where happens-before holds but the timestamp order is wrong (must be empty). */
        public List<Pair> violations() {
            return violations;
        }

        /** Pairs ordered by timestamp with NO happens-before relation: the converse fails here. */
        public List<Pair> concurrentButOrdered() {
            return concurrentButOrdered;
        }

        public boolean soundnessHolds() {
            return violations.isEmpty();
        }

        public Map<String, Set<String>> happensBeforeGraph() {
            return predecessors;
        }
    }

    private CausalityEngine() {
    }

    /**
     * Process events in the given global interleaving order.
     *
     * @param specs events, already ordered as they are scheduled across nodes
     */
    public static Report run(List<EventSpec> specs) {
        Set<String> seenLabels = new LinkedHashSet<>();
        for (EventSpec s : specs) {
            if (!seenLabels.add(s.label())) {
                throw new HLCException("duplicate event label: " + s.label());
            }
        }
        // Per-node clocks, each driven by its own virtual physical clock.
        Map<String, PhysicalClock.VirtualClock> phys = new LinkedHashMap<>();
        Map<String, HLCClock> clocks = new LinkedHashMap<>();
        Map<String, HLCTimestamp> inFlight = new HashMap<>();

        List<Result> results = new ArrayList<>();
        // Direct happens-before predecessor labels: previous event on the node, plus (for
        // RECV) the referenced SEND event.
        Map<String, List<String>> directPreds = new LinkedHashMap<>();
        Map<String, String> lastOnNode = new HashMap<>();

        for (int i = 0; i < specs.size(); i++) {
            EventSpec e = specs.get(i);
            PhysicalClock.VirtualClock vc =
                    phys.computeIfAbsent(e.node(), n -> new PhysicalClock.VirtualClock(e.physicalMicros()));
            HLCClock clock = clocks.computeIfAbsent(e.node(), n -> new HLCClock(vc));

            // Pin what this node believes the physical time to be right now — may regress.
            vc.set(e.physicalMicros());

            HLCTimestamp ts;
            List<String> preds = new ArrayList<>(1);
            switch (e.kind()) {
                case LOCAL -> ts = clock.tickLocal();
                case SEND -> {
                    ts = clock.send();
                    inFlight.put(e.label(), ts);
                }
                case RECV -> {
                    HLCTimestamp messageTs = inFlight.get(e.sendRef());
                    if (messageTs == null) {
                        throw new HLCException("event " + e.label() + " receives from unknown or "
                                + "not-yet-sent message '" + e.sendRef() + "'");
                    }
                    ts = clock.receive(messageTs);
                    preds.add(e.sendRef());
                }
                default -> throw new HLCException("unknown event kind: " + e.kind());
            }
            String prev = lastOnNode.put(e.node(), e.label());
            if (prev != null) {
                preds.add(prev);
            }
            directPreds.put(e.label(), preds);
            results.add(new Result(i, e.label(), e.node(), e.kind(), e.physicalMicros(), ts));
        }

        Map<String, Set<String>> closure = transitiveClosure(specs, directPreds);
        Map<String, Result> byLabel = new LinkedHashMap<>();
        results.forEach(r -> byLabel.put(r.label(), r));

        List<Pair> violations = new ArrayList<>();
        List<Pair> concurrentButOrdered = new ArrayList<>();
        for (int i = 0; i < results.size(); i++) {
            for (int j = 0; j < results.size(); j++) {
                if (i == j) {
                    continue;
                }
                Result a = results.get(i);
                Result b = results.get(j);
                // closure.get(x) holds x's predecessors, so a -> b iff a is in closure[b].
                boolean aHappensBeforeB = closure.get(b.label()).contains(a.label());
                boolean bHappensBeforeA = closure.get(a.label()).contains(b.label());
                int cmp = a.timestamp().compareTo(b.timestamp());
                if (aHappensBeforeB && cmp >= 0) {
                    violations.add(new Pair(a.label(), b.label(), a.timestamp(), b.timestamp(),
                            true, "happens-before but timestamp not strictly less"));
                }
                if (!aHappensBeforeB && !bHappensBeforeA && cmp < 0) {
                    concurrentButOrdered.add(new Pair(a.label(), b.label(), a.timestamp(),
                            b.timestamp(), false,
                            "concurrent events; timestamp order is arbitrary, not causal"));
                }
            }
        }
        return new Report(results, violations, concurrentButOrdered, closure);
    }

    private static Map<String, Set<String>> transitiveClosure(
            List<EventSpec> specs, Map<String, List<String>> directPreds) {
        Map<String, Set<String>> closure = new LinkedHashMap<>();
        for (EventSpec e : specs) {
            closure.put(e.label(), new LinkedHashSet<>());
        }
        // Events are processed in scheduling order; predecessors always appear earlier, so a
        // simple forward accumulation reaches a fixed point.
        for (EventSpec e : specs) {
            Set<String> reachable = closure.get(e.label());
            for (String p : directPreds.get(e.label())) {
                reachable.add(p);
                reachable.addAll(closure.get(p));
            }
        }
        return Collections.unmodifiableMap(closure);
    }
}
