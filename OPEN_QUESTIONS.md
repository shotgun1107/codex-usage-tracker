# 미확인 사항과 검증 계획

상태: 조사 중

로그를 확인해야 답을 얻는 `검증 항목`과 사용자가 선택해야 하는 `설계 결정`을 분리한다.

## 우선 검증 항목

### Q-001. fork·resume·compact의 토큰 기준선

- 상태: 현재 CLI 버전에 대해 부분 해결
- 결과: resume는 같은 누적 카운터를 계속 사용하며 프로세스 재시작 후에도 초기화되지 않았다.
- 결과: fork는 부모 이력과 누적 체크포인트를 복사하고, 복사된 작업의 `turn_id`도 유지했다.
- 결과: fork 이후 새 작업만 새로운 `turn_id`를 사용했으며 부모 누적값은 변하지 않았다.
- 결과: compact는 누적값을 유지했지만 세부 항목이 0인 별도 `last_token_usage.total_tokens`를 기록했다.
- 남은 질문: compact의 불투명한 reported last가 실제 모델 사용량·계정 한도에 포함되는가?
- 완료 기준: compact overhead의 의미와 합산 정책을 정한다.

### Q-002. JSONL과 SQLite의 역할

- 상태: 현재 로컬 버전에 대해 부분 해결
- 결과: 631개 thread 모두 `SQLite threads.id = rollout UUID = session_meta.id`로 조인됐다.
- 결과: `session_meta.session_id`는 고유 thread ID가 아니라 계보 root다.
- 결과: JSONL은 상세 이벤트, SQLite는 인덱스·spawn-edge·최신 요약으로 역할이 나뉜다.
- 결과: lifecycle 통제 실험에서도 JSONL 마지막 누적값과 SQLite `tokens_used`가 일치했다.
- 결과: Git 안·밖과 숨김 백그라운드의 `codex exec`가 모두 `source=exec`로 기록되고 SQLite·JSONL이 연결됐다.
- 남은 질문: 현재 표본에 없는 interactive `cli`와 명시적 `appServer` source에서도 같은 규칙이 유지되는가?
- 완료 기준: 통제 실험에서 실행 종류별 조인과 누락 폴백을 확인한다.

### Q-003. 부모·자식 spawn-edge의 완전성

- 상태: 현재 로컬 subagent에 대해 부분 해결
- 결과: `subagent.thread_spawn` 310개가 spawn-edge child 310개와 정확히 일치했다.
- 결과: edge가 없는 `subagent.other` 175개도 `session_meta.session_id`로 존재하는 root에 연결됐다.
- 결과: 현재 edge에는 누락된 부모·자식, 다중 부모, 순환 관계가 없었다.
- 결과: fork는 spawn-edge가 없지만 JSONL 첫 `session_meta.forked_from_id`로 부모를 명시했다.
- 주의: 현재 통제 실험의 fork 자식 `session_id`는 부모 root가 아니라 자식 자신이므로 fork 연결에 사용하지 않는다.
- 결과: 프로젝트 안·밖의 통제 실험에서 `subagent.thread_spawn` 자식이 모두 직접 spawn-edge와 root `session_id`를 가졌다.
- 남은 질문: 재개된 spawn 자식과 `subagent.other`의 직접 관계가 버전별로 어떻게 달라지는가?
- 완료 기준: 통제 실험에서 실행 종류별 edge와 root 연결을 확인한다.

### Q-004. 첫 token_count 이벤트

- 상태: 현재 CLI 버전에 대해 해결
- 신규 thread: 첫 누적값과 첫 turn 사용량이 일치했다.
- resume thread: 새 기준선을 만들지 않고 기존 누적값을 이어갔다.
- fork thread: 첫 체크포인트는 부모의 복사된 기준선이므로 새 사용량이 아니다.
- 처리 규칙: token_count를 현재 `task_started.turn_id`에 연결하고, fork에서 복사된 동일 turn을 중복 제거한다.
- 회귀 조건: 다른 `cli_version`에서 구조가 바뀌면 다시 검증한다.

### Q-005. Git 메타데이터 누락

- 상태: 현재 CLI 버전에 대해 부분 해결
- 질문: repository URL이 없는 작업을 어떤 정보로 프로젝트에 연결할 수 있는가?
- 현재 관찰: Spike 1 시점 631개 thread 중 repository URL이 있는 thread는 376개였다.
- 현재 관찰: 자기·부모·root·단일 자식 저장소를 사용한 제안 규칙으로 386개를 자동 분류하고 245개는 미분류로 남았다.
- 현재 관찰: 부모와 자식 저장소가 다른 spawn-edge가 64개 있어 자기 Git을 우선해야 한다.
- Spike 3: Git 밖 `exec`는 Git 메타데이터가 없고, 그 부모가 만든 자식도 실제 프로젝트에서 작업했지만 기본 cwd와 Git 정보는 부모의 Git 밖 상태를 유지했다.
- Spike 3: 실제 프로젝트를 사용한 도구 호출의 `workdir`는 rollout에 남아 로컬 Git 판별에 사용할 수 있었다.
- Spike 4: `origin`이 없으면 remote 하나가 있어도 Codex URL은 비었지만, 로컬 Git 조회로 unique remote를 복구할 수 있었다.
- Spike 4: worktree는 원본 remote, submodule은 자기 remote, monorepo 하위 폴더는 루트 remote를 기록했다.
- Spike 4: remote 변경 전후 로그는 각각 당시 URL을 보존하므로 alias 연결이 필요하다.
- 남은 질문: remote 없는 저장소를 여러 기기에서 연결할 명시적 project marker를 둘 것인가?
- 완료 기준: 로컬 전용 저장소의 수동·명시적 연결 방식을 결정한다.

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

### Q-009. 한 turn에서 여러 저장소를 사용한 경우

- 질문: 한 turn이 둘 이상의 Git 저장소를 건드렸을 때 토큰을 어느 프로젝트에 귀속할 것인가?
- 현재 한계: token_count는 모델 응답 구간의 사용량이지 저장소별 사용량이 아니므로 정확한 분할 근거가 없다.
- 제안: 자동 분할하지 않고 `ambiguous_multi_repo`로 저장한 뒤 수동 지정한다.
- 완료 기준: 실제 다중 저장소 turn을 만들고 활동 workdir 집합과 토큰 이벤트 순서를 비교한다.

## 해결된 설계 결정

다음 항목은 2026-08-26 사용자 승인으로 확정됐다.

- 회사 정책이 허용할 때 정제 통계만 비공개 장부에 저장
- 공유 비밀키 HMAC 비식별화
- v1 수동 `collect → sync → report`
- CLI 표와 Markdown 보고서
- `turn_id + token ordinal` 멱등 키
- append-only revision과 supersede 정정
- turn 활동 Git이 부모 상속보다 우선
- 다중 저장소 turn은 ambiguous 처리
- monorepo는 기본 저장소 하나
- v1 장부 이벤트 삭제·롤업 없음

결정 상태는 [DECISIONS.md](DECISIONS.md)에서 관리한다.

## 다음 Spike 순서

1. ~~JSONL·SQLite source map과 조인 키 확인~~ — 기존 로그 기준 완료
2. ~~fork·resume·compact 토큰 기준선 실험~~ — 현재 CLI 버전 기준 완료, compact overhead 의미는 결정 대기
3. ~~CLI·백그라운드·오케스트레이션 귀속 통제 실험~~ — 완료, turn 활동 위치가 필요한 사례 확인
4. ~~Git 메타데이터 누락과 미분류 폴백 실험~~ — 완료, local-only 연결 방식은 결정 대기

## Shape & Spike 종료 점검

- JSONL·SQLite 역할과 조인 키: 확인
- lifecycle 토큰 중복 규칙: 확인, compact overhead 의미만 보류
- CLI·백그라운드·오케스트레이션 귀속: 확인
- Git 누락·worktree·submodule·monorepo 폴백: 확인
- 결정 확정: 완료
- PRD·스키마 명세: 완료
- 현재 단계: C4·ADR 설계 검토, Build 승인 대기
