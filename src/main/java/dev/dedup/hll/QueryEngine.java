package dev.dedup.hll;

import java.util.ArrayList;
import java.util.Base64;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * 单机内存查询引擎：顺序执行 JSON 请求中的 op 批处理。
 *
 * 不使用任何 SQL/现成查询引擎——每个算子都在本类与 HLL 类中直接实现。
 * 草图保存在内存 Map 中，进程结束即消失；持久化通过 export/load 完成。
 *
 * 支持的 op：create / add / add-hash / merge / estimate / export / list / drop。
 * 任一 op 失败则批处理在该 op 处中止（之前的 op 已生效），返回 ok=false 与失败下标。
 */
public final class QueryEngine {

    private final Map<String, HllSketch> sketches = new LinkedHashMap<>();

    /** 执行一个已解析的请求对象。 */
    public Map<String, Object> execute(Map<String, Object> request) {
        Object opsObj = request.get("ops");
        if (!(opsObj instanceof List)) {
            return error("INVALID_REQUEST", "顶层必须包含 \"ops\" 数组", -1, null);
        }
        List<?> ops = (List<?>) opsObj;
        if (ops.isEmpty()) {
            return error("INVALID_REQUEST", "\"ops\" 不能为空", -1, null);
        }

        List<Map<String, Object>> planSteps = new ArrayList<>();
        List<Object> results = new ArrayList<>();

        for (int i = 0; i < ops.size(); i++) {
            Object opObj = ops.get(i);
            if (!(opObj instanceof Map)) {
                return error("INVALID_REQUEST", "ops[" + i + "] 必须是对象", i, planSteps);
            }
            @SuppressWarnings("unchecked")
            Map<String, Object> op = (Map<String, Object>) opObj;
            Object typeObj = op.get("op");
            String type = typeObj == null ? null : String.valueOf(typeObj);
            try {
                results.add(runOp(type, op, planSteps, i));
            } catch (RequestException re) {
                return error(re.code, re.getMessage(), i, planSteps);
            } catch (IncompatibleSketchException ice) {
                return error("INCOMPATIBLE_SKETCH", ice.getMessage(), i, planSteps);
            } catch (SketchFormatException sfe) {
                return error("BAD_FORMAT", sfe.getMessage(), i, planSteps);
            }
        }

        Map<String, Object> resp = new LinkedHashMap<>();
        resp.put("ok", true);
        resp.put("opsProcessed", ops.size());
        resp.put("results", results);
        resp.put("executionPlan", buildPlan(request, planSteps));
        resp.put("metadata", metadata());
        return resp;
    }

    public int sketchCount() {
        return sketches.size();
    }

    private Map<String, Object> runOp(String op, Map<String, Object> spec,
                                      List<Map<String, Object>> planSteps, int index) {
        if (op == null) {
            throw new RequestException("INVALID_REQUEST", "缺少 \"op\" 字段");
        }
        switch (op) {
            case "create":
                return opCreate(spec, planSteps, index);
            case "add":
                return opAdd(spec, planSteps, index);
            case "add-hash":
                return opAddHash(spec, planSteps, index);
            case "merge":
                return opMerge(spec, planSteps, index);
            case "estimate":
                return opEstimate(spec, planSteps, index);
            case "export":
                return opExport(spec, planSteps, index);
            case "load":
                return opLoad(spec, planSteps, index);
            case "list":
                return opList(planSteps, index);
            default:
                throw new RequestException("UNKNOWN_OP", "未知算子: " + op);
        }
    }

    private Map<String, Object> opCreate(Map<String, Object> spec,
                                         List<Map<String, Object>> planSteps, int index) {
        String name = requireName(spec);
        int p = requirePrecision(spec);
        planSteps.add(step(index, "create",
                "新建空草图 name=" + name + "，m=" + (1 << p) + "；内存占用 " + (1 << p) + " 字节寄存器"));
        if (sketches.containsKey(name)) {
            throw new RequestException("SKETCH_EXISTS", "草图已存在: " + name);
        }
        sketches.put(name, new HllSketch(new HllConfig(p)));
        Map<String, Object> r = new LinkedHashMap<>();
        r.put("op", "create");
        r.put("name", name);
        r.put("created", true);
        return r;
    }

    private Map<String, Object> opAdd(Map<String, Object> spec,
                                      List<Map<String, Object>> planSteps, int index) {
        String name = requireName(spec);
        HllSketch sketch = requireSketch(name);
        Object valuesObj = spec.get("values");
        if (!(valuesObj instanceof List) || ((List<?>) valuesObj).isEmpty()) {
            throw new RequestException("INVALID_REQUEST", "\"values\" 必须是非空数组");
        }
        List<?> values = (List<?>) valuesObj;
        planSteps.add(step(index, "add",
                "对 " + values.size() + " 个值做类型规范化 -> Murmur3 128(固定种子) -> 取 h1 -> "
                        + "高 " + sketch.config().precision() + " 位定位寄存器、剩余位算 rank 取最大值（重复插入幂等）"));
        int added = 0;
        for (Object v : values) {
            sketch.add(ValueCanonicalizer.encode(v));
            added++;
        }
        Map<String, Object> r = new LinkedHashMap<>();
        r.put("op", "add");
        r.put("name", name);
        r.put("valuesSubmitted", added);
        r.put("note", "已接受插入；HLL 不保留原值，无法给出精确去重计数");
        return r;
    }

    private Map<String, Object> opAddHash(Map<String, Object> spec,
                                          List<Map<String, Object>> planSteps, int index) {
        // 供确定性测试/统计：直接注入 h1（无符号十六进制或十进制 long）
        String name = requireName(spec);
        HllSketch sketch = requireSketch(name);
        Object hashesObj = spec.get("hashes");
        if (!(hashesObj instanceof List) || ((List<?>) hashesObj).isEmpty()) {
            throw new RequestException("INVALID_REQUEST", "\"hashes\" 必须是非空数组");
        }
        List<?> hashes = (List<?>) hashesObj;
        planSteps.add(step(index, "add-hash",
                "直接注入 " + hashes.size() + " 个 64 位 h1（跳过哈希，用于确定性测试）"));
        int n = 0;
        for (Object h : hashes) {
            sketch.addHash(parseU64(h));
            n++;
        }
        Map<String, Object> r = new LinkedHashMap<>();
        r.put("op", "add-hash");
        r.put("name", name);
        r.put("hashesSubmitted", n);
        return r;
    }

    private Map<String, Object> opMerge(Map<String, Object> spec,
                                        List<Map<String, Object>> planSteps, int index) {
        String target = requireName(spec);
        Object sourcesObj = spec.get("sources");
        if (!(sourcesObj instanceof List) || ((List<?>) sourcesObj).size() < 2) {
            throw new RequestException("INVALID_REQUEST", "\"sources\" 必须是至少 2 个草图名的数组");
        }
        List<?> sourceNames = (List<?>) sourcesObj;
        // 目标既可能是 sources[0]（原地合并），也可能不在 sources 中（先创建或新建）
        boolean targetIsFirst = target.equals(String.valueOf(sourceNames.get(0)));
        HllSketch[] src = new HllSketch[sourceNames.size()];
        HllConfig firstConfig = null;
        for (int i = 0; i < sourceNames.size(); i++) {
            String sn = String.valueOf(sourceNames.get(i));
            HllSketch s = sketches.get(sn);
            if (s == null) {
                throw new RequestException("SKETCH_NOT_FOUND", "源草图不存在: " + sn);
            }
            src[i] = s;
            if (firstConfig == null) {
                firstConfig = s.config();
            } else {
                String reason = firstConfig.incompatibilityReason(s.config());
                if (reason != null) {
                    throw new IncompatibleSketchException("源草图 " + sourceNames.get(0) + " 与 " + sn + "：" + reason);
                }
            }
        }
        HllSketch existing = sketches.get(target);
        if (existing != null && !targetIsFirst) {
            String reason = existing.config().incompatibilityReason(firstConfig);
            if (reason != null) {
                throw new IncompatibleSketchException("目标草图 " + target + " 与源草图：" + reason);
            }
        }
        if (existing == null) {
            existing = new HllSketch(firstConfig);
            sketches.put(target, existing);
        }
        planSteps.add(step(index, "merge",
                "兼容性检查(p 与 hashId 必须完全一致) -> 对 " + sourceNames.size()
                        + " 个草图逐寄存器取 max（分片可重叠，合并天然幂等）-> 写入 " + target));
        for (int i = 0; i < src.length; i++) {
            if (targetIsFirst && i == 0) {
                continue; // 原地合并时跳过自己
            }
            existing.mergeWith(src[i]);
        }
        Map<String, Object> r = new LinkedHashMap<>();
        r.put("op", "merge");
        r.put("target", target);
        r.put("sources", sourceNames);
        r.put("merged", true);
        return r;
    }

    private Map<String, Object> opEstimate(Map<String, Object> spec,
                                           List<Map<String, Object>> planSteps, int index) {
        String name = requireName(spec);
        HllSketch sketch = requireSketch(name);
        planSteps.add(step(index, "estimate",
                "读寄存器 -> 调和平均（零寄存器时按线性计数）-> 估计值 + RSE 置信区间；"
                        + "输出标注为估计值而非精确计数"));
        Map<String, Object> r = new LinkedHashMap<>();
        r.put("op", "estimate");
        r.put("name", name);
        r.putAll(estimateResult(sketch));
        return r;
    }

    private Map<String, Object> opExport(Map<String, Object> spec,
                                         List<Map<String, Object>> planSteps, int index) {
        String name = requireName(spec);
        HllSketch sketch = requireSketch(name);
        String format = String.valueOf(spec.getOrDefault("format", "json"));
        planSteps.add(step(index, "export",
                "导出草图（格式=" + format + "）：" + ("binary".equals(format)
                        ? "HLCD v1 二进制 + CRC32" : "HLL-JSON v1，寄存器 Base64")
                        + "；可用 load 重新导入"));
        Map<String, Object> r = new LinkedHashMap<>();
        r.put("op", "export");
        r.put("name", name);
        if ("binary".equals(format)) {
            byte[] bin = BinarySketchCodec.encode(sketch);
            r.put("format", "binary");
            r.put("encoding", "base64");
            r.put("bytes", bin.length);
            r.put("dataBase64", Base64.getEncoder().encodeToString(bin));
        } else if ("json".equals(format)) {
            r.put("format", "json");
            r.put("sketch", SketchJsonCodec.toJson(sketch));
        } else {
            throw new RequestException("INVALID_REQUEST", "未知导出格式: " + format + "（支持 json/binary）");
        }
        return r;
    }

    /** 供 CLI 的 load 隐式算子之外使用：从请求导入草图。 */
    Map<String, Object> opLoad(Map<String, Object> spec, List<Map<String, Object>> planSteps, int index) {        String name = requireName(spec);
        HllSketch loaded;
        Object formatObj = spec.get("format");
        String format = formatObj == null ? null : String.valueOf(formatObj);
        if ("binary".equals(format)) {
            Object data = spec.get("dataBase64");
            if (!(data instanceof String)) {
                throw new RequestException("INVALID_REQUEST", "binary 导入需要 dataBase64 字段");
            }
            byte[] bytes;
            try {
                bytes = Base64.getDecoder().decode((String) data);
            } catch (IllegalArgumentException iae) {
                throw new SketchFormatException("dataBase64 不是合法 Base64: " + iae.getMessage());
            }
            loaded = BinarySketchCodec.decode(bytes);
        } else if ("json".equals(format)) {
            Object sketchObj = spec.get("sketch");
            if (!(sketchObj instanceof Map)) {
                throw new RequestException("INVALID_REQUEST", "json 导入需要 sketch 对象");
            }
            @SuppressWarnings("unchecked")
            Map<String, Object> sm = (Map<String, Object>) sketchObj;
            loaded = SketchJsonCodec.fromJson(sm);
        } else {
            throw new RequestException("INVALID_REQUEST", "load 需要 format=json|binary");
        }
        planSteps.add(step(index, "load",
                "导入草图到 name=" + name + "；严格校验魔数/版本/精度/寄存器值域"
                        + ("binary".equals(format) ? "/CRC32" : "")));
        sketches.put(name, loaded);
        Map<String, Object> r = new LinkedHashMap<>();
        r.put("op", "load");
        r.put("name", name);
        r.put("loaded", true);
        r.put("precision", loaded.config().precision());
        return r;
    }

    private Map<String, Object> opList(List<Map<String, Object>> planSteps, int index) {
        planSteps.add(step(index, "list", "列出当前内存中全部草图及其配置（不输出估计值）"));
        List<Object> items = new ArrayList<>();
        for (Map.Entry<String, HllSketch> e : sketches.entrySet()) {
            Map<String, Object> item = new LinkedHashMap<>();
            item.put("name", e.getKey());
            item.put("precision", e.getValue().config().precision());
            item.put("registerCount", e.getValue().registerCount());
            item.put("hashId", e.getValue().config().hashId());
            item.put("nonzeroRegisters", e.getValue().nonzeroRegisters());
            items.add(item);
        }
        Map<String, Object> r = new LinkedHashMap<>();
        r.put("op", "list");
        r.put("sketches", items);
        return r;
    }

    /** 构造统一的估计结果（所有字段明确标注估计性质与误差）。 */
    public static Map<String, Object> estimateResult(HllSketch sketch) {
        double raw = sketch.estimateRaw();
        long est = Math.round(raw);
        double rse = sketch.relativeStandardError();
        Map<String, Object> r = new LinkedHashMap<>();
        r.put("cardinalityEstimate", est);
        r.put("cardinalityEstimateRaw", round6(raw));
        r.put("exact", false);
        r.put("estimationNote", "HLL 近似估计值，不是精确计数；空集时为 0");
        r.put("relativeStandardError", round6(rse));
        r.put("relativeStandardErrorPct", round6(rse * 100));
        r.put("confidenceIntervals", confidenceIntervals(raw, rse));
        r.put("zeroRegisters", sketch.zeroRegisters());
        r.put("nonzeroRegisters", sketch.nonzeroRegisters());
        return r;
    }

    private static Map<String, Object> confidenceIntervals(double raw, double rse) {
        Map<String, Object> ci = new LinkedHashMap<>();
        ci.put("note", "对称高斯近似区间，仅在大基数时近似有效；不覆盖哈希碰撞；覆盖率为名义值非保证值");
        ci.put("68pct", interval(raw, rse, 1.0));
        ci.put("95pct", interval(raw, rse, 1.96));
        return ci;
    }

    private static Map<String, Object> interval(double raw, double rse, double sigma) {
        double lo = Math.max(0, raw * (1 - sigma * rse));
        double hi = raw * (1 + sigma * rse);
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("low", Math.round(lo));
        m.put("high", Math.round(hi));
        return m;
    }

    private Map<String, Object> buildPlan(Map<String, Object> request, List<Map<String, Object>> steps) {
        Map<String, Object> plan = new LinkedHashMap<>();
        plan.put("planFormat", "HLL-EXECUTION-PLAN/v1");
        plan.put("opCount", ((List<?>) request.get("ops")).size());
        plan.put("steps", steps);
        return plan;
    }

    private Map<String, Object> metadata() {
        Map<String, Object> md = new LinkedHashMap<>();
        md.put("engine", "single-jvm-in-memory-hll");
        md.put("hashAlgorithm", Murmur3Hash128.HASH_ID);
        md.put("hashSeedHex", "0x" + Integer.toHexString(Murmur3Hash128.FIXED_SEED));
        md.put("canonicalization", ValueCanonicalizer.describe());
        md.put("sketchState", "仅内存；export/load 负责持久化");
        md.put("precisionRange", HllConfig.MIN_PRECISION + ".." + HllConfig.MAX_PRECISION);
        return md;
    }

    private static Map<String, Object> step(int index, String op, String description) {
        Map<String, Object> s = new LinkedHashMap<>();
        s.put("index", index);
        s.put("operator", op);
        s.put("description", description);
        return s;
    }

    private static String requireName(Map<String, Object> op) {
        Object name = op.get("name");
        if (!(name instanceof String) || ((String) name).isEmpty()) {
            throw new RequestException("INVALID_REQUEST", "缺少非空字符串字段 \"name\"");
        }
        return (String) name;
    }

    private static int requirePrecision(Map<String, Object> op) {
        Object p = op.get("precision");
        if (!(p instanceof Number)) {
            throw new RequestException("INVALID_REQUEST", "create 必须提供 precision 数字");
        }
        int pi = ((Number) p).intValue();
        if (pi < HllConfig.MIN_PRECISION || pi > HllConfig.MAX_PRECISION) {
            throw new RequestException("INVALID_REQUEST",
                    "precision 越界: " + pi + "，允许 [" + HllConfig.MIN_PRECISION + "," + HllConfig.MAX_PRECISION + "]");
        }
        return pi;
    }

    private HllSketch requireSketch(String name) {
        HllSketch s = sketches.get(name);
        if (s == null) {
            throw new RequestException("SKETCH_NOT_FOUND", "草图不存在: " + name);
        }
        return s;
    }

    private static long parseU64(Object h) {
        if (h instanceof Number) {
            return ((Number) h).longValue();
        }
        if (h instanceof String) {
            String t = ((String) h).trim();
            try {
                if (t.startsWith("0x") || t.startsWith("0X")) {
                    return Long.parseUnsignedLong(t.substring(2), 16);
                }
                return Long.parseLong(t);
            } catch (NumberFormatException nfe) {
                throw new RequestException("INVALID_REQUEST", "无法解析 64 位哈希: " + t);
            }
        }
        throw new RequestException("INVALID_REQUEST", "哈希必须是数字或十六进制字符串");
    }

    private static double round6(double d) {
        return Math.round(d * 1_000_000d) / 1_000_000d;
    }

    private Map<String, Object> error(String code, String message, int opIndex,
                                      List<Map<String, Object>> planSteps) {
        Map<String, Object> resp = new LinkedHashMap<>();
        resp.put("ok", false);
        Map<String, Object> err = new LinkedHashMap<>();
        err.put("code", code);
        err.put("message", message);
        err.put("failedOpIndex", opIndex);
        resp.put("error", err);
        if (planSteps != null) {
            resp.put("executedStepsBeforeFailure", planSteps);
        }
        return resp;
    }

    /** 业务可预期错误（非法请求）。 */
    static class RequestException extends RuntimeException {
        private static final long serialVersionUID = 1L;
        final String code;

        RequestException(String code, String message) {
            super(message);
            this.code = code;
        }
    }
}
