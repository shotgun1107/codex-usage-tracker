# Codex Usage Tracker

여러 기기와 멀티에이전트 작업에 흩어진 Codex 실제 토큰 사용량을 Git 프로젝트별로 통합하는 local-first 개인용 도구입니다.

현재 Build 단계이며 첫 구현 단위는 다음과 같습니다.

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
- 표준 라이브러리 기반 단위 테스트

Codex 대화·코드·명령·로컬 경로·raw remote는 중앙 장부에 저장하지 않는 것을 원칙으로 합니다. 공개 소스 저장소와 사용자별 비공개 데이터 장부는 분리합니다.

## 테스트

```powershell
python -m unittest discover -s tests -t . -v
```

현재 단위·통합 테스트 105개와 로컬 rollout 전체 익명 구조 검사를 통과했습니다. 2026-08-26 스냅샷에서 54,004개 logical checkpoint 중 43,989개를 자동 귀속했고, 근거가 부족한 10,015개는 추측하지 않고 미분류했습니다.

## 문서

- [프로젝트 브리프](PROJECT.md)
- [v1 PRD](PRD.md)
- [중앙 장부 스키마](SCHEMA.md)
- [v1 아키텍처](ARCHITECTURE.md)
- [결정 기록](DECISIONS.md)
- [자료조사](RESEARCH.md)
