//! GREASE（RFC 8701）处理测试。

use tls_observer::record::CONTENT_HANDSHAKE;
use tls_observer::test_support::*;
use tls_observer::{Config, Observer};

const GREASES: [u16; 16] = [
    0x0A0A, 0x1A1A, 0x2A2A, 0x3A3A, 0x4A4A, 0x5A5A, 0x6A6A, 0x7A7A, 0x8A8A, 0x9A9A, 0xAAAA, 0xBABA,
    0xCACA, 0xDADA, 0xEAEA, 0xFAFA,
];

#[test]
fn grease_everywhere_is_filtered_not_unknown() {
    for g in GREASES {
        let msg = ClientHelloBuilder::new()
            .with_grease_extension(g)
            .with_sni("g.example")
            .with_alpn(&["h2"])
            .with_supported_versions(&[g, 0x0304])
            .build_message();
        // builder 默认 cipher_suites 首项是 0x2A2A（GREASE）。
        let data = record(CONTENT_HANDSHAKE, 0x0301, &msg);
        let mut obs = Observer::new(Config::default());
        obs.feed(&data).unwrap();
        let ch = obs
            .client_hello()
            .unwrap_or_else(|| panic!("grease {g:#06x}"));
        assert_eq!(ch.sni.as_deref(), Some("g.example"));
        assert!(ch.grease_extensions.contains(&g));
        assert!(ch.grease_versions.contains(&g));
        assert!(!ch.supported_versions.contains(&g));
        assert!(!ch.cipher_suites.contains(&0x2A2A));
        assert!(ch.grease_cipher_suites.contains(&0x2A2A));
        // 没有任何“未知扩展”的类型号等于 GREASE。
        assert!(ch.unknown_extensions.iter().all(|u| u.ext_type != g));
    }
}

#[test]
fn duplicate_grease_across_different_slots() {
    // 同一个 GREASE 值出现在“扩展”和“版本”两个不同槽位：合法。
    let g = 0xFAFA;
    let msg = ClientHelloBuilder::new()
        .with_grease_extension(g)
        .with_supported_versions(&[g, 0x0303])
        .with_sni("ok.example")
        .build_message();
    let mut obs = Observer::new(Config::default());
    obs.feed(&record(CONTENT_HANDSHAKE, 0x0301, &msg)).unwrap();
    assert!(obs.client_hello().is_some());
}

#[test]
fn two_different_grease_extensions_then_known() {
    let msg = ClientHelloBuilder::new()
        .with_grease_extension(0x0A0A)
        .with_grease_extension(0x3A3A)
        .with_sni("multi.example")
        .with_alpn(&["h2", "http/1.1"])
        .build_message();
    let mut obs = Observer::new(Config::default());
    obs.feed(&record(CONTENT_HANDSHAKE, 0x0301, &msg)).unwrap();
    let ch = obs.client_hello().expect("hello");
    assert_eq!(ch.grease_extensions, vec![0x0A0A, 0x3A3A]);
    assert_eq!(ch.sni.as_deref(), Some("multi.example"));
}

#[test]
fn near_miss_values_are_not_grease() {
    // 0x2A3A / 0x3A2A 不是 GREASE；用作扩展类型属于普通（未知）扩展。
    for lookalike in [0x2A3Au16, 0x3A2A, 0x0A0B, 0x2B2B] {
        let msg = ClientHelloBuilder::new()
            .with_unknown_extension(lookalike, &[0x01])
            .with_sni("x.example")
            .build_message();
        let mut obs = Observer::new(Config::default());
        obs.feed(&record(CONTENT_HANDSHAKE, 0x0301, &msg)).unwrap();
        let ch = obs.client_hello().expect("hello");
        assert!(
            ch.unknown_extensions
                .iter()
                .any(|u| u.ext_type == lookalike),
            "{lookalike:#06x} should be treated as unknown extension"
        );
    }
}
