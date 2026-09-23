package ppd;

import java.util.function.Function;

/** 对表达式树做递归列引用替换。 */
final class ExprMapper {

    private ExprMapper() {}

    static Expr apply(Expr e, Function<Expr.Ref, Expr> f) {
        return switch (e) {
            case Expr.Lit lit -> lit;
            case Expr.Ref r -> f.apply(r);
            case Expr.Cmp c -> new Expr.Cmp(c.op(), apply(c.left(), f), apply(c.right(), f));
            case Expr.Logic l -> new Expr.Logic(l.op(), apply(l.left(), f), apply(l.right(), f));
            case Expr.Not n -> new Expr.Not(apply(n.inner(), f));
            case Expr.IsNull is -> new Expr.IsNull(apply(is.inner(), f), is.negated());
            case Expr.Arith a -> new Expr.Arith(a.op(), apply(a.left(), f), apply(a.right(), f));
        };
    }
}
