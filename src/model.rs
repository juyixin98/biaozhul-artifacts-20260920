use serde::{Deserialize, Serialize};

/// A registry of all known packages and their versions.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Registry {
    pub packages: Vec<Package>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Package {
    pub name: String,
    pub versions: Vec<PackageVersion>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct PackageVersion {
    /// SemVer version string, e.g. "1.2.3".
    pub version: String,
    #[serde(default)]
    pub dependencies: Vec<Dependency>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Dependency {
    pub name: String,
    /// SemVer range, e.g. "^1.2", ">=1.0.0, <2.0.0", "*".
    pub range: String,
    /// If set, this dependency is optional and only activates when the
    /// request enables the extra "<package>/<optional>".
    #[serde(default)]
    pub optional: Option<String>,
    /// If set, this dependency only applies when the request's platform
    /// matches this value exactly (e.g. "linux", "windows", "macos").
    #[serde(default)]
    pub platform: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Requirement {
    pub name: String,
    pub range: String,
}

/// POST /resolve request body.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ResolveRequest {
    pub registry: Registry,
    pub requirements: Vec<Requirement>,
    #[serde(default)]
    pub platform: Option<String>,
    /// Enabled extras, each in the form "<package>/<extra>".
    #[serde(default)]
    pub extras: Vec<String>,
}

/// A resolved lockfile. Can be fed back to POST /replay.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Lockfile {
    pub requirements: Vec<Requirement>,
    #[serde(default)]
    pub platform: Option<String>,
    #[serde(default)]
    pub extras: Vec<String>,
    /// package name -> exact version
    pub locked: std::collections::BTreeMap<String, String>,
}

/// POST /replay request body.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ReplayRequest {
    pub registry: Registry,
    pub lockfile: Lockfile,
}
