// Resolved policy: the illustrative built-in license tables plus the
// project-provided custom allow matrix. Everything here is a *rule*; the
// service does not render legal conclusions (see README).

use std::collections::{BTreeMap, BTreeSet};

use crate::model::{Link, PolicyReq, Strength, UnknownPolicy};
use crate::spdx::Atom;

/// Illustrative built-in copyleft classification. NOT legal advice;
/// override/extend via the request `policy.copyleft` map.
pub fn builtin_classification(id_upper: &str) -> Option<CopyleftClass> {
    Some(match id_upper {
        // permissive
        "MIT" | "ISC" | "BSD-2-CLAUSE" | "BSD-3-CLAUSE" | "APACHE-2.0"
        | "0BSD" | "UNLICENSE" | "ZLIB" => CopyleftClass::None,
        // weak / file-level
        "LGPL-2.1-ONLY" | "LGPL-2.1-OR-LATER" | "LGPL-3.0-ONLY"
        | "LGPL-3.0-OR-LATER" | "MPL-2.0" | "EPL-1.0" | "EPL-2.0"
        | "CDDL-1.0" | "CDDL-1.1" => CopyleftClass::Weak,
        // strong / network
        "GPL-2.0-ONLY" | "GPL-2.0-OR-LATER" | "GPL-3.0-ONLY"
        | "GPL-3.0-OR-LATER" | "AGPL-3.0-ONLY" | "AGPL-3.0-OR-LATER"
        | "AGPL-1.0-ONLY" | "AGPL-1.0-OR-LATER" => CopyleftClass::Strong,
        _ => return None,
    })
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum CopyleftClass {
    None,
    Weak,
    Strong,
}

impl CopyleftClass {
    fn parse(s: &str) -> Result<CopyleftClass, String> {
        match s.trim().to_ascii_lowercase().as_str() {
            "none" | "permissive" => Ok(CopyleftClass::None),
            "weak" => Ok(CopyleftClass::Weak),
            "strong" => Ok(CopyleftClass::Strong),
            other => Err(format!(
                "invalid copyleft class '{}': expected none|weak|strong",
                other
            )),
        }
    }

    #[allow(dead_code)]
    pub fn strength(&self) -> Option<Strength> {
        match self {
            CopyleftClass::None => None,
            CopyleftClass::Weak => Some(Strength::Weak),
            CopyleftClass::Strong => Some(Strength::Strong),
        }
    }
}
/// Illustrative built-in recognized exceptions.
pub const BUILTIN_EXCEPTIONS: &[&str] = &[
    "CLASSPATH-EXCEPTION-2.0",
    "OCAML-LGPL-LINKING-EXCEPTION",
    "GCC-EXCEPTION-3.1",
    "LGPL-2.0-ONLY",
    "LGPL-2.0-PLUS",
    "WXWINDOWS-LIBRARY-EXCEPTION-3.1",
    "AUTOCONF-EXCEPTION-3.0",
];

#[allow(dead_code)]
pub fn builtin_exception(id_upper: &str) -> bool {
    BUILTIN_EXCEPTIONS.contains(&id_upper)
}

#[derive(Debug, Clone)]
pub struct ResolvedPolicy {
    allowed: BTreeMap<String, BTreeSet<String>>,
    known: BTreeSet<String>,
    exceptions: BTreeSet<String>,
    classifications: BTreeMap<String, CopyleftClass>,
    compatible_with: BTreeMap<String, BTreeSet<String>>,
    pub strong_on: Vec<Link>,
    pub weak_on: Vec<Link>,
    pub unknown: UnknownPolicy,
}

fn norm(s: &str) -> String {
    s.trim().to_ascii_uppercase()
}

fn parse_links(items: &[String], fallback: Vec<Link>, field: &str) -> Result<Vec<Link>, String> {
    if items.is_empty() {
        return Ok(fallback);
    }
    let mut out = Vec::new();
    for it in items {
        match it.trim().to_ascii_lowercase().as_str() {
            "static" => out.push(Link::Static),
            "dynamic" => out.push(Link::Dynamic),
            other => return Err(format!("invalid link type '{}' in {}", other, field)),
        }
    }
    out.sort_by_key(|l| match l {
        Link::Static => 0,
        Link::Dynamic => 1,
    });
    out.dedup();
    Ok(out)
}

impl ResolvedPolicy {
    pub fn resolve(req: &PolicyReq) -> Result<ResolvedPolicy, String> {
        // Allowed matrix: normalize contexts to lowercase and validate.
        let mut allowed: BTreeMap<String, BTreeSet<String>> = BTreeMap::new();
        for (lic, ctxs) in &req.allowed_licenses {
            if lic.trim().is_empty() {
                return Err("policy.allowed_licenses has an empty license key".to_string());
            }
            let mut set = BTreeSet::new();
            for c in ctxs {
                match c.trim().to_ascii_lowercase().as_str() {
                    "*" | "root" => {
                        set.insert("*".to_string());
                    }
                    "static" => {
                        set.insert("static".to_string());
                    }
                    "dynamic" => {
                        set.insert("dynamic".to_string());
                    }
                    other => {
                        return Err(format!(
                            "invalid context '{}' for license '{}': expected *|static|dynamic",
                            other, lic
                        ))
                    }
                }
            }
            allowed.insert(norm(lic), set);
        }

        let mut known: BTreeSet<String> = BTreeSet::new();
        // every license mentioned in the allow matrix or compatibility
        // matrix is treated as known
        for k in allowed.keys() {
            known.insert(k.clone());
        }
        for lic in &req.extra_licenses {
            known.insert(norm(lic));
        }

        // Start classifications from built-ins for the known set, then overrides.
        let mut classifications: BTreeMap<String, CopyleftClass> = BTreeMap::new();
        for lic in &known {
            if let Some(c) = builtin_classification(lic) {
                classifications.insert(lic.clone(), c);
            }
        }
        for (lic, cls) in &req.copyleft {
            classifications.insert(norm(lic), CopyleftClass::parse(cls)?);
            known.insert(norm(lic));
        }

        // The illustrative built-in exceptions are always recognized; the
        // request can extend (but not shrink) that set.
        let mut exceptions: BTreeSet<String> = BUILTIN_EXCEPTIONS.iter().map(|s| s.to_string()).collect();
        exceptions.extend(req.exceptions.iter().map(|e| norm(e)));
        exceptions.retain(|e| !e.is_empty());

        let mut compatible: BTreeMap<String, BTreeSet<String>> = BTreeMap::new();
        for (obl, targets) in &req.compatible_with {
            let mut set = BTreeSet::new();
            for t in targets {
                set.insert(norm(t));
            }
            compatible.insert(norm(obl), set);
        }

        // Illustrative default propagation: strong across static+dynamic,
        // weak across static only. All overridable.
        let strong_on = parse_links(
            req.strong_propagates_on.as_deref().unwrap_or(&[]),
            vec![Link::Static, Link::Dynamic],
            "strong_propagates_on",
        )?;
        let weak_on = parse_links(
            req.weak_propagates_on.as_deref().unwrap_or(&[]),
            vec![Link::Static],
            "weak_propagates_on",
        )?;

        Ok(ResolvedPolicy {
            allowed,
            known,
            exceptions,
            classifications,
            compatible_with: compatible,
            strong_on,
            weak_on,
            unknown: req.unknown_licenses,
        })
    }

    pub fn is_known(&self, license: &str) -> bool {
        self.known.contains(&norm(license))
    }

    pub fn is_recognized_exception(&self, exception: &str) -> bool {
        self.exceptions.contains(&norm(exception))
    }

    pub fn classify(&self, license: &str) -> CopyleftClass {
        self.classifications
            .get(&norm(license))
            .copied()
            .unwrap_or(CopyleftClass::None)
    }

    pub fn propagation_links(&self, strength: Strength) -> &[Link] {
        match strength {
            Strength::Strong => &self.strong_on,
            Strength::Weak => &self.weak_on,
        }
    }

    /// Whether `license` is selectable for a package that arrives via the
    /// given incoming link contexts. `contexts` empty means a root package.
    pub fn allow_status(
        &self,
        license: &str,
        contexts: &[Link],
    ) -> Result<(), AllowViolation> {
        let key = norm(license);
        let entry = match self.allowed.get(&key) {
            Some(e) => e,
            None => return Err(AllowViolation::NotInMatrix),
        };
        if contexts.is_empty() {
            if entry.contains("*") {
                Ok(())
            } else {
                Err(AllowViolation::RootContext)
            }
        } else {
            for c in contexts {
                let tag = match c {
                    Link::Static => "static",
                    Link::Dynamic => "dynamic",
                };
                if !entry.contains(tag) {
                    return Err(AllowViolation::EdgeContext(tag));
                }
            }
            Ok(())
        }
    }

    /// Whether a target atom license is compatible with an incoming
    /// copyleft obligation whose source license is `obl_license`.
    pub fn compatible(&self, obl_license: &str, target_license: &str) -> bool {
        let obl = norm(obl_license);
        let target = norm(target_license);
        // A license is always compatible with itself.
        if obl == target {
            return true;
        }
        // Default rule (when the project defines no entry for this
        // obligation): only the same license complies.
        match self.compatible_with.get(&obl) {
            Some(set) => set.contains(&target) || set.contains("*"),
            None => false,
        }
    }

    /// Whether a chosen atom itself is valid: known/unknown policy, allowed
    /// matrix for its contexts, and a recognized exception if it has one.
    pub fn atom_static_ok(
        &self,
        atom: &Atom,
        contexts: &[Link],
    ) -> Result<(), AtomStaticError> {
        if !self.is_known(&atom.license) {
            match self.unknown {
                UnknownPolicy::Reject => return Err(AtomStaticError::Unknown),
                UnknownPolicy::Allow => {}
            }
        }
        if let Some(e) = &atom.exception {
            if !self.is_recognized_exception(e) {
                return Err(AtomStaticError::UnrecognizedException);
            }
        }
        self.allow_status(&atom.license, contexts)
            .map_err(AtomStaticError::Allow)
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum AllowViolation {
    NotInMatrix,
    RootContext,
    EdgeContext(&'static str),
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum AtomStaticError {
    Unknown,
    UnrecognizedException,
    Allow(AllowViolation),
}
