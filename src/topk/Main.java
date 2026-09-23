package topk;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * CLI entry point.
 *
 *   java topk.Main --data data/sample.json --k 3 --shards 4 \
 *                  [--budget 1000] [--merge-order 2,0,1,3] \
 *                  [--export-plan plan.json] [--export-data snapshot.json]
 *
 * Prints the grouped TopK result as JSON to stdout.
 */
public final class Main {

    public static void main(String[] args) throws IOException {
        String dataPath = null;
        String exportPlan = null;
        String exportData = null;
        String mergeOrderArg = null;
        int k = -1;
        int shards = 1;
        int budget = 1000;

        for (int i = 0; i < args.length; i++) {
            switch (args[i]) {
                case "--data": dataPath = args[++i]; break;
                case "--k": k = Integer.parseInt(args[++i]); break;
                case "--shards": shards = Integer.parseInt(args[++i]); break;
                case "--budget": budget = Integer.parseInt(args[++i]); break;
                case "--merge-order": mergeOrderArg = args[++i]; break;
                case "--export-plan": exportPlan = args[++i]; break;
                case "--export-data": exportData = args[++i]; break;
                default:
                    System.err.println("unknown arg: " + args[i]);
                    System.exit(2);
            }
        }
        if (dataPath == null || k < 0) {
            System.err.println("usage: Main --data FILE --k N [--shards N] [--budget N] "
                    + "[--merge-order a,b,c] [--export-plan FILE] [--export-data FILE]");
            System.exit(2);
        }

        DataSet data = new DataSet();
        Map<String, Object> doc = Json.parseObject(Files.readString(Path.of(dataPath)));
        Object rowsObj = doc.get("rows");
        if (!(rowsObj instanceof List)) {
            System.err.println("data file must contain a 'rows' array");
            System.exit(2);
        }
        for (Object o : (List<?>) rowsObj) {
            Map<?, ?> m = (Map<?, ?>) o;
            data.add((String) m.get("group"), ((Number) m.get("value")).longValue());
        }

        List<Integer> mergeOrder = null;
        if (mergeOrderArg != null) {
            mergeOrder = new java.util.ArrayList<>();
            for (String part : mergeOrderArg.split(",")) {
                mergeOrder.add(Integer.parseInt(part.trim()));
            }
        }

        QueryEngine engine = new QueryEngine(data, budget);
        TopKQuery query = new TopKQuery(k, shards, mergeOrder, budget);
        QueryEngine.Result result = engine.execute(query);

        Map<String, Object> out = new LinkedHashMap<>();
        out.put("k", k);
        out.put("shards", shards);
        out.put("result", result.toJson());
        System.out.println(Json.write(out));

        if (exportPlan != null) {
            Files.writeString(Path.of(exportPlan), result.plan.toJsonString());
            System.err.println("plan exported to " + exportPlan);
        }
        if (exportData != null) {
            Files.writeString(Path.of(exportData), Json.write(data.toJson()));
            System.err.println("data exported to " + exportData);
        }
    }
}
