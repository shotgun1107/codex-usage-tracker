# 데이터 스키마 초안

상태: 검증 전 초안  
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

- `session_meta`: session/thread 식별, source, cwd, Codex 버전, Git 정보
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
  "turn_id": "turn-id-or-null",
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
  "source": {
    "kind": "desktop|cli|background|child|unknown",
    "rollout_fingerprint": "opaque-id",
    "record_position": "opaque-cursor"
  }
}
```

`null`은 정보가 없음을 의미한다. 필드가 없는 구버전과 숫자 0을 구분해야 한다.

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
- 첫 이벤트, 누적값 감소, fork·resume·compact는 검증 후 확정
- 날짜 귀속은 이벤트 종료 시각을 기준으로 하는 근사임을 표시

## 프로젝트 귀속 규칙 제안

```text
수동 지정
→ 현재 작업의 Git repository
→ 가장 가까운 분류된 부모 작업
→ 로컬 매핑 캐시
→ 미분류
```

부모와 자식 Git 정보가 충돌할 때의 최종 정책은 아직 확정하지 않았다.

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

