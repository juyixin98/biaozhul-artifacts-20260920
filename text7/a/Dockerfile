# 多阶段构建：Maven 编译 -> JRE 运行
FROM maven:3.9-eclipse-temurin-21 AS build
WORKDIR /build
# 先拷 pom 利用依赖缓存层
COPY pom.xml .
RUN mvn -q -B dependency:go-offline
COPY src ./src
RUN mvn -q -B clean package -DskipTests

FROM eclipse-temurin:21-jre
WORKDIR /app
RUN groupadd --system app && useradd --system --gid app app
COPY --from=build /build/target/*.jar /app/app.jar
USER app
EXPOSE 8080
ENTRYPOINT ["java", "-jar", "/app/app.jar"]
