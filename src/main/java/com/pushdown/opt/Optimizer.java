package com.pushdown.opt;

import com.pushdown.exec.Engine;
import com.pushdown.expr.Expr;
import com.pushdown.expr.Expr.And;
import com.pushdown.expr.Expr.Bin;
import com.pushdown.expr.Expr.Col;
import com.pushdown.expr.Expr.IsNotNull;
import com.pushdown.expr.Expr.IsNull;
import com.pushdown.expr.Expr.Lit;
import com.pushdown.expr.Expr.Not;
import com.pushdown.expr.Expr.Or;
import com.pushdown.plan.Plan;
import com.pushdown.plan.Plan.Filter;
import com.pushdown.plan.Plan.Join;
import com.pushdown.plan.Plan.JoinType;
import com.pushdown.plan.Plan.Project;
import com.pushdown.plan.Plan.Scan;
import java.util.ArrayList;
import java.util.Collections;
import java.util.HashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;

/**
 * Predicate pushdown optimizer.
 *
 * Decisions are driven by two analyses over each conjunct:
 *  - column provenance (which join side the referenced columns come from);
 *  - NULL-rejection (whether the predicate can still be TRUE when the columns
 *    of one side are all NULL).
 *
 * Rules:
 *  - INNER JOIN: single-side predicates push to that side; constant predicates
 *    push to the left side; two-side predicates stay above.
 *  - LEFT JOIN: left-side predicates push left; a right-side predicate that
 *    is NULL-rejecting on the right columns makes the LEFT JOIN degenerate to
 *    an INNER JOIN and then pushes into the right input (pushing it under a
 *    LEFT JOIN directly would let preserved NULL-padded rows bypass the
 *    filter); a right-side predicate that is not NULL-rejecting stays above
 *    the join; anything else stays above the join.
 * Every decision, including refusals to push, is recorded as a Reason.
 */
public final class Optimizer {

    public record Reason(String rule, String detail) {}
    public record Result(Plan plan, List<Reason> reasons) {}

    private final Map<String, Engine.Table> catalog;
    private final List<Reason> reasons = new ArrayList<>();

    private Optimizer(Map<String, Engine.Table> catalog) { this.catalog = catalog; }

    public static Result optimize(Plan p, Map<String, Engine.Table> catalog) {
        Optimizer o = new Optimizer(catalog);
        Plan np = o.opt(p);
        return new Result(np, o.reasons);
    }

    private Plan opt(Plan p) {
        return switch (p) {
            case Scan s -> s;
            case Filter f -> pushAll(splitAnd(f.pred()), opt(f.child()));
            case Project pr -> new Project(pr.items(), opt(pr.child()));
            case Join j -> new Join(j.type(), opt(j.left()), opt(j.right()), j.on());
        };
    }

    // ---------------------------------------------------------------- AND split

    private static List<Expr> splitAnd(Expr e) {
        List<Expr> out = new ArrayList<>();
        collectAnd(e, out);
        return out;
    }

    private static void collectAnd(Expr e, List<Expr> out) {
        if (e instanceof And a) {
            collectAnd(a.l(), out);
            collectAnd(a.r(), out);
        } else {
            out.add(e);
        }
    }

    // ------------------------------------------------------------- pushdown

    private Plan pushAll(List<Expr> conjuncts, Plan child) {
        Plan cur = child;
        List<Expr> remaining = new ArrayList<>();
        for (Expr c : conjuncts) {
            Expr folded = foldConstant(c);
            if (folded instanceof Lit l && Boolean.TRUE.equals(l.value())) {
                reasons.add(new Reason("constant-true-dropped",
                        "predicate is constant TRUE, dropped: " + show(c)));
                continue;
            }
            Placement pl = place(folded, cur);
            cur = pl.plan();
            if (!pl.placed()) remaining.add(folded);
        }
        for (Expr r : remaining) cur = new Filter(r, cur);
        return cur;
    }

    private record Placement(Plan plan, boolean placed) {}

    /** Try to push {@code pred} as deep as possible below {@code node}. */
    private Placement place(Expr pred, Plan node) {
        return switch (node) {
            case Join j -> placeIntoJoin(pred, j);
            case Filter f -> {
                Placement inner = place(pred, f.child());
                yield new Placement(new Filter(f.pred(), inner.plan()), inner.placed());
            }
            case Project pr -> {
                reasons.add(new Reason("kept-above-project",
                        "cannot push predicate through projection: " + show(pred)));
                yield new Placement(node, false);
            }
            case Scan s -> new Placement(new Filter(pred, s), true);
        };
    }

    private Placement placeIntoJoin(Expr pred, Join j) {
        Set<Col> cols = Expr.columnsOf(pred);
        Set<Col> leftCols = new HashSet<>(Engine.schemaOf(j.left(), catalog));
        Set<Col> rightCols = new HashSet<>(Engine.schemaOf(j.right(), catalog));
        boolean refL = intersects(cols, leftCols);
        boolean refR = intersects(cols, rightCols);

        if (refL && refR) {
            reasons.add(new Reason("kept-above-join",
                    "predicate references both join sides, kept above " + j.type() + " join: " + show(pred)));
            return new Placement(j, false);
        }

        if (!refL && !refR) {
            // Constant predicate (e.g. FALSE after folding).
            if (j.type() == JoinType.LEFT) {
                reasons.add(new Reason("constant-kept-above-left-join",
                        "constant predicate kept above LEFT JOIN: pushing it into the right input "
                                + "would turn preserved rows into filtered ones and vice versa: " + show(pred)));
                return new Placement(j, false);
            }
            reasons.add(new Reason("constant-pushed-inner",
                    "constant predicate pushed to the left input of INNER JOIN "
                            + "(empty left side empties the join, same as filtering above): " + show(pred)));
            return new Placement(new Join(j.type(), new Filter(pred, j.left()), j.right(), j.on()), true);
        }

        if (refL) {
            Placement inner = place(pred, j.left());
            reasons.add(new Reason("push-left",
                    "predicate references only left-side columns, pushed below " + j.type() + " join: " + show(pred)));
            return new Placement(new Join(j.type(), inner.plan(), j.right(), j.on()), true);
        }

        // Right-side only.
        if (j.type() == JoinType.INNER) {
            Placement inner = place(pred, j.right());
            reasons.add(new Reason("push-right-inner",
                    "right-side predicate pushed below INNER JOIN: " + show(pred)));
            return new Placement(new Join(j.type(), j.left(), inner.plan(), j.on()), true);
        }
        if (nullRejecting(pred, rightCols)) {
            Placement inner = place(pred, j.right());
            reasons.add(new Reason("push-right-null-rejecting",
                    "right-side predicate is NULL-rejecting on the right input: preserved NULL-padded "
                            + "rows can never satisfy it, so the LEFT JOIN degenerates to an INNER JOIN "
                            + "and the predicate is pushed into the right input: " + show(pred)));
            // NOT Filter(p, LeftJoin(T, Filter(p, U))): preserved rows would bypass the
            // removed filter. The correct rewrite strengthens the join to INNER.
            return new Placement(new Join(JoinType.INNER, j.left(), inner.plan(), j.on()), true);
        }
        reasons.add(new Reason("kept-for-null-preservation",
                "right-side predicate is NOT NULL-rejecting; pushing it below the LEFT JOIN could drop "
                        + "or fabricate preserved rows, so it stays above the join: " + show(pred)));
        return new Placement(j, false);
    }

    private static boolean intersects(Set<Col> a, Set<Col> b) {
        return !Collections.disjoint(a, b);
    }

    // ------------------------------------------------------- NULL-rejection

    /**
     * Conservative static NULL-rejection test: returns true only if the
     * predicate can never evaluate to TRUE when every column in {@code cols}
     * is NULL.
     */
    public static boolean nullRejecting(Expr p, Set<Col> cols) {
        return switch (p) {
            case Lit l -> !Boolean.TRUE.equals(l.value());       // constant FALSE/NULL rejects everything
            case Col c -> cols.contains(c);                       // NULL in boolean position -> UNKNOWN
            case Bin b -> !Collections.disjoint(Expr.columnsOf(b), cols); // NULL operand -> UNKNOWN/NULL
            case And a -> nullRejecting(a.l(), cols) || nullRejecting(a.r(), cols);
            case Or o -> nullRejecting(o.l(), cols) && nullRejecting(o.r(), cols);
            case IsNull i -> false;                               // TRUE exactly when the value IS NULL
            case IsNotNull i -> !Collections.disjoint(Expr.columnsOf(i.e()), cols);
            case Not n -> n.e() instanceof IsNull i
                    && !Collections.disjoint(Expr.columnsOf(i.e()), cols);
        };
    }

    // -------------------------------------------------------- constant fold

    private Expr foldConstant(Expr e) {
        if (e instanceof Lit) return e;
        if (!Expr.columnsOf(e).isEmpty()) return e;
        Object v = Expr.eval(e, List.of(), new Object[0]);
        reasons.add(new Reason("constant-fold",
                "predicate has no column references, folded to " + v + ": " + show(e)));
        return new Lit(v);
    }

    // -------------------------------------------------------------- display

    public static String show(Expr e) {
        return switch (e) {
            case Lit l -> l.value() == null ? "NULL" : String.valueOf(l.value());
            case Col c -> c.table() + "." + c.name();
            case Bin b -> "(" + show(b.l()) + " " + b.op() + " " + show(b.r()) + ")";
            case And a -> "(" + show(a.l()) + " AND " + show(a.r()) + ")";
            case Or o -> "(" + show(o.l()) + " OR " + show(o.r()) + ")";
            case Not n -> "(NOT " + show(n.e()) + ")";
            case IsNull i -> "(" + show(i.e()) + " IS NULL)";
            case IsNotNull i -> "(" + show(i.e()) + " IS NOT NULL)";
        };
    }
}
