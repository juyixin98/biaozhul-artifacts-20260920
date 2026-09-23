# Locked dependencies / toolchain

## Third-party dependencies

**None.** The project compiles and runs on a plain JDK only — there is no
Maven/Gradle build file and no external jar is downloaded or required.

The following JDK modules/APIs are used (all part of the JDK image):

| Purpose                        | API                                                              | Module     |
|--------------------------------|------------------------------------------------------------------|------------|
| HTTP server                    | `com.sun.net.httpserver.HttpServer` (JDK-bundled, supported API) | `jdk.httpserver` |
| HTTP client (integration tests)| `java.net.http.HttpClient`                                       | `java.net.http`  |
| Collections, records, NIO files| `java.util`, `java.nio.file`                                     | `java.base`      |
| Logging                        | `java.util.logging`                                              | `java.logging`   |

## Locked toolchain used to build and verify

| Component            | Locked value                                              |
|----------------------|-----------------------------------------------------------|
| JDK distribution     | Eclipse Temurin OpenJDK 17.0.20.1+1 (HotSpot, x86_64)     |
| Language level       | Java 17 (records, pattern switch, text-free)              |
| Build tool           | none — plain `javac` (see `scripts/build.sh`)             |
| Test framework       | none — ~80-line in-repo harness (`test/sessionwindow/TestRunner.java`) |
| Verified on          | Linux 6.8, x86_64                                         |

Any JDK >= 17 from any vendor should work because only standard/bundled APIs
are used. To pin a different JDK, point `JAVA_HOME` at it:

```bash
JAVA_HOME=/path/to/jdk-17 ./scripts/test.sh
```

There is deliberately no dependency-lock file in the Maven/Gradle sense: with
zero third-party artifacts there is nothing to resolve. The JDK version above
is the reproducibility contract.
