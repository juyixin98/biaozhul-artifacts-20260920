package com.example.tvl.engine;

import com.example.tvl.sql.Ast.Query;
import com.example.tvl.sql.SqlParser;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * 查询服务：解析 SQL → 绑定参数 → 编译计划（每查询一次）→ 逐批次向量执行。
 *
 * 同一个计划可服务任意多个批次，批次之间互不影响（跨批次状态由调用方持有）。
 * 每个批次同时给出向量结果和逐行参考解释器结果以便核对（可在请求中关闭）。
 */
public final class QueryService {

    public static final class Compiled {
        private final Query query;
        private final Schema schema;
        private final ParameterSet params;
        private final QueryPlanner.Plan plan;

        Compiled(Query query, Schema schema, ParameterSet params, QueryPlanner.Plan plan) {
            this.query = query;
            this.schema = schema;
            this.params = params;
            this.plan = plan;
        }

        public Query query() {
            return query;
        }

        public Schema schema() {
            return schema;
        }

        public ParameterSet params() {
            return params;
        }

        public QueryPlanner.Plan plan() {
            return plan;
        }
    }

    public Compiled compile(String sql, Schema schema, List<?> rawParams) {
        Query query = SqlParser.parse(sql);
        int expected = SqlParser.paramCount(sql);
        ParameterSet params = ParameterSet.bind(expected, rawParams);
        QueryPlanner.Plan plan = QueryPlanner.compile(query, schema, params);
        return new Compiled(query, schema, params, plan);
    }

    /** 执行单个批次，返回可直接 JSON 序列化的结果结构。 */
    public Map<String, Object> executeBatch(Compiled c, Batch batch, boolean includeTvl) {
        int n = batch.rowCount();

        // 生产路径：向量化
        byte[] codes = VectorExecutor.evalWhere(c.plan.where(), batch, c.params);

        // 参考路径：逐行解释器（用于核对，也用于产出 UNKNOWN 计数等诊断信息）
        List<String> tvl = includeTvl ? new ArrayList<>(n) : null;
        if (includeTvl) {
            for (int r = 0; r < n; r++) {
                SqlBool ref = RowInterpreter.evalWhere(c.plan.where(), batch, r, c.params);
                if (ref.code() != codes[r]) {
                    throw new IllegalStateException(
                            "内部一致性错误：第 " + r + " 行向量结果 " + codes[r]
                                    + " 与逐行解释器 " + ref.code() + " 不一致");
                }
                tvl.add(ref.name());
            }
        }

        List<String> projectedNames = new ArrayList<>();
        List<Integer> projectedIdx = new ArrayList<>();
        for (int[] p : c.plan.projection()) {
            int idx = p[0];
            projectedNames.add(c.schema.columns().get(idx).name());
            projectedIdx.add(idx);
        }

        List<List<Object>> rows = new ArrayList<>();
        int trueCount = 0;
        int unknownCount = 0;
        int falseCount = 0;
        for (int r = 0; r < n; r++) {
            SqlBool b = SqlBool.fromCode(codes[r]);
            switch (b) {
                case TRUE -> trueCount++;
                case FALSE -> falseCount++;
                case UNKNOWN -> unknownCount++;
            }
            // WHERE 只保留 TRUE；UNKNOWN 与 FALSE 一样被排除
            if (b != SqlBool.TRUE) {
                continue;
            }
            List<Object> out = new ArrayList<>(projectedIdx.size());
            for (int idx : projectedIdx) {
                Column col = batch.column(idx);
                if (col.isNull(r)) {
                    out.add(null);
                } else {
                    out.add(switch (col.type()) {
                        case INTEGER -> col.getLong(r);
                        case FLOAT -> col.getDouble(r);
                        case TEXT -> col.getString(r);
                    });
                }
            }
            rows.add(out);
        }

        Map<String, Object> result = new LinkedHashMap<>();
        result.put("rowCount", n);
        result.put("selected", rows.size());
        result.put("trueRows", trueCount);
        result.put("falseRows", falseCount);
        result.put("unknownRows", unknownCount);
        result.put("rows", rows);
        if (includeTvl) {
            result.put("tvl", tvl);
        }
        return result;
    }

    /** 便捷入口：一次请求多个批次。 */
    public List<Map<String, Object>> executeBatches(Compiled c, List<Batch> batches, boolean includeTvl) {
        List<Map<String, Object>> out = new ArrayList<>(batches.size());
        for (Batch b : batches) {
            out.add(executeBatch(c, b, includeTvl));
        }
        return out;
    }
}
