# mrdoc 파이프라인 마이그레이션 계획안

대상 설계: `docs/mrdoc-pipeline.html` (이하 "설계")
대상 코드: 현재 `slack-pr-notifier` MR 리뷰 파이프라인 (이하 "현재 구현")

---

## 0. 결론 요약

**마이그레이션 가능. 단, 그대로 이식이 아니라 "적응 이식"이다.**

설계는 코드를 보지 않고 작성됐지만, 핵심 골격(base↔head 대비, 결정론 툴과 LLM 위성의 분리,
파일 기반 산출물, append-only ledger)이 현재 구현의 아키텍처와 충돌 없게 설계돼 있다.
충돌하는 지점은 대부분 "설계가 가정한 실행 환경"(sonnet 오케스트레이터 + Agent 툴)이며,
이는 현재 구현의 Python/FastAPI 기반으로 대체해도 설계 철학을 훼손하지 않는다(§3-A1).

| 분류 | 항목 |
|---|---|
| **그대로 재사용** | Slack 전달 경로 전체, SQLite `event_log`(=ledger), 보안(자격증명 스트리핑·레닥션·봇 판정), 상태머신·revise 루프, `report_html.py`의 결정론 렌더 철학 |
| **확장해서 재사용** | `gitlab_client` diff 조회(→ changeset), `ai_runner` claude CLI 러너(→ 위성 멀티턴), `git_workspace`(→ base/snapshot 트리) |
| **신규 구현** | structure/literals 파서, levelcheck, collect, analyzer·verifier·reporter 위성 계약, 단일 루프, slack-summary |
| **1차 제외** | doc-mapper, checklist, coverage (설계상 [부가]로 표기된 3노드) |

### 착수 조건 (gate)

설계 footer가 명시한 선행 측정을 Phase 0 게이트로 둔다:

> 1차 착수 전 선행 측정: 리터럴 밀도 — 절당 3개 이상이면 설계 그대로, 1개 미만이면 레벨 판정 근거 재설계

이 측정이 실패하면(우리 문서가 리터럴을 거의 안 씀) L1/L2/L3 레벨 체계의 근거가 사라지므로
Phase 1 이후를 진행하지 않고 레벨 근거를 재설계한다.

---

## 1. 검토 기준: 현재 구현 개요

현재 MR 리뷰 흐름:

```
GitLab webhook/poller
  → ingest.handle_mr_open            (app/ingest.py:152)
  → review_session 생성 (SQLite, CAS 상태머신: reviewing/merging/revising/manual/merged)
  → _build_ai_review                 (app/ingest.py:290)  ← 단발 AI 요약 (ai_reviewer.py, --tools "")
  → report_html.render_review_report (app/report_html.py:72)
  → Slack 게시 + 스레드 리포트 업로드 (_deliver_review_report)
[의견] 버튼 → opinion 큐 → revise_executor(단일 워커) → git workspace → commit/push → 재알림
```

핵심 특징:

- 리뷰 생성이 `handle_mr_open` 안에서 인라인 `await` — 완료까지 Slack 게시가 블록된다.
- diff·본문 조회는 GitLab API(`gitlab_client._list_mr_files` / `_normalize_diff`) — 로컬 트리 대비 없음.
- 워크스페이스 `workspaces/<project_id>/<mr_iid>/`(`git_workspace.workspace_path`)는 **head sha가 키에 없음** — 설계가 경고하는 "연속 푸시 5번 → 같은 워크스페이스 덮어쓰기" 위험이 실재한다. 단, 현재는 리뷰 생성이 API 기반이라 이 경로를 안 타서 발현되지 않을 뿐이다.
- `ai_runner`는 이미 claude CLI에 `--allowedTools`(acceptEdits)와 `--max-budget-usd`를 전달한다(`app/ai_runner.py:201-210`). 위성 실행 인프라의 절반이 이미 있다.
- `report_html.py`는 "모든 값을 이스케이프하는 결정론 템플릿팅" — 설계의 render 노드 철학과 동일하다.

---

## 2. 노드별 매핑과 판정

| # | 설계 노드 | 산출물 | 현재 대응물 | 판정 | 갭 |
|---|---|---|---|---|---|
| 0 | `mrdoc changeset` | 00-changeset.md | `gitlab_client._list_mr_files`(gitlab_client.py:163) + `_normalize_diff`(:256) | **부분 존재 (~70%)** | file_id sha8, diff_refs, SKIPPED 정규화, rename 판정, 워크스페이스 파일 기반 재작성 |
| 0a | `mrdoc structure` | 05-structure.md | 없음 | **신규** | 마크다운 절 경계 파서, norm(text) 해시, MOVED 판정 |
| 0b | `mrdoc literals` | 06-literals.md | 없음 | **신규 (설계의 축)** | removed/added/changed 차집합 3종 |
| [1] | doc-mapper | 10-map/*.md | 없음 | **1차 제외** (설계상 부가) | — |
| [2] | `mrdoc checklist` | 15-checklist.md | 없음 | **1차 제외** (설계상 부가) | — |
| 3 | doc-analyzer | 20-analysis/<id>.md | `ai_reviewer.py` | **계약 상이 → 신규** | 단발 `--tools ""` 요약 vs 멀티턴 Read/Write 위성. FINDING/CHECK/레벨 계약, per-file 팬아웃 전부 신규 |
| 4 | `mrdoc levelcheck` | (literals 기반 반증) | 없음 | **신규** | 승격만·강등 없음, "승격 후 CHECK 부활" |
| [5] | `mrdoc coverage` | 30-coverage.md | 없음 | **1차 제외** (설계상 부가) | — |
| 5b | doc-verifier | 32-verify.md | 없음 | **신규 (Phase 3)** | before/after 충실도, 순수 서술문 절의 L1↔L2 감사 |
| 6 | 단일 루프 | (dispatch) | 없음 | **신규 — Python 구현** | exit 0/4/2 순기계 루프, 재호출 최대 1회 |
| 7 | `mrdoc collect` | 35-collect.md | 없음 | **신규** | 역추적 사슬, quote 대조, verdict 계산, must_read 상위 5 |
| 8 | doc-reporter | 40-reportdata.md | 없음 | **신규** | "문장만 쓰고 숫자·verdict는 복사" |
| 9 | `mrdoc render` | report.html + slack-summary.txt | `report_html.py` | **철학 일치, 데이터 재구조** | verdict 대조(불일치→exit 2), refs 대조, slack-summary.txt 신규 |

### 직교하여 그대로 유지하는 자산

mrdoc은 "reviewing 상태에서 리뷰를 어떻게 생성하는가"를 대체하는 것이지, 세션 수명주기와
무관하다. 다음은 변경 없이 유지한다:

- **상태머신**(`state_machine.cas_transition`)과 revise 루프(`revise_executor`) — mrdoc 산출물이
  `_build_ai_review` 자리를 대체해도 의견 반영·커밋·재알림 흐름은 그대로 소비 가능.
- **Slack 전달**(`slack_client`, `notify_queue`, 스레드 업로드) — report.html과 slack-summary.txt를
  기존 전달 채널에 그대로 올린다.
- **event_log**(`app/db.py:60`, 세션 인덱스 있음) — 설계의 append-only ledger와 역할이 동일하다.
- **보안** — 자격증명 스트리핑, 레닥션(`git_workspace._redact`), 봇 액터 판정은 위성 호출 경로에도 동일 적용.

---

## 3. 아키텍처 결정사항 (설계에서 벗어나는 부분)

### A1. 오케스트레이터: sonnet Agent 툴 → Python 구현

설계는 오케스트레이터를 "sonnet low, Agent+Bash만 부여"로 가정한다. 현재 구현에는 Agent 툴이
없고, 도입할 필요도 없다. 근거:

- 설계 루프는 **판단 분기가 없는 순기계**다 — `exit 0 → 블록 실행 → 조인 → ledger append → loop`,
  `exit 4 → 완료`, `exit 2 → abort`. "오케스트레이터는 스테이지 지식조차 없다"는 설계 원칙을
  Python 하드코딩 웨이브 테이블이 LLM보다 더 확실하게 지킨다.
- 토큰 비용·지연·비결정성이 사라진다. 설계 자신이 경고하는 "오케스트레이터 컨텍스트에 툴 결과가
  쌓이는 것"이 원천 불가능해진다.
- **상향 호환**: `mrdoc dispatch`(웨이브 테이븈 조회)와 ledger append의 CLI 계약을 그대로 유지하면
  나중에 LLM 오케스트레이터로 교체할 수 있다. Python 구현은 그 계약의 첫 소비자일 뿐이다.

구현: `app/mrdoc/orchestrator.py` — 웨이브 테이블 + 루프. `mrdoc ledger append` 서브커맨드는
별도 CLI로 두지 않고 루프 안에서 `event_log` INSERT로 인라인화한다(파이프라인 외부에서 ledger를
조작할 일이 없으므로). `mrdoc status`(산출물 존재로 상태 도출)는 운영 디버깅용으로 유지 —
설계가 명시하는 "ledger와 독립된 두 번째 소스, 불일치 = 유실 감지" 장치다.

### A2. 위성 에이전트: claude CLI 멀티턴 (기존 ai_runner 패턴 확장)

설계의 위성 규약(Read/Write만, Grep 금지, BUDGET wrap-up, Report-first, RECEIPT copy)은
claude CLI `-p` 멀티턴 호출로 실현한다. `ai_runner`가 이미 `--allowedTools`·`--max-budget-usd`를
쓰므로 확장 지점이 명확하다. 확인이 필요한 갭(Phase 0 스파이크 대상):

- `maxTurns` 플래그 지원 여부와 위성당 턴 상한 설정
- `--allowedTools "Read,Write"` 수준의 세분화와 Grep 물리 차단 확인
- Read discipline(지정 파일 외 읽기 시도)의 실제 거동

폴백: 멀티턴이 불안정하면 "컨텍스트를 파일로 주입하는 단발 호출"로 강등 가능하다. 이 경우
Report-first/RECEIPT 규약은 유지되고 비결정성만 약간 증가한다(결정론 툴 체인이 대부분을
담당하므로 영향은 국소적).

### A3. 워크스페이스: sha8 키 + base/snapshot 두 트리

```
workspaces/<project_id>/<mr_iid>/.work/<mr_iid>-<head_sha8>/
  base/       ← diff_refs.base_sha 체크아웃
  snapshot/   ← head 체크아웃
  00-changeset.md, 05-structure.md, ... , report.html
```

- 키에 head sha8이 들어가므로 연속 푸시에도 이전 라운드 산출물이 보존된다(멱등·재시도 안전).
- 현재 `ensure_workspace`·`checkout`을 재사용해 두 트리를 만든다. base는 `git worktree` 또는
  얕은 아카이브 체크아웃 — 대형 MR에서의 비용은 Phase 0-3에서 측정한다.
- 리터럴 차집합은 두 트리의 파일 시스템 대비로 계산한다. GitLab API diff는 changeset 정규화
  입력으로만 쓰고, 최종 판정은 로컬 대비로 한다(레이트리밋·truncation 회피).

### A4. 산출물은 파일 기반, DB 스키마 변경 없음

설계의 모든 노드 산출물은 워크스페이스 안 마크다운 파일이다. 이를 그대로 가져간다 —
`review_session`/`opinion`/`event_log` 테이블은 변경하지 않고, event_log가 ledger를 겸한다.
이유: 파일은 "위성이 쓰고 파서가 읽는" 계약의 자연스러운 매체이고, 상태 재구성(`mrdoc status`)이
산출물 존재 검사로 가능하다. DB에 산출물을 정규화해 넣으면 계약이 DB 스키마에 결합되어
설계의 산출물 소비 관계(TREE/CHANGED/MOVED, 차집합 3종 등 frontmatter 계약)가 경직된다.

### A5. 라우팅: 문서 MR만 mrdoc으로, 코드 MR은 기존 경로 유지

mrdoc은 마크다운 문서 MR 특화 설계다. `handle_mr_open` 진입 시 변경 파일 중 마크다운 비중이
임계치 이상이면 mrdoc 파이프라인, 아니면 기존 단발 리뷰로 분기한다(기능 플래그
`mrdoc_enabled` 뒤에 둔다). 혼합 MR은 마크다운 비중으로 판정하고, 비(非)마크다운 파일은
changeset에서 SKIPPED 처리해 리포트에 명시한다. 코드 MR 전면 전환은 1차 범위 밖이다.

---

## 4. 단계별 계획

### Phase 0 — 검증 스파이크 (게이트)

| # | 항목 | 내용 | 완료 조건 |
|---|---|---|---|
| P0-1 | CLI 멀티턴 위성 실증 | analyzer 시제 1개를 실측 MR 1건으로 구동. Read/Write 퍼미션, maxTurns, BUDGET wrap-up, exit 규약 확인 | 위성 규약 준수 입증 또는 폴백(단발 파일 주입) 결정 |
| P0-2 | **리터럴 밀도 선행 측정** | 최근 문서 MR 표본(≥10건)에서 절당 리터럴 개수 측정 | 절당 ≥3 → 설계 그대로 착수 / <1 → 레벨 근거 재설계 후 재검토. **결과를 이 문서 §7에 기록** |
| P0-3 | base/snapshot 트리 | 대형 MR에서 두 트리 체크아웃 비용 측정, GitLab API diff와 로컬 diff 정합 확인 | 비용·정합성 기록, 워크스페이스 설계 확정 |

P0은 코드를 main에 머지하지 않고 스파이크 브랜치/로컬 스크립트로 진행한다.

### Phase 1 — 툴 체인 (결정론 부분)

`app/mrdoc/` 패키지 신설. 전부 순수 Python이므로 **단위테스트 완전 커버 가능** — 이 phase의
품질 기준은 테스트 커버리지다.

- `changeset.py` — 파일 인덱스, file_id sha8, diff_refs, SKIPPED, rename 판정
- `structure.py` — 마크다운 절 경계, norm(text) 해시, MOVED 판정 (설계의 "판정을 포기하고
  삭제+신규로 떨어뜨린다" 원칙 포함)
- `literals.py` — 차집합 3종(removed/added/changed)
- `workspace.py` — §3-A3 두 트리 생성
- `dispatch.py` + `status.py` — 웨이브 테이블, 산출물 존재 기반 상태 도출
- `orchestrator.py` — 루프(§3-A1), event_log 통합

### Phase 2 — analyzer + collect + render (기능 플래그 뒤 1차 가동)

- doc-analyzer 위성: per-file 팬아웃(3–5 하드캡, max_files 40), FINDING/CHECK/레벨 계약
- `collect.py`: 역추적 사슬 검증, quote 대조, dedup, severity, verdict(BLOCKER>0→BLOCK,
  MAJOR>0 또는 L1>0→REVIEW, 나머지 PASS), must_read 상위 5
- render: `report_html.py`에 mrdoc 데이터 모양 추가(기존 코드 MR 렌더와 공존), slack-summary.txt 신규
- 라우팅(`mrdoc_enabled` + 마크다운 비중)과 **"리뷰 시작" 선행 게시 → 완료 시 스레드 업데이트** UX
  (파이프라인이 수 분 걸리므로 인라인 await 폐지, 백그라운드 태스크화 — sha8 키 덕에 재시도 멱등)

### Phase 3 — verifier + 단일 루프 완성

- doc-verifier 위성: before/after 충실도, 순수 서술문 절 L1↔L2 감사, 32-verify.md 산출
- levelcheck 완성: 승격만, 강등 없음, "승격 후 CHECK 부활"
- 재호출 루프: CHECK 미응답 1건 종료조건, 최대 1회. 역추적·quote 실패 finding은 재요청 없이 버림

### Phase 4 — 부가 3개 + 완전판 (2차)

- doc-mapper, checklist, coverage 노드 추가
- 역추적 사슬 완전판: CHECK → FINDING.check_id → SENTENCE.refs 전 구간
- must_read 정밀화(1 BLOCKER → 2 L1 절 → 3 MAJOR → 4 finding 걸린 L2절, 상위 5)

**1차 범위 = Phase 0–3.** 서브커맨드 8종(changeset·structure·literals·levelcheck·collect·render·
dispatch·status), 에이전트 4종(Python 오케스트레이터 + analyzer·verifier·reporter CLI 위성).

---

## 5. 리스크와 대응

| # | 리스크 | 영향 | 대응 |
|---|---|---|---|
| R1 | claude CLI 멀티턴 위성 미실증 (maxTurns, Read discipline) | Phase 2–3 전체 차단 | P0-1 스파이크가 게이트. 실패 시 단발 파일 주입 폴백 |
| R2 | 리터럴 밀도 낮음 | 레벨 체계(L1/L2/L3) 근거 상실 | P0-2 측정이 게이트. 미달 시 레벨 근거 재설계 후 재검토 |
| R3 | 파이프라인 지연(수 분) | Slack 알림 지연 체감 | "리뷰 시작" 선행 게시 + 완료 업데이트. sha8 키로 멱등 재시도 |
| R4 | opus per-file 팬아웃 비용 | 건당 비용 증가 | 위성당 BUDGET wrap-up, 팬아웃 3–5 캡, max_files 40, 모델·예산 설정화 |
| R5 | 코드 MR 혼입 | 마크다운 특화 툴의 오판 | 라우팅 임계 + SKIPPED 명시. 코드 MR은 기존 경로 유지 |
| R6 | Windows 경로·cwd 불일치 | 위성이 다른 루트에 성공하는 "조용한 무한 실패" | 설계의 절대경로 강제 그대로 적용(dispatch가 전 경로 절대경로 출력), 경로 구분자 정규화 |

---

## 6. 설정 변경 (config.py)

추가 항목(전부 기본값 안전 — 플래그 off면 기존 동작 100% 유지):

```python
mrdoc_enabled: bool = False            # 마스터 기능 플래그
mrdoc_doc_ratio_threshold: float = 0.8 # 마크다운 비중 라우팅 임계
mrdoc_satellite_model: str = ""        # 미설정 시 ai_model 상속
mrdoc_satellite_budget_usd: float = 1.0  # 위성당 예산 (ai_max_budget_usd 패턴 재사용)
mrdoc_max_files: int = 40
mrdoc_fanout: int = 5
```

DB 스키마 변경: **없음** (§3-A4).

---

## 7. Phase 0 측정 기록 (스파이크 완료 시 기입)

| 항목 | 측정값 | 판정 |
|---|---|---|
| 리터럴 밀도 (절당 개수, 표본 N건) | _기입_ | 설계 그대로 / 재설계 |
| CLI 멀티턴 위성 규약 준수 | _기입_ | 멀티턴 채택 / 폴백 |
| base/snapshot 트리 비용 | _기입_ | 워크스페이스 설계 확정 |

