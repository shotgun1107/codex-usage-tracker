# ADR-006: 재생성 가능한 SQLite read model

상태: 승인됨

날짜: 2026-08-26

## 상황

append-only JSONL은 공유와 감사에는 적합하지만 필터·그룹·누적 조회에는 비효율적이다. SQLite를 직접 공유하면 merge할 수 없다.

## 결정

- JSONL 장부를 사용량 통계의 공유 정본으로 둔다.
- 각 기기에 폐기·재생성 가능한 SQLite read model을 둔다.
- replay는 latest revision, voided, alias, manual assignment를 결정적으로 적용한다.
- source cursor와 outbox 같은 operational state는 같은 SQLite 파일에서 별도 테이블로 관리한다.
- 장부 clone만으로 usage·mapping·quota 조회 데이터를 재생성할 수 있어야 한다.

## 대안

- 매 조회마다 JSONL 전체 스캔: 구현은 단순하지만 반복 조회와 누적 계산이 느리다.
- SQLite를 중앙 정본으로 공유: Git merge와 동시 쓰기에 맞지 않는다.
- 별도 서버 DB: v1의 serverless 목표를 벗어난다.

## 결과

- 빠른 조회와 장부 복구성을 함께 얻는다.
- replay migration과 schema version 관리가 필요하다.
- operational state 자체는 장부만으로 완전히 복구되지 않을 수 있다.
