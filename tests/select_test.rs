//! Selection behavior: ARM variants, missing platform, ambiguity,
//! nested indexes, cycle guard, digest verification and order independence.

mod common;

use common::*;
use manifest_selector::error::ApiError;
use manifest_selector::select::{select_with, SelectOptions};
use manifest_selector::{digest::Digest, reference::Ref};
use serde_json::{json, Value};

fn multiarch_store() -> (manifest_selector::store::Store, String) {
    let mut store = manifest_selector::store::Store::new();
    let repo = "demo/app";

    let amd64 = manifest_leaf("amd64");
    let arm_v5 = manifest_leaf("arm-v5");
    let arm_v6 = manifest_leaf("arm-v6");
    let arm_v7 = manifest_leaf("arm-v7");
    let arm64_v8 = manifest_leaf("arm64-v8");
    let riscv64 = manifest_leaf("riscv64");

    // Order deliberately put together so "first match" would give a wrong
    // answer for several requests below.
    let root = index_of(vec![
        descriptor(&arm_v5, platform("linux", "arm", Some("v5"))),
        descriptor(&amd64, platform("linux", "amd64", None)),
        descriptor(&riscv64, platform("linux", "riscv64", None)),
        descriptor(&arm_v7, platform("linux", "arm", Some("v7"))),
        descriptor(&arm64_v8, platform("linux", "arm64", Some("v8"))),
        descriptor(&arm_v6, platform("linux", "arm", Some("v6"))),
    ]);

    let root_digest = insert(&mut store, repo, root);
    for leaf in [amd64, arm_v5, arm_v6, arm_v7, arm64_v8, riscv64] {
        insert(&mut store, repo, leaf);
    }
    tag(&mut store, repo, "latest", &root_digest);
    (store, repo.to_string())
}

#[test]
fn selects_explicit_arm_variants_independently_of_array_order() {
    let (store, repo) = multiarch_store();
    for variant in ["v5", "v6", "v7"] {
        let result = common::select(
            &store,
            &repo,
            "latest",
            &request("linux", "arm", Some(variant)),
        )
        .unwrap();
        let p = result.chosen.platform.clone().unwrap();
        assert_eq!(p.architecture.as_deref(), Some("arm"));
        assert_eq!(p.variant.as_deref(), Some(variant));
        assert_eq!(result.chosen.path.len(), 2);
        assert_eq!(result.chosen.path[0].kind, "index");
        assert_eq!(result.chosen.path[1].kind, "manifest");
    }
}

#[test]
fn selects_arm64_v8() {
    let (store, repo) = multiarch_store();
    let result = common::select(
        &store,
        &repo,
        "latest",
        &request("linux", "arm64", Some("v8")),
    )
    .unwrap();
    assert_eq!(
        result.chosen.platform.as_ref().unwrap().variant.as_deref(),
        Some("v8")
    );
}

#[test]
fn arm_without_named_variant_prefers_highest_variant() {
    let (store, repo) = multiarch_store();
    let result = common::select(&store, &repo, "latest", &request("linux", "arm", None)).unwrap();
    assert_eq!(
        result.chosen.platform.as_ref().unwrap().variant.as_deref(),
        Some("v7")
    );
}

#[test]
fn missing_platform_is_a_clear_no_match_error() {
    let (store, repo) = multiarch_store();
    let err =
        common::select(&store, &repo, "latest", &request("linux", "mips64", None)).unwrap_err();
    match err {
        ApiError::NoMatch { message, tried } => {
            assert!(message.contains("mips64"), "message: {message}");
            assert!(tried.iter().any(|p| p["architecture"] == json!("riscv64")));
            assert!(tried.iter().any(|p| p["architecture"] == json!("arm")));
        }
        other => panic!("expected NoMatch, got {other:?}"),
    }
}

#[test]
fn requested_variant_that_does_not_exist_is_no_match() {
    let (store, repo) = multiarch_store();
    let err = common::select(
        &store,
        &repo,
        "latest",
        &request("linux", "arm", Some("v4")),
    )
    .unwrap_err();
    assert!(matches!(err, ApiError::NoMatch { .. }));
}

#[test]
fn two_candidates_with_identical_conditions_are_ambiguous() {
    let mut store = manifest_selector::store::Store::new();
    let repo = "demo/amb";
    let leaf_a = manifest_leaf("a");
    let leaf_b = manifest_leaf("b");
    let plat = platform("linux", "arm64", Some("v8"));
    let root = index_of(vec![
        descriptor(&leaf_a, plat.clone()),
        descriptor(&leaf_b, plat),
    ]);
    let root_digest = insert(&mut store, repo, root);
    let da = insert(&mut store, repo, leaf_a);
    let db = insert(&mut store, repo, leaf_b);
    tag(&mut store, repo, "latest", &root_digest);

    let err = common::select(
        &store,
        repo,
        "latest",
        &request("linux", "arm64", Some("v8")),
    )
    .unwrap_err();
    match err {
        ApiError::Ambiguous {
            candidates,
            message,
        } => {
            let digests: Vec<&str> = candidates
                .iter()
                .map(|c| c["digest"].as_str().unwrap())
                .collect();
            assert!(digests.contains(&da.as_str()));
            assert!(digests.contains(&db.as_str()));
            assert!(message.contains("refusing to pick by file order"));
        }
        other => panic!("expected Ambiguous, got {other:?}"),
    }
}

#[test]
fn ambiguity_is_independent_of_descriptor_order() {
    // Same content as the ambiguity test, but with B first: the error must
    // still report both and must never silently return B.
    let mut store = manifest_selector::store::Store::new();
    let repo = "demo/amb2";
    let leaf_a = manifest_leaf("a");
    let leaf_b = manifest_leaf("b");
    let plat = platform("linux", "arm64", Some("v8"));
    let root = index_of(vec![
        descriptor(&leaf_b, plat.clone()),
        descriptor(&leaf_a, plat),
    ]);
    let root_digest = insert(&mut store, repo, root);
    insert(&mut store, repo, leaf_a);
    insert(&mut store, repo, leaf_b);
    tag(&mut store, repo, "latest", &root_digest);

    let err = common::select(
        &store,
        repo,
        "latest",
        &request("linux", "arm64", Some("v8")),
    )
    .unwrap_err();
    assert!(matches!(err, ApiError::Ambiguous { .. }));
}

#[test]
fn walks_nested_indexes_and_reports_full_path() {
    let mut store = manifest_selector::store::Store::new();
    let repo = "demo/nested";
    let leaf = manifest_leaf("leaf");
    let inner = index_of(vec![descriptor(
        &leaf,
        platform("linux", "arm", Some("v7")),
    )]);
    let inner_digest = insert(&mut store, repo, inner.clone());
    let leaf_digest = insert(&mut store, repo, leaf);
    let root = index_of(vec![index_descriptor(
        &inner_digest,
        platform("linux", "arm", None),
        canonical(&inner).len(),
    )]);
    let root_digest = insert(&mut store, repo, root);
    tag(&mut store, repo, "latest", &root_digest);

    let result =
        common::select(&store, repo, "latest", &request("linux", "arm", Some("v7"))).unwrap();
    let path: Vec<&str> = result.chosen.path.iter().map(|n| n.kind).collect();
    assert_eq!(path, vec!["index", "index", "manifest"]);
    assert_eq!(result.chosen.digest, leaf_digest);
}

#[test]
fn missing_blob_is_reported_not_downloaded() {
    let mut store = manifest_selector::store::Store::new();
    let repo = "demo/missing";
    let ghost = "sha256:".to_string() + &"ab".repeat(32);
    let root = index_of(vec![json!({
        "mediaType": OCI_MANIFEST,
        "digest": ghost,
        "size": 10,
        "platform": { "os": "linux", "architecture": "amd64" },
    })]);
    let root_digest = insert(&mut store, repo, root);
    tag(&mut store, repo, "latest", &root_digest);

    let err = common::select(&store, repo, "latest", &request("linux", "amd64", None)).unwrap_err();
    match err {
        ApiError::MissingBlob { digest, at } => {
            assert_eq!(digest, ghost);
            assert!(at.contains("manifests[0]"));
        }
        other => panic!("expected MissingBlob, got {other:?}"),
    }
}

#[test]
fn digest_mismatch_is_rejected_with_actual_digest() {
    let mut store = manifest_selector::store::Store::new();
    let repo = "demo/tampered";
    let leaf = manifest_leaf("leaf");
    let claimed = "sha256:".to_string() + &"cd".repeat(32);
    insert_claimed(&mut store, repo, &leaf, &claimed);
    tag(&mut store, repo, "latest", &claimed);

    let err = common::select(&store, repo, "latest", &request("linux", "amd64", None)).unwrap_err();
    match err {
        ApiError::DigestMismatch {
            claimed: c, actual, ..
        } => {
            assert_eq!(c, claimed);
            assert_ne!(actual, claimed);
            assert_eq!(actual, Digest::sha256(&canonical(&leaf)).to_string());
        }
        other => panic!("expected DigestMismatch, got {other:?}"),
    }
}

#[test]
fn cycle_guard_rejects_self_referential_structure() {
    // A digest-consistent cycle is not constructible without a hash
    // collision, so disable verification for this one unit test and plant a
    // self loop: index L whose only child descriptor names L itself.
    let mut store = manifest_selector::store::Store::new();
    let repo = "demo/cycle";
    let claimed = "sha256:".to_string() + &"ef".repeat(32);
    let looping = index_of(vec![json!({
        "mediaType": OCI_INDEX,
        "digest": claimed,
        "size": 10,
        "platform": { "os": "linux", "architecture": "arm64" },
    })]);
    insert_claimed(&mut store, repo, &looping, &claimed);
    tag(&mut store, repo, "latest", &claimed);

    let reference = Ref::parse(&format!("{repo}:latest")).unwrap();
    let no_verify = SelectOptions {
        verify_digests: false,
    };
    let err = select_with(
        &store,
        &reference,
        &request("linux", "arm64", None),
        &no_verify,
    )
    .unwrap_err();
    match err {
        ApiError::Cycle { path } => {
            assert_eq!(path.len(), 2);
            assert_eq!(path[0], claimed);
            assert_eq!(path[1], claimed);
        }
        other => panic!("expected Cycle, got {other:?}"),
    }
}

#[test]
fn direct_manifest_reference_skips_index_logic() {
    let mut store = manifest_selector::store::Store::new();
    let repo = "demo/direct";
    let leaf = manifest_leaf("direct");
    let d = insert(&mut store, repo, leaf);
    tag(&mut store, repo, "v1", &d);

    let result = common::select(&store, repo, "v1", &request("linux", "amd64", None)).unwrap();
    assert_eq!(result.match_type, "direct");
    assert_eq!(result.chosen.digest, d);
}

#[test]
fn digest_pinned_reference_that_disagrees_with_tag_is_bad_request() {
    let (store, repo) = multiarch_store();
    let wrong = "sha256:".to_string() + &"01".repeat(32);
    let reference = Ref::parse(&format!("{repo}:latest@{wrong}")).unwrap();
    let err =
        manifest_selector::select::select(&store, &reference, &request("linux", "amd64", None))
            .unwrap_err();
    assert!(matches!(err, ApiError::BadRequest(_)));
}

#[test]
fn os_features_must_all_be_present() {
    let mut store = manifest_selector::store::Store::new();
    let repo = "demo/feat";
    let plain = manifest_leaf("plain");
    let with_smp = manifest_leaf("smp");
    let root = index_of(vec![
        descriptor(&plain, {
            let mut p = platform("linux", "amd64", None);
            p["osFeatures"] = Value::Array(vec![]);
            p
        }),
        descriptor(&with_smp, {
            let mut p = platform("linux", "amd64", None);
            p["osFeatures"] = json!(["smp"]);
            p
        }),
    ]);
    let root_digest = insert(&mut store, repo, root);
    insert(&mut store, repo, plain);
    let smp_digest = insert(&mut store, repo, with_smp);
    tag(&mut store, repo, "latest", &root_digest);

    let req = manifest_selector::select::PlatformRequest {
        os: Some("linux".into()),
        architecture: Some("amd64".into()),
        variant: None,
        os_version: None,
        os_features: vec!["smp".into()],
    };
    let reference = Ref::parse(&format!("{repo}:latest")).unwrap();
    let result = manifest_selector::select::select(&store, &reference, &req).unwrap();
    assert_eq!(result.chosen.digest, smp_digest);
}
