# 依赖清单（锁定）

## 运行时依赖

**无任何第三方依赖。** 服务端与测试代码仅使用 JDK 标准库，包括：

- `com.sun.net.httpserver.HttpServer`（JDK 内置 HTTP 服务，JDK 自带，非外部库）
- `java.net.http.HttpClient`（测试客户端，JDK 11+）
- `java.util`（TreeSet / PriorityQueue / ConcurrentHashMap 等）

无 Maven/Gradle 构建依赖，无需要下载的 jar；因此不存在需要版本锁定的第三方构件。

## 构建工具链（已实际使用并锁定版本）

| 工具 | 锁定版本 | 来源 |
|---|---|---|
| javac / java | **OpenJDK 21.0.12.1**（编译目标 `--release 11`，可在 JDK 11+ 运行） | Ubuntu 24.04 官方包 `openjdk-21-jdk-headless 21.0.12.1+1-1~24.04.4` |
| bash | 5.x（脚本） | 系统自带 |
| curl | 7.x / 8.x（仅示例脚本使用，非测试依赖） | 系统自带 |

本次开发环境无 root 权限，JDK 以 `apt-get download` + `dpkg-deb -x` 免安装解压在
`~/jdk21/usr/lib/jvm/java-21-openjdk-amd64`，脚本已内置该路径的自动探测；
常规机器上设置 `JAVA_HOME` 或将 `java/javac` 放入 `PATH` 即可。

包版本复现（apt）：

```
openjdk-21-jdk-headless_21.0.12.1+1-1~24.04.4_amd64.deb
openjdk-21-jre-headless_21.0.12.1+1-1~24.04.4_amd64.deb
```

## 验证

```
java -version
# openjdk version "21.0.12.1" 2026-08-18
```

> 说明：项目刻意不引入 JSON 库、Web 框架或测试框架（JUnit 等），
> 以保证“零依赖、可离线构建”。JSON 解析/序列化与测试断言均为项目内的小型实现。
