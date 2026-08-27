# ADR-007: fail-closed Git 장부 동기화

상태: 승인

## 배경

여러 기기가 같은 비공개 장부에 기록하므로 동시 push와 잘못된 파일 수정이 발생할 수 있다. 자동 병합이 장부 과거를 바꾸거나 정제되지 않은 데이터를 올리면 복구와 개인정보 보호가 모두 깨진다.

## 결정

- 한 기기는 `devices/<device-id>/` 아래 JSONL만 변경한다.
- 설정된 장부 경로는 Git 저장소 최상위 경로와 정확히 같아야 한다.
- 기존 파일은 HEAD 내용 전체를 prefix로 유지하는 append-only 변경만 허용한다.
- 이번 실행 이전에 만들어진 미전송 커밋도 같은 기기 경계와 append-only 규칙으로 다시 검사한다.
- commit 전과 rebase 후 전체 장부의 schema·privacy·key ID를 검증한다.
- 로컬 커밋 후 `fetch → rebase → replay → push` 순서로 실행한다.
- force push, 충돌 자동 해결, 다른 경로 자동 포함은 금지한다.
- fetch·rebase·push 실패 시 로컬 커밋을 보존해 다음 sync에서 재시도한다.
- `collect`와 `sync`는 같은 로컬 lock을 사용한다.

## 결과

기기별 파일 분리로 정상적인 동시 수집은 rebase할 수 있다. 같은 파일의 충돌, 과거 줄 수정, HMAC 키 불일치, 개인정보 검사 실패는 사용자가 원인을 확인할 때까지 안전하게 중단된다.
