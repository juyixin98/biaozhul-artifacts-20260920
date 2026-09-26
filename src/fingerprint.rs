//! FNV-1a 64-bit fingerprint of a schema's *wire contract*.
//!
//! The fingerprint is embedded in every BSE1 file so a reader can tell which
//! schema version produced the data without an external registry.
//!
//! Importantly the canonical form contains field **numbers, types and
//! rules** but NOT field names: renaming a field is a compatible change and
//! must not change the fingerprint. Adding/removing an optional field changes
//! the fingerprint (data really is from a different schema version) but not
//! its decodability.

use crate::schema::{Presence, Repetition, Schema, Type};

const FNV_OFFSET: u64 = 0xcbf2_9ce4_8422_b327;
const FNV_PRIME: u64 = 0x0000_0100_0000_01b3;

fn fnv1a64(bytes: &[u8]) -> u64 {
    let mut h = FNV_OFFSET;
    for &b in bytes {
        h ^= b as u64;
        h = h.wrapping_mul(FNV_PRIME);
    }
    h
}

/// Compute the wire-contract fingerprint of a schema.
pub fn fingerprint(schema: &Schema) -> u64 {
    // Build a stable canonical text. Messages are sorted by name; within a
    // message fields are sorted by number. The root marker is included.
    let mut names: Vec<&str> = schema.message_names().iter().map(String::as_str).collect();
    names.sort_unstable();

    let mut text = String::new();
    text.push_str("BSE1|root=");
    text.push_str(&schema.root);
    for name in names {
        let msg = schema.message(name).expect("name came from schema");
        text.push_str(";m=");
        text.push_str(name);
        let mut fields: Vec<_> = msg.fields.iter().collect();
        fields.sort_by_key(|f| f.number);
        for f in fields {
            text.push_str("|f=");
            text.push_str(&f.number.to_string());
            text.push(',');
            text.push_str(type_token(f.ty));
            if let Some(r) = &f.references {
                text.push_str("->");
                text.push_str(r);
            }
            text.push(',');
            text.push_str(match f.presence {
                Presence::Required => "req",
                Presence::Optional => "opt",
            });
            text.push(',');
            text.push_str(match f.repetition {
                Repetition::Single => "1",
                Repetition::Packed => "packed",
                Repetition::Unpacked => "unpacked",
            });
        }
    }
    fnv1a64(text.as_bytes())
}

fn type_token(t: Type) -> &'static str {
    t.as_str()
}

/// Hex rendering used in JSON output and file names.
pub fn to_hex(fp: u64) -> String {
    format!("{fp:016x}")
}
