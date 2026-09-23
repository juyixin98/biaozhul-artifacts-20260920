package colscan.query;

/** 聚合表达式。COUNT(*) 用 column=null 表示；其余聚合忽略 NULL（绝不把 NULL 当 0）。 */
public final class AggSpec {

    public static final String COUNT = "count";   // COUNT(*)
    public static final String COUNT_COL = "count_col";
    public static final String SUM = "sum";
    public static final String AVG = "avg";
    public static final String MIN = "min";
    public static final String MAX = "max";

    public final String alias;
    public final String func;
    public final String column; // COUNT(*) 时为 null

    public AggSpec(String alias, String func, String column) {
        this.alias = alias;
        this.func = func;
        this.column = column;
    }

    public static boolean isValidFunc(String func) {
        return COUNT.equals(func) || COUNT_COL.equals(func) || SUM.equals(func)
                || AVG.equals(func) || MIN.equals(func) || MAX.equals(func);
    }
}
