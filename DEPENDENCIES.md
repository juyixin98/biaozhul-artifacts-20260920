# Dependency lock

This project deliberately ships with **zero third-party dependencies** at runtime
and test time. Everything is provided by the JDK itself.

## Toolchain (locked / verified)

| Component | Version | Source | Notes |
|---|---|---|---|
| JDK | Temurin OpenJDK **17.0.20.1+1** (build 17.0.20.1+1) | `https://api.adoptium.net/v3/binary/latest/17/ga/linux/x64/jdk/hotspot/normal/eclipse` | Any JDK 17+ should work; this is the version used for verification. Language level: Java 17. |
| HTTP server | JDK built-in `com.sun.net.httpserver.HttpServer` | JDK | no servlet container |
| HMAC | JDK built-in `javax.crypto.Mac` (HmacSHA256) | JDK | cursor authentication tag |
| JSON | hand-written parser/serializer (`Json.java`) | this repo | ~200 lines, no Jackson/Gson |
| HTTP client (tests only) | JDK built-in `java.net.http.HttpClient` | JDK | no JUnit/TestNG/Apache HttpClient |

## Runtime dependencies

```
mvn dependency:tree   # (optional) -> no dependencies
```

`<dependencies />` in `pom.xml` is an explicitly empty, committed dependency set.

## Build-time dependencies

- `javac`/`jar` from a JDK 17 or newer — no Gradle, no Maven, no network needed.
- Maven is optional; `maven-compiler-plugin` is pinned to **3.13.0** in `pom.xml`
  but is not required by the canonical scripts.

## SHA-256 of the toolchain archive used during verification

See `RESULTS.md` (recorded after download/extraction).
