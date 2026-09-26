package com.migration.planner;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.migration.planner.api.HttpApiServer;
import com.migration.planner.api.PlanService;
import com.migration.planner.graph.GraphLoader;
import com.migration.planner.model.MigrationPlan;
import com.migration.planner.model.PlanRequest;
import com.migration.planner.model.SchemaGraph;
import com.migration.planner.plan.PlanException;

import java.nio.file.Files;
import java.nio.file.Path;

/**
 * Entry point.
 *
 * <pre>
 *   java -jar migration-planner.jar [--port N] [--graph path/to/graph.json]
 *   java -jar migration-planner.jar --cli request.json [--graph path/to/graph.json]
 * </pre>
 *
 * Server mode listens on the given port (default 8080). CLI mode reads one plan
 * request from a JSON file and prints the plan JSON to stdout.
 */
public final class Main {

    private Main() {
    }

    public static void main(String[] args) throws Exception {
        int port = 8080;
        Path graphPath = null;
        Path cliRequest = null;
        for (int i = 0; i < args.length; i++) {
            switch (args[i]) {
                case "--port" -> port = Integer.parseInt(args[++i]);
                case "--graph" -> graphPath = Path.of(args[++i]);
                case "--cli" -> cliRequest = Path.of(args[++i]);
                default -> {
                    System.err.println("unknown argument: " + args[i]);
                    System.exit(2);
                }
            }
        }

        ObjectMapper mapper = new ObjectMapper();
        GraphLoader loader = new GraphLoader(mapper);
        SchemaGraph graph = graphPath == null ? loader.loadDefault() : loader.load(graphPath);
        PlanService service = new PlanService(graph);

        if (cliRequest != null) {
            runCli(service, mapper, cliRequest);
            return;
        }

        HttpApiServer server = HttpApiServer.start(service, mapper, port);
        System.out.println("migration-planner listening on port " + server.port()
                + " (graph " + graph.graphId() + " v" + graph.graphVersion() + ")");
        Thread.currentThread().join();
    }

    private static void runCli(PlanService service, ObjectMapper mapper, Path requestFile)
            throws Exception {
        PlanRequest request = mapper.readValue(Files.readString(requestFile), PlanRequest.class);
        try {
            MigrationPlan plan = service.plan(request);
            System.out.println(mapper.writerWithDefaultPrettyPrinter().writeValueAsString(plan));
        } catch (PlanException e) {
            var body = mapper.createObjectNode();
            body.put("ok", false);
            var error = body.putObject("error");
            error.put("code", e.code().name());
            error.put("message", e.getMessage());
            error.set("details", mapper.valueToTree(e.details()));
            System.out.println(mapper.writerWithDefaultPrettyPrinter().writeValueAsString(body));
        }
    }
}
