//! Unit tests for digest parsing, verification and variant ordering.

use manifest_selector::digest::{verify, Digest};

#[test]
fn parses_and_rejects_digests() {
    let d =
        Digest::parse("sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef")
            .unwrap();
    assert_eq!(d.algorithm(), "sha256");

    assert!(Digest::parse("sha256:abc").is_err()); // too short
    assert!(Digest::parse("nocolon").is_err());
    assert!(Digest::parse("sha256:zz").is_err());
    assert!(Digest::parse(":abcd").is_err());
    assert!(Digest::parse("SHA256:abcd").is_err()); // algorithm lowercase only
}

#[test]
fn sha256_roundtrip_verifies() {
    let payload = br#"{"hello":"world"}"#;
    let d = Digest::sha256(payload);
    assert_eq!(
        d.as_str(),
        "sha256:93a23971a914e5eacbf0a8d25154cda309c3c1c72fbb9914d47c60f3cb681588"
    );
    verify(&d, payload).expect("fresh digest must verify");
    verify(&d, b"tampered").expect_err("different bytes must fail");
}

#[test]
fn unsupported_algorithm_is_rejected() {
    let d = Digest::parse("sha512:aa").unwrap();
    assert!(verify(&d, b"x").is_err());
}
