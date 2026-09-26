package com.migration.planner.plan;

import com.migration.planner.model.AlternativePath;
import com.migration.planner.model.Checkpoint;
import com.migration.planner.model.MigrationEdge;
import com.migration.planner.model.MigrationPlan;
import com.migration.planner.model.PlanStep;
import com.migration.planner.model.RollbackInfo;
import com.migration.planner.model.SchemaGraph;

import java.util.ArrayList;
import java.util.List;
import java.util.Optional;

/** Turns a planner search result into a full plan with steps and checkpoints. */
public final class PlanBuilder {

    public MigrationPlan build(SchemaGraph graph, PathPlanner.SearchResult result,
                               String from, String to, String tzdbVersion) {
        List<PlanStep> steps = buildSteps(result.chosen().edges());
        List<Checkpoint> checkpoints = buildCheckpoints(graph, result.chosen());
        List<AlternativePath> alternatives = result.alternatives().stream()
                .map(p -> new AlternativePath(p.nodes(), p.totalCost()))
                .toList();
        return new MigrationPlan(
                true,
                from,
                to,
                result.chosen().totalCost(),
                steps,
                checkpoints,
                alternatives,
                !alternatives.isEmpty(),
                result.truncated(),
                graph.warnings(),
                graph.graphId(),
                graph.graphVersion(),
                tzdbVersion);
    }

    private List<PlanStep> buildSteps(List<MigrationEdge> edges) {
        List<PlanStep> steps = new ArrayList<>();
        for (int i = 0; i < edges.size(); i++) {
            MigrationEdge e = edges.get(i);
            steps.add(new PlanStep(i + 1, e.id(), e.from(), e.to(), e.cost(),
                    e.description(), e.preconditions()));
        }
        return steps;
    }

    private List<Checkpoint> buildCheckpoints(SchemaGraph graph, PathPlanner.ScoredPath chosen) {
        List<Checkpoint> checkpoints = new ArrayList<>();
        long cumulative = 0L;
        List<MigrationEdge> edges = chosen.edges();
        for (int i = 0; i < edges.size(); i++) {
            MigrationEdge edge = edges.get(i);
            cumulative += edge.cost();
            checkpoints.add(new Checkpoint(i + 1, edge.to(), cumulative, rollbackOf(graph, edge)));
        }
        return checkpoints;
    }

    private RollbackInfo rollbackOf(SchemaGraph graph, MigrationEdge edge) {
        Optional<MigrationEdge> inverse = graph.findInverse(edge);
        if (inverse.isPresent()) {
            return RollbackInfo.available(inverse.get().id(), inverse.get().cost());
        }
        String reason = edge.reversible()
                ? "edge " + edge.id() + " declares reversible=true but no real inverse edge exists"
                : "no inverse edge from " + edge.to() + " to " + edge.from();
        return RollbackInfo.unavailable(reason);
    }
}
