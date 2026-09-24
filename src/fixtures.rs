//! Built-in acceptance fixtures, also exposed through `GET /fixtures/<name>`.
//!
//! Scenarios:
//!
//! - `diamond`: A and B both depend on X with disjoint-but-resolvable
//!   ranges; only backtracking (not greedy highest) succeeds.
//! - `mutex`: two deps impose non-overlapping ranges on X — unsat.
//! - `cycle_ok`: mutually-circular packages with permissive ranges — sat,
//!   lockfile records the cycle.
//! - `cycle_unsat`: a cycle whose versions cannot be jointly satisfied — unsat.
//! - `optional`: optional feature gating + a platform-conditional edge.

use crate::model::{PackageVersion, Preference, Registry, RootRequirement, SolveRequest};
use crate::platform::Platform;
use crate::semver_range::Req;

fn pv(name: &str, version: &str, deps: Vec<(&str, &str, bool, Option<&str>)>) -> PackageVersion {
    PackageVersion {
        name: name.into(),
        version: version.parse().unwrap(),
        yanked: false,
        dependencies: deps
            .into_iter()
            .map(|(n, r, optional, feature)| crate::model::Dependency {
                name: n.into(),
                req: Req::parse(r).unwrap(),
                optional,
                feature: feature.map(|f| f.into()),
                target: None,
            })
            .collect(),
    }
}

/// Diamond that defeats greedy-highest:
///
/// - app -> a ^1.0 and b ^1.0
/// - a@1.0.0 needs x <1.5 ; a@1.1.0 needs x <1.2
/// - b needs x >=1.4
///
/// Greedy picks a 1.1.0 + highest x -> dead end; backtracking to a 1.0.0
/// and x 1.4.x resolves.
pub fn diamond() -> SolveRequest {
    let registry = Registry {
        packages: [
            ("a".to_string(), vec![
                pv("a", "1.0.0", vec![("x", "<1.5", false, None)]),
                pv("a", "1.1.0", vec![("x", "<1.2", false, None)]),
            ]),
            ("b".to_string(), vec![
                pv("b", "1.0.0", vec![("x", ">=1.4.0", false, None)]),
            ]),
            ("x".to_string(), vec![
                pv("x", "1.0.0", vec![]),
                pv("x", "1.4.0", vec![]),
                pv("x", "1.4.5", vec![]),
                pv("x", "1.5.0", vec![]),
            ]),
        ]
        .into_iter()
        .collect(),
    };
    SolveRequest {
        registry,
        requirements: vec![
            RootRequirement { name: "a".into(), req: Req::parse("^1.0").unwrap() },
            RootRequirement { name: "b".into(), req: Req::parse("^1.0").unwrap() },
        ],
        ..Default::default()
    }
}

/// Mutex ranges: a needs x ^1.0, b needs x ^2.0; both are mandatory.
pub fn mutex() -> SolveRequest {
    let registry = Registry {
        packages: [
            ("a".to_string(), vec![pv("a", "1.0.0", vec![("x", "^1.0", false, None)])]),
            ("b".to_string(), vec![pv("b", "1.0.0", vec![("x", "^2.0", false, None)])]),
            ("x".to_string(), vec![
                pv("x", "1.9.0", vec![]),
                pv("x", "2.0.0", vec![]),
            ]),
        ]
        .into_iter()
        .collect(),
    };
    SolveRequest {
        registry,
        requirements: vec![
            RootRequirement { name: "a".into(), req: Req::parse("*").unwrap() },
            RootRequirement { name: "b".into(), req: Req::parse("*").unwrap() },
        ],
        ..Default::default()
    }
}

/// A satisfiable cycle: ping <-> pong, permissive ranges.
pub fn cycle_ok() -> SolveRequest {
    let registry = Registry {
        packages: [
            ("ping".to_string(), vec![pv("ping", "1.0.0", vec![("pong", "^1.0", false, None)])]),
            ("pong".to_string(), vec![
                pv("pong", "1.0.0", vec![("ping", "^1.0", false, None)]),
                pv("pong", "1.1.0", vec![("ping", "^1.0", false, None)]),
            ]),
        ]
        .into_iter()
        .collect(),
    };
    SolveRequest {
        registry,
        requirements: vec![RootRequirement { name: "ping".into(), req: Req::parse("*").unwrap() }],
        ..Default::default()
    }
}

/// Circular diamond with a version mismatch: ping needs pong ^2, but the only
/// pong that tolerates ping 1.x is pong 1.x (which needs ping ^1); pong 2.x
/// needs ping ^2 and ping 2 doesn't exist -> unsat.
pub fn cycle_unsat() -> SolveRequest {
    let registry = Registry {
        packages: [
            ("ping".to_string(), vec![
                pv("ping", "1.0.0", vec![("pong", "^2.0", false, None)]),
            ]),
            ("pong".to_string(), vec![
                pv("pong", "1.5.0", vec![("ping", "^1.0", false, None)]),
                pv("pong", "2.0.0", vec![("ping", "^2.0", false, None)]),
            ]),
        ]
        .into_iter()
        .collect(),
    };
    SolveRequest {
        registry,
        requirements: vec![RootRequirement { name: "ping".into(), req: Req::parse("*").unwrap() }],
        ..Default::default()
    }
}

/// Optional feature + platform condition:
/// - app 1.0 has a mandatory `core` dep, optional `cache` dep (feature "fast"),
///   and a linux-only `io_uring` dep.
/// - on linux with feature fast: core, cache, io_uring selected
/// - on macos without feature: only core
pub fn optional() -> SolveRequest {
    let mut app = pv("app", "1.0.0", vec![
        ("core", "^1.0", false, None),
        ("cache", "^1.0", true, Some("fast")),
    ]);
    app.dependencies.push(crate::model::Dependency {
        name: "io_uring".into(),
        req: Req::parse("^1.0").unwrap(),
        optional: false,
        feature: None,
        target: Some(crate::platform::Target::Os("linux".into())),
    });
    let registry = Registry {
        packages: [
            ("app".to_string(), vec![app]),
            ("core".to_string(), vec![pv("core", "1.2.0", vec![])]),
            ("cache".to_string(), vec![pv("cache", "1.0.0", vec![])]),
            ("io_uring".to_string(), vec![pv("io_uring", "1.0.0", vec![])]),
        ]
        .into_iter()
        .collect(),
    };
    let mut features = std::collections::BTreeMap::new();
    features.insert("app".to_string(), vec!["fast".to_string()]);
    SolveRequest {
        registry,
        requirements: vec![RootRequirement { name: "app".into(), req: Req::parse("*").unwrap() }],
        features,
        platform: Some(Platform { os: Some("linux".into()), arch: Some("x86_64".into()), family: Some("unix".into()) }),
        preferences: [(
            "cache".to_string(),
            Preference::Lowest,
        )]
        .into_iter()
        .collect(),
        ..Default::default()
    }
}

pub fn by_name(name: &str) -> Option<SolveRequest> {
    Some(match name {
        "diamond" => diamond(),
        "mutex" => mutex(),
        "cycle_ok" => cycle_ok(),
        "cycle_unsat" => cycle_unsat(),
        "optional" => optional(),
        _ => return None,
    })
}
