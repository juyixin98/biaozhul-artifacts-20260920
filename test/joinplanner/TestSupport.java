package joinplanner;

import joinplanner.json.JsonParser;
import joinplanner.json.JsonWriter;
import joinplanner.plan.PlanService;
import joinplanner.plan.EnumerateService;
import joinplanner.model.Spec;
import joinplanner.model.SpecParser;
import joinplanner.sim.SimulationService;

import java.util.Map;

/** Convenience entry points used by tests. */
public final class TestSupport {

    private TestSupport() {}

    @SuppressWarnings("unchecked")
    public static Map<String, Object> parse(String json) {
        return (Map<String, Object>) JsonParser.parse(json);
    }

    public static Spec spec(String json) {
        return SpecParser.parse(parse(json));
    }

    public static Map<String, Object> plan(String json) {
        return new PlanService().plan(spec(json));
    }

    public static Map<String, Object> enumerate(String json) {
        return new EnumerateService().enumerate(spec(json));
    }

    public static Map<String, Object> simulate(String json) {
        return new SimulationService().run(parse(json));
    }

    public static String pretty(Object o) {
        return JsonWriter.writePretty(o);
    }
}
