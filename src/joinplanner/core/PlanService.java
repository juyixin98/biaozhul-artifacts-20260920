package joinplanner.core;

import java.util.LinkedHashMap;
import java.util.Map;

import joinplanner.json.JsonParseException;
import joinplanner.json.JsonParser;
import joinplanner.web.ServiceResult;

/** Orchestrates /api/plan: parse → validate → DP → explain. */
public final class PlanService {

    public static final PlanService INSTANCE = new PlanService();

    public ServiceResult handle(String body) {
        Object parsed;
        try {
            parsed = JsonParser.parse(body);
        } catch (JsonParseException e) {
            return error(400, "INVALID_JSON", e.getMessage());
        }

        ValidatedProblem problem;
        try {
            problem = new ProblemParser().parse(parsed);
        } catch (BadRequestException e) {
            return error(400, "BAD_REQUEST", e.getMessage());
        }

        JoinPlanner planner;
        DpResult dp;
        try {
            planner = new JoinPlanner(problem);
            dp = planner.plan();
        } catch (DisconnectedGraphException e) {
            Map<String, Object> m = new LinkedHashMap<>();
            m.put("error", "DISCONNECTED_GRAPH");
            m.put("message", e.getMessage()
                    + "; no single inner-join plan exists without Cartesian products");
            m.put("components", e.components());
            m.put("remedy", "add join predicates connecting the components, or resend "
                    + "with \"allowCrossProducts\": true to plan Cartesian products");
            return ServiceResult.of(422, m);
        }

        OrderEnumerator enumerator = new OrderEnumerator(problem, planner);
        var orders = enumerator.enumerateLeftDeep();
        Map<String, Object> resp = new PlanResponseBuilder(problem).build(dp, orders);
        return ServiceResult.ok(resp);
    }

    private ServiceResult error(int status, String code, String message) {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("error", code);
        m.put("message", message);
        return ServiceResult.of(status, m);
    }
}
