# Dependency lockfile

**Runtime / compile third-party Java dependencies: NONE.**

The project deliberately uses only the Java standard library:

| Capability | Artifact | JDK module |
|---|---|---|
| HTTP server | `com.sun.net.httpserver.HttpServer` | `jdk.httpserver` |
| HTTP client (tests only) | `java.net.http.HttpClient` | `java.net.http` |
| File IO / channels | `java.nio.file`, `java.nio.channels` | `java.base` |
| Collections, concurrency | `java.util`, `java.util.concurrent` | `java.base` |
| JSON | bundled source `src/colscan/Json.java` (hand-written) | — |
| Test framework | none — plain `main` + assertions (`test/colscan/Assert.java`) | — |

There is therefore no Maven/Gradle resolution and no transitive dependency tree to pin.

## Toolchain pin

| Tool | Pinned requirement | Verified version (recorded 2026-09-23) |
|---|---|---|
| javac / java | JDK 17 or newer (source compiled with `javac -Xlint:all -Werror`) | Temurin OpenJDK **17.0.20.1** (build 17.0.20.1+1), Linux x86_64 |
| bash | any bash 4+ (scripts) | 5.x |
| curl | any (demo only, not required by the server) | system curl |
| jq | any 1.x (demo output formatting only) | system jq |

No JARs are downloaded or vendored. `out/` and the data directories are build/runtime
output only (see `.gitignore`).
