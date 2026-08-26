# ADR-005: turn 단위 프로젝트 귀속과 lifecycle 중복 제거

상태: 제안됨

날짜: 2026-08-26

## 상황

프로젝트 밖 오케스트레이터의 자식은 실제 Git 프로젝트에서 도구를 실행해도 session Git 정보가 비어 있을 수 있다. fork는 부모 이력과 token checkpoint를 복사한다.

## 결정

- usage event의 프로젝트는 thread 전체가 아니라 turn별로 판별한다.
- 우선순위는 `수동 → 단일 활동 Git → 자기 Git → 부모·root → 자식 합의 → local mapping`이다.
- 둘 이상의 활동 remote는 `ambiguous_multi_repo`로 남긴다.
- 기본 logical token key는 HMAC 처리한 `turn_id + token ordinal`이다.
- fork 복사 prefix는 부모 원본 thread에 귀속하고 같은 logical key로 중복 제거한다.
- resume는 같은 누적 counter를 이어간다.
- compact의 불투명 reported last는 별도 보존하고 일반 합계에서 제외한다.
- 누적값 감소는 추정하지 않고 quarantine한다.

## 대안

- thread의 session Git만 사용: 프로젝트 밖 오케스트레이션을 놓친다.
- 부모 프로젝트 무조건 상속: 자식 Git이 다른 실제 사례를 오분류한다.
- `last_token_usage` 합산: 반복 checkpoint와 compact에서 과다 계산한다.
- fork 자식 첫 cumulative 합산: 부모 이력을 이중 계산한다.

## 결과

- 실제 멀티에이전트 사용 흐름에 가까운 집계가 가능하다.
- turn activity 분석과 fork prefix 판별로 parser가 복잡해진다.
- 한 turn의 여러 프로젝트 토큰은 자동 분할하지 못한다.
