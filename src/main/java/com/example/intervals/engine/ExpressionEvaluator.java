package com.example.intervals.engine;

import com.example.intervals.algebra.IntervalAlgebra;
import com.example.intervals.error.ErrorCode;
import com.example.intervals.error.IntervalException;
import com.example.intervals.json.ExprDto;
import com.example.intervals.model.IntervalSet;

import java.util.Map;

/**
 * Evaluates an {@link ExprDto} expression tree against a map of named input
 * sets.
 *
 * <p>Validation is explicit at every node: a node is exactly a leaf
 * ({@code set}), a unary op with {@code arg}, or a binary op with
 * {@code left}/{@code right}; arity mismatches and unknown references are
 * rejected with stable error codes. A depth cap guards against pathological
 * nesting. All work is immutable: inputs are never mutated.
 */
public final class ExpressionEvaluator<T extends Comparable<? super T>> {

    /** Maximum permitted nesting depth of an expression tree. */
    public static final int MAX_DEPTH = 100;

    private final IntervalAlgebra<T> algebra = new IntervalAlgebra<>();
    private final Map<String, IntervalSet<T>> inputs;

    public ExpressionEvaluator(Map<String, IntervalSet<T>> inputs) {
        this.inputs = Map.copyOf(inputs);
    }

    public IntervalSet<T> evaluate(ExprDto expr) {
        if (expr == null) {
            throw new IntervalException(ErrorCode.INVALID_EXPRESSION,
                    "missing 'expression'");
        }
        return eval(expr, 1);
    }

    private IntervalSet<T> eval(ExprDto node, int depth) {
        if (depth > MAX_DEPTH) {
            throw new IntervalException(ErrorCode.INVALID_EXPRESSION,
                    "expression nesting exceeds maximum depth " + MAX_DEPTH);
        }

        boolean isLeaf = node.set() != null;
        boolean isOp = node.op() != null;
        if (isLeaf == isOp) {
            throw new IntervalException(ErrorCode.INVALID_EXPRESSION,
                    "expression node must be exactly one of a leaf {'set':...} "
                            + "or an operation {'op':...}");
        }

        if (isLeaf) {
            return resolveLeaf(node.set());
        }

        Operation op = Operation.require(node.op());
        return switch (op) {
            case COMPLEMENT -> {
                ensureNoBinaryChildren(node, op);
                if (node.arg() == null) {
                    throw arity(op, "missing 'arg'");
                }
                yield algebra.complement(eval(node.arg(), depth + 1));
            }
            case UNION, INTERSECTION, DIFFERENCE -> {
                if (node.arg() != null) {
                    throw arity(op, "binary op must use 'left'/'right', not 'arg'");
                }
                if (node.left() == null || node.right() == null) {
                    throw arity(op, "binary op requires both 'left' and 'right'");
                }
                IntervalSet<T> left = eval(node.left(), depth + 1);
                IntervalSet<T> right = eval(node.right(), depth + 1);
                yield switch (op) {
                    case UNION -> algebra.union(left, right);
                    case INTERSECTION -> algebra.intersection(left, right);
                    case DIFFERENCE -> algebra.difference(left, right);
                    default -> throw new IllegalStateException("unreachable: " + op);
                };
            }
        };
    }

    private IntervalSet<T> resolveLeaf(String name) {
        if (name.isBlank()) {
            throw new IntervalException(ErrorCode.UNKNOWN_SET_REF,
                    "expression references an empty set name");
        }
        IntervalSet<T> set = inputs.get(name);
        if (set == null) {
            throw new IntervalException(ErrorCode.UNKNOWN_SET_REF,
                    "expression references unknown set '" + name + "'; available: " + inputs.keySet());
        }
        return set;
    }

    private void ensureNoBinaryChildren(ExprDto node, Operation op) {
        if (node.left() != null || node.right() != null) {
            throw arity(op, "unary op must use 'arg', not 'left'/'right'");
        }
    }

    private IntervalException arity(Operation op, String detail) {
        return new IntervalException(ErrorCode.ARITY_MISMATCH,
                op.canonical() + " has arity " + op.arity() + ": " + detail);
    }
}
