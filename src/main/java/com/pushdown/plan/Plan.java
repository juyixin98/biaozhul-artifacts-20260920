package com.pushdown.plan;

import com.pushdown.expr.Expr;
import java.util.List;

/** Logical/physical plan tree: Scan, Filter, Project, Join (INNER / LEFT). */
public sealed interface Plan {

    record Scan(String table) implements Plan {}
    record Filter(Expr pred, Plan child) implements Plan {}
    record Project(List<Item> items, Plan child) implements Plan {}
    record Join(JoinType type, Plan left, Plan right, Expr on) implements Plan {}

    enum JoinType { INNER, LEFT }

    record Item(Expr expr, String alias) {}
}
