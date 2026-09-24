//! SPDX license-expression subset: tokenizer, parser and DNF normalization.
//!
//! Supported grammar (a strict subset of the SPDX 2.3 expression grammar):
//!
//! ```text
//! expr   := term (OR term)*
//! term   := factor (AND factor)*
//! factor := LicenseID (WITH ExceptionID)? | '(' expr ')'
//! ```
//!
//! `AND`/`OR`/`WITH` are case-sensitive keywords (per SPDX convention).
//! License and exception identifiers are non-empty runs of letters, digits
//! and the characters `. - + _` (the `+` suffix used by some license ids is
//! treated as part of the identifier; the postfix `GPL-2.0+` operator is not
//! separately modeled).

use serde::Serialize;

/// One concrete license, optionally qualified by a `WITH` exception.
#[derive(Debug, Clone, PartialEq, Eq, Hash, Serialize)]
pub struct LicenseTerm {
    pub license: String,
    pub exception: Option<String>,
}

impl LicenseTerm {
    pub fn new(license: &str) -> Self {
        LicenseTerm {
            license: license.to_string(),
            exception: None,
        }
    }

    pub fn with(license: &str, exception: &str) -> Self {
        LicenseTerm {
            license: license.to_string(),
            exception: Some(exception.to_string()),
        }
    }

    pub fn render(&self) -> String {
        match &self.exception {
            Some(e) => format!("{} WITH {}", self.license, e),
            None => self.license.clone(),
        }
    }
}

/// Boolean expression over [`LicenseTerm`] leaves.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Expr {
    Term(LicenseTerm),
    And(Box<Expr>, Box<Expr>),
    Or(Box<Expr>, Box<Expr>),
}

impl Expr {
    /// Render back into an SPDX-like expression (with minimal parentheses).
    pub fn render(&self) -> String {
        match self {
            Expr::Term(t) => t.render(),
            Expr::And(a, b) => format!("{} AND {}", self.render_child(a, false), self.render_child(b, false)),
            Expr::Or(a, b) => format!("{} OR {}", self.render_child(a, true), self.render_child(b, true)),
        }
    }

    fn render_child(&self, child: &Expr, or_context: bool) -> String {
        match child {
            Expr::And(..) if or_context => format!("({})", child.render()),
            _ => child.render(),
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
enum Token {
    Id(String),
    And,
    Or,
    With,
    LParen,
    RParen,
}

fn is_ident_char(c: char) -> bool {
    c.is_ascii_alphanumeric() || matches!(c, '.' | '-' | '+' | '_')
}

fn tokenize(input: &str) -> Result<Vec<Token>, String> {
    let mut tokens = Vec::new();
    let mut chars = input.chars().peekable();
    while let Some(&c) = chars.peek() {
        if c.is_whitespace() {
            chars.next();
            continue;
        }
        match c {
            '(' => {
                chars.next();
                tokens.push(Token::LParen);
            }
            ')' => {
                chars.next();
                tokens.push(Token::RParen);
            }
            _ if is_ident_char(c) => {
                let mut s = String::new();
                while let Some(&ch) = chars.peek() {
                    if is_ident_char(ch) {
                        s.push(ch);
                        chars.next();
                    } else {
                        break;
                    }
                }
                let tok = match s.as_str() {
                    "AND" => Token::And,
                    "OR" => Token::Or,
                    "WITH" => Token::With,
                    _ => Token::Id(s),
                };
                tokens.push(tok);
            }
            other => return Err(format!("unexpected character {other:?}")),
        }
    }
    Ok(tokens)
}

struct Parser {
    tokens: Vec<Token>,
    pos: usize,
}

impl Parser {
    fn peek(&self) -> Option<&Token> {
        self.tokens.get(self.pos)
    }

    fn next(&mut self) -> Option<Token> {
        let t = self.tokens.get(self.pos).cloned();
        if t.is_some() {
            self.pos += 1;
        }
        t
    }

    fn expect_rparen(&mut self) -> Result<(), String> {
        match self.next() {
            Some(Token::RParen) => Ok(()),
            other => Err(format!("expected ')' but found {}", token_desc(other))),
        }
    }

    /// expr := term (OR term)*
    fn parse_or(&mut self) -> Result<Expr, String> {
        let mut left = self.parse_and()?;
        while matches!(self.peek(), Some(Token::Or)) {
            self.next();
            let right = self.parse_and()?;
            left = Expr::Or(Box::new(left), Box::new(right));
        }
        Ok(left)
    }

    /// term := factor (AND factor)*
    fn parse_and(&mut self) -> Result<Expr, String> {
        let mut left = self.parse_factor()?;
        while matches!(self.peek(), Some(Token::And)) {
            self.next();
            let right = self.parse_factor()?;
            left = Expr::And(Box::new(left), Box::new(right));
        }
        Ok(left)
    }

    /// factor := LicenseID (WITH ExceptionID)? | '(' expr ')'
    fn parse_factor(&mut self) -> Result<Expr, String> {
        match self.next() {
            Some(Token::Id(license)) => {
                let exception = if matches!(self.peek(), Some(Token::With)) {
                    self.next();
                    match self.next() {
                        Some(Token::Id(e)) => Some(e),
                        other => {
                            return Err(format!(
                                "WITH must be followed by an exception id but found {}",
                                token_desc(other)
                            ))
                        }
                    }
                } else {
                    None
                };
                Ok(Expr::Term(LicenseTerm {
                    license,
                    exception,
                }))
            }
            Some(Token::LParen) => {
                let inner = self.parse_or()?;
                self.expect_rparen()?;
                Ok(inner)
            }
            other => Err(format!("expected license id but found {}", token_desc(other))),
        }
    }
}

fn token_desc(t: Option<Token>) -> String {
    match t {
        None => "end of expression".to_string(),
        Some(Token::Id(s)) => format!("\"{s}\""),
        Some(Token::And) => "\"AND\"".to_string(),
        Some(Token::Or) => "\"OR\"".to_string(),
        Some(Token::With) => "\"WITH\"".to_string(),
        Some(Token::LParen) => "\"(\"".to_string(),
        Some(Token::RParen) => "\")\"".to_string(),
    }
}

/// Parse an SPDX expression subset into an [`Expr`].
pub fn parse(input: &str) -> Result<Expr, String> {
    let tokens = tokenize(input)?;
    if tokens.is_empty() {
        return Err("empty license expression".to_string());
    }
    let mut parser = Parser { tokens, pos: 0 };
    let expr = parser.parse_or()?;
    if parser.pos != parser.tokens.len() {
        return Err(format!(
            "unexpected trailing token {}",
            token_desc(parser.tokens.get(parser.pos).cloned())
        ));
    }
    Ok(expr)
}

/// A conjunction of concrete license terms = one selectable alternative.
pub type Conjunction = Vec<LicenseTerm>;

/// Convert an expression to disjunctive normal form: a list of conjunctions.
///
/// For example `(MIT OR GPL-2.0-only) AND Apache-2.0` becomes
/// `[MIT AND Apache-2.0, GPL-2.0-only AND Apache-2.0]`.
pub fn to_dnf(expr: &Expr) -> Vec<Conjunction> {
    let mut alt = match expr {
        Expr::Term(t) => vec![vec![t.clone()]],
        Expr::And(a, b) => {
            let da = to_dnf(a);
            let db = to_dnf(b);
            let mut out = Vec::with_capacity(da.len() * db.len());
            for ca in &da {
                for cb in &db {
                    let mut c = ca.clone();
                    c.extend(cb.iter().cloned());
                    out.push(c);
                }
            }
            out
        }
        Expr::Or(a, b) => {
            let mut out = to_dnf(a);
            out.extend(to_dnf(b));
            out
        }
    };
    // Canonicalize term order inside a conjunction so equal alternatives
    // compare equal; de-duplicate, but keep first-seen order.
    for c in &mut alt {
        c.sort_by_key(|t| t.render());
        c.dedup();
    }
    alt.sort_by_key(|a| std::cmp::Reverse(render_conjunction(a).len()));
    alt.dedup();
    alt
}

fn render_conjunction(c: &Conjunction) -> String {
    c.iter()
        .map(|t| t.render())
        .collect::<Vec<_>>()
        .join(" AND ")
}

/// Render a DNF conjunction as a human-readable string.
pub fn conjunction_text(c: &Conjunction) -> String {
    render_conjunction(c)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_simple_and_or_with() {
        let e = parse("MIT OR (GPL-2.0-only WITH Classpath-exception-2.0 AND Apache-2.0)").unwrap();
        assert_eq!(
            e.render(),
            "MIT OR (GPL-2.0-only WITH Classpath-exception-2.0 AND Apache-2.0)"
        );
    }

    #[test]
    fn dnf_distributes_or_over_and() {
        let e = parse("(MIT OR GPL-2.0-only) AND Apache-2.0").unwrap();
        let dnf = to_dnf(&e);
        assert_eq!(dnf.len(), 2);
        let texts: Vec<String> = dnf.iter().map(conjunction_text).collect();
        assert!(texts.contains(&"Apache-2.0 AND MIT".to_string()));
        assert!(texts.contains(&"Apache-2.0 AND GPL-2.0-only".to_string()));
    }

    #[test]
    fn rejects_bad_inputs() {
        assert!(parse("").is_err());
        assert!(parse("MIT AND").is_err());
        assert!(parse("MIT WITH").is_err());
        assert!(parse("(MIT").is_err());
        assert!(parse("MIT)").is_err());
        assert!(parse("MIT OR OR Apache-2.0").is_err());
    }
}
