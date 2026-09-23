# 依赖锁定 / Dependency Lock

本项目为**零第三方依赖**的纯 JDK 实现：HTTP 使用 JDK 内置
`com.sun.net.httpserver.HttpServer`（`jdk.httpserver` 模块），JSON 为自带的最小实现
（`src/ij/Json.java`）。因此没有 Maven/Gradle 依赖坐标需要锁定。

## 构建/运行时要求

- JDK >= 11（开发与验收使用 JDK 17）
- 仅使用 JDK 内置模块：`java.base`、`java.net.http`（测试 HTTP 客户端）、`jdk.httpserver`
- 编译参数 `--release 11`：11/17/21 均可编译运行

## 本机实际使用的 JDK（可复现）

- 发行版：Eclipse Temurin OpenJDK 17.0.20.1+1 (x86_64 Linux, HotSpot)
- 下载入口（API 会重定向到当时最新 17 GA；本机拿到的版本如下）：
  https://api.adoptium.net/v3/binary/latest/17/ga/linux/x64/jdk/hotspot/normal/eclipse
- 文件名：OpenJDK17U-jdk_x64_linux_hotspot_17.0.20.1_1.tar.gz
- SHA-256：3808d1d15e3ec6bd5b84057fb5d84c33d8a1536a258146bcea2e603fc726e08e
- 本机解压位置：/home/admin/tools/jdk-17.0.20.1+1（仓库外，不入库）

`scripts/find-java.sh` 按 $JAVA_HOME -> /home/admin/tools/jdk-17* -> 系统 JDK -> PATH
的顺序定位 JDK；也可用 `JAVA_HOME=/path/to/jdk` 显式指定。
