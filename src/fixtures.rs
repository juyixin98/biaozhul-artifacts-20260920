//! Built-in demo data, generated at startup with *real* SHA-256 digests.
//!
//! Repositories provided:
//! - `demo/app:latest` — arm/v5,v6,v7,arm64,amd64,s390x + a nested-index
//!   subtree (exercises ARM variants + deep paths).
//! - `demo/ambiguous:1` — two leaves with identical conditions (must report
//!   AMBIGUOUS, never pick by file order).
//! - `demo/features:1` — leaves gated by CPU feature sets.
//! - `demo/direct:1` — tag pointing straight at an image manifest.
//! - `demo/corrupt:1` — one leaf's bytes were tampered with while keeping its
//!   real digest key (strict verification must fail with DIGEST_MISMATCH).
//! - `demo/cycle:latest` — two indexes mounted under synthetic digest keys,
//!   referring to each other (cycle detection; traversed with strict=false
//!   because their keys are not real content hashes).
//!
//! All normal blobs hash to their keys and pass full strict verification.
//! The corrupt demo stores wrong bytes under a *real* key (untrusted), the
//! cycle demo uses obviously synthetic `sha256:aaa…/bbb…` keys (trusted).

use crate::digest::Digest;
use crate::model::{Descriptor, ImageManifest, IndexManifest, MT_IMAGE, MT_INDEX};
use crate::store::Registry;

fn config_blob(name: &str) -> (String, Vec<u8>) {
    let bytes = serde_json::json!({
        "architecture": "fixture",
        "os": "linux",
        "comment": format!("config for {name}"),
    })
    .to_string()
    .into_bytes();
    let digest = Digest::of_bytes(&bytes).to_string();
    (digest, bytes)
}

fn image_blob(reg: &Registry, name: &str) -> (Descriptor, Vec<u8>) {
    let (cfg_digest, cfg_bytes) = config_blob(name);
    let cfg_desc = Descriptor {
        media_type: Some("application/vnd.oci.image.config.v1+json".to_string()),
        digest: cfg_digest.clone(),
        size: cfg_bytes.len() as i64,
        urls: vec![],
        platform: None,
        annotations: None,
    };
    // one tiny deterministic "layer"
    let layer_bytes = format!("layer-payload-for-{name}").into_bytes();
    let layer_digest = Digest::of_bytes(&layer_bytes).to_string();
    let layer_desc = Descriptor {
        media_type: Some("application/vnd.oci.image.layer.v1.tar+gzip".to_string()),
        digest: layer_digest.clone(),
        size: layer_bytes.len() as i64,
        urls: vec![],
        platform: None,
        annotations: None,
    };

    let manifest = ImageManifest {
        schema_version: 2,
        media_type: Some(MT_IMAGE.to_string()),
        config: cfg_desc,
        layers: vec![layer_desc],
    };
    let raw = serde_json::to_vec_pretty(&manifest).unwrap();

    reg.put_verified(&cfg_digest, cfg_bytes).unwrap();
    reg.put_verified(&layer_digest, layer_bytes).unwrap();
    let mdigest = reg.put_verified(&Digest::of_bytes(&raw).to_string(), raw.clone()).unwrap();

    (
        Descriptor {
            media_type: Some(MT_IMAGE.to_string()),
            digest: mdigest.to_string(),
            size: raw.len() as i64,
            urls: vec![],
            platform: None,
            annotations: None,
        },
        raw,
    )
}

fn platform(
    mut d: Descriptor,
    os: &str,
    arch: &str,
    variant: Option<&str>,
    features: &[&str],
) -> Descriptor {
    d.platform = Some(crate::model::Platform {
        architecture: arch.to_string(),
        os: os.to_string(),
        variant: variant.map(str::to_string),
        os_version: None,
        os_features: vec![],
        features: features.iter().map(|s| s.to_string()).collect(),
    });
    d
}

fn put_index(reg: &Registry, manifests: Vec<Descriptor>) -> (String, Descriptor) {
    let idx = IndexManifest {
        schema_version: 2,
        media_type: Some(MT_INDEX.to_string()),
        manifests,
    };
    let raw = serde_json::to_vec_pretty(&idx).unwrap();
    let digest = reg
        .put_verified(&Digest::of_bytes(&raw).to_string(), raw.clone())
        .unwrap()
        .to_string();
    let desc = Descriptor {
        media_type: Some(MT_INDEX.to_string()),
        digest: digest.clone(),
        size: raw.len() as i64,
        urls: vec![],
        platform: None,
        annotations: None,
    };
    (digest, desc)
}

/// Build every built-in repository. Returns a human-readable summary.
pub fn build_all(reg: &Registry) -> Vec<(String, String, String)> {
    let mut summary = Vec::new();

    // ---- demo/app: multi-arch incl. ARM ladder + nested index -----------
    let armv5 = image_blob(reg, "app-armv5").0;
    let armv6 = image_blob(reg, "app-armv6").0;
    let armv7 = image_blob(reg, "app-armv7").0;
    let arm64 = image_blob(reg, "app-arm64").0;
    let amd64 = image_blob(reg, "app-amd64").0;
    let s390x = image_blob(reg, "app-s390x").0;

    // nested sub-index: arm family grouped under a child index without its
    // own platform (tests recursion + path reporting).
    let arm_family = vec![
        platform(armv5, "linux", "arm", Some("v5"), &[]),
        platform(armv6, "linux", "arm", Some("v6"), &[]),
        platform(armv7, "linux", "arm", Some("v7"), &[]),
        platform(arm64, "linux", "arm64", Some("v8"), &[]),
    ];
    let (arm_sub_digest, arm_sub_desc) = put_index(reg, arm_family);

    let top = vec![
        arm_sub_desc, // nested index first on purpose — order must not win
        platform(amd64.clone(), "linux", "amd64", None, &[]),
        platform(s390x, "linux", "s390x", None, &[]),
    ];
    let (root_digest, _) = put_index(reg, top);
    reg.tag("demo/app", "latest", root_digest.clone());
    summary.push(("demo/app".into(), "latest".into(), root_digest));
    let _ = arm_sub_digest;

    // ---- demo/ambiguous: two amd64 leaves, same exact conditions --------
    let a = platform(image_blob(reg, "amb-a").0, "linux", "amd64", None, &[]);
    let b = platform(image_blob(reg, "amb-b").0, "linux", "amd64", None, &[]);
    let (dig, _) = put_index(reg, vec![a, b]);
    reg.tag("demo/ambiguous", "1", dig.clone());
    summary.push(("demo/ambiguous".into(), "1".into(), dig));

    // ---- demo/features: generic / sse4_2 / sse4_2+avx -------------------
    let plain = platform(image_blob(reg, "feat-plain").0, "linux", "amd64", None, &[]);
    let sse = platform(
        image_blob(reg, "feat-sse").0,
        "linux",
        "amd64",
        None,
        &["sse4_2"],
    );
    let avx = platform(
        image_blob(reg, "feat-avx").0,
        "linux",
        "amd64",
        None,
        &["sse4_2", "avx"],
    );
    let (dig, _) = put_index(reg, vec![plain, sse, avx]);
    reg.tag("demo/features", "1", dig.clone());
    summary.push(("demo/features".into(), "1".into(), dig));

    // ---- demo/direct: tag -> image manifest -----------------------------
    let (direct_desc, _) = image_blob(reg, "direct");
    reg.tag("demo/direct", "1", direct_desc.digest.clone());
    summary.push(("demo/direct".into(), "1".into(), direct_desc.digest));

    // ---- demo/corrupt: tampered leaf mounted under declared digest ------
    let (mut good_desc, _) = image_blob(reg, "corrupt-leaf");
    let claimed_digest = good_desc.digest.clone();
    let claimed_size = good_desc.size;
    // Replace stored bytes with tampered content under the SAME real
    // digest key. `trusted=false`: strict selection must catch it.
    reg.put_trusted(
        claimed_digest.clone(),
        b"TAMPERED-CONTENT-not-the-original-manifest".to_vec(),
        false,
    );
    good_desc.platform = Some(crate::model::Platform {
        architecture: "amd64".into(),
        os: "linux".into(),
        variant: None,
        os_version: None,
        os_features: vec![],
        features: vec![],
    });
    // Keep the descriptor claiming the original size too — either check
    // must be able to fail.
    let _ = claimed_size;
    let (dig, _) = put_index(reg, vec![good_desc]);
    reg.tag("demo/corrupt", "1", dig.clone());
    summary.push(("demo/corrupt".into(), "1".into(), dig));

    // ---- demo/cycle: A -> B -> A, both trusted-mounted ------------------
    let fake_a = "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa".to_string();
    let fake_b = "sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb".to_string();
    let index_b = serde_json::to_vec_pretty(&IndexManifest {
        schema_version: 2,
        media_type: Some(MT_INDEX.to_string()),
        manifests: vec![Descriptor {
            media_type: Some(MT_INDEX.to_string()),
            digest: fake_a.clone(),
            size: 128,
            urls: vec![],
            platform: None,
            annotations: None,
        }],
    })
    .unwrap();
    let index_a = serde_json::to_vec_pretty(&IndexManifest {
        schema_version: 2,
        media_type: Some(MT_INDEX.to_string()),
        manifests: vec![Descriptor {
            media_type: Some(MT_INDEX.to_string()),
            digest: fake_b.clone(),
            size: index_b.len() as i64,
            urls: vec![],
            platform: None,
            annotations: None,
        }],
    })
    .unwrap();
    reg.put_trusted(fake_b, index_b, true);
    reg.put_trusted(fake_a.clone(), index_a, true);
    reg.tag("demo/cycle", "latest", fake_a.clone());
    summary.push(("demo/cycle".into(), "latest".into(), fake_a));

    summary
}
