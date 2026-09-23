//! Receipt verification and admission control — the security core of the host.
//!
//! A result is returned to callers **only** when every check below passes:
//!
//! 1. The receipt is a real cryptographic receipt (`Composite` / `Succinct` /
//!    `Groth16`). A `Fake` (dev-mode) receipt is rejected outright unless the
//!    host was explicitly started with `--dev-mode`. Dev receipts can never
//!    masquerade as real proofs.
//! 2. `Receipt::verify(expected_image_id)` passes: the seal validates under the
//!    zkVM verifier, the guest halted successfully (exit 0), and the executed
//!    image ID equals the image ID embedded in this binary.
//! 3. The journal decodes with the expected magic/version and a length in
//!    `1..=256`.
//! 4. The journal's input commitment equals the SHA-256 of the *canonical
//!    encoding of the inputs the host actually sent* — proving the seal binds
//!    this particular batch, not some other batch that hashes to a journal.
//! 5. The claimed median equals a fresh, host-side recomputation.

use anyhow::{bail, Result};
use batch_median_core::{
    canonical_input_bytes, input_commitment, validate, Journal, JOURNAL_MAGIC, JOURNAL_VERSION,
    MAX_BATCH_LEN,
};
use risc0_zkvm::{Digest, InnerReceipt, Receipt};

/// What kind of receipt a producer actually generated. Only cryptographic
/// variants are admitted in the default (proof-required) mode.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ReceiptFlavor {
    /// Segment-level STARK receipts, linear in execution length.
    Composite,
    /// Single recursive STARK, constant size.
    Succinct,
    /// Groth16 SNARK, smallest.
    Groth16,
    /// Development placeholder with no cryptographic integrity.
    Fake,
}

impl ReceiptFlavor {
    pub fn as_str(self) -> &'static str {
        match self {
            ReceiptFlavor::Composite => "composite-stark",
            ReceiptFlavor::Succinct => "succinct-stark",
            ReceiptFlavor::Groth16 => "groth16",
            ReceiptFlavor::Fake => "fake-dev-only",
        }
    }
}

/// Classify a receipt's inner variant. Free function because foreign types
/// cannot host inherent methods.
pub fn flavor_of(receipt: &Receipt) -> ReceiptFlavor {
    match &receipt.inner {
        InnerReceipt::Composite(_) => ReceiptFlavor::Composite,
        InnerReceipt::Succinct(_) => ReceiptFlavor::Succinct,
        InnerReceipt::Groth16(_) => ReceiptFlavor::Groth16,
        InnerReceipt::Fake(_) => ReceiptFlavor::Fake,
        // `InnerReceipt` is #[non_exhaustive]; treat any unknown variant as
        // untrusted rather than silently accepting it.
        _ => ReceiptFlavor::Fake,
    }
}

/// Outcome of admitting a verified receipt.
#[derive(Debug, Clone)]
pub struct VerifiedOutput {
    /// The parsed public journal.
    pub journal: Journal,
    /// Cryptographic flavor of the receipt that proved it.
    pub flavor: ReceiptFlavor,
    /// Number of zkVM user cycles reported by the prover (when available).
    pub user_cycles: u64,
    /// Number of segments reported by the prover (when available).
    pub segments: u64,
}

/// Verify a receipt against the expected program image and bind it to the
/// concrete inputs the host sent.
///
/// `allow_dev` must be `true` **only** when the operator explicitly opted into
/// development mode (`--dev-mode` / `RISC0_DEV_MODE=1`). In the default mode
/// it is `false`, and any fake receipt is rejected before cryptographic
/// verification is even attempted.
///
/// Returns a detailed error (never a silently wrong answer) on any failure.
pub fn verify_and_admit(
    receipt: &Receipt,
    expected_image_id: Digest,
    sent_inputs: &[i64],
    allow_dev: bool,
) -> Result<VerifiedOutput> {
    // (1) Dev-mode admission control — explicit, visible, and default-deny.
    let flavor = flavor_of(receipt);
    if flavor == ReceiptFlavor::Fake {
        if !allow_dev {
            bail!(
                "REJECTED: receipt is a development (FakeReceipt) placeholder and dev mode is \
                 not enabled; a development receipt cannot be accepted as a real proof"
            );
        }
    } else if allow_dev {
        // Real proof produced despite dev mode being requested is fine; we
        // simply record the true flavor below.
    }

    // Even in allow_dev mode, do not let an attacker-supplied env var silently
    // flip the verifier: verify with an explicit context. In strict mode use
    // the ambient default (dev mode is compile-time disabled).
    #[cfg(feature = "strict")]
    let verify_result = receipt.verify(expected_image_id);
    #[cfg(not(feature = "strict"))]
    let verify_result = {
        let ctx = risc0_zkvm::VerifierContext::default().with_dev_mode(allow_dev);
        receipt.verify_with_context(&ctx, expected_image_id)
    };
    if let Err(e) = verify_result {
        bail!("receipt cryptographic verification failed: {e}");
    }

    // (3) Journal structure.
    let journal = match Journal::decode(&receipt.journal.bytes) {
        Ok(j) => j,
        Err(e) => bail!("journal validation failed: {}", e.as_str()),
    };

    // (4) Bind the journal to the inputs the host actually sent.
    let sent = sent_inputs;
    if journal.length as usize != sent.len() {
        bail!(
            "input binding failed: journal claims length {} but host sent {} inputs",
            journal.length,
            sent.len()
        );
    }
    // Re-derive the commitment from the raw inputs. `sha::Impl` is the same
    // hash on both sides of the boundary.
    let recomputed = input_commitment(sent);
    if recomputed != journal.input_commitment {
        // Hash the canonical bytes for an actionable error message.
        let canon = canonical_input_bytes(sent);
        bail!(
            "input binding failed: journal commitment {} does not match commitment {} of the \
             sent inputs ({} canonical bytes)",
            hex_encode(journal.input_commitment.as_bytes()),
            hex_encode(recomputed.as_bytes()),
            canon.len(),
        );
    }

    // (5) Semantic re-check of the answer. Cheap, and turns the claim into
    // something the host independently endorses.
    if let Err(e) = validate(sent) {
        bail!("post-verification validation failed: {}", e.as_str());
    }
    let expected_median = batch_median_core::median_of(sent);
    if expected_median != journal.median {
        bail!(
            "semantic check failed: journal median {} but host recomputed {expected_median}",
            journal.median
        );
    }

    Ok(VerifiedOutput {
        journal,
        flavor,
        user_cycles: 0,
        segments: 0,
    })
}

/// Assert the raw journal bytes carry the expected magic/version header before
/// they are even parsed. Used by the CLI on the execution path as well.
pub fn header_check(journal_bytes: &[u8]) -> Result<()> {
    if journal_bytes.len() < 6 {
        bail!("journal too short for a header");
    }
    if journal_bytes[0..4] != JOURNAL_MAGIC {
        bail!("journal magic mismatch");
    }
    let v = u16::from_le_bytes([journal_bytes[4], journal_bytes[5]]);
    if v != JOURNAL_VERSION {
        bail!("journal version {v} unsupported (expected {JOURNAL_VERSION})");
    }
    if journal_bytes.len() != 24 + 32 {
        bail!(
            "journal length {} out of bounds (max field {})",
            journal_bytes.len(),
            MAX_BATCH_LEN
        );
    }
    Ok(())
}

fn hex_encode(bytes: &[u8]) -> String {
    let mut s = String::with_capacity(2 * bytes.len());
    for b in bytes {
        s.push_str(&format!("{b:02x}"));
    }
    s
}
