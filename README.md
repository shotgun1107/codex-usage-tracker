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
- 변경 rollout 감지와 `init·collect·doctor` CLI
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

수집과 진단:

```powershell
codex-usage collect
codex-usage doctor
```

`collect`는 변경되지 않은 rollout을 건너뛰고, 변경된 rollout만 누적 기준선부터 다시 계산한 뒤 이미 저장된 `source_event_id`를 제외합니다. 손상됐거나 계속 쓰이는 파일은 cursor를 전진시키지 않고 다음 실행에서 재시도합니다.

## 테스트

```powershell
python -m unittest discover -s tests -t . -v
```

현재 자동 테스트 127개 중 126개를 통과했고 1개는 테스트 호스트의 Windows 로그온 세션 부재로 skip됐습니다. 실제 로컬 익명 검증에서는 rollout 712개에서 사용량 이벤트 56,008개를 생성했으며, 10,744개는 근거 부족으로 추측하지 않고 미분류했습니다.

## 문서

- [프로젝트 브리프](PROJECT.md)
- [v1 PRD](PRD.md)
- [중앙 장부 스키마](SCHEMA.md)
- [v1 아키텍처](ARCHITECTURE.md)
- [결정 기록](DECISIONS.md)
- [자료조사](RESEARCH.md)
