plugins {
    java
    // NOTE: bump to 4.1.x to match the `realtime-messaging` project once your
    // local toolchain has Spring Boot 4. 3.4.x is the conservative default here.
    id("org.springframework.boot") version "3.4.1"
    id("io.spring.dependency-management") version "1.1.6"
}

group = "com.portfolio"
version = "0.1.0-SNAPSHOT"

java {
    toolchain { languageVersion = JavaLanguageVersion.of(21) }
}

repositories { mavenCentral() }

val anthropicVersion: String by project
val elasticsearchClientVersion: String by project
val minioVersion: String by project
val testcontainersVersion: String by project

// Spring Boot's dependency management pins testcontainers (1.20.x on Boot 3.4); override the
// managed version — 1.21+ is required to talk to Docker Engine 29 daemons.
extra["testcontainers.version"] = testcontainersVersion

dependencies {
    // Web (WebFlux for clean SSE streaming) + actuator/metrics
    implementation("org.springframework.boot:spring-boot-starter-webflux")
    implementation("org.springframework.boot:spring-boot-starter-actuator")
    implementation("io.micrometer:micrometer-registry-prometheus")
    implementation("org.springframework.boot:spring-boot-starter-validation")

    // Data: Redis (semantic cache, locks), JPA/Postgres (metadata, cost ledger, eval), Kafka
    implementation("org.springframework.boot:spring-boot-starter-data-redis-reactive")
    implementation("org.springframework.boot:spring-boot-starter-data-jpa")
    runtimeOnly("org.postgresql:postgresql")
    implementation("org.springframework.kafka:spring-kafka")

    // Elasticsearch (BM25 + dense_vector kNN)
    implementation("co.elastic.clients:elasticsearch-java:$elasticsearchClientVersion")
    implementation("com.fasterxml.jackson.core:jackson-databind")

    // Object storage (raw documents)
    implementation("io.minio:minio:$minioVersion")

    // Claude
    implementation("com.anthropic:anthropic-java:$anthropicVersion")

    // Test
    testImplementation("org.springframework.boot:spring-boot-starter-test")
    testImplementation("io.projectreactor:reactor-test")
    testImplementation("org.springframework.kafka:spring-kafka-test")
    testImplementation(platform("org.testcontainers:testcontainers-bom:$testcontainersVersion"))
    testImplementation("org.testcontainers:junit-jupiter")
    testImplementation("org.testcontainers:elasticsearch")
    testImplementation("org.testcontainers:kafka")
    testImplementation("org.testcontainers:postgresql")
}

tasks.withType<Test> {
    useJUnitPlatform()
    // Print per-test outcomes so a silently-skipped integration test (e.g. Docker
    // detection failure) is visible in CI logs instead of passing as green.
    testLogging { events("passed", "skipped", "failed") }
}
