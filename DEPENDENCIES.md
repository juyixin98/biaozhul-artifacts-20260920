# Dependency lock

This project deliberately has **zero third-party dependencies**. It uses only the
Java standard library, so there is no dependency download and no version drift.

## Toolchain (pinned for the build/test environment)

| Component | Version used to build & verify | Notes |
|-----------|-------------------------------|-------|
| JDK       | OpenJDK 17.0.20.1 (Temurin/OpenJDK packaging on Ubuntu 24.04) | `javac`/`java`; source & binary use only Java 17 language/API features |
| Build tool | none | Plain `javac` invocation (see `scripts/build.sh`) |
| HTTP server | JDK built-in `com.sun.net.httpserver.HttpServer` | bundled with the JRE |
| JSON | hand-written parser/serializer in `src/joinplanner/json/` | no Jackson/Gson |
| Test framework | hand-written assertion runner in `test/joinplanner/TestFramework.java` | no JUnit |

## Runtime requirements

- **Java 17 or newer.** No other runtime is needed; no network access is used at build
  or run time.
- Verified on `Linux 6.8 (Ubuntu 24.04), x86_64`. Any OS with a Java 17 JDK works.

## Why no Maven/Gradle?

The task requires locked dependencies and an offline-friendly, auditable build. With
no external libraries, a dependency manifest (pom.xml/build.gradle) would add a
toolchain requirement without adding any dependency; the build is therefore a single
deterministic `javac` command wrapped by `scripts/build.sh`.
