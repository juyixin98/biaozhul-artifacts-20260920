//! A small SemVer range language.
//!
//! Supported grammar (whitespace-separated comparators inside an AND group;
//! groups can be joined with `||` for OR):
//!
//! ```text
//! range      := group ( "||" group )*
//! group      := comparator*             (empty group is invalid)
//! comparator := op? partial
//! op         := "^" | "~" | ">=" | "<=" | "=" | ">" | "<"
//! partial    := "*" | "1" | "1.x" | "1.2" | "1.2.x" | "1.2.3"
//!             | "1.2.3-beta.1"   (prerelease only on full triples)
//! ```
//!
//! Bare partial versions mean:
//! - `*` / `x` ........................... `>=0.0.0`
//! - `1` / `1.x` .......................... `>=1.0.0 <2.0.0`
//! - `1.2` / `1.2.x` ...................... `>=1.2.0 <1.3.0`
//! - `1.2.3` .............................. exact `=1.2.3`
//!
//! Inequalities on partials are normalized npm-style (`>1` becomes
//! `>=2.0.0`, `<=1.2` becomes `<1.3.0`, ...).
//!
//! Prerelease policy (npm-style): a prerelease version satisfies a comparator
//! only when (a) the comparator itself mentions a prerelease with the same
//! `major.minor.patch`, or (b) the global `allow_prerelease` flag is set on the
//! match call.

use semver::{Op, Prerelease, Version};
use serde::{Deserialize, Serialize};

/// A parsed comparator. For inequality/exact ops `minor`/`patch` are always
/// `Some`; caret/tilde comparators may leave positions open (`None`).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Comparator {
    pub op: Op,
    pub major: u64,
    pub minor: Option<u64>,
    pub patch: Option<u64>,
    pub pre: Prerelease,
}

/// A range is an OR of AND groups of comparators.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(try_from = "String", into = "String")]
pub struct Req {
    groups: Vec<Vec<Comparator>>,
    raw: String,
}

impl Req {
    pub fn parse(input: &str) -> Result<Req, String> {
        let raw = input.trim().to_string();
        if raw.is_empty() {
            return Err("empty version requirement".to_string());
        }
        let mut groups = Vec::new();
        for part in raw.split("||") {
            let mut comps = Vec::new();
            for token in part.split_whitespace() {
                if token.is_empty() {
                    continue;
                }
                comps.push(parse_comparator(token)?);
            }
            if comps.is_empty() {
                return Err(format!("empty OR group in requirement {raw:?}"));
            }
            groups.push(comps);
        }
        Ok(Req { groups, raw })
    }

    pub fn as_str(&self) -> &str {
        &self.raw
    }

    /// Does `v` satisfy every comparator in at least one OR group?
    ///
    /// `allow_prerelease` globally lifts the npm prerelease gate (e.g. when the
    /// caller explicitly opts into prereleases for the whole resolution).
    pub fn matches(&self, v: &Version, allow_prerelease: bool) -> bool {
        self.groups
            .iter()
            .any(|g| g.iter().all(|c| comparator_matches(c, v, allow_prerelease)))
    }

    /// Whether some comparator explicitly names a prerelease at the same
    /// `major.minor.patch` as `v` — the standard npm opt-in.
    pub fn allows_prerelease_of(&self, v: &Version) -> bool {
        if v.pre.is_empty() {
            return true;
        }
        self.groups.iter().any(|g| {
            g.iter().any(|c| {
                c.minor == Some(v.minor)
                    && c.patch == Some(v.patch)
                    && c.major == v.major
                    && c.pre != Prerelease::EMPTY
            })
        })
    }

    /// All explicit prerelease tokens mentioned anywhere in the requirement.
    pub fn prerelease_tokens(&self) -> Vec<(u64, u64, u64, String)> {
        let mut out = Vec::new();
        for g in &self.groups {
            for c in g {
                if c.pre != Prerelease::EMPTY {
                    out.push((c.major, c.minor.unwrap_or(0), c.patch.unwrap_or(0), c.pre.to_string()));
                }
            }
        }
        out
    }
}

impl std::fmt::Display for Req {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(&self.raw)
    }
}

impl std::str::FromStr for Req {
    type Err = String;
    fn from_str(s: &str) -> Result<Self, Self::Err> {
        Req::parse(s)
    }
}

impl TryFrom<String> for Req {
    type Error = String;
    fn try_from(s: String) -> Result<Self, Self::Error> {
        Req::parse(&s)
    }
}

impl From<Req> for String {
    fn from(r: Req) -> String {
        r.raw
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum CmpOp {
    Caret,
    Tilde,
    Exact,
    Gt,
    Gte,
    Lt,
    Lte,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum WildLevel {
    /// The token was `*` / `x`.
    Full,
    /// Wildcard at the minor position, e.g. `1.x`.
    Major,
    /// Wildcard at the patch position, e.g. `1.2.x`.
    Minor,
    /// Full triple.
    None,
}

fn parse_comparator(tok: &str) -> Result<Comparator, String> {
    let (op, rest) = if let Some(r) = tok.strip_prefix('^') {
        (CmpOp::Caret, r)
    } else if let Some(r) = tok.strip_prefix('~') {
        (CmpOp::Tilde, r)
    } else if let Some(r) = tok.strip_prefix(">=") {
        (CmpOp::Gte, r)
    } else if let Some(r) = tok.strip_prefix("<=") {
        (CmpOp::Lte, r)
    } else if let Some(r) = tok.strip_prefix("==") {
        (CmpOp::Exact, r)
    } else if let Some(r) = tok.strip_prefix('=') {
        (CmpOp::Exact, r)
    } else if let Some(r) = tok.strip_prefix('>') {
        (CmpOp::Gt, r)
    } else if let Some(r) = tok.strip_prefix('<') {
        (CmpOp::Lt, r)
    } else if tok == "*" || tok.eq_ignore_ascii_case("x") || tok.eq_ignore_ascii_case("latest") {
        return Ok(Comparator {
            op: Op::GreaterEq, major: 0, minor: Some(0), patch: Some(0), pre: Prerelease::EMPTY,
        });
    } else {
        (CmpOp::Exact, tok)
    };

    let (maj, min, pat, pre, wild) = parse_partial(rest)?;
    let major = maj.unwrap_or(0);

    let cmp = match op {
        _ if wild == WildLevel::Full => Comparator {
            op: Op::GreaterEq, major: 0, minor: Some(0), patch: Some(0), pre: Prerelease::EMPTY,
        },
        CmpOp::Exact => match wild {
            WildLevel::Full => Comparator {
                op: Op::GreaterEq, major: 0, minor: Some(0), patch: Some(0), pre: Prerelease::EMPTY,
            },
            WildLevel::Major => Comparator {
                op: Op::Caret, major, minor: None, patch: None, pre: Prerelease::EMPTY,
            },
            WildLevel::Minor => Comparator {
                op: Op::Tilde, major, minor: Some(min.unwrap_or(0)), patch: None, pre: Prerelease::EMPTY,
            },
            WildLevel::None => Comparator {
                op: Op::Exact,
                major,
                minor: Some(min.unwrap_or(0)),
                patch: Some(pat.unwrap_or(0)),
                pre,
            },
        },
        CmpOp::Caret => Comparator {
            op: Op::Caret,
            major,
            minor: min,
            patch: pat,
            pre: if wild == WildLevel::None { pre } else { Prerelease::EMPTY },
        },
        CmpOp::Tilde => Comparator {
            op: Op::Tilde,
            major,
            minor: if wild == WildLevel::Major { None } else { min },
            patch: pat,
            pre: if wild == WildLevel::None { pre } else { Prerelease::EMPTY },
        },
        // Inequalities on partial versions are pulled up to full triple bounds.
        CmpOp::Gt => match wild {
            WildLevel::None => ineq(Op::Greater, major, min, pat, pre),
            WildLevel::Major => bound(Op::GreaterEq, major + 1, 0, 0),
            WildLevel::Minor => bound(Op::GreaterEq, major, min.unwrap_or(0) + 1, 0),
            WildLevel::Full => unreachable!(),
        },
        CmpOp::Gte => match wild {
            WildLevel::None => ineq(Op::GreaterEq, major, min, pat, pre),
            WildLevel::Major => bound(Op::GreaterEq, major, 0, 0),
            WildLevel::Minor => bound(Op::GreaterEq, major, min.unwrap_or(0), 0),
            WildLevel::Full => unreachable!(),
        },
        CmpOp::Lt => match wild {
            WildLevel::None => ineq(Op::Less, major, min, pat, pre),
            // <1 / <1.x means <1.0.0; <1.2 / <1.2.x means <1.2.0.
            WildLevel::Major => bound(Op::Less, major, 0, 0),
            WildLevel::Minor => bound(Op::Less, major, min.unwrap_or(0), 0),
            WildLevel::Full => unreachable!(),
        },
        CmpOp::Lte => match wild {
            WildLevel::None => ineq(Op::LessEq, major, min, pat, pre),
            WildLevel::Major => bound(Op::Less, major + 1, 0, 0),
            WildLevel::Minor => bound(Op::Less, major, min.unwrap_or(0) + 1, 0),
            WildLevel::Full => unreachable!(),
        },
    };
    Ok(cmp)
}

fn ineq(op: Op, major: u64, minor: Option<u64>, patch: Option<u64>, pre: Prerelease) -> Comparator {
    Comparator { op, major, minor, patch, pre }
}

fn bound(op: Op, major: u64, minor: u64, patch: u64) -> Comparator {
    Comparator { op, major, minor: Some(minor), patch: Some(patch), pre: Prerelease::EMPTY }
}

type ParsedPartial = (Option<u64>, Option<u64>, Option<u64>, Prerelease, WildLevel);

fn parse_partial(s: &str) -> Result<ParsedPartial, String> {
    let (nums, pre_str) = match s.split_once('-') {
        Some((n, p)) => (n, Some(p)),
        None => (s, None),
    };
    let mut parsed: Vec<Option<u64>> = Vec::new();
    let mut wild = WildLevel::None;
    for (i, p) in nums.split('.').enumerate() {
        if i >= 3 {
            return Err(format!("too many numeric components in {s:?}"));
        }
        if p == "*" || p.eq_ignore_ascii_case("x") {
            parsed.push(None);
            // The leftmost wildcard position decides the level; `x.x` stays
            // a full wildcard rather than downgrading to Major.
            if !matches!(wild, WildLevel::Full) {
                wild = match i {
                    0 => WildLevel::Full,
                    1 => WildLevel::Major,
                    _ => WildLevel::Minor,
                };
            }
        } else {
            let n: u64 = p.parse().map_err(|_| format!("bad numeric component {p:?} in {s:?}"))?;
            parsed.push(Some(n));
        }
    }
    for w in parsed.windows(2) {
        if w[0].is_none() && w[1].is_some() {
            return Err(format!("wildcard must be the final component in {s:?}"));
        }
    }
    // A bare partial without an explicit `x` (`1`, `1.2`) behaves like its
    // wildcard counterpart (`1.x`, `1.2.x`).
    if wild == WildLevel::None {
        wild = match parsed.len() {
            1 => WildLevel::Major,
            2 => WildLevel::Minor,
            _ => WildLevel::None,
        };
    }
    let maj = parsed.first().copied().flatten();
    let min = parsed.get(1).copied().flatten();
    let pat = parsed.get(2).copied().flatten();

    let pre = match (pre_str, maj, min, pat) {
        (None, _, _, _) => Prerelease::EMPTY,
        (Some(p), Some(_), Some(_), Some(_)) => {
            Prerelease::new(p).map_err(|e| format!("bad prerelease in {s:?}: {e}"))?
        }
        (Some(_), _, _, _) => return Err(format!("prerelease requires a full major.minor.patch in {s:?}")),
    };
    Ok((maj, min, pat, pre, wild))
}

fn bound_version(major: u64, minor: u64, patch: u64, pre: &Prerelease) -> Version {
    Version { major, minor, patch, pre: pre.clone(), build: Default::default() }
}

/// Compare `v` against a single comparator with the npm-style prerelease gate.
/// We do not delegate to `semver::Comparator::matches` because its prerelease
/// gate cannot be lifted by callers that globally allow prereleases.
fn comparator_matches(c: &Comparator, v: &Version, allow_prerelease: bool) -> bool {
    if !v.pre.is_empty() && !allow_prerelease {
        // Only a comparator naming a prerelease on the exact same tuple opts in.
        if !(c.pre != Prerelease::EMPTY
            && c.major == v.major
            && c.minor == Some(v.minor)
            && c.patch == Some(v.patch))
        {
            return false;
        }
    }
    match c.op {
        Op::Exact => {
            v.major == c.major
                && v.minor == c.minor.unwrap_or(0)
                && v.patch == c.patch.unwrap_or(0)
                && v.pre == c.pre
        }
        Op::Greater | Op::GreaterEq | Op::Less | Op::LessEq => {
            let b = bound_version(c.major, c.minor.unwrap_or(0), c.patch.unwrap_or(0), &c.pre);
            match c.op {
                Op::Greater => v > &b,
                Op::GreaterEq => v >= &b,
                Op::Less => v < &b,
                Op::LessEq => v <= &b,
                _ => unreachable!(),
            }
        }
        Op::Caret => caret_matches(c, v),
        Op::Tilde => tilde_matches(c, v),
        Op::Wildcard => true,
        _ => false,
    }
}

/// `^` semantics, including the zero-major boundary rules, using `None`
/// positions to remember which components were open in the source token.
fn caret_matches(c: &Comparator, v: &Version) -> bool {
    if v.major != c.major {
        return false;
    }
    let lower = bound_version(c.major, c.minor.unwrap_or(0), c.patch.unwrap_or(0), &c.pre);
    if v < &lower {
        return false;
    }
    match (c.minor, c.patch) {
        // ^1 / ^1.x / ^1.2 / ^1.2.x: any higher minor/patch is fine when
        // major > 0.
        (_, _) if c.major > 0 => true,
        // ^0.2.3 / ^0.2.x: minor pinned.
        (Some(m), _) if m > 0 => v.minor == m,
        // ^0 / ^0.x: everything in 0.x.
        (None, None) => true,
        // ^0.0 / ^0.0.x: everything in 0.0.x.
        (Some(0), None) => v.minor == 0,
        // ^0.0.3: patch pinned.
        (Some(0), Some(p)) => v.minor == 0 && v.patch == p,
        _ => false,
    }
}

/// `~` semantics: allow patch-level changes within the pinned minor; a token
/// without a minor (`~1`) allows minor-level changes within the major.
fn tilde_matches(c: &Comparator, v: &Version) -> bool {
    if v.major != c.major {
        return false;
    }
    match c.minor {
        None => true, // ~1 -> 1.x.y, lower bound already checked
        Some(m) => {
            if v.minor != m {
                return false;
            }
            let lower = bound_version(c.major, m, c.patch.unwrap_or(0), &c.pre);
            v >= &lower
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn v(s: &str) -> Version {
        s.parse().unwrap()
    }

    #[test]
    fn exact_and_wildcards() {
        let r = Req::parse("1.2.3").unwrap();
        assert!(r.matches(&v("1.2.3"), false));
        assert!(!r.matches(&v("1.2.4"), false));

        for s in ["*", "x", "x.x", "x.x.x"] {
            let r = Req::parse(s).unwrap();
            assert!(r.matches(&v("0.0.1"), false));
            assert!(r.matches(&v("9.9.9"), false));
        }

        let r = Req::parse("1.x").unwrap();
        assert!(r.matches(&v("1.0.0"), false));
        assert!(r.matches(&v("1.9.9"), false));
        assert!(!r.matches(&v("2.0.0"), false));

        let r = Req::parse("1.2").unwrap();
        assert!(r.matches(&v("1.2.0"), false));
        assert!(r.matches(&v("1.2.99"), false));
        assert!(!r.matches(&v("1.3.0"), false));
    }

    #[test]
    fn caret_and_tilde() {
        let r = Req::parse("^1.2.3").unwrap();
        assert!(!r.matches(&v("1.2.2"), false));
        assert!(r.matches(&v("1.2.3"), false));
        assert!(r.matches(&v("1.9.0"), false));
        assert!(!r.matches(&v("2.0.0"), false));

        let r = Req::parse("^0.2.3").unwrap();
        assert!(r.matches(&v("0.2.9"), false));
        assert!(!r.matches(&v("0.3.0"), false));

        let r = Req::parse("^0.0.3").unwrap();
        assert!(r.matches(&v("0.0.3"), false));
        assert!(!r.matches(&v("0.0.4"), false));

        let r = Req::parse("^0").unwrap();
        assert!(r.matches(&v("0.0.1"), false));
        assert!(r.matches(&v("0.5.0"), false));
        assert!(!r.matches(&v("1.0.0"), false));

        let r = Req::parse("^0.0").unwrap();
        assert!(r.matches(&v("0.0.9"), false));
        assert!(!r.matches(&v("0.1.0"), false));

        let r = Req::parse("~1.2.3").unwrap();
        assert!(r.matches(&v("1.2.9"), false));
        assert!(!r.matches(&v("1.3.0"), false));

        let r = Req::parse("~1").unwrap();
        assert!(r.matches(&v("1.0.0"), false));
        assert!(r.matches(&v("1.9.0"), false));
        assert!(!r.matches(&v("2.0.0"), false));
    }

    #[test]
    fn inequalities_and_and_or() {
        let r = Req::parse(">=1.0.0 <2.0.0").unwrap();
        assert!(r.matches(&v("1.5.0"), false));
        assert!(!r.matches(&v("2.0.0"), false));

        let r = Req::parse("1.2.3 || >=2.0.0 <3.0.0").unwrap();
        assert!(r.matches(&v("1.2.3"), false));
        assert!(r.matches(&v("2.5.0"), false));
        assert!(!r.matches(&v("1.5.0"), false));
        assert!(!r.matches(&v("3.0.0"), false));

        assert!(Req::parse("1.2.3 ||").is_err());
        assert!(Req::parse(">1.2.3-rc").is_ok());
    }

    #[test]
    fn partial_inequalities() {
        assert!(Req::parse(">1").unwrap().matches(&v("2.0.0"), false));
        assert!(!Req::parse(">1").unwrap().matches(&v("1.9.9"), false));
        assert!(Req::parse("<1").unwrap().matches(&v("0.9.9"), false));
        assert!(!Req::parse("<1").unwrap().matches(&v("1.0.0"), false));
        assert!(Req::parse("<=1.2").unwrap().matches(&v("1.2.9"), false));
        assert!(!Req::parse("<=1.2").unwrap().matches(&v("1.3.0"), false));
    }

    #[test]
    fn prerelease_gate() {
        let r = Req::parse(">=1.0.0").unwrap();
        assert!(!r.matches(&v("1.5.0-beta"), false));
        assert!(r.matches(&v("1.5.0-beta"), true));

        let r = Req::parse(">=1.5.0-beta.1").unwrap();
        assert!(r.matches(&v("1.5.0-beta.2"), false));
        assert!(!r.matches(&v("1.6.0-beta.1"), false));
        assert!(r.matches(&v("1.5.0"), false));
    }
}
