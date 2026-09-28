# 리바이스 루프 LLM 워크플로우 제안

> **상태: 제안 (미승인·미구현).** 슬랙 [의견] 제출 이후 도는 리바이스 루프에 다단계 LLM
> 워크플로우를 넣기 위한 설계 의견서다. 구현 착수 전에 §12(문서 계약 충돌)와 §13(미결 사항)을
> 먼저 결론내야 한다.

---

## 1. 지금 구조와 문제

현재 [의견]이 관문을 통과하면 `ClaudeCliRunner`가 **헤드리스 `claude` 1회 호출**로 끝난다
(`app/ai_runner.py:161`). 의견 본문과 체크아웃된 워크스페이스를 던져주고 "각 의견을 반영하고
`{applied, unapplied, summary}` JSON으로 보고하라"고 시키는 단발 구조다.

이 구조에서 검증되지 않는 것이 셋 있다.

| # | 검증되지 않는 것 | 지금 일어나는 일 |
|---|---|---|
| 1 | 사람이 말한 "그 부분"이 문서에 **실제로 있는가** | 없으면 모델이 비슷한 곳을 고치거나 지어낸다 |
| 2 | 같이 고쳐야 하는 곳(같은 버전 문자열, 그 절을 가리키는 링크)이 있는가 | 아무도 찾지 않는다 |
| 3 | "반영했다"는 보고가 **사실인가** | 보고를 쓴 모델 자신 말고 대조하는 주체가 없다 |

목표는 이 셋을 각각 단계로 세우되, **판단만 LLM에게 주고 검색·존재검사·대조는 결정론 도구가
하게** 만드는 것이다.

### 확정된 전제

| 항목 | 결정 |
|---|---|
| 대상 범위 | **문서(.md/.mdx) 전용** |
| 대상을 못 찾았을 때 | **라운드를 소모하지 않고 슬랙으로 되물음** |
| LLM 호출 수 | **3회 — 구조화(3a) / 편집(3b) / 검증(3c)** |
| 부분 실패 | **성공분만 커밋, 실패분은 3c 판정문을 사유로 남김** |

> "라운드 1 차감"은 **`round`를 증가시키지 않는다**로 해석했다(음수 방지). 다른 의도라면 정정 필요.

---

## 2. 원안 8단계 평가

| 원안 | 평가 | 제안 |
|---|---|---|
| 1. 의견 구조화 (코드 참고 X) | ⚠️ 수정 | **본문 대신 목차**를 준다. 컨텍스트 0이면 앵커를 지어낸다 |
| 2. 대상 존재 검사 | ⚠️ 담당 변경 | LLM → **기계**(섹션 인덱스 + 문자열 검색) |
| 3. 연관 부분 탐색 | ⚠️ 담당 변경 | LLM → **기계**(리터럴 역검색 + 링크 추적) |
| 4. 동반 변경 판정 | ✅ 유지 | 단, 3번 결과 중 **선별만** LLM. 5번과 한 호출로 합침 |
| 5. 반영 | ✅ 유지 | 앵커 ±20줄 창 한정 |
| 6. 반영 검사 | ❌ 그대로면 자기채점 | **기계 게이트 + 분리된 검증 호출** 2겹 |
| 7. 변경분 추출 + 리포트 | ⚠️ 축소 | 추출은 mrdoc 방식, **렌더는 기존 것 재사용** |
| 8. 슬랙 전달 | ✅ 유지 | 기존 경로 그대로 (수정 불필요) |

### 2.1 왜 1번을 "코드 참고 X"로 두면 안 되는가

사람의 의견은 "이 부분", "여기 좀", "위에 있는 그거"처럼 지시어가 많다. 아무 컨텍스트 없이
구조화하면 모델이 대상을 **지어낸다**. 지어낸 대상은 2번 존재 검사에서 걸리지만, 그때는 이미
"무엇을 물어봐야 하는지"조차 알 수 없는 상태가 된다.

대신 **문서 전문이 아니라 목차**(변경 파일 목록 + 헤딩 트리)만 준다. 지어낼 여지는 막고
지시어는 해석 가능해진다. 그리고 구조화 산출물에 **"원문에 그대로 있을 문자열"(`search_terms`)**을
반드시 뽑게 한다 — 이것이 다음 단계에서 기계가 검증할 수 있는 유일한 앵커다.

### 2.2 왜 2·3·4번을 LLM에게 주면 안 되는가

이 저장소에는 이미 필요한 도구가 **순수 함수로** 있다 (§6 참조). 존재검사·연관탐색·동반변경
후보 추출은 전부 결정론으로 된다. LLM은 *무엇을 찾을지*(쿼리 생성)와 *찾은 것 중 무엇이
진짜인지*(선별)만 하면 된다.

같은 결론이 `docs/mrdoc-pipeline.html`에 이미 명문화돼 있다:

> **경계는 하나다 — 두 문자열의 대조로 환원되면 툴이다.** 존재하나 · 개수 맞나 · 원문에 있나 ·
> 공집합이냐 · 같냐.

> **Grep을 아무 위성에도 주지 않는다 — 검색은 툴이 리터럴로 한다. Grep이 있으면 LLM이 검색어를
> 짓기 시작하고, 검색어 생성은 대조할 대상이 없어 검증이 불가능하다.**

### 2.3 왜 6번이 원안의 가장 약한 고리인가

편집한 모델에게 자기 결과를 검사시키면 통과한다. `docs/mr-review-pipeline.html` v3.1 개정 이유가
정확히 이것이다 — "3c 검증을 독립 컨텍스트로 분리해 **자기 채점 편향 제거**".

### 2.4 왜 8단계가 LLM 8회가 아닌가

- `ai_max_budget_usd`는 **CLI 호출당** 적용된다 → 8회 = 최대 $8/라운드
- `revise_wall_clock_seconds`는 900초, 그 위에 `revising_timeout` 1200초 스위퍼가 있다
- 단계마다 컨텍스트를 다시 실어 보내는 비용이 붙는다

3회(구조화 / 편집+선별 / 검증)면 8단계를 전부 덮는다.

---

## 3. 권장 워크플로우

```
[기계] 미반영 의견 로드 + 문서 목차 생성
   │        applied_round IS NULL AND created_at <= 잠금 시각
   ▼
[LLM 3a] 구조화 ─────────────────────────────► 00-intent.md
   │   입력: 의견 원문 + 변경 파일 목록 + 헤딩 목차 (본문 X)
   │   출력: 의견별 {대상문서[], 범위, 대상주장, 수정방향, 근거,
   │                통합[], 충돌해소, search_terms[]}
   ▼
[기계] 앵커 확정 ────────────────────────────► 10-anchor.md
   │   search_terms를 섹션 인덱스에서 검색 → (file, section_id, line)
   │   0건이면 MISSING + 근접 후보 3개
   ▼
[기계] 동반 변경 후보 ───────────────────────► 20-impact.md
   │   앵커 유닛의 리터럴/링크를 head 트리 전체에서 역검색
   ▼
[LLM 3b] 선별 + 편집 ────────────────────────► 30-edit.md + 파일 수정
   │   입력: intent + anchor + impact + 앵커 ±20줄 창
   │   출력: 영수증 (처리한 opinion_id / 못 한 것 + 사유 / 만진 파일)
   ▼
[기계] 게이트 ───────────────────────────────► 35-gate.md
   │   경로 화이트리스트 · 리터럴 실증 · 규모 상한 · 잔여물
   │   위반분은 되돌림
   ▼
[LLM 3c] 대조 검증 ──────────────────────────► 40-verify.md
   │   입력: 의견 원문 + intent + git diff  (30-edit.md는 주지 않음)
   │   출력: 의견별 applied|partial|unapplied + 근거(파일:라인) + 판정문
   ▼
[기계] 리포트 추출 ──────────────────────────► 50-summary.md
   │   3슬롯(summary / key_changes / points_to_watch) 채우기
   ▼
[기존] 경로 한정 커밋 · 푸시 · 새 슬랙 알림 · report.html 첨부
```

### 3.1 산출물은 파일로 고정한다

지금은 재시도가 **처음부터 다시 돈다**. 각 단계 산출물을 파일로 떨어뜨리고 **파일 존재로 진행
상태를 유도**하면(`app/mrdoc/dispatch.py:87 derive_state` 패턴) 죽은 지점부터 재개된다.

```
<workspace_root>/.revise/<project_id>/<mr_iid>/r<round>/
  00-intent.md     3a 산출 — 의견별 구조화 스펙
  10-anchor.md     기계 — 앵커 확정 / MISSING + 후보 3개
  20-impact.md     기계 — 동반 변경 후보 (리터럴·링크 근거)
  30-edit.md       3b 영수증
  35-gate.md       기계 게이트 결과
  40-verify.md     3c 판정
  50-summary.md    기계 — 리포트용 3슬롯
  ledger.md        append-only
```

**⚠️ 아티팩트 루트는 반드시 git 클론 바깥이어야 한다.** `git_workspace.commit_all`이
`git add -A` 고정이라(`app/git_workspace.py:266`) 워크스페이스 안에 쓴 중간 파일은 그대로
MR 브랜치에 커밋·푸시된다. 워크스페이스는 `<workspace_root>/{project_id}/{mr_iid}`이므로
`.revise/`는 그 형제 위치에 둔다.

`edit` 노드를 재실행할 때는 `00-intent.md` frontmatter의 `base_sha`로 md 경로만 되돌려
항상 깨끗한 상태에서 시작한다.

---

## 4. LLM 세 자리의 계약

공통 규약은 mrdoc에서 이미 검증된 패턴을 그대로 가져온다.

- **템플릿 문자열을 손으로 쓰지 않고 파서 모듈의 render 함수로 만든다** — 프롬프트와 파서가
  한 소스를 공유해야 드리프트가 물리적으로 불가능해진다
- 필수 필드 `STATUS / UNCOVERED / UNCERTAIN / CONFIDENCE`를 **생략 불가**로 둔다 —
  optional로 두면 "해당 없음"과 "안 함"이 구분되지 않는다
- **Grep을 주지 않는다** — 검색은 기계가 한다
- 리스트-객체는 한 줄 flow(`key: [{a: 1}]`)만 — 블록 시퀀스는 파서가 받지 않는다

### 4.1 3a INTENT — 구조화

| | |
|---|---|
| **입력** | 의견 원문 + 변경 파일 목록 + **헤딩 목차만** (`markdown_tree.parse_sections`로 생성) |
| **출력** | 의견별 `{opinion_id, 대상문서[], 범위: local\|cross-doc, 대상주장, 수정방향, 근거, 통합[], 충돌해소, search_terms[]}` |
| **도구** | 없음 (`--tools ""`, 텍스트 in/out) |

**HARD LIMITS**
1. 목차에 없는 파일을 지목하지 않는다.
2. `search_terms`는 **원문에 그대로 있을 문자열**만 쓴다. 자기가 지어낸 요약문은 금지 —
   이 문자열이 다음 단계에서 기계가 검증할 유일한 앵커다.
3. 수정 방법을 스스로 확장하지 않는다. 의견에 없는 개선은 쓰지 않는다.

> 필드 8개는 `docs/mr-review-pipeline.html` §S4② 3a의 기존 스펙을 그대로 유지하고
> `search_terms`만 추가한 것이다.

### 4.2 3b EDIT — 선별 + 동반 변경 + 편집

| | |
|---|---|
| **입력** | `00-intent.md` + `10-anchor.md` + `20-impact.md` + 앵커 유닛의 **±20줄 창** |
| **출력** | `30-edit.md` 영수증 — 처리한 opinion_id / 못 한 것 + 사유 / 만진 파일 목록 |
| **도구** | 파일 편집 (`--permission-mode acceptEdits`) |

**HARD LIMITS**
1. 앵커·동반 후보로 **지목된 파일 밖을 수정하지 않는다.**
2. 파일 통독 금지. 같은 파일 재Read 금지.
3. 못 한 의견은 반드시 사유와 함께 남긴다. 침묵은 누락으로 처리된다.

> 프롬프트 금지에 의존하지 않는다. §5의 게이트가 **기계로 되돌린다.**

### 4.3 3c VERIFY — 대조 검증

| | |
|---|---|
| **입력** | 의견 **원문** + `00-intent.md` + `git diff`(md 경로 한정) + 앵커 유닛 before/after |
| **출력** | `40-verify.md` — 의견별 `applied\|partial\|unapplied` + 근거(파일:라인) + 판정문 |
| **도구** | Read만 |

**HARD LIMITS**
1. **`30-edit.md`를 주지 않는다** — 자기채점 방지 (문서 §S4② 명문).
2. 판정 축은 **의견 원문**이다. 스펙(3a 산출)은 참고용 — 3a가 원문을 잘못 옮긴 오역도 여기서 잡힌다.
3. 애매하면 `partial`이나 `unapplied`로 둔다. 확신 없는 `applied`는 거짓 영수증이다.
4. 원문을 읽지 않고 판정하지 않는다. 못 읽었으면 UNCOVERED.

파이썬 오케스트레이터가 3c 판정과 3b 영수증을 대조하고 **불일치 시 3c를 신뢰**한다
(워커의 "했다"는 주장이지 증거가 아니다).

### 4.4 재투입 예산

게이트 위반이나 3c 미반영 발견 시 원인을 분류해 재투입한다.

| 원인 | 재투입 대상 |
|---|---|
| 구현 누락 (스펙은 맞는데 안 고침) | 3b — 3c 근거를 첨부해 재투입 |
| 스펙 모호·모순 | 3a — 해당 의견만 재투입 후 갱신 스펙으로 3b 재실행 |

**라운드당 합산 2회 한정, 사람 라운드는 소모하지 않는다.** 소진되면 정직 표기 후 전진 —
모든 경로가 "성공" 또는 "표기 후 진행"으로 끝나 무한 루프가 없다.
(`docs/mr-review-pipeline.html` §S4② 수렴 규칙 그대로 유지)

---

## 5. 결정론 게이트 — 이 설계의 핵심

`35-gate.md`가 LLM 보고를 믿지 않고 기계로 확인한다.

### 5.1 경로 화이트리스트
`git diff --name-only` 결과가 전부 `.md/.mdx`이고 전부 앵커/동반 후보 파일 집합 안인가.
벗어난 파일은 **그 파일만 되돌린다.**

### 5.2 리터럴 실증
의견이 "A → B" 형태의 치환을 요구했다면 `extract_literals`로 확인한다:

```
A ∈ literals(base 유닛)  AND  A ∉ literals(head 유닛)  AND  B ∈ literals(head 유닛)
```

**⚠️ 두 가지 함정을 반드시 우회할 것**

1. `extract_literals`는 `(kind, key, value)`로 **완전 dedup**한다 → 출현 **횟수**가 사라진다.
   "3곳 중 3곳 다 치환됐나"는 판정 불가. "그 값이 head 유닛에 더는 없다"까지만 말할 수 있다.
2. 리터럴 5종(kv/unit/bool/code/link) **밖의 평문 용어 교체는 아예 잡히지 않는다.**
   → `markdown_tree.section_text` + 문자열 `in` 검사를 **병행**해야 한다.

또한 `UnitLiterals.changed`는 같은 키의 값이 여러 개일 때 정렬 후 위치로 zip해 from→to를
만들기 때문에 **매핑이 어긋날 수 있다.** 단일 치환 검증에는 `changed` 튜플을 믿지 말고
위 3항 차집합을 직접 조립하는 편이 안전하다.

### 5.3 규모 상한
변경 파일 수 / 라인 수 상한 초과 시 전량 되돌리고 `kind=failed`.

### 5.4 잔여물 검사
워크스페이스에 untracked 파일이 생겼는지 확인한다. `checkout`이 쓰는 `git checkout -B`는
**untracked를 지우지 않으므로** 라운드 간 누적되고 `git add -A`에 그대로 잡힌다.

---

## 6. 재사용 지점

mrdoc의 결정론 모듈은 **전역 상태·설정·파일시스템 의존이 없는 순수 함수**라 rail 없이 그대로 쓸 수 있다.

| 쓸 것 | 위치 | 용도 |
|---|---|---|
| `parse_sections(text)` | `app/mrdoc/markdown_tree.py:54` | 헤딩 트리 → 3a 목차, 앵커 인덱스 |
| `section_text` / `section_lines` | `app/mrdoc/markdown_tree.py:147` | ±20줄 창, 평문 검색 |
| `extract_literals(text)` | `app/mrdoc/literals.py:74` | 리터럴 실증 (순수 함수) |
| `section_id` / `unit_id` | `app/mrdoc/ids.py:38` | 안정적 id — section_id에서 unit_id 계산 가능 |
| `parse_frontmatter` / `render_frontmatter` | `app/mrdoc/frontmatter.py` | 산출물 헤더 |
| `derive_state` 패턴 | `app/mrdoc/dispatch.py:87` | 파일 존재 기반 재개 |
| `render_review_report` | `app/report_html.py:72` | **3슬롯 덕타이핑** — 리포트 렌더 |
| `_deliver_review_report` | `app/ingest.py:338` | 로컬 아카이브 + 슬랙 스레드 업로드 |
| `post_revise_result` | `app/slack_client.py:302` | 새 라운드 알림 |
| `_redact` / `_redaction_secrets` | `app/git_workspace.py:125` | LLM 산출 텍스트 마스킹 |

**7번(리포트)을 새로 만들지 않는 이유**: `render_review_report`는 `review.summary` /
`review.key_changes` / `review.points_to_watch` 세 속성만 읽는 덕타이핑이고,
`app/revise_executor.py:504`가 이미 `SimpleNamespace`로 이 경로를 쓰고 있다.
`50-summary.md`가 그 3슬롯을 채우면 된다.

- `summary` ← 3c 요약
- `key_changes` ← **리터럴 차집합에서 기계 생성** (원안 "mrdoc와 유사 방법으로 추려낸다"에 해당)
- `points_to_watch` ← 3c 미반영/부분 판정문

mrdoc의 `render_report_html`로 갈아끼우는 것도 나중에 가능하다 — 합성 frontmatter
(`verdict`만 collect와 일치)로 reporter 위성 없이 동작한다.

---

## 7. 구조적 제약 (반드시 지킬 것)

| # | 제약 | 근거 |
|---|---|---|
| 1 | 중간 산출물은 **클론 바깥**에 | `commit_all`이 `git add -A` 고정 (`app/git_workspace.py:266`) |
| 2 | **새 세션 상태를 만들지 않는다** | `review_session.status`에 `CHECK` 제약(`app/db.py:41`) + 마이그레이션 코드 부재 → 테이블 재생성 필요 |
| 3 | **DB 컬럼을 추가하지 않는다** | 같은 이유. `question_refs`·`last_verdict`는 이미 있고 비어 있으니 그것을 쓴다 |
| 4 | 실행기 반환 계약 유지 | `{kind: ok\|failed, unapplied: [{opinion_id, reason}]}` — 코어가 이 모양에 의존 |
| 5 | `applied_round`는 3c "반영" 판정에만 기록 | NULL = 다음 라운드 자동 재포함 (재제출 불필요) |
| 6 | 타이머 불변식 유지 | 큐 대기 < 세션 벽시계 < 스위퍼 임계값. 단계를 늘리면 **셋 다** 올려야 한다 |
| 7 | LLM 산출 텍스트는 DB/슬랙 전에 마스킹 | `_redact(text, _redaction_secrets(settings))` + 슬랙 노출 300자 캡 |
| 8 | 프롬프트에 넘기는 경로는 `Path.as_posix()` | Windows 백슬래시가 JSON 파싱을 깨뜨린 실측 있음 |

---

## 8. 라운드 회계 — 되물음

| 상황 | 처리 |
|---|---|
| 전부 MISSING + 커밋 없음 | `round` **증가 없이** `revising→reviewing` CAS, 재알림에 질문 + 후보 3개 |
| 일부만 MISSING | 커밋이 났으므로 라운드 정상 소모. 못 찾은 의견은 unapplied + 질문 첨부 |
| 되물음 상한 | `event_log`에 `kind='clarify'`를 남기고 **세션당 2회**. 초과 시 그 의견만 `last_verdict="대상 미확인 — 재질문 한도 소진"`으로 마감하고 라운드 정상 진행 |

**새 상태를 만들지 않는다.** 되물음은 "라운드를 소모하지 않은 재알림 + 질문 문안"일 뿐이고,
사람은 **[의견]을 다시 눌러 답한다.** 기존 레일을 그대로 타므로 상태기계·스위퍼·웹훅 분기를
건드릴 필요가 없다.

사람의 답변은 새 `opinion` 행이 되고, `unapplied_opinions`의 `created_at <= locked_at`
컷오프(`app/db.py:137`) 때문에 자동으로 다음 라운드 몫이 된다 — 의도한 동작이다.

---

## 9. 코드 변경 범위 (개요)

### 신규 패키지 `app/revise_workflow/`

워크플로우를 `AIRunner` 구현체 하나로 캡슐화해 실행기 변경을 최소화한다.

| 파일 | 역할 |
|---|---|
| `runner.py` | `StagedRunner` (`AIRunner` 프로토콜 구현) — 노드 루프 + 재투입 예산 |
| `nodes.py` | 결정론 노드: anchor / impact / gate / summary-extract |
| `schema.py` | 3a·3b·3c 산출물의 파서 + 렌더러 (프롬프트와 한 소스) |
| `prompts.py` | 세 시스템 프롬프트 + HARD LIMITS |
| `workspace.py` | 아티팩트 경로 규칙 + 파일 존재 기반 상태 유도 |

### 기존 파일 수정 (전부 가산적, 기본값은 현행 유지)

| 파일 | 변경 |
|---|---|
| `app/ai_runner.py` | `ReviseResult`에 선택 필드 2개 (`commit_paths`, `clarify`). `_select_runner`(`:67`)에 `"staged"` 분기 |
| `app/git_workspace.py` | `_git`(`:167`)을 감싼 `commit_paths` / `restore_paths` / `changed_paths` 추가. `commit_all`은 그대로 |
| `app/revise_executor.py` | `_handle_ok`(`:300`)에서 경로 한정 커밋, clarify 라운드 회계 |
| `app/state_machine.py` | `reset_attempts_only` 추가 (round 증가만 뺀 것). **상태 추가 없음** |
| `app/slack_client.py` | 되물음 블록(질문 + 후보 3개). 미반영 목록에 `opinion_id`/본문 머리말 추가 |
| `app/config.py` | §10 설정 |

> 미반영 목록 개선은 별건이지만 값싸다 — 현재 `_revise_result_payload`가 `reason`만 렌더해
> **어떤 의견이 왜 미반영인지 알 수 없다.**

---

## 10. 설정

```
ai_runner                        "stub" | "claude" | "staged"   # 기본값 유지 → 무회귀
revise_stage_timeout_seconds     600     # 단계 1회 상한
revise_stage_budget_usd          0.5     # 단계별 예산 (아래 주의 참조)
revise_wall_clock_seconds        900  → 2400
revising_timeout                 1200 → 3000    # 불변식: 스위퍼 > 벽시계 + 여유
revise_max_changed_files         10      # 게이트 규모 상한
revise_max_changed_lines         400
revise_clarify_limit             2
```

**⚠️ `ai_max_budget_usd`는 단계별이 아니라 CLI 호출당 적용된다.** 지금 값을 그대로 두면
실질 상한이 3배가 된다. 단계별로 나눠 걸려면 `revise_stage_budget_usd`를 따로 둬야 한다.

---

## 11. 검증

### 단위 테스트 (관행 준수)

LLM 대역은 `monkeypatch.setattr("<module>.subprocess.run", fake_run)`으로 argv/env/cwd/stdin을
전량 캡처한다 (`tests/test_ai_runner.py` 패턴).

| 파일 | 검증 |
|---|---|
| `tests/test_revise_workflow_nodes.py` | anchor/impact/gate를 **LLM 없이** — 화이트리스트 밖 파일 되돌림, 리터럴 미치환 적발, 규모 초과 전량 되돌림 |
| `tests/test_revise_workflow_runner.py` | 스키마 유효 산출물을 쓰는 fake agent로 완주. **거짓말 agent(성공 반환 + 산출물 없음)가 no-progress abort에 걸리는지**. 재개(중간 아티팩트 존재 시 앞 노드 건너뜀) |
| `tests/test_revise_executor.py` (확장) | 경로 한정 커밋, clarify 라운드에서 `round` 불변 |

**각 LLM 경계마다 실패 4종 필수**: non-zero exit / timeout / 파싱 실패 / **거짓말**.
**시크릿 단정 필수**: 자식 env에 앱 시크릿 부재, 산출 문자열에 `_redact` 적용, 슬랙 노출 300자 캡.

fake agent는 문자열을 하드코딩하지 말고 **프로덕션 파서/렌더러를 그대로 호출**한다 —
"템플릿은 렌더러가" 규약을 테스트에서도 강제하기 위함이다.

### 회귀

`uv run pytest` 전체. 기준선 `192 passed`(2026-08-11) 이상.
`AI_RUNNER=stub` / `claude` 경로가 그대로 통과해야 한다.

### 실검증

`AI_RUNNER=staged`로 product-common !11에 의견 1건 제출 →
`.revise/.../r*/` 아티팩트 8개 생성 확인 → 커밋이 md만 건드렸는지 → 슬랙 재알림·리포트 확인.
대상 없는 의견("존재하지 않는 절을 고쳐라")을 넣어 **되물음 + `round` 불변**까지 확인.

---

## 12. 문서 계약 충돌 — 착수 전 결론 필요

`docs/mr-review-pipeline.html:554`가 v3.1 결정으로 못 박고 있다:

> CLAUDE 단일 세션(호출 1회) — 최상위는 오케스트레이터. 3a·3b·3c는 각각 독립 컨텍스트의
> 서브에이전트로 수행. **단계별 별도 CLI 호출 금지.** 오케스트레이터는 의견 원문·문서 전문을
> 직접 읽지 않고 구조화 산출물과 영수증만 다룬다

**3회 별도 호출은 이 문장을 정면으로 뒤집는다.** 뒤집을 근거는 둘 있다.

1. **실측** — headless `claude -p`에서 `--safe-mode`가 `--agents`를 무력화하고,
   `run_in_background` 기본값 탓에 서브에이전트 결과 회수가 **종료 코드 0인 채 조용히 실패**한다
   (스파이크 3회 + 격리 프로브 4조합, 실호출 $2 소요). 단일 세션 오케스트레이션은
   **실패가 관측되지 않는 구조**다.
2. **선례** — 나중에 설계된 mrdoc이 이미 노드별 개별 `codex exec` 호출 + 산출물 파일 존재
   검증으로 갔고(`app/mrdoc/satellites.py`), 그 판단이 `docs/mrdoc-pipeline.html`에 문서화돼 있다.
   `docs/mrdoc-migration-plan.md` §3-A2도 "멀티턴이 불안정하면 **컨텍스트를 파일로 주입하는
   단발 호출로 강등 가능**하다"고 이미 열어 뒀다.

또한 `docs/mrdoc-migration-plan.md` §2가 revise 루프를 **"변경 없이 유지"** 대상으로 명시했으므로
이 역시 뒤집어야 한다.

> **판단**: 뒤집는 것이 맞다고 본다. 다만 **문서 개정 없이 코드만 바꾸면 문서와 구현이 갈라진다.**
> 이 저장소는 개정 이력(`[n] ... v4.x 개정`)과 `.orchestration/reports/` 영수증을 남기는 관행이
> 확립돼 있으므로, v4.3 개정 + 영수증을 착수와 같은 단위로 묶어야 한다.

---

## 13. 미결 사항

1. **"라운드 1 차감"의 정확한 의미** — `round` 미증가로 해석했다. `round - 1`을 의도했다면
   0에서 음수가 되는 경우의 가드가 별도로 필요하다.
2. **같은 파일에 성공 의견과 실패 의견의 편집이 섞였을 때** — 파일 단위로만 분리 가능하다.
   현재 제안은 "성공 의견이 하나라도 있으면 그 파일은 커밋". 더 엄격하게 가려면 편집을
   의견 단위로 직렬화해야 하는데 호출 수가 늘어난다.
3. **대상 문서가 많을 때의 3b fan-out** — 호출 3회 결정을 유지하면 배치 분할이 필요하다.
   mrdoc의 `fanout`(기본 5) 패턴을 그대로 쓸 수 있으나 이는 단계 수가 아닌 **같은 단계의 병렬 배치**다.
4. **"커밋 없는 라운드"의 처리 규칙이 문서에 없다** — 테스트
   (`test_no_commit_round_sends_none_diff_stat_and_compare_url`)로만 굳어져 있다.
   이번 개정에서 같이 명문화하는 것이 좋다.
5. **`ai_max_budget_usd`를 단계별로 쪼갤지 호출당 그대로 둘지** — §10 참조.
