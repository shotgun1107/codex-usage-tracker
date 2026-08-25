# 자료조사 및 검증 기록

상태: 진행 중  
기준일: 2026-08-25

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

## 현재 PC에서 확인한 내용

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

