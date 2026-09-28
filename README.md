# Slack MR Notifier

GitLab Merge Request가 열리면 Slack으로 리뷰를 요청하고, Slack 버튼 하나로 승인하거나
의견을 남기면 LLM이 실제로 문서·코드를 고쳐 push하고 다시 알리는 Python 미들웨어입니다.
문서(`.md`) 위주의 MR은 **mrdoc** 레일로 빠져, 무엇이 어떻게 바뀌었는지를 검증된 HTML
리포트로 만들어 같은 스레드에 붙입니다.

**최종 판단은 사람이 내립니다. 자동 머지는 하지 않습니다.**

```text
GitLab MR 생성
  → POST /webhooks/gitlab/mr
  → AI 요약 + [Yes · 승인] / [No · 변경 요청] Slack 알림
  → Yes: 권한·서명·HEAD SHA 검사 후 머지
  → No : 의견 입력 → LLM이 수정·커밋·푸시 → 라운드 결과를 새 메시지로 알림
  → 문서 MR: mrdoc이 변경 리포트를 스레드에 첨부
```

## 문서

**[docs/index.html](docs/index.html) 이 진입점입니다.** 독자별 읽기 경로가 거기 있습니다.

| 문서 | 읽는 사람 |
|---|---|
| [docs/overview.html](docs/overview.html) | 처음 보는 사람 — 뭘 해주고 뭘 안 하는가, 화면은 어떻게 생겼는가 |
| [docs/architecture.html](docs/architecture.html) | 합류 개발자 — 스택·두 레일·상태기계·데이터 |
| [docs/trust-model.html](docs/trust-model.html) | 전원 — LLM 출력을 왜 믿을 수 있는가 |
| [docs/setup.html](docs/setup.html) | 운영자 — **설치·설정·운영의 권위 문서** |

심화 규격은 `docs/`의 `mr-review-pipeline.html`, `revise-workflow.html`,
`mrdoc-pipeline.html`에 있습니다. 안내 문서와 어긋나면 심화 문서가 옳습니다.

## 빠른 시작

Python 3.12 이상이 필요합니다.

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
copy .env.example .env    # 값을 채운 뒤
uvicorn app.main:app --reload
```

- 상태 확인 `http://localhost:8000/health`
- API 문서 `http://localhost:8000/docs`
- 테스트 `pytest` · 린트 `ruff check .`

> 백그라운드 작업이 프로세스 안의 스레드와 큐로 돌아갑니다. **워커는 1개를 유지하세요.**

설정 키 전체, GitLab 웹훅·Slack App 등록, 승인자 매핑(`REVIEWER_MAP`), 단계별로 켜는
순서, 장애 대응과 알려진 결함은 **[docs/setup.html](docs/setup.html)** 에 있습니다.

## 알아 둘 제약

- GitLab REST API에는 버전 독립적인 `REQUEST_CHANGES` 상태가 없습니다. [No · 변경 요청]은
  코멘트를 남기고 수정 루프를 돌릴 뿐, **그 자체로 머지를 차단하지는 않습니다.**
  차단이 필요하면 GitLab 승인 규칙을 별도로 설정하세요.
- 승인은 `GITLAB_TOKEN` 소유자 계정으로 기록됩니다. 프로젝트가 작성자 승인이나 커미터
  승인을 막고 있다면 그 계정이 만든 MR은 승인할 수 없습니다.
- 모든 LLM 기능은 기본이 꺼져 있습니다. 하나씩 켜세요.

## 보안 검증

- GitLab 요청: `X-Gitlab-Token`을 `GITLAB_WEBHOOK_SECRET`과 상수 시간 비교
- Slack 요청: `X-Slack-Signature`와 5분 타임스탬프 검증
- 버튼 데이터: `ACTION_TOKEN_SECRET`으로 HMAC 서명, 24시간 만료
- 승인자 제한: `REVIEWER_MAP`의 저장소별 매핑 (미등록 저장소는 fail-closed)
- 승인 대상 고정: 알림 시점의 HEAD SHA를 머지 API에 전달해, 그사이 바뀐 MR은 거절
