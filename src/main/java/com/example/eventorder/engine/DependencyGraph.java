package com.example.eventorder.engine;

import com.example.eventorder.model.DependencySpec;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.TreeMap;
import java.util.TreeSet;

/**
 * Directed dependency graph: edge before -&gt; after. Adjacency lists are sorted
 * so every traversal is deterministic.
 */
public final class DependencyGraph {

    private final Map<String, List<String>> successors;
    private final Map<String, List<String>> predecessors;
    private final Set<String> selfLoops;

    private DependencyGraph(Map<String, List<String>> successors,
                            Map<String, List<String>> predecessors,
                            Set<String> selfLoops) {
        this.successors = successors;
        this.predecessors = predecessors;
        this.selfLoops = selfLoops;
    }

    public static DependencyGraph build(ValidatedInput input) {
        Map<String, TreeSet<String>> sortedSucc = new TreeMap<>();
        Map<String, TreeSet<String>> sortedPred = new TreeMap<>();
        for (String id : input.events().keySet()) {
            sortedSucc.put(id, new TreeSet<>());
            sortedPred.put(id, new TreeSet<>());
        }
        Set<String> selfLoops = new TreeSet<>();
        for (DependencySpec dep : input.dependencies()) {
            if (dep.before().equals(dep.after())) {
                selfLoops.add(dep.before());
            }
            // Self-loops are still recorded as ordinary edges so indegree-based
            // algorithms (Kahn) see them; CycleFinder reports them separately.
            sortedSucc.get(dep.before()).add(dep.after());
            sortedPred.get(dep.after()).add(dep.before());
        }
        Map<String, List<String>> successors = new TreeMap<>();
        Map<String, List<String>> predecessors = new TreeMap<>();
        sortedSucc.forEach((id, targets) -> successors.put(id, List.copyOf(targets)));
        sortedPred.forEach((id, sources) -> predecessors.put(id, List.copyOf(sources)));
        return new DependencyGraph(successors, predecessors, selfLoops);
    }

    /** Sorted successor ids of {@code id} (empty list when none). */
    public List<String> successorsOf(String id) {
        return successors.getOrDefault(id, List.of());
    }

    /** Sorted predecessor ids of {@code id} (empty list when none). */
    public List<String> predecessorsOf(String id) {
        return predecessors.getOrDefault(id, List.of());
    }

    /** All event ids in sorted order. */
    public List<String> nodeIds() {
        return new ArrayList<>(successors.keySet());
    }

    public Set<String> selfLoops() {
        return selfLoops;
    }

    /** Indegree map derived from the predecessor lists (self-loops included). */
    public Map<String, Integer> indegrees() {
        Map<String, Integer> indegree = new TreeMap<>();
        predecessors.forEach((id, sources) -> indegree.put(id, sources.size()));
        return indegree;
    }
}
