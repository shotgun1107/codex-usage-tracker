# 자료조사 및 검증 기록

상태: 진행 중  
최근 갱신: 2026-08-26

조사 내용은 `공식 문서`, `로컬 확인`, `외부 자료`, `미검증 제보`로 구분한다.

## 공식 문서에서 확인한 내용

[Codex App Server 공식 문서](https://learn.chatgpt.com/docs/app-server)는 다음 기능을 제공한다.

- 활성 thread의 토큰 사용량 업데이트
- 계정 전체 토큰 활동과 일별 버킷
- 계정 한도 사용률, 한도 창 길이, 초기화 시각
- thread 시작·재개·fork·compact
- fork의 `sessionId`와 `forkedFromId`
- thread 계보 조회에 사용할 수 있는 부모·조상 개념

공식 문서는 로컬 rollout JSONL과 SQLite의 모든 내부 스키마를 안정된 계약으로 보장하지 않는다. 따라서 로컬 파서는 Codex 버전을 함께 기록하고 실제 데이터를 검증해야 한다.

## 초기 로컬 조사 (2026-08-25)

### JSONL rollout

- `session_meta`에 `cwd`, `source`, `thread_source`, `cli_version`, `git` 등이 존재한다.
- 현재 Git 하위 필드는 `repository_url`, `branch`, `commit_hash`다.
- 확인한 579개 파일 중:
  - Git 객체 존재: 439개
  - repository URL 존재: 341개
  - branch 존재: 336개
  - commit hash 존재: 350개
  - Git 객체 없음: 140개
- Git 정보가 항상 존재하지 않으므로 부모 계보, 로컬 매핑, 미분류 처리가 필요하다.
- 모델과 reasoning effort는 `turn_context`에서 확인됐다.
- 하나의 rollout에서 모델이 변경된 사례가 확인됐다.
- `cli_version`은 `session_meta`에서 확인됐다.

### token_count

한 표본 rollout에서:

- 토큰 이벤트 약 9,900개 확인
- `total_token_usage`는 누적 증가
- 같은 누적값이 반복된 이벤트 199개 확인
- 반복 이벤트에서 `last_token_usage`는 0이 아니었으므로 이를 단순 합산하면 과다 계산
- 누적값이 증가한 이벤트에서는 현재 누적값과 이전 누적값의 차이가 `last_token_usage`와 일치

따라서 기본 계산 후보는 다음과 같다.

```text
증가량 = 현재 누적 체크포인트 - 이전 누적 체크포인트
```

단, 첫 이벤트·fork·resume·compact의 기준선은 추가 검증이 필요하다.

compact가 포함된 작은 표본 10개에서는 누적값 감소가 발견되지 않았다. 그러나 모든 표본에서 첫 누적값과 `last_token_usage`가 달라 첫 이벤트를 그대로 증가량으로 취급할 수 없다는 문제가 확인됐다.

### SQLite state

현재 `state_5.sqlite`에서 다음 테이블을 확인했다.

- `threads`
- `thread_spawn_edges`
- `thread_sections`
- `thread_dynamic_tools`
- `projects`
- `project_roots`

`thread_spawn_edges`의 현재 열:

```text
parent_thread_id
child_thread_id
status
```

현재 스냅샷에서 thread 579개와 spawn-edge 273개가 있었고, edge가 가리키는 부모와 자식은 모두 `threads`에 존재했다.

현재 관찰을 기준으로 한 데이터 역할 후보:

```text
토큰·모델·Git 정보       → JSONL rollout
부모·자식 오케스트레이션 → SQLite state
```

## Spike 1: JSONL·SQLite 조인 검증 (2026-08-26)

기존 로그만 읽어 JSONL rollout과 `state_5.sqlite`의 식별자, 계보, Git 정보, 토큰 합계를 비교했다. 대화·코드·실제 경로 값은 수집하지 않았다.

### 식별자 조인

검사 시점의 thread 631개가 모두 다음 규칙으로 1:1 연결됐다.

```text
thread_id
= SQLite threads.id
= rollout 파일명의 UUID
= JSONL session_meta.id
```

- SQLite thread 631개 모두 대응 JSONL 존재
- JSONL 631개 모두 대응 SQLite thread 존재
- `threads.rollout_path` 631개 모두 실제 파일 존재
- `session_meta.id` 중복 없음
- `session_meta.session_id`는 thread ID가 아니라 계보 루트이므로 여러 thread에서 반복됨

따라서 thread 조인에는 `session_meta.id`를 사용하고, `session_meta.session_id`는 `root_session_id`로 보존해야 한다.

### 실행 종류

JSONL `session_meta.source`와 SQLite `threads.source`의 분류와 개수가 일치했다.

| source | 개수 |
|---|---:|
| `vscode` | 142 |
| `exec` | 4 |
| `subagent.thread_spawn` | 310 |
| `subagent.other` | 175 |

공식 App Server가 정의하는 `cli`, `appServer` 등은 현재 로컬 표본에 없으므로 별도 통제 실험이 필요하다.

### 오케스트레이션 계보

- `subagent.thread_spawn` 310개는 모두 `thread_spawn_edges`의 child와 일치
- 모든 spawn-edge의 부모·자식 thread가 SQLite에 존재
- 다중 부모와 순환 관계는 발견되지 않음
- 최대 직접 spawn 깊이는 2
- 모든 자식의 `session_meta.session_id`가 최상위 계보 root thread를 가리킴
- edge가 없는 `subagent.other` 175개도 `session_id`로 존재하는 계보 root에 연결됨

따라서 현재 버전에서는 다음 두 관계를 함께 사용한다.

```text
직접 부모  → SQLite thread_spawn_edges
계보 루트  → JSONL session_meta.session_id
```

### Git 정보와 프로젝트 귀속

SQLite와 JSONL 양쪽에 Git 저장소가 있는 376개 thread의 정규화 주소는 모두 일치했다. 255개 thread에는 양쪽 모두 저장소 주소가 없었다.

spawn-edge 310개의 부모·자식 Git 관계:

| 관계 | 개수 |
|---|---:|
| 부모·자식 저장소 동일 | 85 |
| 부모·자식 저장소 다름 | 64 |
| 자식만 저장소 있음 | 108 |
| 부모만 저장소 있음 | 4 |
| 양쪽 모두 없음 | 49 |

부모와 자식 저장소가 다른 경우가 64개이므로 부모 프로젝트를 무조건 상속하면 잘못 분류된다. 자식 자신의 Git 정보를 부모 상속보다 우선해야 한다.

Git 정보가 없는 오케스트레이터 중 자식들이 하나의 저장소만 가리켜 프로젝트를 역추론할 수 있는 사례가 확인됐다. 단, 자식들이 여러 저장소를 가리키면 자동 귀속하지 않아야 한다.

현재 데이터에서 제안 규칙으로 자동 분류 가능한 범위:

| 판별 근거 | thread 수 |
|---|---:|
| 자기 Git 정보 | 376 |
| 직접·간접 부모 Git 정보 | 6 |
| 계보 root Git 정보 | 3 |
| 단일 자식 저장소 역추론 | 1 |
| 미분류 | 245 |

Codex SQLite의 `projects`, `project_roots`는 비어 있었고 모든 `threads.project_id`도 비어 있었다. 현재 환경에서는 Codex UI 프로젝트 그룹을 프로젝트 판별 근거로 사용할 수 없다.

### 토큰 합계 비교

- JSONL token_count가 있는 rollout: 625개
- token_count 이벤트: 277,996개
- archived thread 286개: SQLite `tokens_used`와 JSONL 마지막 누적값 전부 일치
- active thread 336개: 전부 일치
- active thread 3개: 실행 중 스냅샷 차이로 불일치
- token_count가 없는 rollout: 6개
- `cache_write_input_tokens`는 일부 이벤트에 없으므로 nullable 필드가 필요

결론:

```text
JSONL → 세부 토큰 이벤트·모델·Git 정보의 상세 정본
SQLite → thread 인덱스·spawn 계보·최신 토큰 요약
```

SQLite `tokens_used`는 빠른 조회와 검증에는 유용하지만 입력·출력·캐시·추론 분해와 이벤트 이력이 없으므로 중앙 장부의 단독 원천으로 사용할 수 없다.

## Spike 2: thread lifecycle 토큰 기준선 실험 (2026-08-26)

Codex CLI `0.150.0-alpha.8`과 App Server를 사용해 `신규 turn → 프로세스 재시작 후 resume → fork → compact → compact 이후 turn` 순서로 통제 실험했다. 대화 내용은 기록하지 않고 토큰 체크포인트와 lifecycle 구조만 비교했다.

### resume

원본 thread의 누적 총 토큰은 다음과 같이 이어졌다.

```text
첫 turn 완료       14,376
resume 직후        14,376  (변화 없음)
resume turn 완료   28,782  (증가 14,406)
```

- resume는 같은 thread와 rollout 파일을 계속 사용했다.
- 프로세스를 다시 시작해도 누적 카운터는 초기화되지 않았다.
- SQLite `tokens_used`와 JSONL의 마지막 누적 총 토큰이 일치했다.

따라서 resume를 새 카운터 구간으로 취급하면 안 된다.

### fork

부모가 28,782 토큰인 시점에 fork했다.

```text
fork 생성 직후 자식 체크포인트   28,782  (부모 이력 복사)
fork의 새 turn 완료              46,153  (자식의 실제 증가 17,371)
부모 체크포인트                  28,782  (fork turn의 영향 없음)
```

- 자식 rollout 첫 `session_meta`에는 자식 thread ID와 `forked_from_id`가 기록됐다.
- 그 뒤에 부모 `session_meta`와 fork 시점까지의 부모 이력이 복사됐다.
- 복사된 부모 레코드 21개의 payload가 원본과 모두 동일했다.
- 복사된 두 작업은 부모와 같은 `turn_id`를 유지했고, fork 이후 작업만 새로운 `turn_id`를 가졌다.
- fork는 `thread_spawn_edges`에 spawn 자식으로 기록되지 않았다.

따라서 fork 파일의 첫 누적값을 새 사용량으로 더하면 부모 사용량을 중복 계산한다. 토큰 이벤트를 감싸는 `task_started.turn_id`와 turn 내부 이벤트 순번을 함께 사용하면 복사된 작업을 전역 중복 제거할 수 있다.

현재 버전에서는 자식의 `session_id`가 부모 root가 아니라 자식 자신이었다. 공식 App Server 문서의 session tree 설명과 차이가 있으므로 fork 관계는 `session_id`로 추론하지 않고 명시적인 `forked_from_id`를 사용해야 한다.

### compact

부모를 compact했을 때 누적 총 토큰은 28,782로 유지됐고, compact 뒤 첫 일반 turn에서 44,513으로 증가했다.

```text
compact 전 누적             28,782
compact 직후 누적           28,782  (일반 delta 0)
compact 직후 reported last   4,891  (세부 항목은 모두 0)
다음 일반 turn 누적         44,513  (증가 15,731)
```

- `compacted`, `context_compacted`, compact 전용 task가 기록됐다.
- compact 직후 `total_token_usage`는 반복됐으므로 누적 체크포인트 차이는 0이다.
- 동시에 `last_token_usage.total_tokens=4,891`이 기록됐지만 입력·출력·캐시·추론 세부값은 모두 0이었다.
- compact 뒤에도 누적 카운터는 감소하거나 초기화되지 않았다.

따라서 일반 프로젝트 토큰 delta는 누적 체크포인트 차이로 계산할 수 있다. 다만 4,891 토큰이 실제 compact 모델 사용량인지, 계정 한도에 반영되는지는 현재 로그만으로 확정할 수 없으므로 원본 값을 별도 보존하고 합산 정책은 보류한다.

### lifecycle 결론

```text
신규 일반 thread  → 첫 누적값이 첫 사용량
resume             → 기존 카운터를 그대로 계속 사용
fork               → 부모 이력은 복사본이며 중복 제거, 새 turn만 증가량 인정
compact            → 누적 차이는 0, 불투명한 reported last는 별도 보존
```

이 결과는 현재 CLI 버전의 통제 실험 결과다. 내부 JSONL 형식은 안정된 공개 계약이 아니므로 수집 이벤트에 `cli_version`과 `parser_version`을 함께 기록한다.

## 토큰 의미

```text
총 토큰 = 입력 토큰 + 출력 토큰
캐시 읽기 토큰 ⊂ 입력 토큰
캐시 쓰기 토큰 ⊂ 입력 관련 세부 지표
추론 출력 토큰 ⊂ 출력 토큰
```

캐시와 추론 토큰을 총합에 다시 더하지 않는다. 프로젝트 실제 토큰과 계정 한도 소모율은 서로 다른 지표로 취급한다.

## 선행 도구

| 도구 | 확인한 방향 | 우리 프로젝트와의 관계 |
|---|---|---|
| [CodexBar](https://github.com/steipete/CodexBar) | 여러 제공자의 사용량·한도·비용 표시 | 한도 표시 참고 |
| [codex-usage](https://github.com/hashmil/codex-usage) | rollout JSONL 기반 CLI 집계 | JSONL 파서 참고 |
| [token-tracker](https://github.com/JedIV/token-tracker) | JSONL을 SQLite로 정규화하고 웹 UI 제공 | 저장 구조 참고 |
| [codexometer](https://github.com/merefield/codexometer) | 로컬 토큰과 한도 추정 TUI | 한도 연구 참고 |
| [codex-usage-analyzer](https://github.com/klinki/codex-usage-analyzer) | JSONL·SQLite·그래프·한도 비교 | 분석 구조 참고 |
| [ccusage Codex](https://ccusage.com/guide/codex/) | Codex 일별·모델별 사용량 파싱 | 재사용 가능성 조사 필요 |

기존 도구와 비교한 현재 핵심 방향은 `Git 저장소별 그룹핑`, `여러 기기 통합`, `오케스트레이션 자식 작업 포함`이다.

## 조사 자료로만 보관할 항목

다음 자료는 구현 근거로 확정하지 않고 검증 질문의 단서로만 사용한다.

- Codex 로그 대용량 사례
- active/archived 중복 가능성
- 오래된 버전의 token_count 지원 시작 시점
- 버전별 JSONL 직렬화 차이
- 고아 자식 thread 발생 가능성

검증 계획은 [OPEN_QUESTIONS.md](OPEN_QUESTIONS.md)에 기록한다.
