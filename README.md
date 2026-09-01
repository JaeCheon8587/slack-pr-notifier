# Slack MR Notifier

GitLab Merge Request가 생성되면 Slack으로 리뷰 요청을 보내고, Slack 버튼으로
GitLab MR을 승인하거나 변경 요청 코멘트를 남기는 Python 미들웨어입니다.

Claude Code CLI로 MR 변경 내용을 요약하며, 최종 판단은 사람이 내립니다. 자동 머지는
수행하지 않습니다.

```text
GitLab MR 생성
  → POST /webhooks/gitlab
  → GitLab diff/변경 파일 조회
  → Claude Code CLI로 요약
  → Slack AI 요약 + Yes/No 메시지
  → Yes: GitLab MR 승인
  → No: Slack에서 사유 입력 → GitLab MR 코멘트 등록
```

> GitLab의 범용 REST API에는 `REQUEST_CHANGES` 상태를 만드는 버전 독립적인 호출이
> 없습니다. 따라서 No 동작은 변경 요청 코멘트를 남기지만,
> 그 자체로 머지를 차단하지 않습니다. 머지 차단이 필요하면 GitLab 승인 규칙이나
> 외부 상태 검사를 별도로 설정해야 합니다.

## 로컬 실행

Python 3.12 이상이 필요합니다.

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
uvicorn app.main:app --reload
```

- 상태 확인: `http://localhost:8000/health`
- API 문서: `http://localhost:8000/docs`

## 환경 변수

`.env.example`을 복사해 `.env`에 값을 설정합니다. 실제 Secret과 Token은 커밋하지
않습니다.

```env
GITLAB_URL=https://gitlab.company.example
GITLAB_WEBHOOK_SECRET=GitLab-Webhook에-등록한-secret-token
GITLAB_TOKEN=GitLab-project-or-personal-access-token
GITLAB_VERIFY_SSL=true
SLACK_BOT_TOKEN=xoxb-your-bot-token
SLACK_SIGNING_SECRET=Slack-App-Signing-Secret
SLACK_CHANNEL_ID=C0123456789
SLACK_ALLOWED_USER_IDS=U0123456789
ACTION_TOKEN_SECRET=버튼-데이터-서명용-긴-임의값
AI_ENABLED=true
AI_MODEL=claude-opus-4-8
AI_EFFORT=high
AI_MAX_INPUT_CHARS=240000
AI_TIMEOUT_SECONDS=180
AI_MAX_BUDGET_USD=1.0
```

- `GITLAB_URL`: 사내 GitLab 루트 URL. `/api/v4`는 코드가 자동으로 붙입니다.
- `GITLAB_TOKEN`: `api` scope가 있는 Project Access Token 또는 Personal Access Token.
  토큰 소유자는 대상 프로젝트에서 MR을 승인할 수 있는 역할과 승인 규칙 자격이 있어야
  합니다.
- `GITLAB_VERIFY_SSL`: 기본값 `true`. 사내 인증서를 OS 신뢰 저장소에 등록하는 방식을
  권장합니다. 개발 환경에서 검증을 끌 때만 `false`로 설정합니다.
- `SLACK_BOT_TOKEN`: Slack App의 `chat:write` 권한 필요
- `SLACK_CHANNEL_ID`: 알림을 보낼 채널 ID
- `SLACK_ALLOWED_USER_IDS`: 승인 가능한 Slack 사용자 ID. 쉼표로 여러 명을 지정하며,
  비워두면 워크스페이스의 모든 사용자가 버튼을 누를 수 있음
- `ACTION_TOKEN_SECRET`: DB 없이 버튼에 담긴 MR 정보를 검증하는 서버 전용 키
- AI 분석은 로그인된 `claude` CLI를 헤드리스 모드로 호출합니다. MR 입력은 도구가
  비활성화된 안전 모드에서 처리하며, 실패하거나 시간 초과되면 AI 요약 없이 Slack
  알림을 계속 전송합니다.
- `AI_MAX_INPUT_CHARS`: CLI 호출 전 입력 크기 상한입니다. 예산을 넘긴 파일 내용과
  diff는 프롬프트 및 서버 로그에 생략 사실을 남깁니다.
- `AI_MAX_BUDGET_USD`: Claude CLI 한 번 호출에 허용할 최대 비용 상한입니다.

GitLab에는 `GITLAB_TOKEN` 소유자의 계정으로 승인이 기록됩니다. 프로젝트 설정에서 MR
작성자 승인이나 커미터 승인을 막은 경우 해당 계정이 만든 MR은 승인할 수 없습니다.

## GitLab Webhook

대상 프로젝트의 **Settings → Webhooks**에서 다음 URL을 등록합니다.

```text
https://<public-host>/webhooks/gitlab
```

- Secret token: `GITLAB_WEBHOOK_SECRET`과 동일한 값
- Trigger: **Merge request events**
- SSL verification: Enable

여러 프로젝트를 연결할 때는 각 프로젝트 웹훅에 같은 URL과 Secret token을 등록할 수
있습니다. API 호출은 웹훅 payload의 숫자형 Project ID를 사용합니다.

## Slack App

1. Slack App의 OAuth 권한에 `chat:write`를 추가합니다.
2. App을 워크스페이스에 설치하고 알림 채널에 초대합니다.
3. **Interactivity & Shortcuts**를 활성화합니다.
4. Request URL에 다음 주소를 등록합니다.

```text
https://<public-host>/webhooks/slack/actions
```

GitLab과 Slack은 같은 실행 중인 서버 및 터널 주소를 사용합니다. Quick Tunnel을 다시
실행하여 주소가 바뀌면 GitLab Webhook과 Slack Request URL을 모두 갱신해야 합니다.

## 보안 검증

- GitLab 요청: `X-Gitlab-Token`을 `GITLAB_WEBHOOK_SECRET`과 상수 시간 비교
- Slack 요청: `X-Slack-Signature` 및 5분 타임스탬프 검증
- Slack 버튼 데이터: `ACTION_TOKEN_SECRET`으로 HMAC 서명 및 24시간 만료
- 승인자 제한: `SLACK_ALLOWED_USER_IDS`
- 승인 대상 고정: 웹훅 시점의 HEAD SHA를 GitLab 승인 API에 전달하여 변경된 MR 거부

## 테스트

```powershell
pytest
ruff check .
```
