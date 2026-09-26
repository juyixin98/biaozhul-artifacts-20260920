package com.example.intervals.engine;

import com.example.intervals.algebra.IntervalAlgebra;
import com.example.intervals.error.ErrorCode;
import com.example.intervals.error.IntervalException;
import com.example.intervals.json.ExprDto;
import com.example.intervals.model.Interval;
import com.example.intervals.model.IntervalSet;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;

import java.util.List;
import java.util.Map;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertThrows;

class ExpressionEvaluatorTest {

    private ExpressionEvaluator<Integer> evaluator;

    @BeforeEach
    void setUp() {
        IntervalAlgebra<Integer> algebra = new IntervalAlgebra<>();
        IntervalSet<Integer> a = algebra.normalize(List.of(Interval.closedOpen(1, 5)));
        IntervalSet<Integer> b = algebra.normalize(List.of(Interval.closedOpen(3, 8)));
        evaluator = new ExpressionEvaluator<>(Map.of("A", a, "B", b));
    }

    private ExprDto leaf(String name) {
        return new ExprDto(null, name, null, null, null);
    }

    private ExprDto bin(String op, ExprDto l, ExprDto r) {
        return new ExprDto(op, null, null, l, r);
    }

    private ExprDto un(String op, ExprDto arg) {
        return new ExprDto(op, null, arg, null, null);
    }

    @Test
    void resolvesLeafAndBinaryOps() {
        // A ∩ B = [3,5)
        IntervalSet<Integer> got = evaluator.evaluate(bin("intersection", leaf("A"), leaf("B")));
        assertEquals(List.of(Interval.closedOpen(3, 5)), got.intervals());
    }

    @Test
    void acceptsAliases() {
        IntervalSet<Integer> byAlias = evaluator.evaluate(bin("and", leaf("A"), leaf("B")));
        IntervalSet<Integer> canonical = evaluator.evaluate(bin("intersection", leaf("A"), leaf("B")));
        assertEquals(canonical, byAlias);
    }

    @Test
    void differenceIsLeftMinusRight() {
        // A - B = [1,3)
        IntervalSet<Integer> got = evaluator.evaluate(bin("minus", leaf("A"), leaf("B")));
        assertEquals(List.of(Interval.closedOpen(1, 3)), got.intervals());
    }

    @Test
    void complementOfIntervalIsTwoRanges() {
        IntervalSet<Integer> got = evaluator.evaluate(un("not", leaf("A")));
        assertEquals(2, got.intervals().size());
        // (-inf,1) and [5,+inf)
        assertEquals(Interval.between(null, true, 1, true), got.intervals().get(0));
        assertEquals(Interval.between(5, false, null, false), got.intervals().get(1));
    }

    @Test
    void nestedExpression() {
        // (A ∪ B) - (A ∩ B) = symmetric difference
        ExprDto expr = bin("difference",
                bin("union", leaf("A"), leaf("B")),
                bin("intersection", leaf("A"), leaf("B")));
        IntervalSet<Integer> got = evaluator.evaluate(expr);
        assertEquals(List.of(Interval.closedOpen(1, 3), Interval.closedOpen(5, 8)),
                got.intervals());
    }

    @Test
    void rejectsUnknownSet() {
        IntervalException e = assertThrows(IntervalException.class,
                () -> evaluator.evaluate(leaf("Z")));
        assertEquals(ErrorCode.UNKNOWN_SET_REF, e.errorCode());
    }

    @Test
    void rejectsUnknownOperation() {
        IntervalException e = assertThrows(IntervalException.class,
                () -> evaluator.evaluate(bin("xor", leaf("A"), leaf("B"))));
        assertEquals(ErrorCode.UNKNOWN_OPERATION, e.errorCode());
    }

    @Test
    void rejectsArityMismatches() {
        // binary missing right
        IntervalException m1 = assertThrows(IntervalException.class,
                () -> evaluator.evaluate(new ExprDto("union", null, null, leaf("A"), null)));
        assertEquals(ErrorCode.ARITY_MISMATCH, m1.errorCode());

        // unary with left/right
        IntervalException m2 = assertThrows(IntervalException.class,
                () -> evaluator.evaluate(new ExprDto("complement", null, null, leaf("A"), null)));
        assertEquals(ErrorCode.ARITY_MISMATCH, m2.errorCode());
    }

    @Test
    void rejectsNodeThatIsBothLeafAndOp() {
        IntervalException e = assertThrows(IntervalException.class,
                () -> evaluator.evaluate(new ExprDto("union", "A", null, null, null)));
        assertEquals(ErrorCode.INVALID_EXPRESSION, e.errorCode());
    }

    @Test
    void rejectsNullExpression() {
        IntervalException e = assertThrows(IntervalException.class,
                () -> evaluator.evaluate(null));
        assertEquals(ErrorCode.INVALID_EXPRESSION, e.errorCode());
    }
}
