# Slack PR Notifier

GitHub Pull Request가 생성되면 Slack으로 리뷰 요청을 보내고, Slack의 버튼으로
GitHub 리뷰를 승인하거나 변경 요청하는 Python 미들웨어입니다.

현재 MVP 흐름에는 AI 분석과 자동 머지가 포함되지 않습니다.

```text
GitHub PR 생성
  → POST /webhooks/github
  → Slack Yes/No 메시지
  → Yes: GitHub APPROVE 리뷰
  → No: Slack에서 사유 입력 → GitHub REQUEST_CHANGES 리뷰
```

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

`.env.example`을 참고하여 `.env`에 다음 값을 설정합니다. 실제 Secret과 Token은
커밋하지 않습니다.

```env
GITHUB_WEBHOOK_SECRET=GitHub-Webhook에-등록한-secret
GITHUB_TOKEN=GitHub-fine-grained-token
SLACK_BOT_TOKEN=xoxb-your-bot-token
SLACK_SIGNING_SECRET=Slack-App-Signing-Secret
SLACK_CHANNEL_ID=C0123456789
SLACK_ALLOWED_USER_IDS=U0123456789
ACTION_TOKEN_SECRET=버튼-데이터-서명용-긴-임의값
```

- `GITHUB_TOKEN`: 대상 저장소의 **Pull requests: Read and write** 권한 필요
- `SLACK_BOT_TOKEN`: Slack App의 `chat:write` 권한 필요
- `SLACK_CHANNEL_ID`: 알림을 보낼 채널 ID
- `SLACK_ALLOWED_USER_IDS`: 승인 가능한 Slack 사용자 ID. 쉼표로 여러 명을 지정하며,
  비워두면 워크스페이스의 모든 사용자가 버튼을 누를 수 있음
- `ACTION_TOKEN_SECRET`: DB 없이 버튼에 담긴 PR 정보를 검증하는 서버 전용 키

자기 자신이 만든 PR은 같은 GitHub 사용자의 토큰으로 승인할 수 없습니다. 실제 승인
테스트에는 다른 리뷰어 계정의 토큰 또는 GitHub App이 필요합니다.

## GitHub Webhook

저장소의 Webhook에 다음 URL을 등록합니다.

```text
https://<public-host>/webhooks/github
```

- Content type: `application/json`
- Secret: `GITHUB_WEBHOOK_SECRET`과 동일한 값
- Event: `Pull requests`
- SSL verification: Enable

## Slack App

1. Slack App의 OAuth 권한에 `chat:write`를 추가합니다.
2. App을 워크스페이스에 설치하고 알림 채널에 초대합니다.
3. **Interactivity & Shortcuts**를 활성화합니다.
4. Request URL에 다음 주소를 등록합니다.

```text
https://<public-host>/webhooks/slack/actions
```

GitHub와 Slack은 같은 실행 중인 서버 및 터널 주소를 사용합니다. Quick Tunnel을 다시
실행하여 주소가 바뀌면 두 Webhook URL을 모두 갱신해야 합니다.

## 보안 검증

- GitHub 요청: `X-Hub-Signature-256` 검증
- Slack 요청: `X-Slack-Signature` 및 5분 타임스탬프 검증
- Slack 버튼 데이터: `ACTION_TOKEN_SECRET`으로 HMAC 서명 및 24시간 만료
- 승인자 제한: `SLACK_ALLOWED_USER_IDS`

## 테스트

```powershell
pytest
ruff check .
```
