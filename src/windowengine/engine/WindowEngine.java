package windowengine.engine;

import windowengine.QueryRequest;
import windowengine.Relation;
import windowengine.Row;
import windowengine.Schema;
import windowengine.Value;
import windowengine.plan.FunctionCall;
import windowengine.plan.QueryPlan;
import windowengine.plan.WindowFunction;
import windowengine.plan.WindowSpec;

import java.util.ArrayList;
import java.util.Collections;
import java.util.List;

/**
 * 单机内存查询执行器：分区 -&gt; 分区内排序 -&gt; 窗口算子 -&gt; 按原始行序收集输出。
 *
 * 输出顺序约定：结果行严格按输入行顺序返回（不是分区/排序后的顺序），
 * 每行 = 原始列 + 追加的窗口输出列。这样调用方可以直接把结果与输入按行对齐。
 */
public final class WindowEngine {

    public Relation execute(QueryRequest request) {
        Relation input = request.data();
        QueryPlan plan = request.plan();
        WindowSpec spec = plan.window();
        List<FunctionCall> functions = plan.functions();
        Schema inSchema = input.schema();

        // 输出 schema：原列 + 函数别名（SUM 的结果列可能出现 NULL，但逻辑类型仍是 LONG）
        Schema outSchema = inSchema;
        for (FunctionCall fn : functions) {
            outSchema = outSchema.withColumn(fn.alias(), fn.function().resultType());
        }

        int[] orderColumns = spec.orderBy().stream()
                .mapToInt(ok -> inSchema.requireIndex(ok.column()))
                .toArray();
        int[] argColumns = new int[functions.size()];
        for (int f = 0; f < functions.size(); f++) {
            if (functions.get(f).function() == WindowFunction.SUM) {
                argColumns[f] = inSchema.requireIndex(functions.get(f).argumentColumn());
            } else {
                argColumns[f] = -1;
            }
        }

        List<List<Row>> partitions = new Partitioner(inSchema, spec.partitionBy())
                .partition(input.rows());
        RowComparator comparator = new RowComparator(inSchema, spec.orderBy());
        WindowOperator operator = new WindowOperator(spec);

        // resultBySource[sourceIndex] = 该输入行对应的完整输出行
        int totalColumns = outSchema.size();
        Value[][] resultBySource = new Value[input.rowCount()][];

        for (List<Row> partition : partitions) {
            List<Row> sorted = new ArrayList<>(partition);
            Collections.sort(sorted, comparator); // List.sort 是稳定排序
            Value[][] windowValues = operator.apply(sorted, orderColumns, functions, argColumns);
            for (int i = 0; i < sorted.size(); i++) {
                Row row = sorted.get(i);
                Value[] full = new Value[totalColumns];
                System.arraycopy(row.cells(), 0, full, 0, inSchema.size());
                System.arraycopy(windowValues[i], 0, full, inSchema.size(), functions.size());
                resultBySource[row.sourceIndex()] = full;
            }
        }

        List<Row> outRows = new ArrayList<>(input.rowCount());
        for (int i = 0; i < input.rowCount(); i++) {
            outRows.add(new Row(resultBySource[i], i));
        }
        return new Relation(outSchema, outRows);
    }
}
