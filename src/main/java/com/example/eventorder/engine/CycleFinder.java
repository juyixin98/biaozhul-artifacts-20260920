package com.example.eventorder.engine;

import com.example.eventorder.model.Conflict;
import java.util.ArrayDeque;
import java.util.ArrayList;
import java.util.Deque;
import java.util.HashMap;
import java.util.HashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.TreeSet;

/**
 * Finds dependency cycles and renders each as a minimal readable chain.
 * For every non-trivial strongly connected component the shortest cycle through
 * the component's lexicographically smallest node is reported, so output is
 * deterministic and as small as the component allows.
 */
public final class CycleFinder {

    private CycleFinder() {
    }

    public static List<Conflict> findCycles(DependencyGraph graph) {
        List<Conflict> conflicts = new ArrayList<>();
        for (String id : graph.selfLoops()) {
            conflicts.add(new Conflict("CYCLE", List.of(id, id),
                    "event '" + id + "' depends on itself"));
        }
        for (Set<String> scc : stronglyConnectedComponents(graph)) {
            if (scc.size() <= 1) {
                continue; // single-node components without self-loop are acyclic
            }
            String start = new TreeSet<>(scc).first();
            List<String> cycle = shortestCycleThrough(graph, scc, start);
            conflicts.add(new Conflict("CYCLE", cycle,
                    "circular dependency: " + String.join(" -> ", cycle)));
        }
        return conflicts;
    }

    /** Tarjan's algorithm; components returned in deterministic (sorted-first-member) order. */
    static List<Set<String>> stronglyConnectedComponents(DependencyGraph graph) {
        TarjanState state = new TarjanState();
        for (String id : graph.nodeIds()) {
            if (!state.index.containsKey(id)) {
                strongConnect(graph, id, state);
            }
        }
        List<Set<String>> components = new ArrayList<>(state.components);
        components.sort((a, b) -> new TreeSet<>(a).first().compareTo(new TreeSet<>(b).first()));
        return components;
    }

    private static void strongConnect(DependencyGraph graph, String node, TarjanState state) {
        state.index.put(node, state.nextIndex);
        state.lowLink.put(node, state.nextIndex);
        state.nextIndex++;
        state.stack.push(node);
        state.onStack.add(node);

        for (String target : graph.successorsOf(node)) {
            if (!state.index.containsKey(target)) {
                strongConnect(graph, target, state);
                state.lowLink.merge(node, state.lowLink.get(target), Math::min);
            } else if (state.onStack.contains(target)) {
                state.lowLink.merge(node, state.index.get(target), Math::min);
            }
        }

        if (state.lowLink.get(node).equals(state.index.get(node))) {
            Set<String> component = new HashSet<>();
            String member;
            do {
                member = state.stack.pop();
                state.onStack.remove(member);
                component.add(member);
            } while (!member.equals(node));
            state.components.add(component);
        }
    }

    /**
     * BFS for the shortest walk start -&gt; ... -&gt; start inside {@code scc}. Successor
     * lists are sorted, so among equal-length cycles the lexicographically smallest
     * path is chosen. An SCC of size &gt; 1 always contains such a walk.
     */
    private static List<String> shortestCycleThrough(DependencyGraph graph, Set<String> scc,
                                                     String start) {
        Map<String, String> parent = new HashMap<>();
        Deque<String> queue = new ArrayDeque<>();
        for (String next : graph.successorsOf(start)) {
            if (scc.contains(next) && !next.equals(start)) {
                parent.put(next, start);
                queue.add(next);
            }
        }
        String back = null;
        while (!queue.isEmpty()) {
            String current = queue.poll();
            if (graph.successorsOf(current).contains(start)) {
                back = current;
                break;
            }
            for (String next : graph.successorsOf(current)) {
                if (scc.contains(next) && !next.equals(current) && !parent.containsKey(next)) {
                    parent.put(next, current);
                    queue.add(next);
                }
            }
        }

        List<String> reversedPath = new ArrayList<>();
        for (String node = back; node != null; node = node.equals(start) ? null : parent.get(node)) {
            reversedPath.add(node);
            if (node.equals(start)) {
                break;
            }
        }
        List<String> cycle = new ArrayList<>(reversedPath.size() + 1);
        for (int i = reversedPath.size() - 1; i >= 0; i--) {
            cycle.add(reversedPath.get(i));
        }
        cycle.add(start);
        return cycle;
    }

    private static final class TarjanState {
        private final Map<String, Integer> index = new HashMap<>();
        private final Map<String, Integer> lowLink = new HashMap<>();
        private final Deque<String> stack = new ArrayDeque<>();
        private final Set<String> onStack = new HashSet<>();
        private final List<Set<String>> components = new ArrayList<>();
        private int nextIndex;
    }
}
