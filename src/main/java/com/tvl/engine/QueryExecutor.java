package com.tvl.engine;

import com.tvl.columnar.Batch;
import com.tvl.columnar.ColumnVector;
import com.tvl.columnar.Schema;
import com.tvl.core.TruthVector;
import com.tvl.sql.Expr;
import com.tvl.sql.Parser;
import com.tvl.sql.Select;
import com.tvl.types.DataType;

import java.util.ArrayList;
import java.util.List;

/**
 * 查询执行器。每个批次独立执行（无状态跨批），但参数绑定与类型检查只做一次，
 * 这样既能覆盖"跨批次"行为，也保证批次间模式一致。
 */
public final class QueryExecutor {

    /** 已绑定并校验过的查询计划。 */
    public static final class Plan {
        final Select statement;
        final List<DataType> paramTypes;
        final List<Object> params;
        final Schema schema;
        final List<DataType> outputTypes;
        final List<String> outputNames;

        Plan(Select statement, List<DataType> paramTypes, List<Object> params,
             Schema schema, List<DataType> outputTypes, List<String> outputNames) {
            this.statement = statement;
            this.paramTypes = paramTypes;
            this.params = params;
            this.schema = schema;
            this.outputTypes = outputTypes;
            this.outputNames = outputNames;
        }

        public Select statement() {
            return statement;
        }

        public List<DataType> paramTypes() {
            return paramTypes;
        }

        public List<Object> params() {
            return params;
        }

 public Schema schema() {
            return schema;
        }

        public List<DataType> outputTypes() {
            return outputTypes;
        }

        public List<String> outputNames() {
            return outputNames;
        }
    }

    /** 仅解析 + 绑定 + 类型检查，不碰数据。供需要捕获"参数类型错误"的调用方使用。 */
    public Plan prepare(QueryRequest request) {
        Parser.Parsed parsed = Parser.parseDetailed(request.sql());
        Select stmt = parsed.statement();

        if (request.params().size() != parsed.paramCount()) {
            throw new com.tvl.types.TypeCheckException(
                    "参数个数不匹配: SQL 中有 " + parsed.paramCount()
                            + " 个 ?，实际绑定 " + request.params().size() + " 个");
        }
        if (request.paramTypes().size() != request.params().size()) {
            throw new com.tvl.types.TypeCheckException(
                    "参数声明类型个数 " + request.paramTypes().size()
                            + " 与参数值个数 " + request.params().size() + " 不一致");
        }
        // 参数值按声明类型逐个强校验（拒绝字符串数值混转）。
        List<Object> bound = new ArrayList<>(request.params().size());
        for (int i = 0; i < request.params().size(); i++) {
            bound.add(com.tvl.types.Values.coerce(
                    request.paramTypes().get(i), request.params().get(i),
                    "参数 ?" + (i + 1)));
        }

        if (request.batches().isEmpty()) {
            throw new IllegalArgumentException("至少需要一个批次（空批次请用 rows=[] 配合 schema 表达）");
        }
        Schema schema = request.batches().get(0).schema();
        for (int i = 1; i < request.batches().size(); i++) {
            if (!schema.names().equals(request.batches().get(i).schema().names())
                    || !typesEqual(schema, request.batches().get(i).schema())) {
                throw new IllegalArgumentException(
                        "第 " + (i + 1) + " 个批次的 schema 与第一个批次不一致");
            }
        }

        List<DataType> outputTypes = new Analyzer(schema, request.paramTypes()).analyze(stmt);
        List<String> outputNames;
        if (stmt.selectsAll()) {
            outputNames = new ArrayList<>(schema.names());
        } else {
            outputNames = stmt.items().stream().map(Select.ProjectionItem::name).toList();
        }
        return new Plan(stmt, request.paramTypes(), bound, schema, outputTypes, outputNames);
    }

    public List<BatchResult> execute(QueryRequest request) {
        Plan plan = prepare(request);
        VectorEngine vector = new VectorEngine(plan.paramTypes, plan.params);
        RowInterpreter interp = request.crossCheck()
                ? new RowInterpreter(plan.paramTypes, plan.params) : null;

        List<BatchResult> results = new ArrayList<>(request.batches().size());
        for (int bi = 0; bi < request.batches().size(); bi++) {
            Batch batch = request.batches().get(bi);
            TruthVector truth = vector.evaluateWhere(plan.statement.where(), batch);

            String check = null;
            if (interp != null) {
                TruthVector rowTruth = interp.evaluateWhere(plan.statement.where(), batch);
                check = truth.equals(rowTruth)
                        ? "MATCH"
                        : "MISMATCH: 向量=" + truth + " 逐行=" + rowTruth + "（批次#" + bi + "）";
            }

            Batch output = project(plan, batch, vector, truth.selectedBitmap());
            results.add(new BatchResult(batch.rowCount(), truth, output, check));
        }
        return results;
    }

    /** 按选择位图过滤并投影；SELECT * 时直接零拷贝原列、按位图压缩。 */
    private Batch project(Plan plan, Batch in, VectorEngine vector, boolean[] selected) {
        List<Expr> exprs;
        if (plan.statement.selectsAll()) {
            exprs = new ArrayList<>();
            for (String name : plan.schema.names()) {
                exprs.add(new Expr.ColumnRef(name));
            }
        } else {
            exprs = plan.statement.items().stream().map(Select.ProjectionItem::expr).toList();
        }

        int kept = 0;
        for (boolean s : selected) {
            if (s) {
                kept++;
            }
        }
        List<ColumnVector> outCols = new ArrayList<>(exprs.size());
        for (int ci = 0; ci < exprs.size(); ci++) {
            ColumnVector full = vector.materialize(exprs.get(ci), in);
            outCols.add(compress(full, selected, kept));
        }
        Schema outSchema = new Schema(plan.outputNames,
                zipTypes(plan.outputNames, plan.outputTypes));
        return new Batch(outSchema, outCols);
    }

    private static java.util.LinkedHashMap<String, DataType> zipTypes(
            List<String> names, List<DataType> types) {
        java.util.LinkedHashMap<String, DataType> map = new java.util.LinkedHashMap<>();
        for (int i = 0; i < names.size(); i++) {
            map.put(names.get(i), types.get(i));
        }
        return map;
    }

    /** 按位图压缩列向量（只拷贝选中行）。 */
    static ColumnVector compress(ColumnVector v, boolean[] selected, int kept) {
        boolean[] nulls = new boolean[kept];
        int w = 0;
        switch (v.type()) {
            case INTEGER: {
                long[] src = v.longs();
                long[] dst = new long[kept];
                for (int i = 0; i < selected.length; i++) {
                    if (selected[i]) {
                        dst[w] = src[i];
                        nulls[w] = v.isNullAt(i);
                        w++;
                    }
                }
                return new ColumnVector(DataType.INTEGER, dst, nulls);
            }
            case DOUBLE: {
                double[] src = v.doubles();
                double[] dst = new double[kept];
                for (int i = 0; i < selected.length; i++) {
                    if (selected[i]) {
                        dst[w] = src[i];
                        nulls[w] = v.isNullAt(i);
                        w++;
                    }
                }
                return new ColumnVector(DataType.DOUBLE, dst, nulls);
            }
            case STRING: {
                String[] src = v.strings();
                String[] dst = new String[kept];
                for (int i = 0; i < selected.length; i++) {
                    if (selected[i]) {
                        dst[w] = src[i];
                        nulls[w] = v.isNullAt(i);
                        w++;
                    }
                }
                return new ColumnVector(DataType.STRING, dst, nulls);
            }
            case BOOLEAN: {
                Boolean[] src = v.booleans();
                Boolean[] dst = new Boolean[kept];
                for (int i = 0; i < selected.length; i++) {
                    if (selected[i]) {
                        dst[w] = src[i];
                        nulls[w] = v.isNullAt(i);
                        w++;
                    }
                }
                return new ColumnVector(DataType.BOOLEAN, dst, nulls);
            }
            default:
                throw new IllegalStateException(String.valueOf(v.type()));
        }
    }

    private static boolean typesEqual(Schema a, Schema b) {
        for (String name : a.names()) {
            if (a.typeOf(name) != b.typeOf(name)) {
                return false;
            }
        }
        return true;
    }
}
