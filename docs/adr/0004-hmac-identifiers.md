# ADR-004: HMAC 비식별화와 로컬 secret

상태: 승인됨

날짜: 2026-08-26

## 상황

remote URL·경로·thread ID는 회사명과 비공개 프로젝트 정보를 노출할 수 있다. 동시에 여러 기기에서 같은 프로젝트와 turn을 같은 ID로 계산해야 한다.

## 결정

- normalized remote와 raw thread·turn ID를 공유 비밀키 기반 HMAC-SHA-256으로 변환한다.
- 모든 기기는 사용자가 전달한 동일 256-bit 이상 키를 사용한다.
- 키는 Windows Credential Manager의 secret provider에 저장하고 Git에 넣지 않는다.
- 비가역 key ID를 이벤트에 저장해 잘못된 키를 조기에 감지한다.
- raw remote·경로·branch·commit·프롬프트·명령은 중앙 장부에 저장하지 않는다.
- commit 전에 privacy guard가 금지 패턴을 검사한다.

## 대안

- raw 값 저장: 구현은 쉽지만 개인정보 경계에 어긋난다.
- salt 없는 SHA-256: 알려진 저장소 이름을 사전 대입할 수 있다.
- 기기별 random ID: 같은 프로젝트를 여러 기기에서 자동 통합할 수 없다.

## 결과

- 장부만 유출돼도 원문을 직접 알기 어렵다.
- 키를 잃으면 새 기기에서 기존 ID를 재현할 수 없다.
- 키 교체는 project·thread ID migration 설계가 필요하므로 v1에서는 지원하지 않는다.
