//! `delta-client` — demo CLI driving the real delta protocol.
//!
//! Subcommands:
//!
//! ```text
//! delta-client put   <BASE> <name> <file>
//! delta-client get   <BASE> <name> <out-file>
//! delta-client list  <BASE>
//! delta-client sig   <file> [block_len] <out-signature>
//! delta-client sync  <BASE> <name> <local-file> [block_len]
//! ```
//!
//! `sync` is the end-to-end scenario: the client has the old artifact locally,
//! asks the server (which holds the new version) for a delta by uploading only
//! the block signature, applies the returned patch LOCALLY, verifies the
//! strong hash, updates the file, and prints the byte savings.

use artifact_delta::patch::{apply_patch, Patch};
use artifact_delta::signature::{default_block_len, strong_hash, Signature};
use artifact_delta::store::hex;
use std::io::Read;
use std::process::ExitCode;

fn main() -> ExitCode {
    let args: Vec<String> = std::env::args().collect();
    match run(&args) {
        Ok(()) => ExitCode::SUCCESS,
        Err(e) => {
            eprintln!("error: {e}");
            ExitCode::FAILURE
        }
    }
}

fn run(args: &[String]) -> Result<(), String> {
    let cmd = args.get(1).map(String::as_str).unwrap_or("help");
    match cmd {
        "put" => {
            let (base, name, path) = need3(args)?;
            let data = read(&path)?;
            let resp = ureq::put(&url(&base, &format!("artifacts/{name}")))
                .send_bytes(&data)
                .map_err(http_err)?;
            println!(
                "stored {} ({} bytes), server blake3 {}",
                name,
                data.len(),
                header_str(&resp, "x-blake3")
            );
        }
        "get" => {
            let (base, name, path) = need3(args)?;
            let bytes = ureq::get(&url(&base, &format!("artifacts/{name}")))
                .call()
                .map_err(http_err)
                .and_then(read_resp)?;
            std::fs::write(&path, &bytes).map_err(ioe)?;
            println!("wrote {path} ({} bytes)", bytes.len());
        }
        "list" => {
            let base = args.get(2).ok_or("usage: list <BASE>")?;
            let body = ureq::get(&url(base, "artifacts"))
                .call()
                .map_err(http_err)
                .and_then(|r| r.into_string().map_err(ioe))?;
            println!("{body}");
        }
        "sig" => {
            let path = args.get(2).ok_or("usage: sig <file> [block_len] <out>")?;
            let (block_arg, out) = match (args.get(3), args.get(4)) {
                (Some(b), Some(o)) => (Some(b.as_str()), o.as_str()),
                (Some(o), None) => (None, o.as_str()),
                _ => return Err("usage: sig <file> [block_len] <out>".into()),
            };
            let data = read(path)?;
            let block_len = match block_arg {
                Some(b) => b.parse().map_err(|_| "block_len must be a u32")?,
                None => default_block_len(data.len() as u64),
            };
            let sig = Signature::build(&data, block_len).map_err(serr)?;
            let bytes = sig.encode();
            std::fs::write(out, &bytes).map_err(ioe)?;
            println!(
                "signature: {} blocks, block_len={block_len}, {} bytes on the wire",
                sig.blocks.len(),
                bytes.len()
            );
        }
        "sync" => {
            let base = args.get(2).ok_or("usage: sync <BASE> <name> <file> [block_len]")?;
            let name = args.get(3).ok_or("missing name")?;
            let path = args.get(4).ok_or("missing local file")?;
            let block_arg = args.get(5).map(String::as_str);

            let basis = read(path)?;
            let block_len = match block_arg {
                Some(b) => b.parse().map_err(|_| "block_len must be a u32")?,
                None => default_block_len(basis.len() as u64),
            };
            let sig = Signature::build(&basis, block_len).map_err(serr)?;
            let sig_bytes = sig.encode();
            let basis_hash = strong_hash(&basis);

            // 1) What does the server have?
            let meta = ureq::head(&url(base, &format!("artifacts/{name}")))
                .call()
                .map_err(http_err)?;
            let server_hash = header_str(&meta, "x-blake3");
            let target_len: u64 = meta
                .header("x-target-length")
                .ok_or("no x-target-length header")?
                .parse()
                .map_err(|_| "bad x-target-length")?;

            if hex(&basis_hash) == server_hash {
                println!("local file already up to date (blake3 {server_hash})");
                return Ok(());
            }

            // 2) Upload ONLY the signature; receive the patch.
            let resp = ureq::post(&url(base, &format!("artifacts/{name}/delta")))
                .set("content-type", "application/vnd.artifact-delta.signature")
                .set("x-basis-blake3", &hex(&basis_hash))
                .send_bytes(&sig_bytes)
                .map_err(http_err)?;
            let stats = DeltaRespHeaders::from_response(&resp);
            let patch_bytes = read_resp(resp)?;
            let patch = Patch::decode(&patch_bytes).map_err(serr)?;

            // 3) Apply locally (the strong hash of the result is enforced).
            let updated = match apply_patch(&patch, &basis) {
                Ok(v) => v,
                Err(e) => return Err(format!("patch rejected locally: {e}")),
            };
            if hex(&strong_hash(&updated)) != server_hash {
                return Err("reconstructed file hash differs from server hash".into());
            }
            std::fs::write(path, &updated).map_err(ioe)?;

            // 4) Report transfer savings vs. downloading the whole artifact.
            let naive = target_len;
            let transferred = sig_bytes.len() as u64 + patch_bytes.len() as u64;
            let saved = naive.saturating_sub(transferred);
            let pct = if naive > 0 {
                saved as f64 * 100.0 / naive as f64
            } else {
                100.0
            };
            println!("synced {name}: {} -> {naive} bytes", basis.len());
            println!("  blocks matched     : {}", stats.blocks_matched);
            println!("  bytes from basis   : {}", stats.bytes_from_basis);
            println!("  literal new bytes  : {}", stats.literal_bytes);
            println!("  weak hits          : {}", stats.weak_hits);
            println!("  strong rejections  : {}", stats.strong_rejections);
            println!("  signature bytes    : {}", sig_bytes.len());
            println!("  patch bytes        : {}", patch_bytes.len());
            println!("  naive download     : {naive} bytes (100%)");
            println!(
                "  actually transferred: {transferred} bytes ({:.2}% of naive)",
                transferred as f64 * 100.0 / naive.max(1) as f64
            );
            println!("  bytes saved        : {saved} ({pct:.2}% fewer bytes)");
            println!("  new blake3         : {server_hash}");
        }
        _ => {
            eprintln!(
                "usage:\n  {p} put <BASE> <name> <file>\n  {p} get <BASE> <name> <out>\n  {p} list <BASE>\n  {p} sig <file> [block_len] <out>\n  {p} sync <BASE> <name> <local-file> [block_len]",
                p = args.first().map(String::as_str).unwrap_or("delta-client")
            );
            std::process::exit(2);
        }
    }
    Ok(())
}

struct DeltaRespHeaders {
    blocks_matched: String,
    bytes_from_basis: String,
    literal_bytes: String,
    weak_hits: String,
    strong_rejections: String,
}

impl DeltaRespHeaders {
    fn from_response(resp: &ureq::Response) -> Self {
        let g = |k: &str| resp.header(k).unwrap_or("?").to_string();
        DeltaRespHeaders {
            blocks_matched: g("x-blocks-matched"),
            bytes_from_basis: g("x-bytes-from-basis"),
            literal_bytes: g("x-literal-bytes"),
            weak_hits: g("x-weak-hits"),
            strong_rejections: g("x-strong-rejections"),
        }
    }
}

fn need3(args: &[String]) -> Result<(String, String, String), String> {
    Ok((
        args.get(2).ok_or("missing BASE")?.clone(),
        args.get(3).ok_or("missing name")?.clone(),
        args.get(4).ok_or("missing file")?.clone(),
    ))
}

fn url(base: &str, path: &str) -> String {
    format!("{}/{}", base.trim_end_matches('/'), path)
}

fn read(path: &str) -> Result<Vec<u8>, String> {
    std::fs::read(path).map_err(|e| format!("{path}: {e}"))
}
fn ioe(e: std::io::Error) -> String {
    e.to_string()
}
fn serr<E: std::fmt::Display>(e: E) -> String {
    e.to_string()
}
fn header_str(resp: &ureq::Response, name: &str) -> String {
    resp.header(name).unwrap_or("?").to_string()
}
fn http_err(e: ureq::Error) -> String {
    match e {
        ureq::Error::Status(code, resp) => {
            let body = resp.into_string().unwrap_or_default();
            format!("HTTP {code}: {body}")
        }
        other => other.to_string(),
    }
}
fn read_resp(resp: ureq::Response) -> Result<Vec<u8>, String> {
    let mut out = Vec::new();
    resp.into_reader().read_to_end(&mut out).map_err(ioe)?;
    Ok(out)
}
