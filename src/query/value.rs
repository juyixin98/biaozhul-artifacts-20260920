//! Typed scalar domain shared by batches, tries and answers.
//!
//! Values are one of: 64-bit signed integer, UTF-8 string, boolean, IEEE-754
//! double or NULL. NULL participates in no equality: it never matches another
//! NULL (SQL-style "null is not equal to null" natural-join semantics). Trie
//! construction decides whether rows containing NULL are rejected or dropped
//! (see [`super::batch::NullPolicy`]); they are therefore never stored in a
//! trie and can never appear in an intersection.

use std::cmp::Ordering;
use std::hash::{Hash, Hasher};

use serde::de::Error as _;
use serde::{Deserialize, Deserializer, Serialize, Serializer};

/// Total-order wrapper around `f64` so that [`Scalar::Float`] can live in
/// ordered tries and hash maps. Uses bit ordering after canonicalising NaNs
/// and negative zero; good enough for a deterministic synthetic workload
/// (there is no real floating-point arithmetic in the join itself).
#[derive(Clone, Copy, Debug)]
pub struct OrderedF64(pub f64);

impl OrderedF64 {
    fn canonical_bits(self) -> u64 {
        if self.0.is_nan() {
            f64::NAN.to_bits()
        } else if self.0 == 0.0 {
            0 // collapse +0.0 / -0.0
        } else {
            self.0.to_bits()
        }
    }
}

impl PartialEq for OrderedF64 {
    fn eq(&self, other: &Self) -> bool {
        self.canonical_bits() == other.canonical_bits()
    }
}
impl Eq for OrderedF64 {}

impl PartialOrd for OrderedF64 {
    fn partial_cmp(&self, other: &Self) -> Option<Ordering> {
        Some(self.cmp(other))
    }
}

impl Ord for OrderedF64 {
    fn cmp(&self, other: &Self) -> Ordering {
        // IEEE 754 bit ordering, sign bit flipped for negatives.
        fn sort_key(bits: u64) -> u64 {
            bits ^ if bits & (1 << 63) != 0 { u64::MAX } else { 1 << 63 }
        }
        sort_key(self.canonical_bits()).cmp(&sort_key(other.canonical_bits()))
    }
}

impl Hash for OrderedF64 {
    fn hash<H: Hasher>(&self, state: &mut H) {
        self.canonical_bits().hash(state);
    }
}

/// A typed scalar. [`Scalar::Null`] exists so batch ingestion can detect and
/// apply the configured NULL policy; it is never inserted into a trie.
#[derive(Clone, Debug)]
pub enum Scalar {
    Int(i64),
    Str(String),
    Bool(bool),
    Float(OrderedF64),
    Null,
}

impl Scalar {
    pub fn type_name(&self) -> &'static str {
        match self {
            Scalar::Int(_) => "int64",
            Scalar::Str(_) => "utf8",
            Scalar::Bool(_) => "boolean",
            Scalar::Float(_) => "float64",
            Scalar::Null => "null",
        }
    }

    pub fn is_null(&self) -> bool {
        matches!(self, Scalar::Null)
    }

    /// Redacted rendering for diagnostics. Integers are kept (they are the
    /// join keys and useful for debugging), strings are truncated and
    /// replaced by a hash marker so raw payload text never reaches a log.
    pub fn redacted(&self) -> String {
        match self {
            Scalar::Int(i) => i.to_string(),
            Scalar::Bool(b) => b.to_string(),
            Scalar::Float(f) => f.0.to_string(),
            Scalar::Str(s) => {
                let mut h = std::collections::hash_map::DefaultHasher::new();
                s.hash(&mut h);
                format!("str#{}", h.finish() % 1_000_000)
            }
            Scalar::Null => "null".to_string(),
        }
    }
}

impl PartialEq for Scalar {
    fn eq(&self, other: &Self) -> bool {
        match (self, other) {
            (Scalar::Int(a), Scalar::Int(b)) => a == b,
            (Scalar::Str(a), Scalar::Str(b)) => a == b,
            (Scalar::Bool(a), Scalar::Bool(b)) => a == b,
            (Scalar::Float(a), Scalar::Float(b)) => a == b,
            // Two NULLs are NOT equal: natural joins never match nulls.
            _ => false,
        }
    }
}
impl Eq for Scalar {}

impl Hash for Scalar {
    fn hash<H: Hasher>(&self, state: &mut H) {
        std::mem::discriminant(self).hash(state);
        match self {
            Scalar::Int(i) => i.hash(state),
            Scalar::Str(s) => s.hash(state),
            Scalar::Bool(b) => b.hash(state),
            Scalar::Float(f) => f.hash(state),
            Scalar::Null => 0u8.hash(state),
        }
    }
}

impl PartialOrd for Scalar {
    fn partial_cmp(&self, other: &Self) -> Option<Ordering> {
        Some(self.cmp(other))
    }
}

impl Ord for Scalar {
    fn cmp(&self, other: &Self) -> Ordering {
        // Type tag ordering first, then value. NULL sorts last (and is never
        // stored, but batch validation wants a total order).
        fn tag(v: &Scalar) -> u8 {
            match v {
                Scalar::Bool(_) => 0,
                Scalar::Int(_) => 1,
                Scalar::Float(_) => 2,
                Scalar::Str(_) => 3,
                Scalar::Null => 4,
            }
        }
        match (self, other) {
            (Scalar::Bool(a), Scalar::Bool(b)) => a.cmp(b),
            (Scalar::Int(a), Scalar::Int(b)) => a.cmp(b),
            (Scalar::Float(a), Scalar::Float(b)) => a.cmp(b),
            (Scalar::Str(a), Scalar::Str(b)) => a.cmp(b),
            (Scalar::Null, Scalar::Null) => Ordering::Equal,
            _ => tag(self).cmp(&tag(other)),
        }
    }
}

impl Serialize for Scalar {
    fn serialize<S: Serializer>(&self, ser: S) -> std::result::Result<S::Ok, S::Error> {
        match self {
            Scalar::Int(i) => ser.serialize_i64(*i),
            Scalar::Str(s) => ser.serialize_str(s),
            Scalar::Bool(b) => ser.serialize_bool(*b),
            Scalar::Float(f) => ser.serialize_f64(f.0),
            Scalar::Null => ser.serialize_none(),
        }
    }
}

impl<'de> Deserialize<'de> for Scalar {
    fn deserialize<D: Deserializer<'de>>(de: D) -> std::result::Result<Self, D::Error> {
        let v = serde_json::Value::deserialize(de)?;
        match v {
            serde_json::Value::Null => Ok(Scalar::Null),
            serde_json::Value::Bool(b) => Ok(Scalar::Bool(b)),
            serde_json::Value::Number(ref n) => {
                if let Some(i) = n.as_i64() {
                    Ok(Scalar::Int(i))
                } else if let Some(f) = n.as_f64() {
                    Ok(Scalar::Float(OrderedF64(f)))
                } else {
                    Err(D::Error::custom("numeric value exceeds supported domain"))
                }
            }
            serde_json::Value::String(s) => Ok(Scalar::Str(s)),
            other => Err(D::Error::custom(format!(
                "unsupported scalar JSON kind: {}",
                other
            ))),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn null_never_equals_null() {
        assert_ne!(Scalar::Null, Scalar::Null);
        assert!(Scalar::Null < Scalar::Int(0) == false);
        assert!(Scalar::Int(0) < Scalar::Null);
    }

    #[test]
    fn json_round_trip_keeps_int_vs_float() {
        let i: Scalar = serde_json::from_str("42").unwrap();
        let f: Scalar = serde_json::from_str("42.5").unwrap();
        assert!(matches!(i, Scalar::Int(42)));
        assert!(matches!(f, Scalar::Float(_)));
        assert_eq!(serde_json::to_string(&Scalar::Bool(true)).unwrap(), "true");
        assert_eq!(
            serde_json::from_str::<Scalar>("null").unwrap(),
            Scalar::Null
        );
    }

    #[test]
    fn ordered_floats_are_total() {
        assert!(OrderedF64(f64::NAN) == OrderedF64(f64::NAN));
        assert!(OrderedF64(-1.0) < OrderedF64(0.0));
        assert!(OrderedF64(0.0) == OrderedF64(-0.0));
    }
}
