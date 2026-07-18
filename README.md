# Slack PR Notifier

GitHub Pull Request의 변경 내용을 AI로 분석하고, Slack에서 승인 또는 거절한 뒤 승인된 PR을 머지하는 시스템입니다.

## 목표 흐름

1. GitHub에 Pull Request 생성
2. Python 미들웨어가 PR 이벤트 수신
3. AI가 PR 변경 내용 분석
4. Slack에 분석 결과와 Yes/No 버튼 전송
5. Yes 선택 시 검토한 커밋과 현재 PR 커밋이 동일한지 확인 후 머지
6. No 선택 시 머지하지 않고 Slack 메시지 갱신

## 초기 설계 방향

- 미들웨어: Python + FastAPI
- AI 분석: OpenAI API
- GitHub/Slack 연동: 각 서비스의 REST API와 Webhook
- 데이터베이스: 초기 MVP에서는 사용하지 않음
- 실행 형태: 단일 서버 인스턴스

상세 설계와 구현은 Pull Request 단위로 추가할 예정입니다.

## 로컬 실행

Python 3.12 이상이 필요합니다.

```bash
python -m venv .venv
```

Windows PowerShell에서 가상환경을 활성화합니다.

```powershell
.venv\Scripts\Activate.ps1
```

개발 의존성을 설치하고 서버를 실행합니다.

```bash
python -m pip install -e ".[dev]"
uvicorn app.main:app --reload
```

서버 실행 후 아래 주소를 확인할 수 있습니다.

- 상태 확인: `http://localhost:8000/health`
- API 문서: `http://localhost:8000/docs`

## GitHub Webhook

`.env.example`을 참고하여 `.env`에 GitHub Webhook secret을 설정합니다.

```env
GITHUB_WEBHOOK_SECRET=GitHub에_등록할_동일한_비밀값
```

GitHub 저장소 Webhook의 Payload URL에는 외부에서 접근 가능한 주소를 등록합니다.

```text
https://<public-host>/webhooks/github
```

현재 Webhook은 GitHub 서명을 검증하고 `ping` 및 `pull_request` 이벤트의 기본 정보를
응답과 애플리케이션 로그에 기록합니다.

테스트와 정적 검사는 다음 명령으로 실행합니다.

```bash
pytest
ruff check .
```
