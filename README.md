# Codex Usage Tracker

여러 기기와 멀티에이전트 작업에 흩어진 Codex 실제 토큰 사용량을 Git 프로젝트별로 통합하는 local-first 개인용 도구입니다.

현재 구현된 핵심 기능은 다음과 같습니다.

- HMAC 기반 project·thread·turn 식별자
- Git remote URL 정규화
- origin·unique remote·ambiguous remote 선택 규칙
- Codex rollout JSONL metadata·token parser
- 신규·resume·fork·compact delta와 fork 복사 중복 제거
- Codex SQLite thread·spawn-edge read-only adapter
- turn 도구 실행 경로의 로컬 Git 판별
- 부모·자식·fork 계보 복원과 turn별 프로젝트 귀속
- crash-safe source cursor·SQLite outbox
- revision·void·manual mapping·alias를 적용하는 결정적 ledger replay
- 실패 시 직전 세대를 보존하는 SQLite read model 재생성
- 기기별 append-only JSONL writer와 partial-line 내성 reader
- 체크인된 JSON Schema 검증과 경로·remote·raw ID 개인정보 guard
- Windows Credential Manager 기반 공유 HMAC 키 보관
- 변경 rollout 감지와 `init·collect·sync·report·doctor` CLI
- 기기별 변경 경계·append-only 검증을 적용한 fail-closed Git 동기화
- 프로젝트 목록·미분류 조회와 append-only 수동 연결·별칭 CLI
- JSONL/SQLite join·버전·분류·이력·원격 권한을 확인하는 확장 doctor
- 표준 라이브러리 기반 단위 테스트

Codex 대화·코드·명령·로컬 경로·raw remote는 중앙 장부에 저장하지 않는 것을 원칙으로 합니다. 공개 소스 저장소와 사용자별 비공개 데이터 장부는 분리합니다.

## 현재 CLI 사용법

개발 버전을 설치합니다.

```powershell
python -m pip install -e .
```

먼저 별도의 비공개 Git 저장소를 장부용으로 준비한 뒤 첫 기기를 초기화합니다.

```powershell
codex-usage init --ledger C:\path\to\private-ledger
```

첫 초기화에서 표시되는 복구 키는 다른 기기 연결에 필요합니다. Git이나 일반 텍스트 파일에 저장하지 말고 비밀번호 관리자 등에 보관합니다. 두 번째 기기는 다음 명령을 실행하고 복구 키를 화면에 표시되지 않는 입력창에 붙여 넣습니다.

```powershell
codex-usage init --ledger C:\path\to\private-ledger --import-key
```

수집, 장부 동기화, 진단:

```powershell
codex-usage collect
codex-usage sync
codex-usage doctor
```

`collect`는 변경되지 않은 rollout을 건너뛰고, 변경된 rollout만 누적 기준선부터 다시 계산한 뒤 이미 저장된 `source_event_id`를 제외합니다. 손상됐거나 계속 쓰이는 파일은 cursor를 전진시키지 않고 다음 실행에서 재시도합니다.

`sync`는 자기 기기의 `devices/<device-id>/` 아래에 추가된 JSONL만 커밋합니다. 전체 장부의 schema·privacy·HMAC key를 검사한 뒤 `fetch → rebase → replay → push`하며, 충돌·과거 줄 수정·다른 경로 변경은 자동 해결하지 않고 중단합니다. 전송 실패 시 로컬 커밋은 다음 재시도를 위해 보존됩니다.

프로젝트·날짜별 사용량을 조회하거나 Markdown으로 남깁니다.

```powershell
codex-usage report
codex-usage report --period week --group-by project,date,model
codex-usage report --from 2026-08-01 --to 2026-08-31 --project <ID-or-name>
codex-usage report --markdown reports\usage.md
```

지원 필터는 project·model·device·source이며, 그룹은 project·date·thread·model·effort·device·source를 조합할 수 있습니다. 날짜는 기본 `Asia/Seoul` 기준입니다. `delta=null` 이벤트는 합계에서 제외하고 건수를 별도로 표시합니다.

자동 분류되지 않은 작업을 확인하고 기존 프로젝트에 연결합니다.

```powershell
codex-usage project list
codex-usage project unresolved
codex-usage project link --thread <raw-thread-id-or-thr_h1-id> --project <prj_h1-id>
codex-usage project alias --from <old-prj_h1-id> --to <current-prj_h1-id>
codex-usage sync
```

`project unresolved`는 이 기기의 Codex SQLite에서 원본 thread ID를 찾을 수 있을 때만 로컬 화면에 표시합니다. `project link`는 입력받은 원본 ID를 즉시 HMAC 식별자로 바꾸며 Git 장부에는 원본 ID를 기록하지 않습니다. 같은 연결은 멱등 처리하고, 연결 변경은 이전 mapping을 가리키는 새 revision으로 보존합니다.

`doctor`는 Codex JSONL·SQLite 조인, 관측 CLI 버전, 파서 경고, 마지막 수집·동기화 상태, pending outbox, 미분류 작업, 장부와 조회 DB 일치 여부, Git 원격 읽기 권한을 검사합니다. 진단 메시지에는 경로·remote·원본 thread ID를 표시하지 않습니다.

## 테스트

```powershell
python -m unittest discover -s tests -t . -v
```

현재 자동 테스트 154개 중 153개를 통과했고 1개는 테스트 호스트의 Windows 로그온 세션 부재로 skip됐습니다. 로컬 bare remote 기반 수용 테스트에서는 두 기기의 수집·동기화·수동 연결·보고와 새 clone의 DB 재생성을 확인했습니다. 실제 로컬 익명 검증에서는 사용량 이벤트 56,208개를 70개 프로젝트·날짜 행으로 집계하고 터미널·Markdown 보고서를 0.628초에 생성했습니다.

## 문서

- [프로젝트 브리프](PROJECT.md)
- [v1 PRD](PRD.md)
- [중앙 장부 스키마](SCHEMA.md)
- [v1 아키텍처](ARCHITECTURE.md)
- [결정 기록](DECISIONS.md)
- [자료조사](RESEARCH.md)
