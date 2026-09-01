# v1 release smoke checklist

## 자동 검증

```powershell
python -m unittest discover -s tests -t .
python -m pip wheel --no-deps --no-build-isolation . --wheel-dir dist
```

새 가상환경에 wheel을 설치한 뒤 다음 항목을 확인한다.

```powershell
codex-usage --version
codex-usage --help
```

- 설치된 패키지에서 `LedgerSchemaValidator.default()`가 schema를 찾는다.
- `init`, `collect`, `sync`, `doctor`, `project`, `report` 명령이 help에 나타난다.
- 소스 checkout 밖에서 실행해도 동일하게 동작한다.

## 실제 Windows 사용자 세션

일반 PowerShell에서 다음 테스트를 실행한다. 테스트는 임시 Credential Manager 항목을 생성하고 `finally`에서 삭제한다.

```powershell
python -m unittest tests.integration.test_windows_credentials -v
```

- `skipped`가 아니라 `OK`인지 확인한다.
- 첫 기기 `init`에서 만든 복구 키를 별도 비밀번호 관리자에 보관한다.
- 두 번째 기기의 `init --import-key`가 같은 `key_id`를 사용하는지 확인한다.
- 복구 키와 Git 인증정보를 저장소 파일에 기록하지 않는다.

## 비공개 GitHub 장부

- 빈 비공개 저장소를 장부 전용으로 만든다.
- 실제 회사 로그를 사용하기 전 회사 정책을 확인한다.
- smoke test에서는 합성 rollout만 사용한다.
- 두 clone에서 `collect → sync` 후 합계가 같아야 한다.
- `project link → sync`가 다른 clone에 반영돼야 한다.
- 세 번째 clean clone에서 `sync`만으로 조회 DB를 재생성해야 한다.
- `doctor`의 `ledger-remote`, `read-model`, `classification`이 `OK`여야 한다.

## 정리와 출시

- 로컬 smoke가 만든 `.tmp-release-smoke`는 검증 후 `Remove-Item -LiteralPath .\.tmp-release-smoke -Recurse -Force`로 정리한다.
- `git status`가 깨끗한지 확인한다.
- `README.md`의 테스트 개수와 실제 결과가 같은지 확인한다.
- `OPEN_QUESTIONS.md`의 실사용 검증 결과를 갱신한다.
- version과 changelog를 확정한 뒤 tag를 만든다.
- 한도·reset 수집은 핵심 v1 출시 이후 별도 단위로 진행한다.
