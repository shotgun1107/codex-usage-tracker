# 미확인 사항과 검증 계획

상태: 조사 중

로그를 확인해야 답을 얻는 `검증 항목`과 사용자가 선택해야 하는 `설계 결정`을 분리한다.

## 우선 검증 항목

### Q-001. fork·resume·compact의 토큰 기준선

- 질문: 첫 토큰 이벤트와 fork·resume·compact 이후 누적값은 기존 사용량을 포함하는가?
- 위험: 부모 이력이나 첫 기준선을 실제 사용량으로 잘못 더할 수 있다.
- 검증:
  1. 짧은 기준 thread를 만든다.
  2. 토큰 체크포인트를 기록한다.
  3. resume·fork·compact를 각각 수행한다.
  4. 원본과 파생 thread의 첫 체크포인트와 증가량을 비교한다.
- 완료 기준: 각 lifecycle별 기준선·리셋·중복 제외 규칙을 수치로 설명할 수 있다.

### Q-002. JSONL과 SQLite의 역할

- 상태: 현재 로컬 버전에 대해 부분 해결
- 결과: 631개 thread 모두 `SQLite threads.id = rollout UUID = session_meta.id`로 조인됐다.
- 결과: `session_meta.session_id`는 고유 thread ID가 아니라 계보 root다.
- 결과: JSONL은 상세 이벤트, SQLite는 인덱스·spawn-edge·최신 요약으로 역할이 나뉜다.
- 남은 질문: 현재 표본에 없는 `cli`, `appServer`, 별도 백그라운드 실행에서도 같은 규칙이 유지되는가?
- 완료 기준: 통제 실험에서 실행 종류별 조인과 누락 폴백을 확인한다.

### Q-003. 부모·자식 spawn-edge의 완전성

- 상태: 현재 로컬 subagent에 대해 부분 해결
- 결과: `subagent.thread_spawn` 310개가 spawn-edge child 310개와 정확히 일치했다.
- 결과: edge가 없는 `subagent.other` 175개도 `session_meta.session_id`로 존재하는 root에 연결됐다.
- 결과: 현재 edge에는 누락된 부모·자식, 다중 부모, 순환 관계가 없었다.
- 남은 질문: CLI·백그라운드·fork·재개된 자식의 직접 관계도 같은 방식으로 남는가?
- 완료 기준: 통제 실험에서 실행 종류별 edge와 root 연결을 확인한다.

### Q-004. 첫 token_count 이벤트

- 질문: 첫 이벤트는 실제 첫 사용량인가, 이전 누적 상태를 담은 기준선인가?
- 현재 관찰: compact 표본에서 첫 누적값과 `last_token_usage`가 달랐다.
- 검증: 새 thread와 기존 thread에서 첫 이벤트를 비교한다.
- 완료 기준: 신규·기존·fork thread별 첫 이벤트 처리 규칙을 정의한다.

### Q-005. Git 메타데이터 누락

- 질문: repository URL이 없는 작업을 어떤 정보로 프로젝트에 연결할 수 있는가?
- 현재 관찰: Spike 1 시점 631개 thread 중 repository URL이 있는 thread는 376개였다.
- 현재 관찰: 자기·부모·root·단일 자식 저장소를 사용한 제안 규칙으로 386개를 자동 분류하고 245개는 미분류로 남았다.
- 현재 관찰: 부모와 자식 저장소가 다른 spawn-edge가 64개 있어 자기 Git을 우선해야 한다.
- 검증: Git 밖, remote 없는 repo, worktree, submodule, 프로젝트 밖 오케스트레이션을 비교한다.
- 완료 기준: 자동 판별 우선순위와 미분류 조건을 정의한다.

### Q-006. 캐시 read/write와 토큰 필드의 버전 차이

- 질문: 버전별로 캐시 쓰기와 읽기 필드가 어떻게 기록되는가?
- 검증: 서로 다른 `cli_version`의 token_count 필드 집합을 비교한다.
- 완료 기준: 버전별 파싱 규칙과 누락값 의미를 정의한다.

### Q-007. sessions와 archived_sessions의 중복

- 현재 관찰: 한 시점의 로컬 검사에서는 같은 session ID 중복이 없었다.
- 질문: archive·unarchive 도중 또는 버전별로 중복 파일이 생길 수 있는가?
- 검증: archive·unarchive 전후 파일과 session ID를 비교한다.
- 완료 기준: 파일 위치와 무관한 dedup 규칙을 정의한다.

### Q-008. 기존 도구 재사용

- 질문: ccusage 또는 다른 오픈소스 파서를 재사용할 수 있는가?
- 확인 내용: 라이선스, 지원 필드, 버전 호환, 증분 읽기, fork 처리.
- 완료 기준: 재사용·부분 차용·자체 구현 중 하나를 근거와 함께 선택한다.

## 설계 결정 대기

다음은 로그 검증이 아니라 사용자와 프로젝트가 정할 사항이다.

- 회사 데이터의 GitHub 저장 정책
- remote·branch·경로의 비식별화 방식
- 자동 수집 주기와 실행 방식
- v1 기본 조회 인터페이스
- 멱등 키 구성
- 이벤트 정정과 supersede 방식
- 부모 상속과 자식 Git 정보가 충돌할 때의 우선순위
- monorepo 분류 단위
- 장부 보존·롤업 정책

결정 상태는 [DECISIONS.md](DECISIONS.md)에서 관리한다.

## 다음 Spike 순서

1. ~~JSONL·SQLite source map과 조인 키 확인~~ — 기존 로그 기준 완료
2. fork·resume·compact 토큰 기준선 실험
3. CLI·백그라운드·오케스트레이션 귀속 통제 실험
4. Git 메타데이터 누락과 미분류 폴백 실험
