//! Project policy: allowed-license matrix, copyleft classification,
//! `WITH` exception rules and a license-incompatibility matrix.
//!
//! Everything here is a *heuristic rule model*, not a legal determination.
//! Defaults encode a small, opinionated textbook table so the service is
//! usable out of the box; every part is overridable through the request.

use std::collections::{BTreeMap, BTreeSet};

use serde::{Deserialize, Serialize};

use crate::spdx::LicenseTerm;

/// Copyleft strength. Ordering matters: `None < Weak < Strong`.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Serialize, Deserialize, Default)]
#[serde(rename_all = "lowercase")]
pub enum Copyleft {
    /// Permissive (MIT, Apache-2.0, BSD-* ...).
    #[default]
    #[serde(alias = "permissive")]
    None,
    /// Weak copyleft (LGPL, MPL ...): propagates through static linking.
    Weak,
    /// Strong copyleft (GPL, AGPL ...): propagates through any linking.
    Strong,
}

impl Copyleft {
    pub fn as_str(self) -> &'static str {
        match self {
            Copyleft::None => "none",
            Copyleft::Weak => "weak",
            Copyleft::Strong => "strong",
        }
    }
}

/// What an exception id does to the copyleft strength of the term it qualifies.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum ExceptionEffect {
    /// Exception cancels copyleft propagation (e.g. Classpath-exception-2.0).
    Clear,
    /// Strong -> weak (e.g. LGPL-style relinking exception).
    DowngradeWeak,
    /// Exception is informational only.
    Keep,
}

/// One extra license entry provided by the caller.
#[derive(Debug, Clone, Deserialize, Default)]
#[serde(rename_all = "camelCase")]
pub struct LicenseRuleInput {
    /// Copyleft strength. Defaults to `none` for custom licenses.
    #[serde(default)]
    pub copyleft: Copyleft,
    /// Treat this license as proprietary (unlisted default distribution terms).
    #[serde(default)]
    pub proprietary: bool,
}

/// One exception entry provided by the caller.
#[derive(Debug, Clone, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct ExceptionRuleInput {
    pub effect: ExceptionEffect,
}

/// Full request-time policy. Every field is optional; built-in defaults fill
/// the gaps.
#[derive(Debug, Clone, Deserialize)]
#[serde(rename_all = "camelCase")]
#[serde(default)]
pub struct PolicyInput {
    /// Licenses added to the allow list (in addition to built-ins).
    #[serde(default)]
    pub allowed: Vec<String>,
    /// Licenses removed from / kept off the allow list (takes precedence).
    #[serde(default)]
    pub denied: Vec<String>,
    /// Override or extend license classification: id -> rule.
    #[serde(default)]
    pub licenses: BTreeMap<String, LicenseRuleInput>,
    /// Extra exception rules: exception id -> effect.
    #[serde(default)]
    pub exceptions: BTreeMap<String, ExceptionRuleInput>,
    /// Extra incompatible license pairs (symmetric).
    #[serde(default)]
    pub incompatible_pairs: Vec<[String; 2]>,
    /// Remove a built-in incompatibility (symmetric pair).
    #[serde(default)]
    pub compatible_pairs: Vec<[String; 2]>,
    /// Allow licenses not present in the allow list. Default: deny.
    #[serde(default)]
    pub unknown_licenses_allowed: bool,
    /// Allow exception ids without a known rule. Default: deny.
    #[serde(default)]
    pub unknown_exceptions_allowed: bool,
    /// Also flag copyleft incompatibilities on dynamic (rather than only
    /// static) edges. Default: true — conservative.
    #[serde(default = "default_true")]
    pub check_dynamic_links: bool,
    /// Treat weak copyleft burden on a proprietary node as a violation.
    /// Default: true.
    #[serde(default = "default_true")]
    pub proprietary_rejects_weak: bool,
}

fn default_true() -> bool {
    true
}

/// Resolved policy used by the engine.
#[derive(Debug, Clone)]
pub struct Policy {
    pub copyleft: BTreeMap<String, Copyleft>,
    pub proprietary: BTreeSet<String>,
    pub allowed: BTreeSet<String>,
    pub exceptions: BTreeMap<String, ExceptionEffect>,
    pub incompatible: BTreeSet<(String, String)>,
    pub unknown_licenses_allowed: bool,
    pub unknown_exceptions_allowed: bool,
    pub check_dynamic_links: bool,
    pub proprietary_rejects_weak: bool,
}

fn pair_key(a: &str, b: &str) -> (String, String) {
    if a <= b {
        (a.to_string(), b.to_string())
    } else {
        (b.to_string(), a.to_string())
    }
}

fn builtin_copyleft() -> BTreeMap<String, Copyleft> {
    let mut m = BTreeMap::new();
    let permissive = [
        "MIT",
        "Apache-2.0",
        "Apache-1.1",
        "BSD-2-Clause",
        "BSD-3-Clause",
        "ISC",
        "0BSD",
        "Unlicense",
        "Zlib",
        "CC0-1.0",
        "Python-2.0",
        "PSF-2.0",
        "BSL-1.0",
    ];
    for id in permissive {
        m.insert(id.to_string(), Copyleft::None);
    }
    let weak = [
        "LGPL-2.0-only",
        "LGPL-2.0-or-later",
        "LGPL-2.1-only",
        "LGPL-2.1-or-later",
        "LGPL-3.0-only",
        "LGPL-3.0-or-later",
        "MPL-1.1",
        "MPL-2.0",
        "EPL-1.0",
        "EPL-2.0",
        "CDDL-1.0",
        "CDDL-1.1",
    ];
    for id in weak {
        m.insert(id.to_string(), Copyleft::Weak);
    }
    let strong = [
        "GPL-2.0-only",
        "GPL-2.0-or-later",
        "GPL-3.0-only",
        "GPL-3.0-or-later",
        "AGPL-3.0-only",
        "AGPL-3.0-or-later",
        "SSPL-1.0",
    ];
    for id in strong {
        m.insert(id.to_string(), Copyleft::Strong);
    }
    // Special marker for proprietary/commercial licensing: no copyleft of its
    // own; the proprietary set drives the copyleft-into-proprietary rule.
    m.insert("Proprietary".to_string(), Copyleft::None);
    m
}

impl Policy {
    /// Build the resolved policy from request input.
    pub fn build(input: &PolicyInput) -> Self {
        let mut copyleft = builtin_copyleft();
        let mut proprietary: BTreeSet<String> = BTreeSet::new();
        proprietary.insert("Proprietary".to_string());

        for (id, rule) in &input.licenses {
            copyleft.insert(id.clone(), rule.copyleft);
            if rule.proprietary {
                proprietary.insert(id.clone());
            } else {
                proprietary.remove(id);
            }
        }

        // Default allow list: every built-in license.
        let mut allowed: BTreeSet<String> = copyleft.keys().cloned().collect();
        // Custom licenses mentioned by the caller are allowed too, unless the
        // global unknown-licenses switch says otherwise or they are denied.
        for id in input.licenses.keys() {
            allowed.insert(id.clone());
        }
        for id in &input.allowed {
            allowed.insert(id.clone());
            // Allow-listing a license the table does not know also classifies
            // it as permissive unless an explicit rule overrides that.
            copyleft.entry(id.clone()).or_insert(Copyleft::None);
        }
        for id in &input.denied {
            allowed.remove(id);
        }

        let mut exceptions: BTreeMap<String, ExceptionEffect> = BTreeMap::new();
        exceptions.insert("Classpath-exception-2.0".to_string(), ExceptionEffect::Clear);
        exceptions.insert("GCC-exception-2.0".to_string(), ExceptionEffect::Clear);
        exceptions.insert("GCC-exception-3.1".to_string(), ExceptionEffect::Clear);
        exceptions.insert(
            "LGPL-3.0-linking-exception".to_string(),
            ExceptionEffect::DowngradeWeak,
        );
        for (id, rule) in &input.exceptions {
            exceptions.insert(id.clone(), rule.effect);
        }

        let mut incompatible: BTreeSet<(String, String)> = BTreeSet::new();
        // Opinionated textbook defaults (rule model, not legal conclusions):
        // - GPLv2 family vs GPLv3 family
        // - GPL family vs Apache-2.0 patent terms (classic conservative view)
        for (a, b) in [
            ("GPL-2.0-only", "GPL-3.0-only"),
            ("GPL-2.0-only", "GPL-3.0-or-later"),
            ("GPL-2.0-or-later", "GPL-3.0-only"),
            ("GPL-2.0-only", "Apache-2.0"),
            ("GPL-2.0-or-later", "Apache-2.0"),
            ("GPL-2.0-only", "AGPL-3.0-only"),
            ("GPL-3.0-only", "SSPL-1.0"),
        ] {
            incompatible.insert(pair_key(a, b));
        }
        for pair in &input.incompatible_pairs {
            incompatible.insert(pair_key(&pair[0], &pair[1]));
        }
        for pair in &input.compatible_pairs {
            incompatible.remove(&pair_key(&pair[0], &pair[1]));
        }

        Policy {
            copyleft,
            proprietary,
            allowed,
            exceptions,
            incompatible,
            unknown_licenses_allowed: input.unknown_licenses_allowed,
            unknown_exceptions_allowed: input.unknown_exceptions_allowed,
            check_dynamic_links: input.check_dynamic_links,
            proprietary_rejects_weak: input.proprietary_rejects_weak,
        }
    }

    pub fn is_proprietary(&self, license_id: &str) -> bool {
        self.proprietary.contains(license_id)
    }

    /// Copyleft strength of a concrete term after applying its WITH exception.
    /// Returns `None` if the term references an unknown license/exception that
    /// the policy does not accept (the allow check carries the error message).
    pub fn effective_copyleft(&self, term: &LicenseTerm) -> Option<Copyleft> {
        let base = match self.copyleft.get(&term.license) {
            Some(c) => *c,
            None => {
                if self.unknown_licenses_allowed {
                    Copyleft::None
                } else {
                    return None;
                }
            }
        };
        match &term.exception {
            None => Some(base),
            Some(ex) => {
                if base == Copyleft::None {
                    Some(base)
                } else {
                    match self.exceptions.get(ex) {
                        Some(ExceptionEffect::Clear) => Some(Copyleft::None),
                        Some(ExceptionEffect::DowngradeWeak) => {
                            Some(if base == Copyleft::Strong {
                                Copyleft::Weak
                            } else {
                                base
                            })
                        }
                        Some(ExceptionEffect::Keep) => Some(base),
                        None => {
                            if self.unknown_exceptions_allowed {
                                Some(base)
                            } else {
                                None
                            }
                        }
                    }
                }
            }
        }
    }

    /// Whether a concrete term may be used at all under the allow matrix.
    pub fn term_allowance_error(&self, term: &LicenseTerm) -> Option<String> {
        if !self.unknown_licenses_allowed && !self.allowed.contains(&term.license) {
            return Some(format!("license {} is not in the allow list", term.license));
        }
        if let Some(ex) = &term.exception {
            if !self.unknown_exceptions_allowed && !self.exceptions.contains_key(ex) {
                return Some(format!("exception {ex} is not a known exception rule"));
            }
        }
        None
    }

    /// Whether two concrete licenses may be combined on one edge.
    pub fn licenses_incompatible(&self, a: &str, b: &str) -> bool {
        a != b && self.incompatible.contains(&pair_key(a, b))
    }
}

impl Default for PolicyInput {
    fn default() -> Self {
        PolicyInput {
            allowed: Vec::new(),
            denied: Vec::new(),
            licenses: BTreeMap::new(),
            exceptions: BTreeMap::new(),
            incompatible_pairs: Vec::new(),
            compatible_pairs: Vec::new(),
            unknown_licenses_allowed: false,
            unknown_exceptions_allowed: false,
            check_dynamic_links: true,
            proprietary_rejects_weak: true,
        }
    }
}

impl Default for Policy {
    fn default() -> Self {
        Policy::build(&PolicyInput::default())
    }
}
