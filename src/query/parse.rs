//! Small hand-written tokenizer + recursive-descent parser for the query
//! language. No external SQL engine is used: the grammar is intentionally
//! tiny so parse errors can carry a precise position and message.
//!
//! ```text
//! or    := and (OR and)*
//! and   := unary (AND unary)*
//! unary := NOT unary | atom
//! atom  := '(' or ')' | column IS [NOT] NULL | column <op> literal
//! literal := integer | 'text' | TRUE | FALSE
//! ```

use crate::batch::Scalar;
use crate::error::{Result, TritmapError};
use crate::index::CompareOp;

use super::Expr;

#[derive(Debug, Clone, PartialEq, Eq)]
enum Tok {
    Ident(String),
    Int(i64),
    Str(String),
    Op(CompareOp),
    LParen,
    RParen,
    And,
    Or,
    Not,
    Is,
    Null,
    True,
    False,
}

#[derive(Debug, Clone)]
struct Lexeme {
    tok: Tok,
    pos: usize,
}

fn tokenize(src: &str) -> Result<Vec<Lexeme>> {
    let bytes = src.as_bytes();
    let mut i = 0;
    let mut out = Vec::new();
    while i < bytes.len() {
        match bytes[i] {
            b' ' | b'\t' | b'\n' | b'\r' => i += 1,
            b'(' => {
                out.push(Lexeme {
                    tok: Tok::LParen,
                    pos: i,
                });
                i += 1;
            }
            b')' => {
                out.push(Lexeme {
                    tok: Tok::RParen,
                    pos: i,
                });
                i += 1;
            }
            b'=' => {
                out.push(Lexeme {
                    tok: Tok::Op(CompareOp::Eq),
                    pos: i,
                });
                i += 1;
            }
            b'<' | b'>' | b'!' => {
                let (op, adv) = match (bytes[i], bytes.get(i + 1)) {
                    (b'<', Some(b'=')) => (CompareOp::Lte, 2),
                    (b'>', Some(b'=')) => (CompareOp::Gte, 2),
                    (b'<', Some(b'>')) => (CompareOp::Neq, 2),
                    (b'!', Some(b'=')) => (CompareOp::Neq, 2),
                    (b'<', _) => (CompareOp::Lt, 1),
                    (b'>', _) => (CompareOp::Gt, 1),
                    _ => {
                        return Err(TritmapError::QueryParse {
                            pos: i,
                            message: "'!' must be followed by '=' (!=)".into(),
                        })
                    }
                };
                out.push(Lexeme { tok: Tok::Op(op), pos: i });
                i += adv;
            }
            b'\'' => {
                let start = i;
                i += 1;
                let mut s = String::new();
                loop {
                    if i >= bytes.len() {
                        return Err(TritmapError::QueryParse {
                            pos: start,
                            message: "unterminated string literal".into(),
                        });
                    }
                    if bytes[i] == b'\'' {
                        if bytes.get(i + 1) == Some(&b'\'') {
                            s.push('\'');
                            i += 2;
                        } else {
                            i += 1;
                            break;
                        }
                    } else {
                        // Byte copy is safe inside UTF-8 only at char bounds;
                        // gather chars instead.
                        let ch = src[i..].chars().next().unwrap();
                        s.push(ch);
                        i += ch.len_utf8();
                    }
                }
                out.push(Lexeme {
                    tok: Tok::Str(s),
                    pos: start,
                });
            }
            b'0'..=b'9' | b'-' | b'+' => {
                let start = i;
                if bytes[i] == b'-' || bytes[i] == b'+' {
                    i += 1;
                }
                let digits_start = i;
                while i < bytes.len() && bytes[i].is_ascii_digit() {
                    i += 1;
                }
                if i == digits_start {
                    return Err(TritmapError::QueryParse {
                        pos: start,
                        message: "expected digits after sign".into(),
                    });
                }
                let text = &src[start..i];
                let v: i64 = text.parse().map_err(|_| TritmapError::QueryParse {
                    pos: start,
                    message: format!("integer literal '{text}' out of i64 range"),
                })?;
                out.push(Lexeme {
                    tok: Tok::Int(v),
                    pos: start,
                });
            }
            c if c.is_ascii_alphabetic() || c == b'_' => {
                let start = i;
                while i < bytes.len() {
                    let c = bytes[i];
                    if c.is_ascii_alphanumeric() || c == b'_' {
                        i += 1;
                    } else {
                        break;
                    }
                }
                let word = &src[start..i];
                let tok = match word.to_ascii_uppercase().as_str() {
                    "AND" => Tok::And,
                    "OR" => Tok::Or,
                    "NOT" => Tok::Not,
                    "IS" => Tok::Is,
                    "NULL" => Tok::Null,
                    "TRUE" => Tok::True,
                    "FALSE" => Tok::False,
                    _ => Tok::Ident(word.to_string()),
                };
                out.push(Lexeme { tok, pos: start });
            }
            _ => {
                let ch = src[i..].chars().next().unwrap();
                return Err(TritmapError::QueryParse {
                    pos: i,
                    message: format!("unexpected character '{ch}'"),
                });
            }
        }
    }
    Ok(out)
}

struct Parser {
    toks: Vec<Lexeme>,
    cur: usize,
}

impl Parser {
    fn peek(&self) -> Option<&Lexeme> {
        self.toks.get(self.cur)
    }

    fn next(&mut self) -> Option<Lexeme> {
        let t = self.toks.get(self.cur).cloned();
        if t.is_some() {
            self.cur += 1;
        }
        t
    }

    fn parse_or(&mut self) -> Result<Expr> {
        let mut left = self.parse_and()?;
        while let Some(Lexeme { tok: Tok::Or, .. }) = self.peek() {
            self.next();
            let right = self.parse_and()?;
            left = Expr::Or(Box::new(left), Box::new(right));
        }
        Ok(left)
    }

    fn parse_and(&mut self) -> Result<Expr> {
        let mut left = self.parse_unary()?;
        while let Some(Lexeme { tok: Tok::And, .. }) = self.peek() {
            self.next();
            let right = self.parse_unary()?;
            left = Expr::And(Box::new(left), Box::new(right));
        }
        Ok(left)
    }

    fn parse_unary(&mut self) -> Result<Expr> {
        if let Some(Lexeme { tok: Tok::Not, .. }) = self.peek() {
            self.next();
            let inner = self.parse_unary()?;
            return Ok(Expr::Not(Box::new(inner)));
        }
        self.parse_atom()
    }

    fn parse_atom(&mut self) -> Result<Expr> {
        let lx = self.next().ok_or_else(|| TritmapError::QueryParse {
            pos: usize::MAX,
            message: "unexpected end of expression".into(),
        })?;
        match lx.tok {
            Tok::LParen => {
                let inner = self.parse_or()?;
                match self.next() {
                    Some(Lexeme { tok: Tok::RParen, .. }) => Ok(inner),
                    Some(other) => Err(TritmapError::QueryParse {
                        pos: other.pos,
                        message: "expected ')'".into(),
                    }),
                    None => Err(TritmapError::QueryParse {
                        pos: usize::MAX,
                        message: "missing closing ')'".into(),
                    }),
                }
            }
            Tok::Ident(column) => self.parse_column_tail(column, lx.pos),
            other => Err(TritmapError::QueryParse {
                pos: lx.pos,
                message: format!("expected column name or '(' but found {other:?}"),
            }),
        }
    }

    fn parse_column_tail(&mut self, column: String, pos: usize) -> Result<Expr> {
        match self.peek() {
            Some(Lexeme { tok: Tok::Is, .. }) => {
                self.next();
                let negated = if let Some(Lexeme { tok: Tok::Not, .. }) = self.peek() {
                    self.next();
                    true
                } else {
                    false
                };
                match self.next() {
                    Some(Lexeme { tok: Tok::Null, .. }) => {}
                    Some(other) => {
                        return Err(TritmapError::QueryParse {
                            pos: other.pos,
                            message: "expected NULL after IS [NOT]".into(),
                        })
                    }
                    None => {
                        return Err(TritmapError::QueryParse {
                            pos,
                            message: "dangling IS [NOT], expected NULL".into(),
                        })
                    }
                }
                Ok(Expr::IsNull { column, negated })
            }
            Some(Lexeme { tok: Tok::Op(op), .. }) => {
                let op = *op;
                self.next();
                let lit = self.parse_literal()?;
                Ok(Expr::Compare {
                    column,
                    op,
                    literal: lit,
                })
            }
            Some(other) => Err(TritmapError::QueryParse {
                pos: other.pos,
                message: format!("expected comparison operator or IS after column '{column}'"),
            }),
            None => Err(TritmapError::QueryParse {
                pos,
                message: format!("column '{column}' is not followed by an operator"),
            }),
        }
    }

    fn parse_literal(&mut self) -> Result<Scalar> {
        match self.next() {
            Some(Lexeme { tok: Tok::Int(v), .. }) => Ok(Scalar::Int(v)),
            Some(Lexeme { tok: Tok::Str(s), .. }) => Ok(Scalar::Text(s)),
            Some(Lexeme { tok: Tok::True, .. }) => Ok(Scalar::Bool(true)),
            Some(Lexeme { tok: Tok::False, .. }) => Ok(Scalar::Bool(false)),
            Some(other) => Err(TritmapError::QueryParse {
                pos: other.pos,
                message: "expected literal (integer, 'text', TRUE or FALSE)".into(),
            }),
            None => Err(TritmapError::QueryParse {
                pos: usize::MAX,
                message: "missing literal after operator".into(),
            }),
        }
    }
}

/// Parse a predicate expression. See module docs for the grammar.
pub fn parse_predicate(src: &str) -> Result<Expr> {
    let toks = tokenize(src)?;
    if toks.is_empty() {
        return Err(TritmapError::QueryParse {
            pos: 0,
            message: "empty expression".into(),
        });
    }
    let mut p = Parser { toks, cur: 0 };
    // End-of-input errors carry `usize::MAX` internally; report them at the
    // actual end of the source text instead.
    let expr = p.parse_or().map_err(|e| match e {
        TritmapError::QueryParse { pos, message } if pos == usize::MAX => {
            TritmapError::QueryParse {
                pos: src.len(),
                message,
            }
        }
        other => other,
    })?;
    if let Some(trailing) = p.peek() {
        return Err(TritmapError::QueryParse {
            pos: trailing.pos,
            message: "unexpected trailing tokens after complete expression".into(),
        });
    }
    Ok(expr)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_full_grammar() {
        let e = parse_predicate(
            "age >= 18 AND (city = 'NY''C' OR name IS NOT NULL) AND NOT vip = false",
        )
        .unwrap();
        assert_eq!(e.columns(), vec!["age", "city", "name", "vip"]);

        // `AND` is left-associative:
        // ((age>=18) AND ((city='NY''C') OR (name IS NOT NULL))) AND (NOT vip=false)
        let (first_two, not_vip) = match e {
            Expr::And(a, b) => (*a, *b),
            other => panic!("expected outer AND, got {other:?}"),
        };
        let (first, mid) = match first_two {
            Expr::And(a, b) => (*a, *b),
            other => panic!("expected inner AND, got {other:?}"),
        };
        assert!(matches!(first, Expr::Compare { .. }));
        let (city, name) = match mid {
            Expr::Or(a, b) => (*a, *b),
            other => panic!("expected OR, got {other:?}"),
        };
        assert_eq!(
            city,
            Expr::Compare {
                column: "city".into(),
                op: CompareOp::Eq,
                literal: Scalar::Text("NY'C".into())
            }
        );
        assert_eq!(
            name,
            Expr::IsNull {
                column: "name".into(),
                negated: true
            }
        );
        assert_eq!(
            not_vip,
            Expr::Not(Box::new(Expr::Compare {
                column: "vip".into(),
                op: CompareOp::Eq,
                literal: Scalar::Bool(false),
            }))
        );
    }

    #[test]
    fn precedence_or_below_and_not_highest() {
        let e = parse_predicate("NOT a = 1 OR b = 2 AND c IS NULL").unwrap();
        // (NOT a=1) OR ((b=2) AND (c IS NULL))
        match e {
            Expr::Or(a, b) => {
                assert!(matches!(*a, Expr::Not(_)));
                assert!(matches!(*b, Expr::And(_, _)));
            }
            other => panic!("{other:?}"),
        }
    }

    #[test]
    fn parse_errors_are_categorical_and_positioned() {
        let cases = [
            "a = ",            // missing literal
            "a == 1",          // == lexed as '=' then '=' -> trailing/error
            "(a = 1",          // unclosed paren
            "a = 1 2",         // trailing
            "= 1",             // no column
            "a > 'x'",         // parses (type checked at evaluation)
        ];
        // First five must fail with QUERY_PARSE_ERROR carrying a position.
        for q in &cases[..5] {
            let err = parse_predicate(q).expect_err(q);
            match err {
                TritmapError::QueryParse { pos, message } => {
                    assert!(message.len() > 1, "{q}: {message}");
                    let _ = pos;
                }
                other => panic!("{q}: {other:?}"),
            }
        }
        assert!(parse_predicate(cases[5]).is_ok());
    }

    #[test]
    fn negative_and_neq_literals() {
        let e = parse_predicate("x <> -5").unwrap();
        assert_eq!(
            e,
            Expr::Compare {
                column: "x".into(),
                op: CompareOp::Neq,
                literal: Scalar::Int(-5)
            }
        );
    }
}
