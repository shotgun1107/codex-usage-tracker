# ADR-003: 기기별 append-only Git 장부

상태: 제안됨

날짜: 2026-08-26

## 상황

임의 개수의 기기가 중앙 서버 없이 데이터를 합쳐야 한다. 여러 기기가 같은 파일을 수정하면 Git conflict가 발생한다.

## 결정

- 사용자별 비공개 Git 저장소를 정제 이벤트 장부로 사용한다.
- `devices/<device-uuid>/` 아래를 기기별 단독 쓰기 영역으로 둔다.
- usage·mapping·quota를 날짜별 JSONL에 append한다.
- 로컬 SQLite outbox를 먼저 기록한 뒤 장부에 flush한다.
- 정정은 revision·supersedes·voided 이벤트로 추가한다.
- v1에서는 event 삭제·롤업·force push·자동 conflict resolution을 하지 않는다.
- 공개 소스 저장소와 비공개 데이터 장부를 분리한다.

## 대안

- 중앙 API·DB: 운영비와 인증·가용성 관리가 필요하다.
- 기기들이 하나의 일별 파일 공유: 동시 쓰기 conflict가 잦다.
- SQLite 동기화: 바이너리 merge가 불가능하다.

## 결과

- 서버 없이 이력과 복구성을 얻는다.
- Git 저장소 크기는 계속 증가하며 장기 보존 정책은 v1 이후 필요하다.
- Git 인증과 push 실패 처리 UX가 필요하다.
