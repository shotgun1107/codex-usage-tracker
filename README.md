# Codex Usage Tracker

여러 기기와 멀티에이전트 작업에 흩어진 Codex 실제 토큰 사용량을 Git 프로젝트별로 통합하는 local-first 개인용 도구입니다.

현재 Build 단계이며 첫 구현 단위는 다음과 같습니다.

- HMAC 기반 project·thread·turn 식별자
- Git remote URL 정규화
- origin·unique remote·ambiguous remote 선택 규칙
- Codex rollout JSONL metadata·token parser
- 신규·resume·fork·compact delta와 fork 복사 중복 제거
- 표준 라이브러리 기반 단위 테스트

Codex 대화·코드·명령·로컬 경로·raw remote는 중앙 장부에 저장하지 않는 것을 원칙으로 합니다. 공개 소스 저장소와 사용자별 비공개 데이터 장부는 분리합니다.

## 테스트

```powershell
python -m unittest discover -s tests -t . -v
```

현재 단위 테스트 44개와 로컬 rollout 전체 익명 구조 검사를 통과했습니다.

## 문서

- [프로젝트 브리프](PROJECT.md)
- [v1 PRD](PRD.md)
- [중앙 장부 스키마](SCHEMA.md)
- [v1 아키텍처](ARCHITECTURE.md)
- [결정 기록](DECISIONS.md)
- [자료조사](RESEARCH.md)
