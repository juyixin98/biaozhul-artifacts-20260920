package tvl.expr;

/**
 * 表达式 AST 的公共基类。
 *
 * @param pos 该节点在源码中从 0 开始的字符偏移（用于错误定位）
 */
public sealed abstract class Expr permits
        Expr.Literal,
        Expr.Column,
        Expr.UnaryMinus,
        Expr.BinaryArith,
        Expr.Compare,
        Expr.IsNull,
        Expr.Not,
        Expr.Logical {

    private final int pos;

    protected Expr(int pos) {
        this.pos = pos;
    }

    public int pos() {
        return pos;
    }

    /** NULL 字面量用 {@code kind == NULL} 表示。 */
    public enum LiteralKind {
        INTEGER,
        STRING,
        BOOLEAN,
        NULL,
        UNKNOWN
    }

    public static final class Literal extends Expr {
        public final LiteralKind kind;
        public final Object value;

        public Literal(LiteralKind kind, Object value, int pos) {
            super(pos);
            this.kind = kind;
            this.value = value;
        }
    }

    public static final class Column extends Expr {
        public final String name;

        public Column(String name, int pos) {
            super(pos);
            this.name = name;
        }
    }

    public static final class UnaryMinus extends Expr {
        public final Expr operand;

        public UnaryMinus(Expr operand, int pos) {
            super(pos);
            this.operand = operand;
        }
    }

    public enum ArithOp {
        ADD("+"), SUB("-"), MUL("*"), DIV("/");

        public final String symbol;

        ArithOp(String symbol) {
            this.symbol = symbol;
        }
    }

    public static final class BinaryArith extends Expr {
        public final ArithOp op;
        public final Expr left;
        public final Expr right;

        public BinaryArith(ArithOp op, Expr left, Expr right, int pos) {
            super(pos);
            this.op = op;
            this.left = left;
            this.right = right;
        }
    }

    public enum CompareOp {
        EQ("="), NE("<>"), LT("<"), LE("<="), GT(">"), GE(">=");

        public final String symbol;

        CompareOp(String symbol) {
            this.symbol = symbol;
        }
    }

    public static final class Compare extends Expr {
        public final CompareOp op;
        public final Expr left;
        public final Expr right;

        public Compare(CompareOp op, Expr left, Expr right, int pos) {
            super(pos);
            this.op = op;
            this.left = left;
            this.right = right;
        }
    }

    /** {@code IS [NOT] NULL}。 */
    public static final class IsNull extends Expr {
        public final Expr operand;
        public final boolean negated;

        public IsNull(Expr operand, boolean negated, int pos) {
            super(pos);
            this.operand = operand;
            this.negated = negated;
        }
    }

    public static final class Not extends Expr {
        public final Expr operand;

        public Not(Expr operand, int pos) {
            super(pos);
            this.operand = operand;
        }
    }

    public enum LogicalOp {
        AND, OR
    }

    public static final class Logical extends Expr {
        public final LogicalOp op;
        public final Expr left;
        public final Expr right;

        public Logical(LogicalOp op, Expr left, Expr right, int pos) {
            super(pos);
            this.op = op;
            this.left = left;
            this.right = right;
        }
    }
}
