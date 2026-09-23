//! Small binary + JSON encoding helpers.
//!
//! Keys, values and hashes travel over JSON as lower-case hex strings.
//! Persistent RocksDB payloads use a minimal big-endian length-prefixed
//! codec defined in [`store`](crate::store).

use std::fmt;

use serde::{de, Deserialize, Deserializer, Serialize, Serializer};

use crate::error::{Error, Result};

/// Arbitrary byte string serialized as a hex string (`""` for empty bytes).
#[derive(Clone, PartialEq, Eq, Default)]
pub struct HexBytes(pub Vec<u8>);

impl HexBytes {
    pub fn new(b: impl Into<Vec<u8>>) -> Self {
        HexBytes(b.into())
    }
    pub fn as_bytes(&self) -> &[u8] {
        &self.0
    }
}

impl fmt::Debug for HexBytes {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "0x{}", hex::encode(&self.0))
    }
}

impl Serialize for HexBytes {
    fn serialize<S: Serializer>(&self, s: S) -> std::result::Result<S::Ok, S::Error> {
        s.serialize_str(&hex::encode(&self.0))
    }
}

impl<'de> Deserialize<'de> for HexBytes {
    fn deserialize<D: Deserializer<'de>>(d: D) -> std::result::Result<Self, D::Error> {
        let raw = String::deserialize(d)?;
        let raw = raw.strip_prefix("0x").unwrap_or(&raw);
        hex::decode(raw).map(HexBytes).map_err(de::Error::custom)
    }
}

/// Exactly 32 bytes (a SHA-256 hash), serialized as a hex string.
#[derive(Clone, Copy, PartialEq, Eq, Hash)]
pub struct Hex32(pub [u8; 32]);

impl Hex32 {
    pub fn as_hash(&self) -> &[u8; 32] {
        &self.0
    }
}

impl fmt::Debug for Hex32 {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "0x{}", hex::encode(self.0))
    }
}

impl Serialize for Hex32 {
    fn serialize<S: Serializer>(&self, s: S) -> std::result::Result<S::Ok, S::Error> {
        s.serialize_str(&hex::encode(self.0))
    }
}

impl<'de> Deserialize<'de> for Hex32 {
    fn deserialize<D: Deserializer<'de>>(d: D) -> std::result::Result<Self, D::Error> {
        let raw = String::deserialize(d)?;
        let raw = raw.strip_prefix("0x").unwrap_or(&raw);
        let v = hex::decode(raw).map_err(de::Error::custom)?;
        <[u8; 32]>::try_from(v.as_slice())
            .map(Hex32)
            .map_err(|_| de::Error::custom("expected 32-byte (64 hex chars) hash"))
    }
}

/// Big-endian u32 encoder for the persistent binary codec.
pub(crate) fn enc_u32(out: &mut Vec<u8>, n: u32) {
    out.extend_from_slice(&n.to_be_bytes());
}

pub(crate) fn enc_bytes(out: &mut Vec<u8>, b: &[u8]) {
    enc_u32(out, u32::try_from(b.len()).map_err(|_| Error::bad("blob too large")).unwrap());
    out.extend_from_slice(b);
}

/// Cursor over a borrowed byte buffer used while decoding.
pub(crate) struct Cursor<'a> {
    buf: &'a [u8],
}

impl<'a> Cursor<'a> {
    pub(crate) fn new(buf: &'a [u8]) -> Self {
        Cursor { buf }
    }

    pub(crate) fn remaining(&self) -> usize {
        self.buf.len()
    }

    pub(crate) fn take(&mut self, n: usize) -> Result<&'a [u8]> {
        if self.buf.len() < n {
            return Err(Error::Storage("unexpected end of encoded blob".into()));
        }
        let (head, rest) = self.buf.split_at(n);
        self.buf = rest;
        Ok(head)
    }

    pub(crate) fn u64(&mut self) -> Result<u64> {
        let b = self.take(8)?;
        Ok(u64::from_be_bytes(b.try_into().unwrap()))
    }

    pub(crate) fn u32(&mut self) -> Result<u32> {
        let b = self.take(4)?;
        Ok(u32::from_be_bytes(b.try_into().unwrap()))
    }

    pub(crate) fn bytes(&mut self) -> Result<&'a [u8]> {
        let n = self.u32()? as usize;
        self.take(n)
    }
}
