# ADR-001: Python modular monolith CLI

상태: 승인됨

날짜: 2026-08-26

## 상황

v1은 Windows 로컬 Codex 파일, SQLite, Git을 다루고 CLI·Markdown을 제공한다. 웹 서버나 복잡한 배포 시스템은 핵심 가치가 아니다.

## 결정

- Python 3.12+ 단일 package와 console script로 구현한다.
- CLI, application, domain, adapter를 모듈로 분리한 modular monolith를 사용한다.
- domain 계층은 파일시스템·SQLite·Git 구현에 의존하지 않는다.
- 표준 라이브러리를 우선하고 외부 dependency는 명확한 이점이 있을 때만 추가한다.
- 독립 실행 파일 패키징은 정확성 검증 이후로 미룬다.

## 대안

- Rust 단일 바이너리: 배포와 성능은 좋지만 초기 파서 실험과 변경 비용이 크다.
- Electron·Tauri 앱: v1의 CLI·문서화 범위를 넘는다.
- 여러 서비스: 개인용 local-first 제품에 운영 복잡성만 늘어난다.

## 결과

- 빠르게 fixture와 parser를 반복 개발할 수 있다.
- 단일 프로세스 lock과 transaction 경계를 명확히 해야 한다.
- Python runtime이 없는 환경을 위한 패키징은 후속 과제로 남는다.
