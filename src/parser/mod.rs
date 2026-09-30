//! SQL parser: lowers a strict, documented subset of SQL into the [`crate::ast`] IR.
//!
//! Supported (see README "Supported forms"):
//! ```sql
//! SELECT col [AS a], * ,
//!        EXISTS (SELECT 1 FROM inner WHERE outer.k = inner.k [AND ...]) [AS e],
//!        NOT EXISTS (...),
//!        (SELECT COUNT(*)|COUNT(col)|SUM(col)|col FROM inner WHERE ...)
//! FROM outer
//! [WHERE <conjunction of ANDs>]
//! ```
//!
//! Deliberately rejected with [`ErrorKind::UnsupportedForm`]: `NOT IN` /
//! `IN (subquery)`, OR, JOIN, GROUP BY/HAVING, DISTINCT, set operations,
//! nested subqueries, and any aggregate other than ungrouped COUNT/SUM.
//!
//! The NOT IN rejection is semantic rather than incidental. With
//! `o.k NOT IN (SELECT i.k FROM ...)`:
//!   * if the inner list contains any NULL, the expression is UNKNOWN for
//!     every outer row (so the row is dropped) — unlike NOT EXISTS, which
//!     simply finds no matching inner row;
//!   * a NULL `o.k` also yields UNKNOWN.
//! The two forms are therefore NOT interchangeable, and the service refuses
//! the form instead of silently giving NOT EXISTS semantics.

use sqlparser::ast::{
    BinaryOperator, DuplicateTreatment, Expr, FunctionArg, FunctionArgExpr, FunctionArguments,
    GroupByExpr, SelectItem, SetExpr, TableFactor, UnaryOperator, Value as SqlValue,
};
use sqlparser::dialect::GenericDialect;
use sqlparser::parser::Parser;

use crate::ast::*;
use crate::batch::Value;
use crate::error::{EngineError, EngineResult, ErrorKind};

pub fn parse_query(sql: &str) -> EngineResult<Query> {
    let statements = Parser::parse_sql(&GenericDialect {}, sql).map_err(|e| {
        EngineError::new(ErrorKind::Parse, format!("could not parse SQL: {e}"))
    })?;
    if statements.len() != 1 {
        return Err(EngineError::new(
            ErrorKind::Parse,
            "exactly one statement expected",
        ));
    }
    let statement = statements.into_iter().next().unwrap();
    let query = match statement {
        sqlparser::ast::Statement::Query(q) => *q,
        other => {
            return Err(EngineError::unsupported(format!(
                "only SELECT statements are supported (got {other:?})"
            )))
        }
    };
    if query.with.is_some()
        || !query.order_by.is_empty()
        || query.limit.is_some()
        || query.offset.is_some()
        || query.fetch.is_some()
    {
        return Err(EngineError::unsupported(
            "CTE / ORDER BY / LIMIT / OFFSET / FETCH are not supported",
        ));
    }
    let select = match *query.body {
        SetExpr::Select(s) => s,
        other => {
            return Err(EngineError::unsupported(format!(
                "only a single SELECT is supported (got {other:?})"
            )))
        }
    };
    check_plain_select(&select, "outer query")?;

    if select.from.len() != 1 || !select.from[0].joins.is_empty() {
        return Err(EngineError::unsupported(
            "exactly one table in FROM with no JOINs is supported",
        ));
    }
    let (outer_table, outer_alias) = table_factor(&select.from[0].relation)?;

    let mut lower = Lower {
        outer_name: outer_table.clone(),
        outer_alias,
        inner_name: None,
        inner_alias: None,
    };

    let mut items = Vec::new();
    for item in &select.projection {
        items.push(lower.projection_item(item)?);
    }

    let filters = match &select.selection {
        Some(e) => lower.conjunction(e, true)?,
        None => vec![],
    };

    Ok(Query {
        outer_table,
        items,
        filters,
    })
}

fn check_plain_select(select: &sqlparser::ast::Select, where_: &str) -> EngineResult<()> {
    if select.distinct.is_some() {
        return Err(EngineError::unsupported(format!(
            "DISTINCT is not supported in {where_}"
        )));
    }
    if select.top.is_some()
        || select.into.is_some()
        || select.having.is_some()
        || select.qualify.is_some()
        || !select.lateral_views.is_empty()
        || !select.cluster_by.is_empty()
        || !select.distribute_by.is_empty()
        || !select.sort_by.is_empty()
        || !select.named_window.is_empty()
        || !matches!(select.group_by, GroupByExpr::Expressions(ref e) if e.is_empty())
    {
        return Err(EngineError::unsupported(format!(
            "GROUP BY / HAVING / TOP / WINDOW / QUALIFY / INTO / lateral views are unsupported in {where_}"
        )));
    }
    Ok(())
}

// Which kind of subquery body we are lowering.
#[derive(Clone, Copy)]
enum SubBody {
    ExistsPlain,
    ExistsNegated,
    Scalar,
}

struct Lower {
    outer_name: String,
    outer_alias: Option<String>,
    inner_name: Option<String>,
    inner_alias: Option<String>,
}

impl Lower {
    fn projection_item(&mut self, item: &SelectItem) -> EngineResult<OuterItem> {
        match item {
            SelectItem::Wildcard(opts) if opts == &Default::default() => Ok(OuterItem::Column {
                name: "*".to_string(),
                alias: None,
            }),
            SelectItem::Wildcard(_) | SelectItem::QualifiedWildcard(..) => Err(
                EngineError::unsupported("qualified wildcards / wildcard EXCLUDE are unsupported"),
            ),
            SelectItem::UnnamedExpr(e) => self.outer_expr(e, None),
            SelectItem::ExprWithAlias { expr, alias } => {
                self.outer_expr(expr, Some(alias.value.clone()))
            }
        }
    }

    fn outer_expr(&mut self, e: &Expr, alias: Option<String>) -> EngineResult<OuterItem> {
        match unnest(e) {
            Expr::Identifier(id) => Ok(OuterItem::Column {
                name: id.value.clone(),
                alias,
            }),
            Expr::CompoundIdentifier(parts) => {
                let (qualifier, col) = split_qualified(parts)?;
                let q = qualifier.ok_or_else(|| {
                    EngineError::internal("compound identifier without qualifier")
                })?;
                if self.names_outer(&q) {
                    Ok(OuterItem::Column {
                        name: col,
                        alias,
                    })
                } else {
                    Err(EngineError::validation(format!(
                        "qualifier '{q}' does not name the outer table '{}'",
                        self.outer_name
                    )))
                }
            }
            Expr::Exists { subquery, negated } => {
                let body = if *negated {
                    SubBody::ExistsNegated
                } else {
                    SubBody::ExistsPlain
                };
                let sq = self.lower_subquery(subquery, body)?;
                Ok(OuterItem::Subquery(with_alias(sq, alias)))
            }
            Expr::Subquery(subq) => {
                let sq = self.lower_subquery(subq, SubBody::Scalar)?;
                Ok(OuterItem::Subquery(with_alias(sq, alias)))
            }
            Expr::InSubquery { negated, .. }
            | Expr::InList { negated, .. }
            | Expr::InUnnest { negated, .. } => Err(not_in_or_in_error(*negated)),
            other => Err(EngineError::unsupported(format!(
                "unsupported outer SELECT expression: {other:?}"
            ))),
        }
    }

    fn lower_subquery(
        &mut self,
        subq: &sqlparser::ast::Query,
        body: SubBody,
    ) -> EngineResult<Subquery> {
        if subq.with.is_some()
            || !subq.order_by.is_empty()
            || subq.limit.is_some()
            || subq.offset.is_some()
        {
            return Err(EngineError::unsupported(
                "CTE / ORDER BY / LIMIT / OFFSET inside subqueries are not supported",
            ));
        }
        let select = match subq.body.as_ref() {
            SetExpr::Select(s) => s,
            other => {
                return Err(EngineError::unsupported(format!(
                    "unsupported subquery body: {other:?}"
                )))
            }
        };
        check_plain_select(select, "subquery")?;
        if select.from.len() != 1 || !select.from[0].joins.is_empty() {
            return Err(EngineError::unsupported(
                "subquery must read exactly one table with no JOINs",
            ));
        }
        let (inner_table, inner_alias) = table_factor(&select.from[0].relation)?;

        // Set inner resolution context for predicate lowering.
        self.inner_name = Some(inner_table.clone());
        self.inner_alias = inner_alias;

        let kind = match body {
            SubBody::ExistsPlain | SubBody::ExistsNegated => {
                // EXISTS projects nothing usable; accept `*`, `1`, or a single
                // column, and ignore it.
                match select.projection.as_slice() {
                    [SelectItem::Wildcard(_)] => {}
                    [SelectItem::UnnamedExpr(_)] => {}
                    _ => {
                        return Err(EngineError::unsupported(
                            "EXISTS subquery projects `*` or a single ignored expression",
                        ))
                    }
                }
                if matches!(body, SubBody::ExistsPlain) {
                    SubKind::Exists
                } else {
                    SubKind::NotExists
                }
            }
            SubBody::Scalar => self.scalar_kind(&select.projection)?,
        };

        let predicates = match &select.selection {
            Some(e) => self.conjunction(e, false)?,
            None => vec![],
        };

        self.inner_name = None;
        self.inner_alias = None;

        Ok(Subquery {
            kind,
            inner_table,
            predicates,
            output_name: None,
        })
    }

    fn scalar_kind(&self, items: &[SelectItem]) -> EngineResult<SubKind> {
        if items.len() != 1 {
            return Err(EngineError::validation(
                "scalar subquery must project exactly one expression",
            ));
        }
        let expr = match &items[0] {
            SelectItem::UnnamedExpr(e) | SelectItem::ExprWithAlias { expr: e, .. } => e,
            other => {
                return Err(EngineError::unsupported(format!(
                    "unsupported scalar projection: {other:?}"
                )))
            }
        };
        match unnest(expr) {
            Expr::Function(f) => Ok(SubKind::ScalarAgg {
                proj: self.aggregate(f)?,
            }),
            Expr::Identifier(id) => Ok(SubKind::ScalarColumn {
                col: id.value.clone(),
            }),
            Expr::CompoundIdentifier(parts) => {
                let (_, col) = split_qualified(parts)?;
                Ok(SubKind::ScalarColumn { col })
            }
            other => Err(EngineError::unsupported(format!(
                "unsupported scalar projection expression: {other:?}"
            ))),
        }
    }

    fn aggregate(&self, f: &sqlparser::ast::Function) -> EngineResult<ScalarProj> {
        if f.filter.is_some() || f.over.is_some() || f.null_treatment.is_some() {
            return Err(EngineError::unsupported(
                "FILTER / OVER / NULL treatment clauses on aggregates are unsupported",
            ));
        }
        if f.name.0.len() != 1 {
            return Err(EngineError::unsupported(
                "only simple COUNT/SUM function calls are supported",
            ));
        }
        let name = f.name.0[0].value.to_uppercase();

        let (list, args) = match &f.args {
            FunctionArguments::None => (None, Vec::new()),
            FunctionArguments::Subquery(_) => {
                return Err(EngineError::unsupported(
                    "aggregates over subqueries are not supported",
                ))
            }
            FunctionArguments::List(list) => (Some(list), list.args.clone()),
        };
        if list.is_some_and(|l| l.duplicate_treatment.is_some()) {
            match list.unwrap().duplicate_treatment {
                Some(DuplicateTreatment::Distinct) => {
                    return Err(EngineError::unsupported(
                        "COUNT(DISTINCT ...) / SUM(DISTINCT ...) are not supported",
                    ))
                }
                Some(DuplicateTreatment::All) => {}
                None => {}
            }
        }

        match name.as_str() {
            "COUNT" => match args.as_slice() {
                [FunctionArg::Unnamed(FunctionArgExpr::Wildcard)]
                | [FunctionArg::Unnamed(FunctionArgExpr::QualifiedWildcard(_))] => {
                    Ok(ScalarProj::CountStar)
                }
                [FunctionArg::Unnamed(FunctionArgExpr::Expr(e))] => match e {
                    Expr::Identifier(id) => Ok(ScalarProj::CountCol {
                        col: id.value.clone(),
                    }),
                    Expr::CompoundIdentifier(parts) => {
                        Ok(ScalarProj::CountCol {
                            col: split_qualified(parts)?.1,
                        })
                    }
                    Expr::Value(SqlValue::Null) => Ok(ScalarProj::CountCol {
                        col: "__count_null__".to_string(),
                    }),
                    other => Err(EngineError::unsupported(format!(
                        "COUNT supports (*) or a single column (got {other:?})"
                    ))),
                },
                _ => Err(EngineError::validation("COUNT expects (*) or (column)")),
            },
            "SUM" => {
                let arg = match args.as_slice() {
                    [FunctionArg::Unnamed(FunctionArgExpr::Expr(e))] => e,
                    _ => return Err(EngineError::validation("SUM expects (column)")),
                };
                let col = match arg {
                    Expr::Identifier(id) => id.value.clone(),
                    Expr::CompoundIdentifier(parts) => split_qualified(parts)?.1,
                    other => {
                        return Err(EngineError::unsupported(format!(
                            "SUM supports a single column (got {other:?})"
                        )))
                    }
                };
                // int/float resolved against the schema in semantic validation.
                Ok(ScalarProj::SumCol { col, float: false })
            }
            other => Err(EngineError::unsupported(format!(
                "aggregate {other} is not supported (only COUNT/SUM)"
            ))),
        }
    }

    fn conjunction(&self, e: &Expr, at_outer: bool) -> EngineResult<Vec<Predicate>> {
        let mut out = Vec::new();
        self.flatten_and(e, at_outer, &mut out)?;
        Ok(out)
    }

    fn flatten_and(
        &self,
        e: &Expr,
        at_outer: bool,
        out: &mut Vec<Predicate>,
    ) -> EngineResult<()> {
        match unnest(e) {
            Expr::BinaryOp { left, op, right } if matches!(op, BinaryOperator::And) => {
                self.flatten_and(left, at_outer, out)?;
                self.flatten_and(right, at_outer, out)?;
                Ok(())
            }
            Expr::BinaryOp { left, op, right } => {
                let cmp = match op {
                    BinaryOperator::Eq => CmpOp::Eq,
                    BinaryOperator::NotEq => CmpOp::Neq,
                    BinaryOperator::Lt => CmpOp::Lt,
                    BinaryOperator::LtEq => CmpOp::Lte,
                    BinaryOperator::Gt => CmpOp::Gt,
                    BinaryOperator::GtEq => CmpOp::Gte,
                    BinaryOperator::Or => {
                        return Err(EngineError::unsupported(
                            "OR is not supported: predicates must be a conjunction of ANDs",
                        ))
                    }
                    other => {
                        return Err(EngineError::unsupported(format!(
                            "operator {other:?} is not supported in predicates"
                        )))
                    }
                };
                out.push(self.cmp_predicate(left, cmp, right, at_outer)?);
                Ok(())
            }
            Expr::IsNull(inner) => {
                out.push(Predicate::NullTest {
                    col: self.resolve_ref(inner, at_outer)?,
                    expect_null: true,
                });
                Ok(())
            }
            Expr::IsNotNull(inner) => {
                out.push(Predicate::NullTest {
                    col: self.resolve_ref(inner, at_outer)?,
                    expect_null: false,
                });
                Ok(())
            }
            Expr::UnaryOp {
                op: UnaryOperator::Not,
                expr,
            } => Err(EngineError::unsupported(format!(
                "NOT is only supported as NOT EXISTS / IS NOT NULL (got NOT around {expr:?})"
            ))),
            Expr::InSubquery { negated, .. }
            | Expr::InList { negated, .. }
            | Expr::InUnnest { negated, .. } => Err(not_in_or_in_error(*negated)),
            other => Err(EngineError::unsupported(format!(
                "unsupported predicate shape: {other:?}"
            ))),
        }
    }

    fn cmp_predicate(
        &self,
        left: &Expr,
        op: CmpOp,
        right: &Expr,
        at_outer: bool,
    ) -> EngineResult<Predicate> {
        let left = unnest(left);
        let right = unnest(right);
        match (left, right) {
            (Expr::Value(_), Expr::Value(_)) => Err(EngineError::validation(
                "comparing two literals is not a meaningful predicate",
            )),
            (col_expr, Expr::Value(v)) => Ok(Predicate::LitCmp {
                col: self.resolve_ref(col_expr, at_outer)?,
                op,
                value: sql_value(v)?,
            }),
            (Expr::Value(v), col_expr) => Ok(Predicate::LitCmp {
                col: self.resolve_ref(col_expr, at_outer)?,
                op: op.flip(),
                value: sql_value(v)?,
            }),
            (l, r) => Ok(Predicate::ColCmp {
                left: self.resolve_ref(l, at_outer)?,
                op,
                right: self.resolve_ref(r, at_outer)?,
            }),
        }
    }

    /// Resolve a (possibly qualified) column reference to a scope.
    /// Inside a subquery, a bare name refers to the INNER table; outer
    /// columns must be qualified by the outer table name/alias.
    fn resolve_ref(&self, e: &Expr, at_outer: bool) -> EngineResult<ColumnRef> {
        match unnest(e) {
            Expr::Identifier(id) => Ok(ColumnRef {
                scope: if at_outer { Scope::Outer } else { Scope::Inner },
                table: None,
                name: id.value.clone(),
            }),
            Expr::CompoundIdentifier(parts) => {
                let (qualifier, name) = split_qualified(parts)?;
                let q = qualifier.ok_or_else(|| {
                    EngineError::internal("compound identifier without qualifier")
                })?;
                if self.names_outer(&q) {
                    Ok(ColumnRef {
                        scope: Scope::Outer,
                        table: Some(q),
                        name,
                    })
                } else if !at_outer && self.names_inner(&q) {
                    Ok(ColumnRef {
                        scope: Scope::Inner,
                        table: Some(q),
                        name,
                    })
                } else {
                    Err(EngineError::validation(format!(
                        "unknown table qualifier '{q}'"
                    )))
                }
            }
            other => Err(EngineError::unsupported(format!(
                "expected a column reference, got {other:?}"
            ))),
        }
    }

    fn names_outer(&self, q: &str) -> bool {
        self.outer_name == q || self.outer_alias.as_deref() == Some(q)
    }

    fn names_inner(&self, q: &str) -> bool {
        self.inner_name.as_deref() == Some(q) || self.inner_alias.as_deref() == Some(q)
    }
}

fn with_alias(mut sq: Subquery, alias: Option<String>) -> Subquery {
    sq.output_name = alias;
    sq
}

/// Peel redundant parentheses: `(expr)` parses as `Expr::Nested` and is
/// semantically transparent in every position this grammar supports.
fn unnest(mut e: &Expr) -> &Expr {
    while let Expr::Nested(inner) = e {
        e = inner;
    }
    e
}

fn not_in_or_in_error(negated: bool) -> EngineError {
    if negated {
        EngineError::unsupported(
            "NOT IN (subquery) is not supported: its NULL semantics differ from NOT EXISTS \
             (any NULL in the subquery makes the result UNKNOWN for every outer row); \
             rewrite as NOT EXISTS with an explicit equi-correlation",
        )
    } else {
        EngineError::unsupported(
            "IN (subquery) is not supported; use EXISTS with an explicit equi-correlation",
        )
    }
}

fn table_factor(tf: &TableFactor) -> EngineResult<(String, Option<String>)> {
    match tf {
        TableFactor::Table { name, alias, args, .. } => {
            if args.is_some() {
                return Err(EngineError::unsupported(
                    "table-valued functions in FROM are not supported",
                ));
            }
            if name.0.len() != 1 {
                return Err(EngineError::unsupported(
                    "schema-qualified table names are not supported in this service",
                ));
            }
            let table = name.0[0].value.clone();
            let alias = alias.as_ref().map(|a| a.name.value.clone());
            Ok((table, alias))
        }
        other => Err(EngineError::unsupported(format!(
            "only plain table names are supported in FROM (got {other:?})"
        ))),
    }
}

fn split_qualified(parts: &[sqlparser::ast::Ident]) -> EngineResult<(Option<String>, String)> {
    match parts {
        [col] => Ok((None, col.value.clone())),
        [table, col] => Ok((Some(table.value.clone()), col.value.clone())),
        _ => Err(EngineError::unsupported(
            "three-part or deeper qualified names are not supported",
        )),
    }
}

fn sql_value(v: &SqlValue) -> EngineResult<Value> {
    match v {
        SqlValue::Null => Ok(Value::Null),
        SqlValue::Boolean(b) => Ok(Value::Bool(*b)),
        SqlValue::Number(s, _) => {
            if let Ok(i) = s.parse::<i64>() {
                Ok(Value::Int(i))
            } else if let Ok(f) = s.parse::<f64>() {
                Ok(Value::Float(f))
            } else {
                Err(EngineError::validation(format!(
                    "numeric literal '{s}' does not fit int64/float64"
                )))
            }
        }
        SqlValue::SingleQuotedString(s) => Ok(Value::Str(s.clone())),
        other => Err(EngineError::unsupported(format!(
            "literal type {other:?} is not supported"
        ))),
    }
}
