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
- 공식 thread source 종류는 `cli`, `vscode`, `exec`, `appServer`, 여러 `subAgent` 종류, `unknown`이며 별도 `background` source는 없다.
- `commandExecution` item에는 실행 `cwd`가 있고 `collabToolCall`에는 송신·수신·신규 thread ID가 있다.
- `thread/start`는 `cwd`를 받고, 저장된 thread의 `gitInfo`는 metadata API에서 별도로 갱신할 수 있다.
- `thread/inject_items`는 모델 turn 없이 합성 item을 rollout에 영속화할 수 있다.

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

## Spike 3: CLI·백그라운드·오케스트레이션 귀속 실험 (2026-08-26)

Codex CLI `0.150.0-alpha.8`에서 Git 저장소 안·밖의 `codex exec`, 숨김 백그라운드 프로세스, 프로젝트 안·밖 오케스트레이터의 자식 작업을 비교했다. 파일은 변경하지 않았고 원문 명령과 경로는 결과 문서에 보존하지 않았다.

### CLI와 백그라운드 실행

| 실행 조건 | 기록된 source | session Git 정보 | SQLite·JSONL 조인 |
|---|---|---|---|
| Git 저장소 안 `codex exec` | `exec` | remote·branch·commit 있음 | 성공 |
| Git 저장소 밖 `codex exec` | `exec` | 없음 | 성공 |
| 숨김 백그라운드 `codex exec` | `exec` | 실행 위치의 Git 정보 있음 | 성공 |

백그라운드는 별도 thread source가 아니라 실행 방식이다. 프로젝트 귀속은 foreground/background 여부가 아니라 세션과 실제 작업 위치의 Git 정보로 판단해야 한다.

### 프로젝트 안 오케스트레이션

프로젝트 폴더에서 생성한 자식은 다음 정보를 정상적으로 보존했다.

- source: `subagent.thread_spawn`
- 자식 `cwd`: 부모와 같은 프로젝트 폴더
- 자식 Git remote·branch·commit: 모두 존재
- `session_id`: 최상위 부모 계보 root
- SQLite `thread_spawn_edges`: 직접 부모·자식 edge 존재

이 경우 자기 Git 정보만으로 프로젝트를 판별할 수 있고, spawn-edge는 프로젝트 전체 계보 집계에 사용할 수 있다.

### 프로젝트 밖 오케스트레이션

Git이 없는 폴더에서 부모 `exec`를 시작하고 자식에게 실제 Git 프로젝트를 읽게 한 결과:

- 부모는 `source=exec`, Git 정보 없음
- 자식은 `source=subagent.thread_spawn`, spawn-edge 존재
- 자식 `session_meta.cwd`와 `turn_context.cwd`는 부모의 Git 밖 폴더를 유지
- 자식도 session Git 정보 없음
- 자식이 실제 프로젝트 폴더를 사용한 세 번의 도구 호출에는 각각 대상 `workdir`가 기록됨

즉 자식이 다른 프로젝트에서 실제로 작업해도 session·turn의 기본 `cwd`는 자동으로 바뀌지 않는다. 부모 상속만으로는 프로젝트를 알아낼 수 없고, 세션 단위 Git만 사용하면 이 사용량은 미분류가 된다.

### 귀속에 사용할 수 있는 추가 신호

공식 App Server의 `commandExecution.cwd`와 실제 rollout의 도구 호출 `workdir`는 로컬에서 읽을 수 있다. 따라서 대화·명령 원문을 업로드하지 않고도 다음 처리가 가능하다.

```text
turn 안의 도구 실행 workdir 수집
→ 각 workdir의 Git remote를 로컬에서 해석
→ 서로 다른 remote가 정확히 하나면 해당 project_id로 귀속
→ 중앙 장부에는 경로·remote 원문 대신 project_id와 판별 방식만 저장
```

한 turn이 여러 저장소를 사용하면 토큰을 정확히 분할할 근거가 없다. 이 경우 임의로 한 프로젝트에 넣지 않고 `ambiguous_multi_repo`로 보존하거나 사용자가 수동 지정해야 한다.

### Spike 3 결론

프로젝트 귀속 단위는 thread 하나가 아니라 usage event가 속한 turn이어야 한다. 현재 제안 우선순위는 다음과 같다.

```text
명시적 수동 지정
→ turn의 도구 실행 위치에서 확인한 단일 Git 저장소
→ thread 자체 Git 저장소
→ 가장 가까운 부모·root의 프로젝트
→ 단일 자식 저장소 합의
→ 로컬 매핑
→ 미분류 또는 다중 저장소 모호 상태
```

도구 실행 위치를 읽는 작업은 각 기기 안에서만 수행하고, 원문 경로와 명령은 중앙 장부에 올리지 않는다.

## Spike 4: Git 메타데이터 누락과 폴백 실험 (2026-08-26)

모델 토큰을 사용하지 않도록 App Server의 `thread/start`와 `thread/inject_items`를 이용해 합성 thread를 만들었다. Codex CLI `0.150.0-alpha.8`에서 임시 Git 저장소의 remote 없음, `origin` 없음, worktree, submodule, monorepo 하위 폴더, remote 변경을 비교했다.

### Git 구조별 결과

| 조건 | Codex repository URL | branch·commit | 판별 결과 |
|---|---|---|---|
| 일반 저장소 + `origin` | `origin` URL | 있음 | 자동 분류 가능 |
| remote 없음 | 없음 | 있음 | remote 기반 자동 통합 불가 |
| `upstream` 하나, `origin` 없음 | 없음 | 있음 | Codex 로그만으로는 불가, 로컬 Git 조회로 가능 |
| remote 둘, `origin` 없음 | 없음 | 있음 | 어느 remote가 정본인지 모호 |
| Git worktree | 원본과 같은 `origin` | worktree branch·commit 있음 | 원본과 같은 프로젝트 |
| submodule 내부 | submodule 자체 `origin` | 있음 | 상위 저장소와 별도 프로젝트 후보 |
| monorepo 하위 폴더 | 루트와 같은 `origin` | 있음 | 기본적으로 저장소 하나의 프로젝트 |

JSONL과 SQLite의 Git URL·branch·commit은 모든 합성 thread에서 일치했다. 모델 turn이 없었으므로 `tokens_used=0`이었고, 빈 thread가 아니라 합성 item이 영속화된 thread만 JSONL과 SQLite에 남았다.

custom App Server client로 생성했지만 현재 thread source는 `vscode`로 기록됐다. 따라서 `appServer` source가 선택되는 정확한 조건은 여전히 확인되지 않았다.

### origin이 없는 저장소

Codex가 기록하는 repository URL은 사실상 `origin` 중심이다. `origin`이 없으면 remote가 하나뿐이어도 URL이 비어 있었다.

수집기 폴백 후보:

```text
Codex가 기록한 origin URL 있음
→ 해당 URL 사용

origin URL 없음 + cwd가 아직 존재
→ 로컬 Git remote가 정확히 하나면 그 URL 사용
→ remote가 0개면 local-only 또는 수동 매핑
→ remote가 2개 이상이면 ambiguous_remote

cwd가 사라짐
→ 기존 매핑이 없으면 미분류
```

branch와 commit hash만으로는 프로젝트 정체성을 안정적으로 판단하지 않는다. 서로 다른 저장소가 같은 commit을 가질 수 있고, 같은 저장소도 기기별로 branch와 commit이 달라질 수 있기 때문이다.

### worktree·submodule·monorepo

- worktree는 원본 저장소와 같은 remote를 사용하므로 같은 project ID로 합친다.
- submodule 내부 작업은 submodule 자체 remote를 우선한다. 상위 저장소에 합산할지는 별도 roll-up 기능으로 다룬다.
- monorepo 하위 폴더는 기본적으로 같은 remote이므로 하나의 프로젝트다. 하위 프로젝트 분리는 명시적 매핑이 있을 때만 한다.

### remote 변경

같은 저장소의 `origin` URL을 변경하기 전과 후에 만든 thread는 각각 당시 URL을 그대로 보존했다. 과거 이벤트가 자동으로 새 URL로 바뀌지 않으므로 다음 alias가 필요하다.

```text
old normalized remote project ID
→ new normalized remote project ID
```

remote rename·조직 이전·프로토콜 변경을 발견해도 과거 장부를 덮어쓰지 않고 append-only alias 이벤트로 연결한다.

### Spike 4 결론

```text
remote 선택: Codex origin → 로컬 unique remote → 수동 매핑 → 모호/미분류
worktree: 같은 remote면 같은 프로젝트
submodule: 자기 remote 우선
monorepo: 기본은 저장소 하나
remote 변경: alias 이벤트로 과거·현재 연결
```

`origin`이 없고 cwd도 사라진 작업은 사후 자동 복구가 불가능하다. 따라서 수집기는 증분 실행 시 로컬 Git 폴백 결과를 즉시 정제 이벤트에 보존해야 한다.

## Build 검증: JSONL parser와 lifecycle dedup (2026-08-26)

현재 구현을 로컬 Codex 원문 전체에 read-only로 실행하고 원문·경로·ID는 출력하지 않은 채 구조적 집계만 확인했다. 검사 중에도 active rollout이 append되므로 아래 개수는 해당 시점 snapshot이다.

- rollout 665개, CLI 버전 13종 모두 파싱 성공
- parse fatal error 0개
- 약 280,000개 token checkpoint 처리
- fork 복사 제거 후 약 53,000개 logical usage event
- duplicate logical group 17,659개에서 token 수치 충돌 0개
- model·reasoning effort metadata가 복사본마다 다른 group 2,402개는 단일 값 보강 또는 conflict flag 처리
- turn ID가 없는 구버전 checkpoint 2,409개는 `weak_dedupe_key` 처리
- `info`가 없는 token_count 68개는 사용량 event가 아니므로 건너뜀

이 검증으로 현재 13개 `cli_version` 표본에서 신규·resume·fork·compact delta와 전역 fork dedup이 중단 없이 동작함을 확인했다. 버전별 지원을 영구 보장하는 것은 아니므로 unknown 구조는 계속 fail-closed 또는 issue로 남긴다.

## Build 검증: SQLite lineage와 프로젝트 귀속 (2026-08-26)

현재 구현을 같은 로컬 원문에 read-only로 적용했다. 결과에는 개수와 판별 방식만 남기고 thread ID·경로·remote·프로젝트명은 출력하지 않았다. active 파일은 계속 변하므로 아래 수치는 해당 시점 snapshot이다.

- SQLite thread 669개와 spawn-edge 325개를 adapter 오류 없이 읽음
- rollout 669개를 fatal parse error 없이 처리
- fork 복사 제거 후 logical token checkpoint 54,004개
- 부모 충돌·계보 cycle 0개
- 서로 다른 실제 project identity 6개를 로컬에서 식별
- 43,989개 checkpoint(약 81.5%)를 자동 귀속
- 10,015개는 근거가 부족해 `unclassified` 유지

자동 귀속 근거별 개수:

| 근거 | checkpoint |
|---|---:|
| thread 자기 origin | 25,323 |
| 단일 자식 합의 | 17,373 |
| turn 활동 Git | 856 |
| 가장 가까운 부모 | 338 |
| root | 99 |

이 검증은 프로젝트 밖 오케스트레이터와 다수 자식 thread를 실제로 합산할 수 있음을 보여준다. 동시에 오래된 로그나 Git 근거가 없는 작업 약 18.5%는 수동 매핑 없이는 정확히 복구할 수 없으므로, 자동 추측하지 않는 현재 정책을 유지한다.

## Build 검증: local outbox와 read model (2026-08-27)

합성 ledger fixture로 operational state와 rebuildable read model의 장애 복구 규칙을 검증했다.

- source cursor와 여러 outbox event를 한 transaction으로 저장
- 같은 event 재수집은 0건 추가, 같은 ID의 다른 payload는 cursor까지 rollback
- 같은 fingerprint에서 cursor offset 감소 차단, 원천 fingerprint 변경 시 재검사 허용
- ledger 상대 경로만 flush 결과로 허용하고 Windows·POSIX 절대경로와 상위 경로 차단
- event 입력 순서를 뒤집어도 latest revision·void·manual mapping·alias 결과 동일
- revision gap·잘못된 supersedes·서로 다른 key ID 혼합은 fail-closed
- alias cycle은 관련 alias만 제외하고 나머지 usage replay 지속
- read model 재생성 실패 시 직전 generation과 집계 유지
- read model을 재생성해도 source cursor와 pending outbox 유지

구현 과정에서 Python SQLite context manager가 connection 자체를 닫지 않아 Windows 임시 DB 파일이 잠기는 문제를 테스트가 발견했다. 모든 조회 connection을 명시적으로 close하도록 수정한 뒤 파일 잠금 없이 통과했다.

## Build 검증: JSONL ledger와 privacy boundary (2026-08-27)

SQLite outbox에서 기기별 장부 파일로 기록하고 다시 replay·read model까지 연결하는 합성 end-to-end 검증을 수행했다.

- usage는 `devices/<uuid>/usage/YYYY/MM/DD.jsonl`, mapping은 월별, quota는 일별 파일로 분리
- 완전한 canonical JSON line을 `fsync`한 뒤에만 outbox flushed 처리
- 파일 `fsync` 후 DB 갱신 전에 강제 종료한 상황에서 재실행 시 append 0건, existing 1건으로 중복 방지
- 마지막 불완전 line은 reader에서 제외하고 writer가 마지막 LF까지만 보존한 뒤 pending event 재기록
- 완전하지만 잘못된 JSON, event 날짜·종류와 맞지 않는 경로, 같은 ID의 다른 payload는 fail-closed
- 다른 device ID의 outbox event는 자기 기기 경로에 쓰지 않음
- 체크인된 JSON Schema로 usage·mapping·quota의 필수 필드·타입·범위·추가 필드 검증
- 외부 runtime dependency 없이 wheel을 빌드하고 JSON Schema가 배포 데이터에 포함됨을 확인
- schema-valid 문자열이라도 raw project ID, Windows·POSIX 경로, URL·SSH remote, 금지 원문 필드는 privacy guard에서 차단

reader가 안전을 위해 반환한 read-only mapping을 replay가 직렬화하지 못하는 연결 오류도 end-to-end 테스트에서 발견했다. replay 입구에서 일반 JSON object로 정규화하도록 수정했다.

## Build 검증: 실제 collect CLI (2026-08-27)

임시 공유키·기기 ID·장부·SQLite를 사용해 현재 로컬 Codex 원문을 외부 전송 없이 end-to-end 수집했다. 원본 ID·경로·remote·프로젝트명은 결과에 출력하지 않았다.

- rollout 712개 발견 및 Codex SQLite lineage 사용 성공
- 정제 usage event 56,008개를 JSONL 장부와 read model에 생성
- 프로젝트 근거가 부족한 10,744개는 `unclassified` 유지
- parser issue 2,477개를 원문 없이 코드·위치 정보로 보존
- 실제 갱신 중인 rollout 4개를 두 번째 수집에서 감지하고 신규 event 14개만 추가
- 안정된 완전 line 자체가 손상된 기존 rollout은 파일 단위로 격리하고 다른 파일 수집 지속
- 장부를 두 번 읽던 경로를 한 번 읽기 + pending memory merge로 바꿔 최초·재수집 합계 시간을 약 128초에서 95.6초로 단축

합성 CLI acceptance에서는 첫 수집 4개, 같은 파일 재수집 0개, 새 turn 추가 후 1개만 기록되는 흐름을 검증했다.

배포 검증에서는 wheel을 새 임시 가상환경에 설치해 `codex-usage.exe --version`이 `0.1.0`을 반환하고, 설치된 data 경로에서 ledger JSON Schema를 다시 읽는 것까지 확인했다.

Windows Credential Manager adapter는 실제 API까지 호출했지만 현재 자동 테스트 호스트에는 interactive logon session이 없어 `WinError 1312`가 반환됐다. 이 경우만 환경상 skip으로 구분했으며, 일반 데스크톱 로그온 세션에서의 최종 acceptance가 남아 있다.

## Build 검증: 실제 report CLI (2026-08-27)

현재 로컬 로그를 임시 장부와 read model로 수집한 뒤 project·date 기본 보고서를 생성했다. 프로젝트 ID와 이름, 토큰 원문은 검증 출력에서 제외했다.

- ledger usage event 56,208개 중 56,178개를 합계에 포함
- `delta=null` 격리 이벤트 30개는 합계 제외 건수로 표시
- 기본 `project,date` 그룹 결과 70행
- 터미널 표 약 10,200자와 Markdown 약 8,446자를 같은 report model에서 생성
- read-only SQLite 조회·KST 변환·집계·두 형식 렌더링에 0.628초
- 프로젝트·날짜·모델·기기·source 필터와 project·date·thread·model·effort·device·source 그룹 조합을 합성 fixture로 검증
- 승인된 project/device 표시명, 짧은 HMAC fallback, Markdown escape, 한글 터미널 폭을 검증

보고서는 cache·reasoning을 total에 다시 더하지 않으며, 구버전에서 일부 세부 필드만 누락된 합계에는 `*`를 표시한다.

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
