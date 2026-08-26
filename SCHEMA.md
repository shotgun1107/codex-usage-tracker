# 중앙 장부 스키마 v1

상태: 승인됨

스키마 버전: `1`

기계 검증 파일: [`schemas/ledger-event-v1.schema.json`](schemas/ledger-event-v1.schema.json)

## 목적

Codex 원본 JSONL과 SQLite에서 대화 내용을 제거한 사용량 이벤트를 만들고, 여러 기기가 비공개 Git 장부에서 충돌 없이 공유할 수 있도록 정의한다.

```text
Codex JSONL + SQLite
        ↓ 로컬 수집·HMAC 비식별화
기기별 append-only JSONL 장부
        ↓ replay
로컬 SQLite 조회 DB
        ↓
CLI 표·Markdown 보고서
```

## 원천 데이터 역할

### JSONL rollout

- `session_meta.id`: thread 원본 ID
- `session_meta.session_id`: 계보 root 후보
- `session_meta.forked_from_id`: fork 원본 thread
- `session_meta.source`, `cwd`, `cli_version`, `git`
- `turn_context`: turn ID, cwd, model, reasoning effort
- `event_msg.token_count`: 누적 토큰 체크포인트
- `task_started`, `task_complete`, compact lifecycle
- 도구 호출의 실행 `workdir`

### SQLite state

- `threads`: thread 인덱스, rollout 위치, 최신 토큰 요약
- `thread_spawn_edges`: 직접 부모·자식 관계

검증된 조인:

```text
SQLite threads.id
= rollout 파일 UUID
= 첫 session_meta.id
```

SQLite 파일 자체는 장부에 공유하지 않는다.

## 비식별화

모든 기기는 같은 256-bit 이상의 공유 비밀키 `K`를 사용한다. 키는 Git에 저장하지 않는다.

```text
project_id       = HMAC-SHA-256(K, "project:v1:" + normalized_remote)
thread_key       = HMAC-SHA-256(K, "thread:v1:" + raw_thread_id)
turn_key         = HMAC-SHA-256(K, "turn:v1:" + raw_turn_id)
source_event_id  = HMAC-SHA-256(K, "usage:v1:" + raw_turn_id + ":" + ordinal)
key_id           = HMAC-SHA-256(K, "key-id:v1")의 짧은 비가역 표현
```

출력은 타입 접두사와 base64url 또는 hex digest를 사용한다. 예: `prj_h1_...`, `thr_h1_...`.

중앙 장부에 저장하지 않는 값:

- 프롬프트·응답
- 코드·명령·명령 출력
- 인증정보와 HMAC 키
- raw thread·turn ID
- 전체 로컬 경로
- raw remote URL·branch·commit
- 검토되지 않은 프로젝트명

원문 경로와 remote는 로컬 판별 중에만 사용한다.

## Git remote 정규화

1. HTTPS, `ssh://`, scp형 SSH 주소를 `host/path` 형태로 변환한다.
2. 사용자정보·query·fragment와 기본 포트를 제거한다.
3. host를 소문자로 바꾼다.
4. 끝의 `/`와 `.git`을 제거한다.
5. `github.com`의 owner/repository 경로는 소문자로 바꾼다.
6. 다른 host의 path 대소문자는 보존한다.
7. 로컬 파일 remote는 cross-device project ID로 자동 사용하지 않는다.

`origin`이 없으면 로컬 remote가 정확히 하나일 때만 그 URL을 사용한다. 두 개 이상이면 `ambiguous_remote`, 하나도 없으면 local mapping 또는 `unclassified`다.

## usage event

장부 한 줄은 token checkpoint 하나다.

```json
{
  "schema_version": 1,
  "event_type": "usage_checkpoint",
  "event_id": "evt_h1_opaque",
  "source_event_id": "src_h1_opaque",
  "revision": 1,
  "supersedes": null,
  "voided": false,
  "parser_version": "0.1.0",
  "device_id": "00000000-0000-4000-8000-000000000001",
  "key_id": "key_h1_opaque",
  "project_id": "prj_h1_opaque-or-null",
  "project_resolution": "activity_git",
  "activity_repository_count": 1,
  "thread_key": "thr_h1_opaque",
  "root_thread_key": "thr_h1_opaque-or-null",
  "parent_thread_key": "thr_h1_opaque-or-null",
  "forked_from_thread_key": "thr_h1_opaque-or-null",
  "turn_key": "turn_h1_opaque-or-null",
  "token_event_ordinal": 0,
  "operation": "turn",
  "occurred_at": "2026-08-26T05:00:00Z",
  "model": "gpt-model-id-or-null",
  "reasoning_effort": "low-or-null",
  "source_kind": "vscode",
  "cli_version": "0.150.0-alpha.8",
  "cumulative": {
    "input_tokens": 0,
    "cached_input_tokens": 0,
    "cache_write_input_tokens": null,
    "output_tokens": 0,
    "reasoning_output_tokens": 0,
    "total_tokens": 0
  },
  "delta": {
    "input_tokens": 0,
    "cached_input_tokens": 0,
    "cache_write_input_tokens": null,
    "output_tokens": 0,
    "reasoning_output_tokens": 0,
    "total_tokens": 0
  },
  "reported_last": {
    "input_tokens": 0,
    "cached_input_tokens": 0,
    "cache_write_input_tokens": null,
    "output_tokens": 0,
    "reasoning_output_tokens": 0,
    "total_tokens": 0
  },
  "flags": []
}
```

### 필드 규칙

- `event_id`: `source_event_id + revision + canonical payload`의 HMAC
- `source_event_id`: 같은 원천 checkpoint의 logical key
- `revision`: 1부터 증가
- `supersedes`: 직전 revision의 `event_id`
- `voided=true`: 기존 logical event를 합계에서 제외하는 정정
- `null`: 해당 Codex 버전에서 정보가 없음을 의미하며 숫자 0과 다르다.
- `occurred_at`: UTC 저장, 보고서에서 기본 Asia/Seoul 변환
- `source_kind`: `cli|vscode|exec|appServer|subAgent|subAgentReview|subAgentCompact|subAgentThreadSpawn|subAgentOther|unknown`
- `operation`: `turn|compact|unknown`

`project_resolution` 값:

```text
manual
activity_git
self_origin
unique_remote
ancestor
root
descendant_consensus
local_mapping
ambiguous_remote
ambiguous_multi_repo
unclassified
```

## token 계산

```text
delta = 현재 cumulative - 같은 counter 구간의 이전 cumulative
```

- 첫 일반 thread checkpoint: cumulative 전체를 첫 delta로 사용
- 동일 cumulative 반복: delta 0
- resume: 같은 counter를 계속 사용
- fork: 복사된 동일 `turn_key + ordinal` 이벤트를 중복 제거하고 fork 이후 checkpoint만 계산
- fork의 복사 prefix는 부모 원본 thread key로 귀속해 같은 logical event의 내용도 일치시킨다.
- compact: cumulative가 같으면 delta 0, 불투명한 `reported_last`는 보존하지만 합계 제외
- negative delta: `counter_regression` flag, delta를 null로 두고 합계 제외 후 진단
- `input + output = total`이 아니면 `token_total_mismatch` flag
- cached input은 input의 부분집합이고 reasoning output은 output의 부분집합이므로 total에 다시 더하지 않음

현재 버전에서 token checkpoint는 진행 중인 `task_started.turn_id`에 연결하고 turn 안에서 0부터 ordinal을 부여한다.

turn ID가 없는 구버전은 다음 fallback을 사용한다.

```text
HMAC(K, "usage-fallback:v1:" + raw_thread_id + record_kind + stable_record_ordinal + payload_digest)
```

이 경우 `weak_dedupe_key` flag를 추가하고 fork 복사 이력은 별도 prefix 비교로 제외한다.

현재 정의된 진단 flag:

- `weak_dedupe_key`: turn ID가 없는 구버전 fallback
- `counter_regression`: 누적 total 감소로 delta 격리
- `component_regression:<field>`: 세부 counter만 감소
- `token_total_mismatch`, `delta_total_mismatch`: input + output과 total 불일치
- `reported_last_mismatch`: 양수 delta와 reported last 불일치
- `compact_reported_last_excluded`: compact 불투명 사용량을 합계에서 제외
- `metadata_conflict:model`, `metadata_conflict:reasoning_effort`: fork 복사 metadata 충돌

## 프로젝트 귀속

각 usage event에 다음 순서를 적용한다.

```text
manual assignment
→ turn 도구 workdir의 normalized remote가 정확히 하나
→ thread session_meta의 origin
→ cwd의 유일한 non-origin remote
→ 가장 가까운 분류된 부모
→ root 프로젝트
→ 자식 project ID 단일 합의
→ local mapping
→ ambiguous 또는 unclassified
```

- 부모와 자식 Git이 다르면 자식의 활동·자기 Git이 우선한다.
- worktree는 같은 normalized remote면 같은 project ID다.
- submodule은 자기 remote를 우선한다.
- monorepo는 기본적으로 remote 하나의 project ID를 사용한다.
- 한 turn의 활동 remote가 둘 이상이면 `ambiguous_multi_repo`다.
- remote-less 저장소의 다기기 통합은 manual mapping으로 같은 project ID를 지정한다.

## mapping event

```json
{
  "schema_version": 1,
  "event_type": "mapping",
  "event_id": "map_h1_opaque",
  "revision": 1,
  "supersedes": null,
  "device_id": "00000000-0000-4000-8000-000000000001",
  "key_id": "key_h1_opaque",
  "occurred_at": "2026-08-26T05:00:00Z",
  "kind": "manual_assignment",
  "subject_type": "thread",
  "subject_id": "thr_h1_opaque",
  "target_project_id": "prj_h1_opaque",
  "display_value": null
}
```

`kind`:

- `manual_assignment`: thread 또는 turn을 프로젝트에 연결
- `project_alias`: 이전 project ID를 현재 project ID에 연결
- `local_repo_link`: local-only repository key를 project ID에 연결
- `project_name`: 사용자가 승인한 프로젝트 표시명
- `device_name`: 사용자가 선택한 기기 표시명

`project_alias`는 방향 그래프로 적용한다. cycle이 생기면 장부 replay를 중단하지 않고 해당 alias를 무효 처리하고 진단한다.

## quota snapshot

부가 기능이며 Codex가 직접 제공한 값만 기록한다.

```json
{
  "schema_version": 1,
  "event_type": "quota_snapshot",
  "event_id": "quota_h1_opaque",
  "device_id": "00000000-0000-4000-8000-000000000001",
  "key_id": "key_h1_opaque",
  "occurred_at": "2026-08-26T05:00:00Z",
  "scope_key": "account_h1_opaque-or-null",
  "window_minutes": 300,
  "used_percent": 14.0,
  "remaining_percent": 86.0,
  "reset_at": "2026-08-26T10:20:00Z"
}
```

quota snapshot은 프로젝트 token 합계 계산에 사용하지 않는다.

장부 replay 전에 모든 event의 `key_id`가 현재 로컬 key ID와 같은지 검사한다. 하나라도 다르면 서로 다른 익명 ID 공간이 섞인 것이므로 replay와 sync를 중단한다.

## Git 장부 구조

```text
ledger/
└─ devices/
   └─ <device-uuid>/
      ├─ usage/<year>/<month>/<day>.jsonl
      ├─ mappings/<year>/<month>.jsonl
      └─ quota/<year>/<month>/<day>.jsonl
```

- 기기는 자기 디렉터리만 쓴다.
- 파일 날짜는 이벤트 UTC 날짜다.
- 한 줄은 완전한 JSON object이며 부분 line은 무시하고 다음 수집에서 재시도한다.
- 장부 파일은 append-only다.
- v1에서는 삭제·압축·롤업하지 않는다.
- 정렬 순서는 신뢰하지 않고 replay 시 `occurred_at`, logical key, revision을 사용한다.

## 로컬 상태 DB

Git에 올리지 않는 SQLite 캐시의 최소 테이블:

```text
source_cursors
usage_events
mapping_events
project_aliases
quota_snapshots
parser_issues
sync_runs
```

`source_cursors`는 rollout fingerprint, 마지막 byte offset, 마지막 완전 line digest를 저장한다. 원천 파일이 바뀌면 안전하게 앞 구간을 재검사하되 `source_event_id`로 중복을 막는다.

## correction과 replay

1. 모든 장부 line을 읽는다.
2. schema version을 검사한다.
3. 같은 logical key에서 가장 높은 유효 revision을 고른다.
4. `voided=true`를 제외한다.
5. manual mapping과 project alias를 적용한다.
6. usage delta를 로컬 SQLite에 적재한다.
7. 날짜·프로젝트·모델·기기 집계를 생성한다.

동일 revision의 내용이 서로 다르면 `revision_conflict`로 기록하고 합계를 확정하지 않는다.

## 스키마 검증 조건

- JSON Schema로 모든 event line을 검증한다.
- 금지 필드와 경로·remote URL 패턴을 장부 커밋 전에 검사한다.
- 합성 fixture로 신규·resume·fork·compact·worktree·submodule·monorepo·remote 변경을 재현한다.
- 장부를 세 번 replay해도 결과가 동일해야 한다.
- 다른 기기 파일 순서로 읽어도 결과가 동일해야 한다.
