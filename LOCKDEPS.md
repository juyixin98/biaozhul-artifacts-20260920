# Dependency lock — positional-phrase-index

**This project has zero third-party dependencies.** It compiles and runs on
the JDK alone (`javac` / `java`); there is no Maven/Gradle configuration and
no artifact to download. Because there are no external artifacts, there is
no transitive dependency set to lock: this file is the lock record.

## Toolchain (verified during development)

| Component | Version used for verification | Required |
|-----------|-------------------------------|----------|
| OpenJDK (JDK + javac) | `openjdk 17.0.20.1` (Ubuntu 24.04 package) | Java 17 or newer |
| Build tool | none — plain `javac` | — |
| Runtime libraries | JDK standard library only | — |
| Test framework | none — a ~50-line harness in `test/phraseindex/Suite.java` | — |
| HTTP client used in E2E tests | `java.net.http.HttpClient` (JDK built-in) | — |
| HTTP server | `com.sun.net.httpserver.HttpServer` (JDK built-in, supported API) | — |

## Language features used

Sealed interfaces (`sealed`/`permits`), records, switch expressions and
pattern `instanceof` all require Java 17+.

## Reproducing without a lock manager

```bash
javac -version   # must print 17 or newer
./build.sh       # finds sources itself; no classpath, no downloads
./test.sh
```

If a stricter environment is desired, run with only the JDK modules on the
module path (the code uses no non-JDK packages):

```bash
java -cp out phraseindex.HttpServerApp 8080
```
