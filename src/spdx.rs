// SPDX expression subset parser.
//
// Grammar (case-insensitive keywords):
//   expr   := or
//   or     := and ("OR" and)*
//   and    := simple ("AND" simple)*
//   simple := atom ("WITH" exception)? | "(" expr ")"
//   atom   := bare SPDX-style license id (letters, digits, '-', '.', '/', '+')
//
// Semantics of the subset:
//   A OR B  -> choose one alternative
//   A AND B -> all listed licenses must be complied with simultaneously
//   L WITH E -> license L carrying an exception E; the exception may suppress
//               the outbound copyleft obligations that L would emit.

use serde::Serialize;

/// One (license, optional-exception) leaf in an expression.
#[derive(Debug, Clone, PartialEq, Eq, Hash, Serialize)]
pub struct Atom {
    pub license: String,
    pub exception: Option<String>,
}

impl Atom {
    pub fn new(license: &str, exception: Option<&str>) -> Self {
        Atom {
            license: license.to_string(),
            exception: exception.map(|s| s.to_string()),
        }
    }

    pub fn display(&self) -> String {
        match &self.exception {
            Some(e) => format!("{} WITH {}", self.license, e),
            None => self.license.clone(),
        }
    }
}

/// A conjunction of atoms; one alternative in a DNF expansion.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Term {
    pub atoms: Vec<Atom>,
}

/// Parsed SPDX expression, kept as the raw AST and expanded to DNF.
#[derive(Debug, Clone)]
pub struct SpdxExpr {
    pub root: Node,
    /// OR-distributed alternatives; each inner vec is an AND-conjunction.
    pub dnf: Vec<Term>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Node {
    Atom(Atom),
    And(Vec<Node>),
    Or(Vec<Node>),
}

#[derive(Debug, Clone, PartialEq, Eq)]
enum Tok {
    Word(String),
    LParen,
    RParen,
}

fn tokenize(input: &str) -> Result<Vec<Tok>, String> {
    let mut out = Vec::new();
    let bytes = input.as_bytes();
    let mut i = 0;
    while i < bytes.len() {
        let b = bytes[i];
        if b.is_ascii_whitespace() {
            i += 1;
            continue;
        }
        if b == b'(' {
            out.push(Tok::LParen);
            i += 1;
            continue;
        }
        if b == b')' {
            out.push(Tok::RParen);
            i += 1;
            continue;
        }
        let start = i;
        while i < bytes.len() {
            let c = bytes[i];
            if c.is_ascii_whitespace() || c == b'(' || c == b')' {
                break;
            }
            i += 1;
        }
        if i == start {
            return Err(format!("unexpected character '{}' at offset {}", b as char, i));
        }
        out.push(Tok::Word(input[start..i].to_string()));
    }
    Ok(out)
}

struct Parser {
    toks: Vec<Tok>,
    pos: usize,
}

impl Parser {
    fn peek_word(&self) -> Option<&str> {
        match self.toks.get(self.pos) {
            Some(Tok::Word(w)) => Some(w.as_str()),
            _ => None,
        }
    }

    fn is_keyword(w: &str) -> bool {
        w.eq_ignore_ascii_case("AND")
            || w.eq_ignore_ascii_case("OR")
            || w.eq_ignore_ascii_case("WITH")
    }

    fn parse_or(&mut self) -> Result<Node, String> {
        let first = self.parse_and()?;
        let mut alts = vec![first];
        while let Some(w) = self.peek_word() {
            if !w.eq_ignore_ascii_case("OR") {
                break;
            }
            self.pos += 1;
            alts.push(self.parse_and()?);
        }
        Ok(if alts.len() == 1 {
            alts.pop().unwrap()
        } else {
            Node::Or(alts)
        })
    }

    fn parse_and(&mut self) -> Result<Node, String> {
        let first = self.parse_simple()?;
        let mut items = vec![first];
        while let Some(w) = self.peek_word() {
            if !w.eq_ignore_ascii_case("AND") {
                break;
            }
            self.pos += 1;
            items.push(self.parse_simple()?);
        }
        Ok(if items.len() == 1 {
            items.pop().unwrap()
        } else {
            Node::And(items)
        })
    }

    fn parse_simple(&mut self) -> Result<Node, String> {
        match self.toks.get(self.pos).cloned() {
            Some(Tok::LParen) => {
                self.pos += 1;
                let inner = self.parse_or()?;
                match self.toks.get(self.pos) {
                    Some(Tok::RParen) => {
                        self.pos += 1;
                        Ok(inner)
                    }
                    other => Err(format!(
                        "expected ')' at token {}, found {}",
                        self.pos,
                        tok_desc(other)
                    )),
                }
            }
            Some(Tok::Word(w)) => {
                if Self::is_keyword(&w) {
                    return Err(format!("unexpected keyword '{}'", w));
                }
                self.pos += 1;
                let mut exception = None;
                if let Some(Tok::Word(w2)) = self.toks.get(self.pos) {
                    if w2.eq_ignore_ascii_case("WITH") {
                        self.pos += 1;
                        match self.toks.get(self.pos).cloned() {
                            Some(Tok::Word(e)) => {
                                if Self::is_keyword(&e) {
                                    return Err(format!(
                                        "expected exception id after WITH, found keyword '{}'",
                                        e
                                    ));
                                }
                                exception = Some(e);
                                self.pos += 1;
                            }
                            other => {
                                return Err(format!(
                                    "expected exception id after WITH, found {}",
                                    tok_desc(other.as_ref())
                                ))
                            }
                        }
                    }
                }
                Ok(Node::Atom(Atom::new(&w, exception.as_deref())))
            }
            other => Err(format!(
                "expected license id or '(' at token {}, found {}",
                self.pos,
                tok_desc(other.as_ref())
            )),
        }
    }
}

fn tok_desc(t: Option<&Tok>) -> String {
    match t {
        Some(Tok::Word(w)) => format!("'{}'", w),
        Some(Tok::LParen) => "'('".to_string(),
        Some(Tok::RParen) => "')'".to_string(),
        None => "end of expression".to_string(),
    }
}

/// Flatten an AST node into DNF: OR of AND-conjunctions of atoms.
fn to_dnf(node: &Node) -> Vec<Term> {
    match node {
        Node::Atom(a) => vec![Term { atoms: vec![a.clone()] }],
        Node::And(items) => {
            let mut acc: Vec<Term> = vec![Term { atoms: Vec::new() }];
            for item in items {
                let item_alts = to_dnf(item);
                let mut next = Vec::with_capacity(acc.len() * item_alts.len());
                for base in &acc {
                    for alt in &item_alts {
                        let mut atoms = base.atoms.clone();
                        atoms.extend(alt.atoms.iter().cloned());
                        next.push(Term { atoms });
                    }
                }
                acc = next;
            }
            // canonicalize each conjunction: dedup identical atoms, sort by display text
            for t in acc.iter_mut() {
                t.atts_sort_dedup();
            }
            acc
        }
        Node::Or(items) => {
            let mut out = Vec::new();
            for item in items {
                out.extend(to_dnf(item));
            }
            out
        }
    }
}

impl Term {
    fn atts_sort_dedup(&mut self) {
        self.atoms.sort_by(|a, b| {
            a.license
                .cmp(&b.license)
                .then_with(|| a.exception.cmp(&b.exception))
        });
        self.atoms.dedup();
    }
}

/// Parse a SPDX subset expression; returns the AST plus its DNF expansion.
pub fn parse(input: &str) -> Result<SpdxExpr, String> {
    let trimmed = input.trim();
    if trimmed.is_empty() {
        return Err("empty expression".to_string());
    }
    let toks = tokenize(trimmed)?;
    if toks.is_empty() {
        return Err("empty expression".to_string());
    }
    let mut p = Parser { toks, pos: 0 };
    let root = p.parse_or()?;
    if p.pos != p.toks.len() {
        return Err(format!(
            "unexpected token {}: {}",
            p.pos,
            tok_desc(p.toks.get(p.pos))
        ));
    }
    let mut dnf = to_dnf(&root);
    // dedup whole identical conjunctions
    let mut seen = std::collections::HashSet::new();
    dnf.retain(|t| {
        let key = t
            .atoms
            .iter()
            .map(|a| a.display())
            .collect::<Vec<_>>()
            .join(" AND ");
        seen.insert(key)
    });
    if dnf.is_empty() {
        return Err("expression produced no alternatives".to_string());
    }
    Ok(SpdxExpr { root, dnf })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_simple_and_or_with() {
        let e = parse("MIT OR (Apache-2.0 AND GPL-2.0-only WITH Classpath-exception-2.0)").unwrap();
        assert_eq!(e.dnf.len(), 2);
        assert_eq!(e.dnf[0].atoms.len(), 1);
        assert_eq!(e.dnf[1].atoms.len(), 2);
        assert_eq!(
            e.dnf[1].atoms[1].exception.as_deref(),
            Some("Classpath-exception-2.0")
        );
    }

    #[test]
    fn distributes_and_over_or() {
        let e = parse("(MIT OR Apache-2.0) AND BSD-3-Clause").unwrap();
        assert_eq!(e.dnf.len(), 2);
        for t in &e.dnf {
            assert_eq!(t.atoms.len(), 2);
        }
    }

    #[test]
    fn keywords_case_insensitive() {
        let e = parse("mit or apache-2.0").unwrap();
        assert_eq!(e.dnf.len(), 2);
    }

    #[test]
    fn rejects_bad_expressions() {
        assert!(parse("").is_err());
        assert!(parse("MIT OR").is_err());
        assert!(parse("(MIT").is_err());
        assert!(parse("MIT)").is_err());
        assert!(parse("WITH").is_err());
        assert!(parse("MIT WITH").is_err());
        assert!(parse("OR MIT").is_err());
        assert!(parse("MIT OR OR Apache-2.0").is_err());
    }

    #[test]
    fn dedups_repeated_atoms_and_alternatives() {
        let e = parse("MIT AND MIT").unwrap();
        assert_eq!(e.dnf.len(), 1);
        assert_eq!(e.dnf[0].atoms.len(), 1);
        let e = parse("MIT OR MIT").unwrap();
        assert_eq!(e.dnf.len(), 1);
    }

    #[test]
    fn nested_parens() {
        let e = parse("((MIT OR Apache-2.0) AND (BSD-2-Clause OR ISC)) OR GPL-3.0-only").unwrap();
        assert_eq!(e.dnf.len(), 5);
    }
}
