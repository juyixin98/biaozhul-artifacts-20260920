package hllengine.server;

import hllengine.api.ApiException;
import hllengine.engine.Dataset;
import hllengine.engine.LogicalPlan;
import hllengine.engine.PhysicalPlan;
import hllengine.engine.QueryResult;
import hllengine.hll.HllConfig;
import hllengine.hll.HllEstimate;
import hllengine.hll.HllSketch;
import hllengine.json.Json;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;

/**
 * The single-machine, in-memory query engine. Owns two catalogs:
 * datasets (tables) and sketches. Stateless with respect to threading;
 * intended for single-process request processing.
 *
 * <p>Every request is a JSON object {@code {"op": "...", ...}}; batches are
 * {@code {"op":"batch","requests":[...]}} executed in order.
 */
public final class Engine {

    private final Map<String, Dataset> datasets = new LinkedHashMap<>();
    private final Map<String, HllSketch> sketches = new LinkedHashMap<>();

    // ------------------------------------------------------------- dispatch

    public Map<String, Object> handle(Map<String, Object> request) {
        String op;
        try {
            op = Json.asString(request.get("op"), "request.op");
        } catch (hllengine.json.JsonException je) {
            throw new ApiException(ApiException.BAD_FORMAT, je.getMessage());
        }
        try {
            return dispatch(op, request);
        } catch (hllengine.json.JsonException je) {
            // Request-shape validation failures are bad input, never internal errors.
            throw new ApiException(ApiException.BAD_FORMAT, je.getMessage());
        }
    }

    private Map<String, Object> dispatch(String op, Map<String, Object> request) {
        return switch (op) {
            case "createDataset" -> createDataset(request);
            case "appendRows" -> appendRows(request);
            case "listDatasets" -> listDatasets(request);
            case "getDataset" -> getDataset(request);
            case "dropDataset" -> dropDataset(request);
            case "query" -> query(request, false);
            case "explain" -> query(request, true);
            case "createSketch" -> createSketch(request);
            case "add" -> add(request);
            case "addAll" -> addAll(request);
            case "mergeSketches" -> mergeSketches(request);
            case "estimate" -> estimate(request);
            case "listSketches" -> listSketches(request);
            case "exportSketch" -> exportSketch(request);
            case "importSketch" -> importSketch(request);
            case "dropSketch" -> dropSketch(request);
            case "batch" -> batch(request);
            default -> throw new ApiException(ApiException.BAD_REQUEST,
                    "unknown op \"" + op + "\"");
        };
    }

    // ------------------------------------------------------------ datasets

    private Map<String, Object> createDataset(Map<String, Object> req) {
        String name = Json.asString(req.get("name"), "createDataset.name");
        if (datasets.containsKey(name)) {
            throw new ApiException(ApiException.ALREADY_EXISTS, "dataset \"" + name + "\" already exists");
        }
        List<Object> cols = Json.asArray(req.get("columns"), "createDataset.columns");
        if (cols.isEmpty()) {
            throw new ApiException(ApiException.BAD_REQUEST, "createDataset.columns must not be empty");
        }
        List<String> columns = new ArrayList<>();
        for (Object c : cols) {
            String col = Json.asString(c, "createDataset.columns[]");
            if (columns.contains(col)) {
                throw new ApiException(ApiException.BAD_REQUEST, "duplicate column \"" + col + "\"");
            }
            columns.add(col);
        }
        datasets.put(name, new Dataset(name, columns));
        return Map.of("dataset", name, "columns", columns, "rowCount", 0);
    }

    private Map<String, Object> appendRows(Map<String, Object> req) {
        Dataset ds = lookupDataset(Json.asString(req.get("dataset"), "appendRows.dataset"));
        List<Object> rows = Json.asArray(req.get("rows"), "appendRows.rows");
        int before = ds.size();
        ds.appendAll(rows);
        Map<String, Object> out = new LinkedHashMap<>();
        out.put("dataset", ds.name());
        out.put("appended", rows.size());
        out.put("rowCount", ds.size());
        out.put("before", before);
        return out;
    }

    private Map<String, Object> listDatasets(Map<String, Object> req) {
        List<Object> list = new ArrayList<>();
        for (Dataset d : datasets.values()) {
            Map<String, Object> m = new LinkedHashMap<>();
            m.put("name", d.name());
            m.put("columns", d.columns());
            m.put("rowCount", d.size());
            list.add(m);
        }
        return Map.of("datasets", list);
    }

    private Map<String, Object> getDataset(Map<String, Object> req) {
        return lookupDataset(Json.asString(req.get("dataset"), "getDataset.dataset")).exportMap();
    }

    private Map<String, Object> dropDataset(Map<String, Object> req) {
        String name = Json.asString(req.get("dataset"), "dropDataset.dataset");
        if (datasets.remove(name) == null) {
            throw new ApiException(ApiException.NOT_FOUND, "dataset \"" + name + "\" does not exist");
        }
        return Map.of("dropped", "dataset", "name", name);
    }

    private Dataset lookupDataset(String name) {
        Dataset ds = datasets.get(name);
        if (ds == null) {
            throw new ApiException(ApiException.NOT_FOUND, "dataset \"" + name + "\" does not exist");
        }
        return ds;
    }

    // -------------------------------------------------------------- queries

    private Map<String, Object> query(Map<String, Object> req, boolean explainOnly) {
        Map<String, Object> planJson = Json.asObject(req.get("plan"), "query.plan");
        LogicalPlan logical = LogicalPlan.parse(planJson);
        // Fail fast on unknown dataset even in explain mode.
        lookupDataset(logical.dataset());

        PhysicalPlan physical = logical.plan();
        Map<String, Object> out = new LinkedHashMap<>();
        out.put("submittedPlan", planJson);
        out.putAll(logical.describe());
        out.putAll(physical.describe());
        if (!explainOnly) {
            QueryResult result = logical.execute(this::lookupDataset);
            out.put("result", result.toMap());
        } else {
            out.put("explainOnly", true);
        }
        return out;
    }

    // ------------------------------------------------------------- sketches

    private HllConfig configFrom(Map<String, Object> req) {
        int p = req.containsKey("precision")
                ? Json.asInt(req.get("precision"), "precision", HllConfig.MIN_PRECISION, HllConfig.MAX_PRECISION)
                : HllConfig.DEFAULT_PRECISION;
        int seed = req.containsKey("seed") ? (int) Json.asLong(req.get("seed"), "seed") : HllConfig.DEFAULT_SEED;
        String hashId = req.containsKey("hashId")
                ? Json.asString(req.get("hashId"), "hashId")
                : hllengine.hll.MurmurHash3.HASH_ID;
        return new HllConfig(p, seed, hashId);
    }

    private Map<String, Object> createSketch(Map<String, Object> req) {
        String name = Json.asString(req.get("name"), "createSketch.name");
        if (sketches.containsKey(name)) {
            throw new ApiException(ApiException.ALREADY_EXISTS, "sketch \"" + name + "\" already exists");
        }
        HllConfig config = configFrom(req);
        sketches.put(name, new HllSketch(config));
        Map<String, Object> out = new LinkedHashMap<>();
        out.put("sketch", name);
        out.put("config", config.toMap());
        return out;
    }

    private Map<String, Object> add(Map<String, Object> req) {
        HllSketch sketch = lookupSketch(Json.asString(req.get("sketch"), "add.sketch"));
        if (!req.containsKey("value")) {
            throw new ApiException(ApiException.BAD_REQUEST, "add requires a \"value\" field");
        }
        sketch.offerValue(req.get("value"));
        return Map.of("sketch", nameOf(req, "add.sketch"), "observedCount", sketch.observedCount());
    }

    private Map<String, Object> addAll(Map<String, Object> req) {
        HllSketch sketch = lookupSketch(Json.asString(req.get("sketch"), "addAll.sketch"));
        List<Object> values = Json.asArray(req.get("values"), "addAll.values");
        for (Object v : values) {
            sketch.offerValue(v);
        }
        Map<String, Object> out = new LinkedHashMap<>();
        out.put("sketch", nameOf(req, "addAll.sketch"));
        out.put("added", values.size());
        out.put("observedCount", sketch.observedCount());
        return out;
    }

    private Map<String, Object> mergeSketches(Map<String, Object> req) {
        String target = Json.asString(req.get("target"), "mergeSketches.target");
        List<Object> sourceNamesRaw = Json.asArray(req.get("sources"), "mergeSketches.sources");
        if (sourceNamesRaw.isEmpty()) {
            throw new ApiException(ApiException.BAD_REQUEST, "mergeSketches.sources must not be empty");
        }
        List<String> sourceNames = new ArrayList<>();
        Set<String> unique = new LinkedHashSet<>();
        for (Object s : sourceNamesRaw) {
            String n = Json.asString(s, "mergeSketches.sources[]");
            if (!unique.add(n)) {
                throw new ApiException(ApiException.BAD_REQUEST, "duplicate source sketch \"" + n + "\"");
            }
            sourceNames.add(n);
        }
        if (sourceNames.contains(target)) {
            throw new ApiException(ApiException.BAD_REQUEST,
                    "merge target \"" + target + "\" must not also be a source");
        }
        HllSketch targetSketch = lookupSketch(target);
        HllConfig expected = targetSketch.config();
        List<Object> mergedInfo = new ArrayList<>();
        for (String sourceName : sourceNames) {
            HllSketch source = lookupSketch(sourceName);
            String diff = expected.compatibilityDifference(source.config());
            if (diff != null) {
                throw new ApiException(ApiException.INCOMPATIBLE_CONFIG,
                        "cannot merge \"" + sourceName + "\" into \"" + target + "\": " + diff
                                + "; merging requires identical precision, hashId and seed");
            }
        }
        for (String sourceName : sourceNames) {
            targetSketch.merge(lookupSketch(sourceName));
            Map<String, Object> m = new LinkedHashMap<>();
            m.put("source", sourceName);
            m.put("status", "merged");
            mergedInfo.add(m);
        }
        HllEstimate est = targetSketch.estimate();
        Map<String, Object> out = new LinkedHashMap<>();
        out.put("target", target);
        out.put("sources", mergedInfo);
        out.putAll(est.toMap());
        return out;
    }

    private Map<String, Object> estimate(Map<String, Object> req) {
        String name = Json.asString(req.get("sketch"), "estimate.sketch");
        HllSketch sketch = lookupSketch(name);
        Map<String, Object> out = new LinkedHashMap<>();
        out.put("sketch", name);
        out.put("config", sketch.config().toMap());
        out.putAll(sketch.estimate().toMap());
        return out;
    }

    private Map<String, Object> listSketches(Map<String, Object> req) {
        List<Object> list = new ArrayList<>();
        for (Map.Entry<String, HllSketch> e : sketches.entrySet()) {
            HllSketch s = e.getValue();
            Map<String, Object> m = new LinkedHashMap<>();
            m.put("name", e.getKey());
            m.put("config", s.config().toMap());
            m.put("observedCount", s.observedCount());
            m.put("estimatedCardinality", s.estimate().estimatedCardinality());
            m.put("empty", s.isEmpty());
            list.add(m);
        }
        return Map.of("sketches", list);
    }

    private Map<String, Object> exportSketch(Map<String, Object> req) {
        String name = Json.asString(req.get("sketch"), "exportSketch.sketch");
        HllSketch sketch = lookupSketch(name);
        Map<String, Object> out = new LinkedHashMap<>();
        out.put("sketch", name);
        out.putAll(sketch.exportMap());
        return out;
    }

    private Map<String, Object> importSketch(Map<String, Object> req) {
        String name = Json.asString(req.get("name"), "importSketch.name");
        if (sketches.containsKey(name)) {
            throw new ApiException(ApiException.ALREADY_EXISTS, "sketch \"" + name + "\" already exists");
        }
        HllSketch sketch = HllSketch.importFrom(req.containsKey("sketch") ? req.get("sketch") : req.get("serialized"));
        sketches.put(name, sketch);
        Map<String, Object> out = new LinkedHashMap<>();
        out.put("sketch", name);
        out.put("config", sketch.config().toMap());
        out.put("observedCount", sketch.observedCount());
        out.put("estimatedCardinality", sketch.estimate().estimatedCardinality());
        return out;
    }

    private Map<String, Object> dropSketch(Map<String, Object> req) {
        String name = Json.asString(req.get("sketch"), "dropSketch.sketch");
        if (sketches.remove(name) == null) {
            throw new ApiException(ApiException.NOT_FOUND, "sketch \"" + name + "\" does not exist");
        }
        return Map.of("dropped", "sketch", "name", name);
    }

    private HllSketch lookupSketch(String name) {
        HllSketch s = sketches.get(name);
        if (s == null) {
            throw new ApiException(ApiException.NOT_FOUND, "sketch \"" + name + "\" does not exist");
        }
        return s;
    }

    private String nameOf(Map<String, Object> req, String path) {
        return Json.asString(req.get("sketch"), path);
    }

    // ---------------------------------------------------------------- batch

    private Map<String, Object> batch(Map<String, Object> req) {
        List<Object> raw = Json.asArray(req.get("requests"), "batch.requests");
        boolean continueOnError = Boolean.TRUE.equals(req.get("continueOnError"));
        List<Object> results = new ArrayList<>();
        int failures = 0;
        int firstFailure = -1;
        for (int k = 0; k < raw.size(); k++) {
            Map<String, Object> sub = Json.asObject(raw.get(k), "batch.requests[" + k + "]");
            try {
                Map<String, Object> item = new LinkedHashMap<>();
                item.put("index", k);
                item.put("ok", true);
                item.put("op", sub.get("op"));
                item.put("result", handle(sub));
                results.add(item);
            } catch (ApiException ae) {
                Map<String, Object> item = new LinkedHashMap<>();
                item.put("index", k);
                item.put("ok", false);
                item.put("op", sub.get("op"));
                item.put("error", Map.of("code", ae.code(), "message", ae.getMessage()));
                results.add(item);
                failures++;
                if (firstFailure < 0) firstFailure = k;
                if (!continueOnError) {
                    break;
                }
            }
        }
        Map<String, Object> out = new LinkedHashMap<>();
        out.put("results", results);
        out.put("submitted", raw.size());
        out.put("attempted", results.size());
        out.put("failures", failures);
        out.put("firstFailureAtIndex", firstFailure);
        out.put("succeeded", failures == 0);
        return out;
    }
}
