package ppd;

import java.util.ArrayList;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Set;
import java.util.function.Predicate;

/**
 * 谓词下推重写器（纯函数式：输入计划不变，输出新计划）。
 *
 * 对计划树自底向上改写：把 Filter 的合取项尽量移动到 Scan / 连接子树 / 投影下方。
 * 每个合取项的“能推 / 不能推”都记录一条 {@link Decision} 理由。
 *
 * <h2>判定依据</h2>
 * <ol>
 *   <li><b>列来源</b>：谓词只引用某一侧 schema 的列（且可穿透简单投影）才能进入该侧；
 *       跨两侧的谓词只能并入内连接 ON（外连接不行）。</li>
 *   <li><b>NULL 拒绝性</b>：左外连接右侧是“NULL 补充侧”。引用右列的 WHERE 谓词，
 *       仅当其在右列可能为 NULL 时绝不为真（rejects nulls），才可下推——
 *       此时必须同时把左外连接<b>降级为内连接</b>，否则会错误删除保留行。</li>
 *   <li>非 NULL 拒绝的右表谓词（如 {@code r.b IS NULL}、{@code r.b = 1 OR r.b = 2}）
 *       必须留在外连接上方。</li>
 * </ol>
 */
public class PushdownRewriter {

    /** 一条下推决策。 */
    public record Decision(String predicate, String action, String reason) {
        public java.util.Map<String, Object> toJson() {
            java.util.Map<String, Object> m = new java.util.LinkedHashMap<>();
            m.put("predicate", predicate);
            m.put("action", action);
            m.put("reason", reason);
            return m;
        }
    }

    public record RewriteResult(Plan plan, List<Decision> decisions) {}

    public RewriteResult rewrite(Plan plan) {
        List<Decision> decisions = new ArrayList<>();
        Plan rewritten = rewriteNode(plan, decisions);
        return new RewriteResult(rewritten, List.copyOf(decisions));
    }

    // ------------------------------------------------------------------
    // 自底向上
    // ------------------------------------------------------------------

    private Plan rewriteNode(Plan p, List<Decision> d) {
        return switch (p) {
            case Plan.Scan s -> s;
            case Plan.Filter f -> rewriteFilter(f, d);
            case Plan.Project pr ->
                    new Plan.Project(pr.items(), rewriteNode(pr.child(), d));
            case Plan.InnerJoin j ->
                    new Plan.InnerJoin(rewriteNode(j.left(), d), rewriteNode(j.right(), d),
                            j.onPredicates());
            case Plan.LeftJoin j ->
                    new Plan.LeftJoin(rewriteNode(j.left(), d), rewriteNode(j.right(), d),
                            j.onPredicates());
        };
    }

    private Plan rewriteFilter(Plan.Filter f, List<Decision> d) {
        Plan child = rewriteNode(f.child(), d);
        return pushConjuncts(Expr.splitConjunction(f.predicate()), child, d);
    }

    /** 把一组合取项推入已改写的子计划，返回新计划；推不下去的留在 Filter 里。 */
    private Plan pushConjuncts(List<Expr> conjuncts, Plan child, List<Decision> d) {
        return switch (child) {
            case Plan.Scan s -> {
                List<Expr> all = new ArrayList<>();
                for (Expr c : conjuncts) {
                    if (isConstantFalse(c)) {
                        d.add(new Decision(c.toSql(), "PUSH_TO_SCAN",
                                "常量 FALSE：下推到表 " + s.table() + " 的扫描，结果直接为空"));
                    } else {
                        validateRefs(c, s.tableData().schema());
                        d.add(new Decision(c.toSql(), "PUSH_TO_SCAN",
                                "谓词仅引用表 " + s.table() + " 的列，下推为基表过滤，提前丢弃不满足行"));
                    }
                    all.add(c);
                }
                yield filterOr(all, child);
            }
            case Plan.Project pr -> pushThroughProject(conjuncts, pr, d);
            case Plan.InnerJoin ij -> pushIntoInnerJoin(conjuncts, ij, d);
            case Plan.LeftJoin lj -> pushIntoLeftJoin(conjuncts, lj, d);
            default -> filterOr(conjuncts, child);
        };
    }

    private static Plan filterOr(List<Expr> preds, Plan child) {
        if (preds.isEmpty()) return child;
        return new Plan.Filter(Expr.combineConjunction(preds), child);
    }

    // ------------------------------------------------------------------
    // Project 穿透
    // ------------------------------------------------------------------

    private Plan pushThroughProject(List<Expr> conjuncts, Plan.Project pr, List<Decision> d) {
        List<Expr> stay = new ArrayList<>();
        List<Expr> below = new ArrayList<>();
        for (Expr c : conjuncts) {
            if (isConstantFalse(c)) {
                // 投影不删行：常量 FALSE 可直接穿透
                below.add(c);
                d.add(new Decision(c.toSql(), "PUSH_THROUGH_PROJECT",
                        "常量 FALSE 且投影不改变行数，穿透投影继续下推（结果恒为空）"));
                continue;
            }
            List<String> blocked = new ArrayList<>();
            Expr mapped = c.mapRefs(r -> {
                Column out = pr.schema().resolve(r.key());
                if (out == null) {
                    blocked.add(r.key().toString());
                    return r;
                }
                Column origin = out.rootOrigin();
                if (out.origin() == null) {
                    // 计算列 / 常量列：无基表来源
                    blocked.add(out.display());
                    return r;
                }
                return new Expr.Ref(origin.qualifier(), origin.name());
            });
            if (!blocked.isEmpty()) {
                stay.add(c);
                d.add(new Decision(c.toSql(), "STAY_ABOVE_PROJECT",
                        "引用了投影计算列 " + new LinkedHashSet<>(blocked)
                                + "，无法按列来源穿透，保留在投影上方"));
            } else {
                below.add(mapped);
                d.add(new Decision(c.toSql(), "PUSH_THROUGH_PROJECT",
                        "谓词引用列经列来源追踪全部来自底层列，改写为 "
                                + mapped.toSql() + " 穿透投影（投影不删行，语义不变）"));
            }
        }
        Plan newChild = below.isEmpty() ? pr.child()
                : pushConjuncts(below, pr.child(), d);
        Plan result = new Plan.Project(pr.items(), newChild);
        return filterOr(stay, result);
    }

    // ------------------------------------------------------------------
    // InnerJoin
    // ------------------------------------------------------------------

    private Plan pushIntoInnerJoin(List<Expr> conjuncts, Plan.InnerJoin j, List<Decision> d) {
        List<Expr> leftPreds = new ArrayList<>();
        List<Expr> rightPreds = new ArrayList<>();
        List<Expr> onPreds = new ArrayList<>(j.onPredicates());

        for (Expr c : conjuncts) {
            if (isConstantFalse(c)) {
                // 内连接任一侧为空 => 结果为空；落到左侧
                leftPreds.add(c);
                d.add(new Decision(c.toSql(), "PUSH_TO_LEFT_SIDE",
                        "常量 FALSE：内连接在空关系上结果恒为空，下推至左子树"));
                continue;
            }
            Set<RefKey> refs = c.refs();
            boolean inLeft = refsAllIn(refs, j.left().schema());
            boolean inRight = refsAllIn(refs, j.right().schema());

            if (inLeft && !inRight) {
                validateRefs(c, j.left().schema());
                leftPreds.add(c);
                d.add(new Decision(c.toSql(), "PUSH_TO_LEFT_SIDE",
                        "谓词只引用内连接左表列，先过滤左表减少连接探测行数，不改变内连接结果"));
            } else if (inRight && !inLeft) {
                validateRefs(c, j.right().schema());
                rightPreds.add(c);
                d.add(new Decision(c.toSql(), "PUSH_TO_RIGHT_SIDE",
                        "谓词只引用内连接右表列，先过滤右表不改变内连接结果"));
            } else {
                // 两侧都引用（跨侧）：并入 ON；裸名歧义在 schema 解析阶段已抛错
                validateRefs(c, j.schema());
                onPreds.add(c);
                d.add(new Decision(c.toSql(), "MERGE_INTO_ON",
                        "谓词引用连接两侧列，无法单侧下推，并入内连接 ON 条件（与连接后过滤等价）"));
            }
        }

        Plan nl = leftPreds.isEmpty() ? j.left()
                : pushConjuncts(leftPreds, j.left(), d);
        Plan nr = rightPreds.isEmpty() ? j.right()
                : pushConjuncts(rightPreds, j.right(), d);
        return new Plan.InnerJoin(nl, nr, onPreds);
    }

    // ------------------------------------------------------------------
    // LeftJoin：保留侧 / NULL 补充侧 / 降级内连接
    // ------------------------------------------------------------------

    private Plan pushIntoLeftJoin(List<Expr> conjuncts, Plan.LeftJoin lj, List<Decision> d) {
        List<Expr> stay = new ArrayList<>();
        List<Expr> leftPreds = new ArrayList<>();
        List<Expr> rightPreds = new ArrayList<>();
        List<Expr> afterDowngrade = new ArrayList<>(); // 转内连接后再分类的跨侧谓词
        boolean downgrade = false;

        for (Expr c : conjuncts) {
            if (isConstantFalse(c)) {
                // 只能推保留侧：右表被过滤为空会让全部左行变成补 NULL 保留行
                leftPreds.add(c);
                d.add(new Decision(c.toSql(), "PUSH_TO_PRESERVED_SIDE",
                        "常量 FALSE：仅可下推到左外连接保留侧（左表）；"
                                + "推入 NULL 补充侧会使全部左行成为保留行，语义改变"));
                continue;
            }

            Set<RefKey> refs = c.refs();
            boolean inLeft = refsAllIn(refs, lj.left().schema());
            boolean inRight = refsAllIn(refs, lj.right().schema());

            if (inLeft && !inRight) {
                validateRefs(c, lj.left().schema());
                leftPreds.add(c);
                d.add(new Decision(c.toSql(), "PUSH_TO_PRESERVED_SIDE",
                        "谓词只引用保留侧（左表）列：保留行左列永不为补 NULL，"
                                + "连接前过滤与连接后过滤等价"));
                continue;
            }

            if (inRight && !inLeft) {
                validateRefs(c, lj.right().schema());
                // 右列在保留行上同时补 NULL：把谓词引用的右列集合整体置 NULL 测试
                Set<RefKey> rightKeys = new java.util.HashSet<>(refs);
                if (c.rejectsNulls(rightKeys)) {
                    downgrade = true;
                    rightPreds.add(c);
                    d.add(new Decision(c.toSql(), "DOWNGRADE_AND_PUSH_RIGHT",
                            "谓词引用 NULL 补充侧（右表）列且拒绝 NULL：右列补 NULL 时谓词不可能为 TRUE，"
                                    + "未匹配的保留行必被过滤，故把左外连接降级为内连接并下推到右表"));
                } else {
                    stay.add(c);
                    d.add(new Decision(c.toSql(), "STAY_ABOVE_OUTER_JOIN",
                            "谓词引用 NULL 补充侧（右表）列但不拒绝 NULL，"
                                    + "下推会删除/改变外连接保留行（例如保留行右列全为 NULL），"
                                    + "必须保留在左外连接上方"));
                }
                continue;
            }

            // 跨两侧：把右表列集合整体置 NULL，若保留行必被过滤，才可降级为内连接并入 ON
            validateRefs(c, jSchema(lj));
            Set<RefKey> rightKeys = new java.util.HashSet<>();
            for (RefKey k : refs) {
                if (lj.right().schema().resolve(k) != null) rightKeys.add(k);
            }
            if (!rightKeys.isEmpty() && c.rejectsNulls(rightKeys)) {
                downgrade = true;
                afterDowngrade.add(c);
                d.add(new Decision(c.toSql(), "DOWNGRADE_AND_MERGE_INTO_ON",
                        "跨两侧谓词拒绝右表 NULL（保留行必被过滤）：先将左外连接降级为内连接，"
                                + "再并入 ON 条件，结果不变"));
            } else {
                stay.add(c);
                d.add(new Decision(c.toSql(), "STAY_ABOVE_OUTER_JOIN",
                        "谓词同时引用保留侧与 NULL 补充侧列且不保证拒绝右表 NULL；"
                                + "下推或转内连接都会改变外连接保留行语义，保留在连接上方"));
            }
        }

        Plan nl = leftPreds.isEmpty() ? lj.left()
                : pushConjuncts(leftPreds, lj.left(), d);
        Plan nr = rightPreds.isEmpty() ? lj.right()
                : pushConjuncts(rightPreds, lj.right(), d);

        Plan join;
        if (downgrade) {
            List<Expr> on = new ArrayList<>(lj.onPredicates());
            on.addAll(afterDowngrade);
            join = new Plan.InnerJoin(nl, nr, on);
            d.add(new Decision("*", "CONVERT_LEFT_TO_INNER",
                    "存在引用右表且拒绝 NULL 的谓词：左外连接的保留行不可能存活，"
                            + "将 LeftJoin 改写为 InnerJoin（这是安全下推的前提）"));
        } else {
            join = new Plan.LeftJoin(nl, nr, lj.onPredicates());
        }
        return filterOr(stay, join);
    }

    private static Schema jSchema(Plan.LeftJoin lj) {
        List<Column> cols = new ArrayList<>(lj.left().schema().columns());
        cols.addAll(lj.right().schema().columns());
        return new Schema(cols);
    }

    // ------------------------------------------------------------------
    // 辅助
    // ------------------------------------------------------------------

    private boolean refsAllIn(Set<RefKey> refs, Schema s) {
        for (RefKey k : refs) {
            if (s.resolve(k) == null) return false;
        }
        return true;
    }

    private void validateRefs(Expr e, Schema s) {
        for (RefKey k : e.refs()) {
            if (s.resolve(k) == null) {
                throw new EngineException("谓词 " + e.toSql() + " 含无法解析的列: " + k);
            }
        }
    }

    private static boolean isConstantFalse(Expr e) {
        return e instanceof Expr.Lit l && Boolean.FALSE.equals(l.value());
    }
}
