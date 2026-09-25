package drvb.rule;

import java.util.Map;

/** 事件过滤谓词：在事件求值上下文上做布尔判断。无状态实现，随版本不可变。 */
@FunctionalInterface
public interface Predicate {
    boolean test(Map<String, Object> context);

    static Predicate always(boolean value) {
        return ctx -> value;
    }

    default Predicate and(Predicate other) {
        Predicate self = this;
        return ctx -> self.test(ctx) && other.test(ctx);
    }

    default Predicate negate() {
        return ctx -> !test(ctx);
    }
}
