//! The incremental observer: a record-layer state machine plus a handshake
//! reassembler.
//!
//! Data flow:
//!
//! ```text
//! TCP bytes (any chunk boundaries)
//!   -> record header parser (src/record.rs)
//!      -> handshake fragments reassembled ACROSS records
//!         -> plaintext ClientHello parser (src/client_hello.rs)
//!      -> ApplicationData / ChangeCipherSpec: counted, never dissected
//! ```
//!
//! The observer never decrypts and never participates in a handshake; once
//! `ChangeCipherSpec` / `ApplicationData` is seen, or once a ClientHello has
//! been extracted, later handshake fragments are no longer treated as
//! plaintext, so ciphertext cannot accidentally be parsed as a handshake.

use crate::client_hello::{
    parse_client_hello, parse_handshake_head, ClientHello, HANDSHAKE_TYPE_CLIENT_HELLO,
};
use crate::cursor::Reader;
use crate::error::{ParseError, Warning};
use crate::record::{
    parse_record_head, RecordHeader, CONTENT_TYPE_ALERT, CONTENT_TYPE_APPLICATION_DATA,
    CONTENT_TYPE_CHANGE_CIPHER_SPEC, CONTENT_TYPE_HANDSHAKE, MAX_RECORD_FRAGMENT_HARD,
};

/// Configurable upper bounds enforced by the observer.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Limits {
    /// Maximum TLS record fragment length accepted. RFC 8446 hard maximum is
    /// 2^14 + 256 = 16640 bytes.
    pub max_record_fragment: usize,
    /// Maximum reassembled handshake message length. Above the RFC
    /// ClientHello limits but bounded so a hostile length prefix cannot
    /// force unbounded buffering.
    pub max_handshake_message: usize,
}

impl Default for Limits {
    fn default() -> Self {
        Limits {
            max_record_fragment: MAX_RECORD_FRAGMENT_HARD,
            max_handshake_message: 1 << 20, // 1 MiB
        }
    }
}

/// One parsed record header in stream order.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ObservedRecord {
    pub index: usize,
    pub content_type: u8,
    pub content_type_name: &'static str,
    pub legacy_version: (u8, u8),
    pub fragment_length: u16,
}

/// Everything the observer learned from one direction of a TCP stream.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct Observation {
    pub records: Vec<ObservedRecord>,
    pub client_hello: Option<ClientHello>,
    pub warnings: Vec<Warning>,
    pub handshake_records: usize,
    pub change_cipher_spec_records: usize,
    pub alert_records: usize,
    pub application_data_records: usize,
    /// True once any record proves the record layer is encrypted
    /// (ApplicationData seen, or ChangeCipherSpec / ClientHello passed).
    pub encrypted: bool,
    pub bytes_in: usize,
}

/// Incremental, streaming observer.
pub struct Observer {
    limits: Limits,
    /// Stream bytes not yet consumed as complete records.
    carry: Vec<u8>,
    /// Concatenated handshake fragments not yet consumed as complete
    /// handshake messages (handshake messages span records freely).
    hs_stream: Vec<u8>,
    /// Bytes of `hs_stream` already parsed into complete messages.
    hs_pos: usize,
    /// Plaintext handshake dissection disabled (ClientHello seen, or the
    /// record layer switched to encrypted traffic).
    hs_closed: bool,
    obs: Observation,
}

impl Observer {
    pub fn new() -> Self {
        Self::with_limits(Limits::default())
    }

    pub fn with_limits(limits: Limits) -> Self {
        Observer {
            limits,
            carry: Vec::new(),
            hs_stream: Vec::new(),
            hs_pos: 0,
            hs_closed: false,
            obs: Observation::default(),
        }
    }

    pub fn limits(&self) -> Limits {
        self.limits
    }

    /// Current observation (valid even after a fatal error).
    pub fn observation(&self) -> &Observation {
        &self.obs
    }

    /// Feed any number of bytes; chunk boundaries need not line up with
    /// record or message boundaries. Returns the accumulated observation.
    pub fn push(&mut self, chunk: &[u8]) -> Result<&Observation, ParseError> {
        self.obs.bytes_in += chunk.len();
        self.carry.extend_from_slice(chunk);

        // Parse every complete record currently buffered.
        loop {
            if self.carry.len() < 5 {
                break;
            }
            let header = {
                let mut r = Reader::new(&self.carry);
                // A 5-byte prefix is present, so this cannot be Truncated;
                // content/version subset errors remain possible.
                parse_record_head(&mut r)?
            };
            let frag_len = header.fragment_length as usize;
            if frag_len > self.limits.max_record_fragment {
                return Err(ParseError::RecordTooLarge {
                    length: frag_len,
                    max: self.limits.max_record_fragment,
                });
            }
            let total = 5 + frag_len;
            if self.carry.len() < total {
                break; // wait for the rest of this fragment
            }

            let fragment = self.carry[5..total].to_vec();
            self.carry.drain(..total);

            self.record_record(header);
            match header.content_type {
                CONTENT_TYPE_APPLICATION_DATA => {
                    // Ciphertext. Record it, never parse it.
                    self.obs.encrypted = true;
                    self.close_handshake();
                }
                CONTENT_TYPE_CHANGE_CIPHER_SPEC => {
                    // Everything afterwards is encrypted on this direction.
                    self.obs.encrypted = true;
                    self.close_handshake();
                }
                CONTENT_TYPE_ALERT => {
                    // Alerts before encryptions are plaintext but carry no
                    // handshake data; after encryption they may be opaque.
                    // Either way there is nothing to dissect.
                }
                CONTENT_TYPE_HANDSHAKE => {
                    if !self.hs_closed {
                        self.feed_handshake(&fragment)?;
                    }
                }
                _ => unreachable!("content_type subset checked in parser"),
            }
        }

        Ok(&self.obs)
    }

    /// Declare the stream ended. Any half record / half handshake message
    /// left buffered is a truncation error.
    ///
    /// The large `Err` variant is intentional: even on a truncation error the
    /// caller is given the partially accumulated observation (records seen,
    /// counts) rather than losing it.
    #[allow(clippy::result_large_err)]
    pub fn finish(mut self) -> Result<Observation, (ParseError, Observation)> {
        if self.carry.len() >= 5 {
            // Header parsed, fragment incomplete.
            return Err((
                ParseError::Truncated {
                    layer: crate::error::Layer::Record,
                    where_: crate::error::Truncated::LengthPrefixed,
                },
                self.obs,
            ));
        }
        if !self.carry.is_empty() {
            return Err((
                ParseError::Truncated {
                    layer: crate::error::Layer::RecordHeader,
                    where_: crate::error::Truncated::Field,
                },
                self.obs,
            ));
        }
        if !self.hs_closed && self.hs_pos < self.hs_stream.len() {
            let layer = if self.hs_stream.len() - self.hs_pos < 4 {
                crate::error::Layer::HandshakeHeader
            } else {
                crate::error::Layer::HandshakeBody
            };
            return Err((
                ParseError::Truncated {
                    layer,
                    where_: crate::error::Truncated::Reassembly,
                },
                self.obs,
            ));
        }
        self.obs.encrypted |= self.obs.client_hello.is_some();
        Ok(self.obs)
    }

    fn record_record(&mut self, h: RecordHeader) {
        let name = match h.content_type {
            CONTENT_TYPE_CHANGE_CIPHER_SPEC => {
                self.obs.change_cipher_spec_records += 1;
                "change_cipher_spec"
            }
            CONTENT_TYPE_ALERT => {
                self.obs.alert_records += 1;
                "alert"
            }
            CONTENT_TYPE_HANDSHAKE => {
                self.obs.handshake_records += 1;
                "handshake"
            }
            CONTENT_TYPE_APPLICATION_DATA => {
                self.obs.application_data_records += 1;
                "application_data"
            }
            _ => "unknown",
        };
        self.obs.records.push(ObservedRecord {
            index: self.obs.records.len(),
            content_type: h.content_type,
            content_type_name: name,
            legacy_version: h.legacy_version(),
            fragment_length: h.fragment_length,
        });
    }

    fn close_handshake(&mut self) {
        self.hs_closed = true;
        self.hs_stream.clear();
        self.hs_pos = 0;
    }

    /// Append one handshake record fragment and extract every complete
    /// handshake message now available, reassembling across records.
    fn feed_handshake(&mut self, fragment: &[u8]) -> Result<(), ParseError> {
        // Compact consumed prefix before appending.
        if self.hs_pos > 0 {
            self.hs_stream.drain(..self.hs_pos);
            self.hs_pos = 0;
        }
        self.hs_stream.extend_from_slice(fragment);

        loop {
            let available = self.hs_stream.len() - self.hs_pos;
            if available < 4 {
                break; // incomplete handshake header, wait for next record
            }
            let (msg_type, msg_len) = {
                let mut r = Reader::new(&self.hs_stream[self.hs_pos..]);
                parse_handshake_head(&mut r)?
            };
            let msg_len = msg_len as usize;
            if msg_len > self.limits.max_handshake_message {
                return Err(ParseError::HandshakeTooLarge {
                    length: msg_len,
                    max: self.limits.max_handshake_message,
                });
            }
            if available < 4 + msg_len {
                break; // incomplete body, reassemble across more records
            }

            let body_start = self.hs_pos + 4;
            let body = self.hs_stream[body_start..body_start + msg_len].to_vec();
            self.hs_pos += 4 + msg_len;

            if msg_type == HANDSHAKE_TYPE_CLIENT_HELLO {
                let ch = parse_client_hello(&body, &mut self.obs.warnings)?;
                self.obs.client_hello = Some(ch);
                // A client sends exactly one ClientHello, in the clear.
                // Everything later on this direction is encrypted
                // (ApplicationData records) — stop plaintext dissection.
                self.close_handshake();
                self.obs.encrypted = true;
                return Ok(());
            } else {
                // Only ClientHello is in the accepted handshake subset for a
                // client stream. Skip other (fully framed) messages.
                self.obs
                    .warnings
                    .push(Warning::SkippedHandshake { type_: msg_type });
            }
        }
        Ok(())
    }
}

impl Default for Observer {
    fn default() -> Self {
        Self::new()
    }
}

/// One-shot convenience: parse a complete byte buffer with default limits.
/// On error the partially accumulated observation is still returned.
pub fn observe(bytes: &[u8]) -> (Result<(), ParseError>, Observation) {
    let mut o = Observer::new();
    if let Err(e) = o.push(bytes) {
        return (Err(e), o.obs);
    }
    match o.finish() {
        Ok(obs) => (Ok(()), obs),
        Err((e, obs)) => (Err(e), obs),
    }
}
