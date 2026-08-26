# 데이터 스키마 초안

상태: Spike 2 반영 초안
스키마 버전: 미정

이 문서는 원천 데이터와 GitHub 중앙 장부의 구조를 정의한다. 미확인 필드는 구현 전에 Spike로 검증한다.

## 데이터 계층

```text
Codex JSONL + Codex SQLite
        ↓ 수집·정제
기기별 append-only 이벤트 장부
        ↓ 집계
로컬 SQLite 캐시·CLI·Markdown·UI
```

## 원천 데이터

### JSONL rollout

현재 필요한 레코드:

- `session_meta.id`: SQLite와 조인하는 논리 thread ID
- `session_meta.session_id`: 현재 session tree의 root thread ID
- `session_meta.forked_from_id`: fork의 직접 원본 thread ID
- `session_meta`: source, cwd, Codex 버전, Git 정보
- `turn_context`: turn, cwd, model, reasoning effort
- `event_msg.token_count`: 누적 토큰 체크포인트
- lifecycle·collaboration 레코드: fork·compact·부모 관계 보조 정보

현재 Git 정보:

```text
git.repository_url
git.branch
git.commit_hash
```

### SQLite state

현재 필요한 테이블 후보:

```text
threads
thread_spawn_edges(parent_thread_id, child_thread_id, status)
projects
project_roots
```

SQLite 내부 스키마는 Codex 버전에 따라 변할 수 있으므로 직접 장부로 공유하지 않고 읽기 어댑터를 둔다.

### 검증된 조인 규칙

```text
thread_id
= SQLite threads.id
= rollout 파일명의 UUID
= JSONL session_meta.id

root_session_id
= JSONL session_meta.session_id

parent_thread_id
= SQLite thread_spawn_edges.parent_thread_id
```

Spike 1에서 631개 thread가 모두 위 규칙으로 연결됐다. `session_id`는 여러 자식이 공유하므로 thread의 고유 키로 사용하지 않는다.

현재 소스 역할:

```text
JSONL  → 세부 토큰 체크포인트, turn별 모델·effort, Git 정보
SQLite → thread 목록, rollout 위치, 직접 spawn-edge, 최신 tokens_used 요약
```

## 중앙 장부 레코드

### usage_event

각 줄은 대화 내용이 제거된 토큰 체크포인트 하나다.

```json
{
  "schema_version": "draft",
  "source_event_id": "TBD",
  "revision": 1,
  "parser_version": "TBD",
  "device_id": "uuid",
  "project_id": "opaque-id-or-null",
  "project_resolution": "self_git|parent|manual|unclassified",
  "thread_id": "thread-id",
  "session_id": "session-id-or-null",
  "root_session_id": "root-id-or-null",
  "parent_thread_id": "parent-id-or-null",
  "forked_from_thread_id": "fork-source-id-or-null",
  "turn_id": "turn-id-or-null",
  "token_event_ordinal": 0,
  "operation": "turn|compact|unknown",
  "occurred_at": "UTC timestamp",
  "model": "model-id-or-null",
  "reasoning_effort": "effort-or-null",
  "cli_version": "version-or-null",
  "cumulative": {
    "input_tokens": 0,
    "cached_input_tokens": 0,
    "cache_write_input_tokens": 0,
    "output_tokens": 0,
    "reasoning_output_tokens": 0,
    "total_tokens": 0
  },
  "delta": {
    "input_tokens": 0,
    "cached_input_tokens": 0,
    "cache_write_input_tokens": 0,
    "output_tokens": 0,
    "reasoning_output_tokens": 0,
    "total_tokens": 0
  },
  "reported_last": {
    "input_tokens": 0,
    "cached_input_tokens": 0,
    "cache_write_input_tokens": 0,
    "output_tokens": 0,
    "reasoning_output_tokens": 0,
    "total_tokens": 0
  },
  "source": {
    "kind": "vscode|cli|exec|appServer|subAgent|subAgentReview|subAgentCompact|subAgentThreadSpawn|subAgentOther|unknown",
    "rollout_fingerprint": "opaque-id",
    "record_position": "opaque-cursor"
  }
}
```

`null`은 정보가 없음을 의미한다. 필드가 없는 구버전과 숫자 0을 구분해야 한다.

`cache_write_input_tokens`는 현재 로그 일부에서 누락되므로 필수 숫자가 아니라 nullable 버전별 필드로 취급한다.

`reported_last`는 Codex가 기록한 `last_token_usage`의 원형이다. 일반 turn에서는 delta 검증에 사용하고, compact처럼 누적 차이와 의미가 다른 경우에도 삭제하지 않고 보존한다.

### mapping_event

프로젝트 별칭, 수동 연결, 저장소 이전, 기기 표시 이름도 append-only 이벤트로 기록한다.

```json
{
  "schema_version": "draft",
  "mapping_event_id": "TBD",
  "device_id": "uuid",
  "occurred_at": "UTC timestamp",
  "kind": "project_alias|manual_assignment|device_name|supersede",
  "subject_id": "opaque-id",
  "value": "privacy-reviewed-value",
  "supersedes": "older-event-id-or-null"
}
```

공용 파일을 직접 수정하지 않고 모든 기기의 매핑 이벤트를 읽어 최종 상태를 계산한다.

## GitHub 저장 구조 후보

```text
ledger/
└─ devices/
   └─ <device-uuid>/
      ├─ usage/<year>/<month>/<day>.jsonl
      └─ mappings/<year>/<month>.jsonl
```

- 기기 하나는 자신의 디렉터리만 쓴다.
- 한 파일 안에는 일일 합계가 아니라 여러 이벤트가 들어간다.
- 조회용 SQLite는 Git에 올리지 않고 장부에서 재생성한다.

## 계산 규칙 초안

```text
증가량 = 현재 누적 체크포인트 - 같은 카운터 구간의 이전 체크포인트
```

- 동일 누적값 반복: 증가량 0
- 계산한 입력 + 출력과 total이 다르면 경고
- `last_token_usage`는 증가량 검증에 사용
- 신규 일반 thread의 첫 누적값: 첫 사용량으로 처리
- resume: 이전 체크포인트를 이어서 delta 계산
- token_count: 가장 가까운 미완료 `task_started.turn_id`에 연결
- fork: 복사된 부모 turn의 체크포인트는 중복 사용량으로 제외
- compact: 누적값이 같으면 일반 delta 0, 불투명한 `reported_last`는 별도 보존
- 누적값 감소와 compact overhead 합산은 추가 검증·결정 필요
- 날짜 귀속은 이벤트 종료 시각을 기준으로 하는 근사임을 표시

fork 통제 실험에서 복사된 부모 작업은 원본과 같은 `turn_id`와 같은 token payload 순서를 유지했다. 따라서 현재 멱등 키 후보는 다음과 같다.

```text
source_event_id = hash(turn_id + token_event_ordinal)
```

이 규칙은 fork의 복사 이력을 제거할 수 있지만, 여러 기기와 구버전 로그에서도 turn ID가 안정적으로 유지되는지 확인한 뒤 확정한다.

## 프로젝트 귀속 규칙 제안

```text
수동 지정
→ 현재 작업의 Git repository
→ 가장 가까운 분류된 부모 작업
→ session tree root의 프로젝트
→ Git 없는 오케스트레이터의 분류된 자식들이 정확히 하나의 repository만 가리킬 때 해당 repository
→ 로컬 매핑
→ 미분류
```

Spike 1에서 부모와 자식의 Git 저장소가 다른 edge 64개가 확인됐다. 따라서 자기 Git 정보를 부모 상속보다 우선하는 방향은 근거가 생겼지만 최종 정책은 사용자 승인 전이다. 자식 저장소 역추론도 자식들이 여러 저장소를 가리키면 적용하지 않는다.

## 멱등과 정정

미확정 사항:

- `source_event_id` 구성
- 원천 파일 재작성 시 안정적인 위치 식별
- parser_version 변경 후 재수집 정책
- 기존 이벤트를 무효화하거나 대체하는 `supersedes` 규칙

상세 결정은 [DECISIONS.md](DECISIONS.md), 검증 계획은 [OPEN_QUESTIONS.md](OPEN_QUESTIONS.md)를 따른다.

## 개인정보 경계

GitHub 장부에 저장하지 않는 값:

- 프롬프트와 응답
- 코드와 명령 출력
- 인증 정보
- 전체 로컬 경로 원문
- 검토되지 않은 remote·branch·프로젝트명

remote·branch·경로는 원문, HMAC, 별도 ID 중 어떤 형태로 저장할지 결정한 뒤 스키마를 확정한다.
