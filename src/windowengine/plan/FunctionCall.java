package windowengine.plan;

/**
 * 单个窗口函数调用：函数种类、输出列别名、SUM 的入参列（其余函数忽略）。
 */
public record FunctionCall(WindowFunction function, String alias, String argumentColumn) {
}
