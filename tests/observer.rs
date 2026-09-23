//! End-to-end tests over the public observer API.
//!
//! Every byte stream here is synthesized (no network, no real TLS stack):
//! we build record/handshake/ClientHello frames by hand so that malformed,
//! GREASE, duplicated and truncated cases can be injected deterministically.

use tls_record_observer::{is_grease_u16, Limits, Observer, ParseError, Truncated, Warning};

// ---------------------------------------------------------------------------
// Frame builders
// ---------------------------------------------------------------------------

const CT_CCS: u8 = 20;
const CT_ALERT: u8 = 21;
const CT_HANDSHAKE: u8 = 22;
const CT_APP_DATA: u8 = 23;
const HS_CLIENT_HELLO: u8 = 1;
const HS_SERVER_HELLO: u8 = 2;

/// Wrap payload in one TLS record (record-layer version 0x0301 by default,
/// which any TLS 1.0–1.3 ClientHello uses).
fn record(content_type: u8, payload: &[u8]) -> Vec<u8> {
    record_ver(content_type, (3, 1), payload)
}

fn record_ver(content_type: u8, ver: (u8, u8), payload: &[u8]) -> Vec<u8> {
    assert!(payload.len() <= u16::MAX as usize);
    let mut v = Vec::with_capacity(5 + payload.len());
    v.push(content_type);
    v.push(ver.0);
    v.push(ver.1);
    v.extend_from_slice(&(payload.len() as u16).to_be_bytes());
    v.extend_from_slice(payload);
    v
}

/// Wrap a body in a handshake message (type + u24 length).
fn hs_message(msg_type: u8, body: &[u8]) -> Vec<u8> {
    assert!(body.len() <= 0xff_ffff);
    let mut v = Vec::with_capacity(4 + body.len());
    v.push(msg_type);
    let len = (body.len() as u32).to_be_bytes();
    v.extend_from_slice(&len[1..]); // low 3 bytes = u24
    v.extend_from_slice(body);
    v
}

fn u16len(body: &[u8]) -> Vec<u8> {
    let mut v = Vec::with_capacity(2 + body.len());
    v.extend_from_slice(&(body.len() as u16).to_be_bytes());
    v.extend_from_slice(body);
    v
}

fn sni_ext(host: &str) -> Vec<u8> {
    let host = host.as_bytes();
    let mut entry = Vec::new();
    entry.push(0); // host_name
    entry.extend_from_slice(&(host.len() as u16).to_be_bytes());
    entry.extend_from_slice(host);
    let mut data = u16len(&entry); // server_name_list
    let mut ext = Vec::new();
    ext.extend_from_slice(&0x0000u16.to_be_bytes());
    ext.extend_from_slice(&(data.len() as u16).to_be_bytes());
    ext.append(&mut data);
    ext
}

fn alpn_ext(names: &[&str]) -> Vec<u8> {
    let mut list = Vec::new();
    for n in names {
        list.push(n.len() as u8);
        list.extend_from_slice(n.as_bytes());
    }
    let mut data = u16len(&list);
    let mut ext = Vec::new();
    ext.extend_from_slice(&0x0010u16.to_be_bytes());
    ext.extend_from_slice(&(data.len() as u16).to_be_bytes());
    ext.append(&mut data);
    ext
}

fn unknown_ext(type_: u16, data: &[u8]) -> Vec<u8> {
    let mut ext = Vec::new();
    ext.extend_from_slice(&type_.to_be_bytes());
    ext.extend_from_slice(&(data.len() as u16).to_be_bytes());
    ext.extend_from_slice(data);
    ext
}

/// A well-formed TLS 1.2 ClientHello body with SNI + ALPN.
fn client_hello_body(exts: &[Vec<u8>], cipher_suites: &[u16]) -> Vec<u8> {
    let mut body = Vec::new();
    body.extend_from_slice(&0x0303u16.to_be_bytes()); // client_version TLS 1.2
    body.extend_from_slice(&[0x42u8; 32]); // random
    body.push(0); // session_id length 0

    let mut cs = Vec::new();
    for c in cipher_suites {
        cs.extend_from_slice(&c.to_be_bytes());
    }
    body.extend_from_slice(&(cs.len() as u16).to_be_bytes());
    body.extend_from_slice(&cs);

    body.push(1); // compression_methods length 1
    body.push(0); // null compression

    let mut ext_block = Vec::new();
    for e in exts {
        ext_block.extend_from_slice(e);
    }
    body.extend_from_slice(&(ext_block.len() as u16).to_be_bytes());
    body.extend_from_slice(&ext_block);
    body
}

fn valid_client_hello_record() -> Vec<u8> {
    let exts = vec![sni_ext("example.com"), alpn_ext(&["h2", "http/1.1"])];
    let body = client_hello_body(&exts, &[0x1301, 0x1302, 0xc02f]);
    record(CT_HANDSHAKE, &hs_message(HS_CLIENT_HELLO, &body))
}

/// Feed `bytes` one byte at a time: chunking must never change the result.
fn push_bytewise(bytes: &[u8]) -> Result<(), ParseError> {
    let mut o = Observer::new();
    for b in bytes {
        o.push(std::slice::from_ref(b))?;
    }
    o.finish().map(|_| ()).map_err(|(e, _)| e)
}

// ---------------------------------------------------------------------------
// Happy path
// ---------------------------------------------------------------------------

#[test]
fn parses_plaintext_client_hello_sni_alpn() {
    let bytes = valid_client_hello_record();
    let mut o = Observer::new();
    o.push(&bytes).unwrap();
    let obs = o.finish().unwrap();

    assert_eq!(obs.records.len(), 1);
    assert_eq!(obs.handshake_records, 1);
    assert!(obs.encrypted);
    let ch = obs.client_hello.expect("client hello parsed");
    assert_eq!(ch.client_version, (3, 3));
    assert_eq!(ch.random_hex.len(), 64);
    assert_eq!(ch.sni.as_deref(), Some("example.com"));
    assert_eq!(ch.alpn, vec!["h2".to_string(), "http/1.1".to_string()]);
    assert_eq!(ch.cipher_suites, vec![0x1301, 0x1302, 0xc02f]);
    assert!(ch.unknown_extensions.is_empty());
    assert!(obs.warnings.is_empty());
}

#[test]
fn byte_by_byte_feeds_are_equivalent() {
    let bytes = valid_client_hello_record();
    push_bytewise(&bytes).expect("bytewise parse must succeed");
}

// ---------------------------------------------------------------------------
// Cross-record reassembly
// -------------------------------------------------------------------------

#[test]
fn reassembles_handshake_split_across_two_records() {
    let exts = vec![sni_ext("split.test"), alpn_ext(&["h2"])];
    let body = client_hello_body(&exts, &[0x1301]);
    let msg = hs_message(HS_CLIENT_HELLO, &body);

    // Split the *handshake bytes* at an arbitrary offset (after 1 byte) and
    // place the two pieces in two correctly-framed handshake records. The
    // observer must join them at the handshake layer.
    let split = 1;
    let mut bytes = record(CT_HANDSHAKE, &msg[..split]);
    bytes.extend_from_slice(&record(CT_HANDSHAKE, &msg[split..]));

    let mut o = Observer::new();
    o.push(&bytes[..5 + split]).unwrap(); // only the first record so far
                                          // mid-stream: handshake header incomplete, nothing parsed yet
    assert!(o.observation().client_hello.is_none());
    o.push(&bytes[5 + split..]).unwrap();
    let obs = o.finish().unwrap();
    assert_eq!(obs.records.len(), 2);
    assert_eq!(obs.client_hello.unwrap().sni.as_deref(), Some("split.test"));
}

#[test]
fn reassembles_handshake_split_inside_clienthello_body() {
    let exts = vec![sni_ext("deep.example.org"), alpn_ext(&["h3", "h2"])];
    let body = client_hello_body(&exts, &[0x1303]);
    let msg = hs_message(HS_CLIENT_HELLO, &body);

    // Three fragments, split inside body content, each in its own record.
    let a = 4 + 5; // just past handshake header into body
    let b = 4 + body.len() - 7; // end 7 bytes early
    let mut bytes = record(CT_HANDSHAKE, &msg[..a]);
    bytes.extend_from_slice(&record(CT_HANDSHAKE, &msg[a..b]));
    bytes.extend_from_slice(&record(CT_HANDSHAKE, &msg[b..]));

    let mut o = Observer::new();
    for chunk in bytes.chunks(13) {
        o.push(chunk).unwrap();
    }
    let obs = o.finish().unwrap();
    assert_eq!(obs.records.len(), 3);
    let ch = obs.client_hello.unwrap();
    assert_eq!(ch.sni.as_deref(), Some("deep.example.org"));
    assert_eq!(ch.alpn.len(), 2);
}

#[test]
fn multiple_handshake_messages_in_one_record() {
    let body = client_hello_body(&[sni_ext("a.test")], &[0x1301]);
    let ch = hs_message(HS_CLIENT_HELLO, &body);
    // Bytes after ClientHello inside the same record must never be parsed
    // (the client direction contains nothing else plaintext).
    let trailing = hs_message(HS_SERVER_HELLO, &[0xff; 6]);
    let mut hs = ch.clone();
    hs.extend_from_slice(&trailing);

    let mut bytes = record(CT_HANDSHAKE, &hs);
    // Add a following encrypted record too.
    bytes.extend_from_slice(&record(CT_APP_DATA, &[0x16; 30]));

    let mut o = Observer::new();
    o.push(&bytes).unwrap();
    let obs = o.finish().unwrap();
    // The trailing ServerHello is dropped unparsed because plaintext
    // dissection closes as soon as the ClientHello is extracted.
    assert!(obs.client_hello.is_some());
    assert_eq!(obs.application_data_records, 1);
    assert!(obs
        .warnings
        .iter()
        .all(|w| !matches!(w, Warning::SkippedHandshake { .. })));
}

// ---------------------------------------------------------------------------
// GREASE
// ---------------------------------------------------------------------------

#[test]
fn grease_values_are_classified_not_unknown() {
    assert!(is_grease_u16(0x0a0a));
    assert!(is_grease_u16(0xfafa));
    assert!(!is_grease_u16(0x0010));
    assert!(!is_grease_u16(0x0a0b));

    let exts = vec![
        unknown_ext(0x2a2a, &[1, 2, 3]), // GREASE ext
        sni_ext("grease.test"),
        unknown_ext(0x00ab, &[9, 9]), // genuine unknown ext
        alpn_ext(&["h2"]),
    ];
    let body = client_hello_body(&exts, &[0x0a0a, 0x1301]); // GREASE cipher
    let bytes = record(CT_HANDSHAKE, &hs_message(HS_CLIENT_HELLO, &body));

    let (res, obs) = tls_record_observer::observe(&bytes);
    res.unwrap();
    let ch = obs.client_hello.unwrap();
    assert_eq!(ch.grease_cipher_suites, vec![0x0a0a]);
    assert!(ch.cipher_suites.iter().all(|c| !is_grease_u16(*c)));
    let unknown_ids: Vec<u16> = ch.unknown_extensions.iter().map(|u| u.type_).collect();
    assert_eq!(unknown_ids, vec![0x00ab]);
    assert!(obs
        .warnings
        .iter()
        .any(|w| matches!(w, Warning::GreaseExtension { type_: 0x2a2a })));
}

// ---------------------------------------------------------------------------
// Duplicate extensions
// ---------------------------------------------------------------------------

#[test]
fn duplicate_extensions_are_warned_first_wins() {
    // Two identical extension_type blocks (forbidden but must not crash or
    // be silently merged). First occurrence wins.
    let exts = vec![
        sni_ext("first.example"),
        sni_ext("second.example"),
        alpn_ext(&["h2"]),
        alpn_ext(&["http/1.1"]),
    ];
    let body = client_hello_body(&exts, &[0x1301]);
    let bytes = record(CT_HANDSHAKE, &hs_message(HS_CLIENT_HELLO, &body));

    let (res, obs) = tls_record_observer::observe(&bytes);
    res.unwrap();
    let ch = obs.client_hello.unwrap();
    assert_eq!(ch.sni.as_deref(), Some("first.example"));
    assert_eq!(ch.alpn, vec!["h2".to_string()]);
    let dups: Vec<u16> = obs
        .warnings
        .iter()
        .filter_map(|w| match w {
            Warning::DuplicateExtension { type_ } => Some(*type_),
            _ => None,
        })
        .collect();
    assert!(dups.contains(&0x0000));
    assert!(dups.contains(&0x0010));
}

// ---------------------------------------------------------------------------
// Nested length mismatches
// ---------------------------------------------------------------------------

#[test]
fn extensions_length_underrun_is_nested_mismatch() {
    // Declare an extension block longer than the bytes actually present.
    let mut body = Vec::new();
    body.extend_from_slice(&0x0303u16.to_be_bytes());
    body.extend_from_slice(&[0x11u8; 32]);
    body.push(0); // session_id
    body.extend_from_slice(&2u16.to_be_bytes());
    body.extend_from_slice(&0x1301u16.to_be_bytes());
    body.push(1);
    body.push(0);
    body.extend_from_slice(&100u16.to_be_bytes()); // extensions len = 100
    body.extend_from_slice(&[0u8; 4]); // but only 4 bytes follow

    let bytes = record(CT_HANDSHAKE, &hs_message(HS_CLIENT_HELLO, &body));
    let (res, _obs) = tls_record_observer::observe(&bytes);
    match res {
        Err(ParseError::LengthMismatch {
            what,
            declared,
            actual,
            ..
        }) => {
            assert_eq!(what, "client_hello.extensions");
            assert_eq!(declared, 100);
            assert_eq!(actual, 4);
        }
        other => panic!("expected nested LengthMismatch, got {other:?}"),
    }
}

#[test]
fn extension_data_overrun_is_nested_mismatch() {
    // One extension claims 50 data bytes inside a much shorter block.
    let mut bad = Vec::new();
    bad.extend_from_slice(&0x00abu16.to_be_bytes());
    bad.extend_from_slice(&50u16.to_be_bytes());
    bad.extend_from_slice(&[7u8; 3]);
    let body = client_hello_body(&[bad], &[0x1301]);
    let bytes = record(CT_HANDSHAKE, &hs_message(HS_CLIENT_HELLO, &body));

    let (res, _obs) = tls_record_observer::observe(&bytes);
    assert!(matches!(
        res,
        Err(ParseError::LengthMismatch {
            what: "extension.extension_data",
            ..
        })
    ));
}

#[test]
fn sni_inner_list_length_mismatch_is_detected() {
    let mut data = Vec::new();
    data.extend_from_slice(&50u16.to_be_bytes()); // list len 50
    data.push(0);
    data.extend_from_slice(&1u16.to_be_bytes());
    data.push(b'x');
    let bad = unknown_ext(0x0000, &data); // builder accepts any data
    let body = client_hello_body(&[bad], &[0x1301]);
    let bytes = record(CT_HANDSHAKE, &hs_message(HS_CLIENT_HELLO, &body));

    let (res, _obs) = tls_record_observer::observe(&bytes);
    assert!(matches!(
        res,
        Err(ParseError::LengthMismatch {
            what: "server_name.server_name_list",
            ..
        })
    ));
}

// ---------------------------------------------------------------------------
// Truncation
// ---------------------------------------------------------------------------

#[test]
fn truncated_record_header_is_incomplete_then_error_at_finish() {
    let bytes = valid_client_hello_record();
    let mut o = Observer::new();
    o.push(&bytes[..3]).unwrap(); // mid header: no error yet
    assert_eq!(o.observation().records.len(), 0);
    let err = o.finish().unwrap_err().0;
    assert!(matches!(err, ParseError::Truncated { .. }));
}

#[test]
fn truncated_record_fragment_is_reassembly_truncation() {
    let bytes = valid_client_hello_record();
    // Full 5-byte header says e.g. 100+ bytes but the stream ends early.
    let cut = 5 + 10;
    let (res, _obs) = tls_record_observer::observe(&bytes[..cut]);
    assert!(matches!(res, Err(ParseError::Truncated { .. })));
}

#[test]
fn truncated_handshake_message_across_records_errors_at_finish() {
    let body = client_hello_body(&[sni_ext("x.test")], &[0x1301]);
    let msg = hs_message(HS_CLIENT_HELLO, &body);
    let half = msg.len() / 2;
    let bytes = record(CT_HANDSHAKE, &msg[..half]); // second half never arrives

    let (res, obs) = tls_record_observer::observe(&bytes);
    assert!(matches!(
        res,
        Err(ParseError::Truncated {
            where_: Truncated::Reassembly,
            ..
        })
    ));
    // Still reports the record seen.
    assert_eq!(obs.records.len(), 1);
}

// ---------------------------------------------------------------------------
// Ciphertext must never be parsed as a handshake
// ---------------------------------------------------------------------------

#[test]
fn application_data_is_counted_not_dissected() {
    // Random high-entropy-looking bytes that *contain* 0x16 would break a
    // naive "search for handshake bytes" parser. They must be ignored.
    let mut bogus = Vec::new();
    for i in 0..200u32 {
        bogus.push((i.wrapping_mul(2654435761) & 0xff) as u8);
    }
    bogus[10] = 22; // pretend-handshake content type byte
    bogus[11] = 3;
    bogus[12] = 3;

    let mut bytes = valid_client_hello_record();
    bytes.extend_from_slice(&record(CT_APP_DATA, &bogus));
    bytes.extend_from_slice(&record(CT_APP_DATA, &[0; 40]));

    let (res, obs) = tls_record_observer::observe(&bytes);
    res.unwrap();
    assert_eq!(obs.application_data_records, 2);
    assert!(obs.encrypted);
    assert!(obs.client_hello.is_some());
}

#[test]
fn ciphertext_without_clienthello_is_not_guessed_as_one() {
    // Stream starts directly with ApplicationData (e.g. resumed session,
    // or garbage). No ClientHello may be invented.
    let bogus = vec![22u8, 3, 3, 1, 0, 1, 0xff]; // looks a bit like hs bytes
    let bytes = record(CT_APP_DATA, &bogus);
    let (res, obs) = tls_record_observer::observe(&bytes);
    res.unwrap();
    assert!(obs.client_hello.is_none());
    assert_eq!(obs.application_data_records, 1);
}

#[test]
fn change_cipher_spec_closes_plaintext_parsing() {
    // A handshake record fragment after CCS must not be parsed.
    let evil = hs_message(
        HS_CLIENT_HELLO,
        &client_hello_body(&[sni_ext("after-ccs.test")], &[0x1301]),
    );
    let mut bytes = record(CT_CCS, &[1]);
    bytes.extend_from_slice(&record(CT_HANDSHAKE, &evil));
    let (res, obs) = tls_record_observer::observe(&bytes);
    res.unwrap();
    assert!(obs.client_hello.is_none());
    assert_eq!(obs.change_cipher_spec_records, 1);
    assert!(obs.encrypted);
}

#[test]
fn random_garbage_is_not_parsed_as_handshake() {
    // Bytes starting with 0x16 but whose lengths are nonsense.
    for garbage in [
        vec![],
        vec![0xff],
        vec![22, 3, 3, 0, 50, 1, 2, 3], // handshake record, fragment truncated
        vec![99, 3, 3, 0, 0],           // bad content type
    ] {
        let mut o = Observer::new();
        let _ = o.push(&garbage);
        let _ = o.finish(); // must never panic or invent data
    }
}

// ---------------------------------------------------------------------------
// Subset / cap rejections
// ---------------------------------------------------------------------------

#[test]
fn unknown_content_type_rejected_explicitly() {
    let bytes = record(99, &[0; 3]);
    let (res, _obs) = tls_record_observer::observe(&bytes);
    assert!(matches!(res, Err(ParseError::BadContentType(99))));
}

#[test]
fn invalid_record_version_rejected() {
    let bytes = record_ver(CT_HANDSHAKE, (2, 0), &[0; 2]);
    let (res, _obs) = tls_record_observer::observe(&bytes);
    assert!(matches!(res, Err(ParseError::InvalidRecordVersion { .. })));
}

#[test]
fn record_fragment_over_cap_rejected() {
    let limits = Limits {
        max_record_fragment: 64,
        max_handshake_message: 1 << 20,
    };
    let mut o = Observer::with_limits(limits);
    let bytes = record(CT_HANDSHAKE, &[0u8; 65]);
    let err = o.push(&bytes).unwrap_err();
    assert!(matches!(err, ParseError::RecordTooLarge { length: 65, .. }));
}

#[test]
fn handshake_length_prefix_over_cap_rejected() {
    let limits = Limits {
        max_record_fragment: 16640,
        max_handshake_message: 100,
    };
    // Handshake header claims 1000 bytes.
    let mut hs = vec![HS_CLIENT_HELLO];
    hs.extend_from_slice(&1000u32.to_be_bytes()[1..]);
    hs.extend_from_slice(&[0u8; 10]);
    let mut o = Observer::with_limits(limits);
    let err = o.push(&record(CT_HANDSHAKE, &hs)).unwrap_err();
    assert!(matches!(
        err,
        ParseError::HandshakeTooLarge { length: 1000, .. }
    ));
}

#[test]
fn invalid_client_hello_version_rejected() {
    let mut body = Vec::new();
    body.extend_from_slice(&0x0400u16.to_be_bytes()); // TLS 1.3 draft-ish garbage
    body.extend_from_slice(&[0u8; 32]);
    body.push(0);
    body.extend_from_slice(&2u16.to_be_bytes());
    body.extend_from_slice(&0x1301u16.to_be_bytes());
    body.push(1);
    body.push(0);
    body.extend_from_slice(&0u16.to_be_bytes()); // no extensions
    let bytes = record(CT_HANDSHAKE, &hs_message(HS_CLIENT_HELLO, &body));
    let (res, _obs) = tls_record_observer::observe(&bytes);
    assert!(matches!(
        res,
        Err(ParseError::InvalidClientHelloVersion { .. })
    ));
}

#[test]
fn odd_cipher_suite_length_is_malformed_vector() {
    let mut body = Vec::new();
    body.extend_from_slice(&0x0303u16.to_be_bytes());
    body.extend_from_slice(&[0u8; 32]);
    body.push(0);
    body.extend_from_slice(&3u16.to_be_bytes()); // odd length 3
    body.extend_from_slice(&[1, 2, 3]);
    body.push(1);
    body.push(0);
    body.extend_from_slice(&0u16.to_be_bytes());
    let bytes = record(CT_HANDSHAKE, &hs_message(HS_CLIENT_HELLO, &body));
    let (res, _obs) = tls_record_observer::observe(&bytes);
    assert!(matches!(res, Err(ParseError::MalformedVector { .. })));
}

// ---------------------------------------------------------------------------
// Skipped handshake types & alerts
// ---------------------------------------------------------------------------

#[test]
fn alert_records_are_counted() {
    // In a client stream an early plaintext alert is legal to observe.
    let mut bytes = record(CT_ALERT, &[1, 0]);
    bytes.extend_from_slice(&valid_client_hello_record());
    let (res, obs) = tls_record_observer::observe(&bytes);
    res.unwrap();
    assert_eq!(obs.alert_records, 1);
    assert!(obs.client_hello.is_some());
}

#[test]
fn non_clienthello_handshake_is_skipped_with_warning() {
    // ServerHello *before* ClientHello (synthetic; observer must not crash
    // and must keep looking).
    let mut sh_body = Vec::new();
    sh_body.extend_from_slice(&0x0303u16.to_be_bytes());
    sh_body.extend_from_slice(&[0u8; 32]);
    sh_body.push(0);
    sh_body.extend_from_slice(&2u16.to_be_bytes());
    sh_body.extend_from_slice(&0x1301u16.to_be_bytes());
    sh_body.push(1);
    sh_body.push(0);
    sh_body.extend_from_slice(&0u16.to_be_bytes());
    let sh = hs_message(HS_SERVER_HELLO, &sh_body);
    let ch_bytes = valid_client_hello_record();

    let hs_stream = sh;
    // Put both messages across two records (ServerHello fully framed first).
    let mut bytes = record(CT_HANDSHAKE, &hs_stream);
    bytes.extend_from_slice(&ch_bytes);
    let (res, obs) = tls_record_observer::observe(&bytes);
    res.unwrap();
    assert!(matches!(
        obs.warnings[0],
        Warning::SkippedHandshake {
            type_: HS_SERVER_HELLO
        }
    ));
    assert!(obs.client_hello.is_some());
}

// ---------------------------------------------------------------------------
// Unknown extensions are surfaced
// ---------------------------------------------------------------------------

#[test]
fn unknown_extensions_are_recorded_opaque() {
    let exts = vec![
        unknown_ext(0xff01, &[0xde, 0xad]),
        unknown_ext(0x002b, &[]), // supported_versions is not parsed here
    ];
    let body = client_hello_body(&exts, &[0x1301]);
    let bytes = record(CT_HANDSHAKE, &hs_message(HS_CLIENT_HELLO, &body));
    let (res, obs) = tls_record_observer::observe(&bytes);
    res.unwrap();
    let ch = obs.client_hello.unwrap();
    assert_eq!(ch.unknown_extensions.len(), 2);
    assert_eq!(ch.unknown_extensions[0].data_hex, "dead");
    assert_eq!(ch.unknown_extensions[1].data_len, 0);
    assert!(ch.sni.is_none());
    assert!(ch.alpn.is_empty());
}
