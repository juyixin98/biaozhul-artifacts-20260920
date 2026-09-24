# 依赖锁定

本项目**零第三方运行时依赖、零第三方测试依赖**：HTTP 服务使用 JDK 内置的
`com.sun.net.httpserver.HttpServer`，JSON/CSV/RLE 全部为随仓库提供的手写实现，
测试是纯 JDK 的 `main` 方法断言（另用 JDK 11+ 自带的 `java.net.http.HttpClient`
做 HTTP 端到端回环）。

因此没有 Maven/Gradle 依赖图可锁定；为保证可复现构建，锁定工具链版本如下：

| 项 | 锁定值 | 说明 |
|---|---|---|
| JDK | 17（验证版本：`17.0.20.1`，Ubuntu 24.04 OpenJDK） | 最低 Java 17；不使用任何预览特性 |
| Java 语言/字节码级别 | 17 | 直接 `javac` 编译，不加 `--release` 之外的选项（当前构建命令未附加该选项，仅用 JDK17 源码特性） |
| 构建工具 | 无（仓库自带 `build.sh`，仅调用 `javac`） | 不需要 Maven/Gradle |
| 第三方 jar | 无 | classpath 中除 JDK 外只有本项目 `out/` |
| 操作系统验证 | Linux x86_64（内核 6.8，Ubuntu 24.04） | 仅依赖跨平台 JDK API，Windows/macOS 同样可运行 |

实际验证记录见 `RUN_LOG.md`。如未来引入任何第三方依赖，应以 Maven `mvn dependency:go-offline`
生成可校验的依赖清单或 Gradle 锁文件替换本说明。
