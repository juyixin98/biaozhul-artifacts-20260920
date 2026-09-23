package joinplanner.sim;

import java.util.LinkedHashMap;
import java.util.Map;

import joinplanner.core.BadRequestException;
import joinplanner.core.DisconnectedGraphException;
import joinplanner.json.JsonParseException;
import joinplanner.json.JsonParser;
import joinplanner.web.ServiceResult;

/** Orchestrates /api/simulate. */
public final class SimService {

    public static final SimService INSTANCE = new SimService();

    public ServiceResult handle(String body) {
        Object parsed;
        try {
            parsed = JsonParser.parse(body);
        } catch (JsonParseException e) {
            return error(400, "INVALID_JSON", e.getMessage());
        }

        SimConfig cfg;
        try {
            cfg = new SimRequestParser().parse(parsed);
        } catch (BadRequestException e) {
            return error(400, "BAD_REQUEST", e.getMessage());
        }

        Simulator.SimResult result;
        try {
            result = new Simulator().run(cfg);
        } catch (BadRequestException e) {
            return error(400, "BAD_REQUEST", e.getMessage());
        } catch (DisconnectedGraphException e) {
            Map<String, Object> m = new LinkedHashMap<>();
            m.put("error", "DISCONNECTED_GRAPH");
            m.put("message", e.getMessage());
            m.put("components", e.components());
            return ServiceResult.of(422, m);
        } catch (SimOverflowException e) {
            Map<String, Object> m = new LinkedHashMap<>();
            m.put("error", "SIMULATION_TOO_LARGE");
            m.put("message", e.getMessage());
            m.put("limit", e.limit());
            m.put("subset", e.subsetDesc());
            m.put("remedy", SimResponseBuilder.overflowHint(e.limit()));
            return ServiceResult.of(422, m);
        }

        return ServiceResult.ok(new SimResponseBuilder().build(result));
    }

    private ServiceResult error(int status, String code, String message) {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("error", code);
        m.put("message", message);
        return ServiceResult.of(status, m);
    }
}
