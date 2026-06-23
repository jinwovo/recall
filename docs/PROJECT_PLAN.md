# Recall — 프로젝트 기획서 (PROJECT PLAN)

> AI 하이브리드 검색·RAG QA 플랫폼. 포트폴리오/이직용 개인 프로젝트.
> 영문 요약은 루트 [`README.md`](../README.md) 참고.

## 1. 목표와 포지셔닝

`realtime-messaging`(분산·실시간·인프라)와 **겹치지 않는 축**을 채워 포트폴리오를
완성형으로 만든다: **AI/데이터 · 검색 · 트랜잭션/동시성 · 완성도 높은 프론트엔드.**

핵심 메시지: **"LLM을 백엔드 엔지니어링 문제로 풀었다."**
단순 API 래퍼가 아니라 **검색 정합성 · 지연 예산 · LLM 비용 · 근거(citation) · 평가**
를 설계하고 **숫자로 증명**한다.

### 타겟별 강조 포인트
| 타겟 | 어필 |
|---|---|
| 대기업 플랫폼 | 하이브리드 검색 정합성, p95 지연 예산, 부하테스트 실측, 옵저버빌리티 |
| 중견 상장사 | 제품 완성도 + 한국어 처리(Nori) + 도메인 적용성 |
| 외국계·글로벌 | 영문 README/ADR, 클린 아키텍처, 멀티링궐(KO/EN), k8s/IaC, 테스트 커버리지 |

## 2. 핵심 엔지니어링 챌린지 (= 셀링포인트)

각 항목은 README 챕터 + 정량 지표가 된다.

1. **하이브리드 검색** — BM25(Nori) + dense vector kNN + RRF 융합 + cross-encoder 리랭킹.
   → 단독 대비 Recall@10/MRR lift 측정.
2. **RAG + 근거 인용 + 환각 가드레일** — Claude native citations, groundedness 검사, "모름" fallback.
3. **LLM 비용·지연 최적화** — 모델 티어링 / 프롬프트 캐싱 / 시맨틱 캐시 / Batch API.
   → `$/query`, 캐시 적중률, 절감 % 박제.
4. **멀티링궐(KO/EN)** — 한국어 형태소 + 다국어 임베딩 정합.
5. **비동기 색인 파이프라인** — Kafka, 멱등성, 증분 업데이트, 동시 색인 race condition.
6. **평가 주도 개발** — eval harness(Recall@k/MRR/groundedness) + CI 회귀 게이트.

## 3. 기능 명세

### MVP
- 문서 수집(PDF/MD/HTML) → 청킹 → 임베딩 → 색인
- 하이브리드 검색 API (BM25 + 벡터 + RRF + 리랭킹)
- RAG QA: 질문 → 검색 → 답변 + 출처 인용 (SSE 스트리밍)
- 검색 UI + QA 챗 UI(소스 하이라이트) + 색인 상태 대시보드
- 옵저버빌리티(Prometheus/Grafana) + 토큰/비용 대시보드

### 확장 (차별화)
- 시맨틱 캐시 · 모델 티어링 · Batch 색인
- Eval 하베스트 + CI 회귀 게이트
- 멀티테넌시(워크스페이스 격리) · 권한
- k8s(Helm) 배포 · k6 부하테스트 리포트
- "모름" fallback · 질의 의도 분류 · 필터/패싯 검색

## 4. 기술 스택

| 레이어 | 선택 | 이유 |
|---|---|---|
| 백엔드 | Java 21 + Spring Boot 4.1 (WebFlux/SSE) | 기존 레포 일관성·깊이 |
| LLM | Claude (`claude-opus-4-8` 기본, Sonnet/Haiku 티어) · `com.anthropic:anthropic-java` | 티어링·캐싱·SSE·citations |
| 검색/벡터 | Elasticsearch (Nori + dense_vector kNN) | BM25+벡터 단일 스토어, 한국어 |
| 임베딩/리랭크 | bge-m3 + bge-reranker-v2-m3 (Python FastAPI 사이드카) | 멀티링궐, 자체호스팅=비용/깊이 |
| 메시징 | Kafka | 비동기 색인 (실구현) |
| 캐시/스토어 | Redis, PostgreSQL, MinIO/S3 | 시맨틱 캐시·메타/로그/비용·원본 |
| 프론트 | Next.js + TS + Tailwind + shadcn/ui | UI 완성도, SSE |
| 옵저버빌리티 | Micrometer + Prometheus + Grafana | 기존 레포와 일관 |
| 품질/배포 | Testcontainers, k6, GitHub Actions, Docker Compose → k8s(Helm) | 경력직 차별점 |

## 5. 정량 지표 (README에 박을 숫자)

- 검색 품질: Recall@10, MRR@10, nDCG@10 (하이브리드 vs BM25 vs vector 비교표)
- RAG 품질: groundedness/faithfulness %, citation coverage, 환각률
- 지연: e2e p50/p95, TTFT(SSE 첫 토큰), 검색 단계 지연
- 비용: `$/query`, 프롬프트 캐시 적중률, 티어링+캐싱 절감 % (Before/After)
- 처리량: 색인 docs/s, 지속 QPS, 시맨틱 캐시 적중률

## 6. 마일스톤 (주차별, 조정 가능)

| 주차 | 목표 | 산출물 |
|---|---|---|
| W1 | 부트스트랩·Docker Compose·ES/Redis/PG/Kafka·CI | 골격 + health check |
| W2 | 색인 파이프라인 (청킹·임베딩·Kafka 비동기) | 색인 동작 + 멱등성 |
| W3 | 하이브리드 검색 (BM25+벡터+RRF+리랭크) | 검색 API + 첫 Recall 수치 |
| W4 | RAG + native citations + SSE | QA API + 소스 인용 |
| W5 | 비용 최적화 (캐싱·티어링·시맨틱 캐시·Batch) | 비용 대시보드 + 절감 수치 |
| W6 | Eval harness + 옵저버빌리티 + k6 부하 | 지표 CI + Grafana + 부하 리포트 |
| W7 | 프론트 완성 (검색/QA/하이라이트/admin) | 데모 가능한 UI |
| W8 | 문서화(한·영 README, ADR) + k8s(Helm) + 데모 영상 | 제출 준비 |

## 7. README / 트러블슈팅 서사 (한·영 병기)

각 챕터 = "문제 → 시도 → 트레이드오프 → 측정 → 해결":
1. 하이브리드 융합 — 단독 recall 부족 → RRF·리랭킹 튜닝 (수치)
2. 한국어 처리 — Nori + 다국어 임베딩 정합, cross-lingual
3. 동시성/멱등성 — 동시 색인 race condition → 증분·dedup·분산락
4. LLM 비용 폭발 — 캐싱+티어링+시맨틱 캐시 (Before/After $)
5. 환각 — citations + groundedness 가드레일 + "모름" fallback
6. 스트리밍 — SSE TTFT 최적화
7. Eval 주도 — 측정·회귀 방지

## 8. 시드 코퍼스 (데모 도메인)

**선택: ⓐ 개발 기술문서** (Spring / AWS / Kubernetes 공식 문서).
리뷰어가 직접 질문 가능해 데모력 최고, EN+KO 혼합으로 멀티링궐 강조.
대안: ⓑ 금융 전자공시(DART) · ⓒ 사내 위키. 아키텍처는 도메인 무관 — 코퍼스만 교체.

## 9. 디렉터리 구조

```
recall/
├── backend/            Spring Boot (Java 21) — BFF, search, rag, ingestion, cache
├── embedding-service/  Python FastAPI — bge-m3 임베딩 + bge-reranker
├── frontend/           Next.js — 검색/QA UI, admin 대시보드
├── eval/               평가 하베스트 (Recall@k / MRR / groundedness)
├── load-test/          k6 시나리오
├── monitoring/         Prometheus / Grafana
├── infra/              ES(Nori) Dockerfile 등
├── docs/               기획·아키텍처·ADR
└── docker-compose.yml
```
