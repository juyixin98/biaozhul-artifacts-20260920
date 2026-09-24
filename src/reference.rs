//! Parsing of user supplied references.
//!
//! Accepted forms:
//! - `repo:tag`
//! - `repo@digest`
//! - `repo:tag@digest` (tag is resolved, then asserted to equal the digest)
//! - `repo` (defaults to tag `latest`)
//!
//! Repository names here are the registry path minus the host, e.g.
//! `library/alpine`.

use crate::digest::Digest;
use crate::error::ApiError;

#[derive(Debug, Clone)]
pub struct Ref {
    pub repository: String,
    pub tag: Option<String>,
    pub digest: Option<Digest>,
}

/// Validate a tag per the Docker/OCI grammar-ish: `[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}`.
pub fn validate_tag(tag: &str) -> Result<(), ApiError> {
    let valid_len = 1..=128;
    let valid_chars = |b: u8| b.is_ascii_alphanumeric() || matches!(b, b'_' | b'.' | b'-');
    if !valid_len.contains(&tag.len())
        || !tag.bytes().all(valid_chars)
        || tag.starts_with(['.', '-'])
    {
        return Err(ApiError::BadRequest(format!("invalid tag {tag:?}")));
    }
    Ok(())
}

fn validate_repo(name: &str) -> Result<(), ApiError> {
    if name.is_empty()
        || name.len() > 256
        || name.starts_with('/')
        || name.ends_with('/')
        || name.split('/').any(|component| {
            component.is_empty()
                || component.len() > 128
                || !component
                    .bytes()
                    .all(|b| b.is_ascii_alphanumeric() || matches!(b, b'.' | b'_' | b'-'))
                || component.starts_with(['.', '-'])
        })
    {
        return Err(ApiError::BadRequest(format!(
            "invalid repository name {name:?}"
        )));
    }
    Ok(())
}

impl Ref {
    pub fn parse(raw: &str) -> Result<Ref, ApiError> {
        let (repo_part, digest_part) = match raw.rsplit_once('@') {
            Some((r, d)) => (r, Some(d)),
            None => (raw, None),
        };
        let digest = digest_part
            .map(Digest::parse)
            .transpose()
            .map_err(|e| ApiError::BadRequest(e.0))?;

        let (repository, tag) = match repo_part.rsplit_once(':') {
            // A colon only counts as a tag separator when the tag part has no
            // slash (otherwise it could be a host:port, which we do not model).
            Some((repo, tag)) if !tag.contains('/') => (repo.to_string(), Some(tag.to_string())),
            _ => (repo_part.to_string(), Some("latest".to_string())),
        };
        validate_repo(&repository)?;
        if let Some(t) = &tag {
            validate_tag(t)?;
        }
        Ok(Ref {
            repository,
            tag,
            digest,
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_basic_forms() {
        let r = Ref::parse("library/alpine:3.20").unwrap();
        assert_eq!(r.repository, "library/alpine");
        assert_eq!(r.tag.unwrap(), "3.20");
        assert!(r.digest.is_none());

        let r = Ref::parse("demo/weblog").unwrap();
        assert_eq!(r.tag.unwrap(), "latest");

        let r = Ref::parse(
            "demo/weblog@sha256:0000000000000000000000000000000000000000000000000000000000000000",
        )
        .unwrap();
        assert!(r.tag.is_some());
        assert!(r.digest.is_some());
    }

    #[test]
    fn rejects_bad_input() {
        assert!(Ref::parse("").is_err());
        assert!(Ref::parse("/weblog:x").is_err());
        assert!(Ref::parse("weblog:bad tag").is_err());
        assert!(Ref::parse("weblog@not-a-digest").is_err());
        assert!(Ref::parse("weblog@sha256:zz").is_err());
    }
}
