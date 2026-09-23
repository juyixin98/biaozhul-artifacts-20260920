//! Integer amounts and 256-bit intermediate arithmetic.
//!
//! Every token amount in the API and the database is an **integer string**
//! denominated in the asset's smallest unit (e.g. wei). JSON numbers are
//! rejected so that JavaScript clients with 53-bit mantissas cannot silently
//! truncate values.
//!
//! Intermediate swap math can require up to 384 bits (`amount * reserve`),
//! which is performed in [`U256`] (256 bits is ample for u128 operands).
//! A result that no longer fits in u128 is an explicit overflow error rather
//! than a silent wrap.
//!
//! The two `construct_uint!` expansions below trip clippy's
//! `manual_div_ceil`/`assign_op_pattern` lints inside generated code we do not
//! own; allow them module-wide.
#![allow(clippy::manual_div_ceil, clippy::assign_op_pattern)]

use std::fmt;
use std::str::FromStr;

use serde::{de, Deserialize, Deserializer, Serialize, Serializer};
use uint::construct_uint;

construct_uint! {
    /// 256-bit unsigned integer for exact swap intermediates.
    pub struct U256(4);
}
// Wider width used by the router's admissible-bound arithmetic (products of
// three u128 reserve ratios can reach ~426 bits).
construct_uint! {
    pub struct U1024(16);
}

/// A non-negative token amount in smallest units, always `u128` on the wire.
#[derive(Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash, Default)]
pub struct Amount(pub u128);

impl Amount {
    pub const ZERO: Amount = Amount(0);
    pub const MAX: Amount = Amount(u128::MAX);

    pub fn value(self) -> u128 {
        self.0
    }

    pub fn is_zero(self) -> bool {
        self.0 == 0
    }

    /// Convert a checked 256-bit value back into the bounded amount domain.
    pub fn from_u256(v: U256) -> Option<Amount> {
        if v > U256::from(u128::MAX) {
            None
        } else {
            Some(Amount(v.low_u128()))
        }
    }
}

impl From<u128> for Amount {
    fn from(v: u128) -> Self {
        Amount(v)
    }
}

impl From<u64> for Amount {
    fn from(v: u64) -> Self {
        Amount(u128::from(v))
    }
}

impl fmt::Debug for Amount {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "Amount({})", self.0)
    }
}

impl fmt::Display for Amount {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{}", self.0)
    }
}

/// Parse error for an amount string.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ParseAmountError {
    /// Empty string.
    Empty,
    /// Leading `+`/`-` or other non-digit content.
    Invalid,
    /// Digits exceed the u128 range.
    Overflow,
}

impl fmt::Display for ParseAmountError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            ParseAmountError::Empty => f.write_str("amount string is empty"),
            ParseAmountError::Invalid => f.write_str("amount string must contain only decimal digits, no sign"),
            ParseAmountError::Overflow => f.write_str("amount exceeds u128::MAX"),
        }
    }
}

impl std::error::Error for ParseAmountError {}

impl FromStr for Amount {
    type Err = ParseAmountError;

    fn from_str(s: &str) -> Result<Self, Self::Err> {
        if s.is_empty() {
            return Err(ParseAmountError::Empty);
        }
        let mut acc: u128 = 0;
        for b in s.bytes() {
            if !b.is_ascii_digit() {
                return Err(ParseAmountError::Invalid);
            }
            acc = acc
                .checked_mul(10)
                .and_then(|v| v.checked_add(u128::from(b - b'0')))
                .ok_or(ParseAmountError::Overflow)?;
        }
        Ok(Amount(acc))
    }
}

impl Serialize for Amount {
    fn serialize<S: Serializer>(&self, s: S) -> Result<S::Ok, S::Error> {
        s.serialize_str(&self.0.to_string())
    }
}

impl<'de> Deserialize<'de> for Amount {
    fn deserialize<D: Deserializer<'de>>(d: D) -> Result<Self, D::Error> {
        let raw = String::deserialize(d)?;
        Amount::from_str(&raw).map_err(de::Error::custom)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn roundtrip_string_amount() {
        let a: Amount = serde_json::from_str("\"123456789012345678901234567890\"").unwrap();
        assert_eq!(a.value(), 123_456_789_012_345_678_901_234_567_890);
        assert_eq!(serde_json::to_string(&a).unwrap(), "\"123456789012345678901234567890\"");
    }

    #[test]
    fn rejects_json_numbers() {
        let err = serde_json::from_str::<Amount>("100").unwrap_err();
        assert!(err.to_string().contains("invalid type"));
    }

    #[test]
    fn rejects_sign_and_non_digits() {
        assert_eq!(Amount::from_str("").unwrap_err(), ParseAmountError::Empty);
        assert_eq!(Amount::from_str("-1").unwrap_err(), ParseAmountError::Invalid);
        assert_eq!(Amount::from_str("+1").unwrap_err(), ParseAmountError::Invalid);
        assert_eq!(Amount::from_str("1a").unwrap_err(), ParseAmountError::Invalid);
    }

    #[test]
    fn rejects_above_u128() {
        let too_big = (U256::from(u128::MAX) + U256::from(1)).to_string();
        assert_eq!(too_big.parse::<Amount>().unwrap_err(), ParseAmountError::Overflow);
        assert!(Amount::from_u256(U256::from(u128::MAX)).is_some());
        assert!(Amount::from_u256(U256::from(u128::MAX) + U256::from(1)).is_none());
    }
}
