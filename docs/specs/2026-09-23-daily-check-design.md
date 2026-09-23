# 일일 시스템 점검 웹 체크리스트 — 설계서

- 작성일: 2026-09-23
- 상태: 사용자 검토 대기

## 1. 목적

IT 부서원이 매일 아침 담당 시스템을 점검할 때 쓰는 종이 체크리스트를 웹으로 대체한다.
점검 기록은 감사·내부통제 증적으로 쓰이므로 위변조 방지와 이력 보존이 필요하다.

**성공 기준**
- 부서원이 종이 없이 점검을 마치고 제출한다.
- 팀장이 한 화면에서 미점검 시스템과 이상 건을 확인하고 확인(결재) 처리한다.
- 특정 날짜의 점검 기록을 인쇄/PDF 및 CSV로 제출할 수 있다.

## 2. 제약 / 결정 사항

| 항목 | 결정 |
|---|---|
| 운영 환경 | 사내 서버 직접 설치, 외부 인터넷 없음 |
| 기술 스택 | Python 3 + Flask + SQLite(표준 `sqlite3`) + waitress |
| 외부 패키지 | `flask`, `waitress` 2개만 (오프라인 wheel 반입) |
| 화면 | 서버 렌더링(Jinja2 템플릿), JS 프레임워크·빌드 도구 없음 |
| 로그인 | 앱 자체 ID/비밀번호, `werkzeug.security` 해시 |
| 엑셀 출력 | CSV(UTF-8 BOM, 엑셀에서 한글 깨짐 방지) |
| PDF 출력 | 인쇄용 CSS + 브라우저 "PDF로 저장" |
| 백업 | OS 스케줄러로 `checklist.db` 일일 복사 |

## 3. 구조

```
[부서원 PC 브라우저] --사내망 HTTP--> [waitress + Flask 앱] --> checklist.db
```

파일 구성(예정):
```
app.py            # 라우트 전부
db.py             # 스키마 생성, 연결
templates/*.html  # 화면
static/style.css  # 화면 + 인쇄용 스타일
```

## 4. 사용자 역할

| 역할 | 권한 |
|---|---|
| member (부서원) | 점검 작성/제출/수정(사유), 점검 항목 추가·미사용 처리, 이력 조회 |
| leader (팀장) | member 권한 + 현황판, 확인(결재) |
| admin (관리자) | leader 권한 + 사용자·시스템 관리 |

## 5. 화면

1. **로그인** — ID/비밀번호. 5회 연속 실패 시 계정 잠김(관리자가 해제).
2. **오늘 점검** — 내 담당 시스템 목록과 오늘 상태. 다른 시스템도 선택 가능(대무).
   - 시스템 선택 시 종이와 동일한 점검표: 항목별 체크박스, 맨 아래 비고란 1개.
   - 버튼: 임시저장 / 제출.
   - **"+ 항목 추가"**: 문구 입력 후 추가. 아직 제출하지 않은 점검표에는 바로, 이미 제출한 점검표에는 다음 점검부터 표시.
     항목 옆 "미사용" 버튼으로 숨김 가능(삭제 아님).
3. **현황판(팀장)** — 날짜 선택, 시스템별 상태 표시:
   `미점검 / 작성중 / 제출 / 이상 / 확인완료`. 클릭 시 상세 → 확인 버튼.
4. **이력 조회** — 기간·시스템 검색, 인쇄/PDF 화면, CSV 다운로드.
5. **관리(관리자)** — 사용자(추가, 역할, 비밀번호 초기화, 잠금 해제, 미사용),
   시스템(추가, 담당자 지정, 순서, 미사용), 항목(순서 변경).

## 6. 데이터 모델

```sql
users(id, login_id UNIQUE, name, pw_hash, role, fail_count, must_change_pw, active)
systems(id, name, owner_id, sort, active)
items(id, system_id, text, sort, active)
checks(id, date, system_id, user_id, status, remark, has_issue,
       submitted_at, approved_by, approved_at,
       UNIQUE(date, system_id))
check_items(check_id, item_id, item_text, checked)
audit_log(id, at, user_id, target, target_id, action, before, after, reason)
```

- `status`: `draft` → `submitted` → `approved`
- `check_items.item_text`: 점검 당시 문구를 복사 저장(이후 항목 문구 변경과 무관).
- `audit_log.before/after`: JSON 텍스트.

## 7. 증적 규칙

1. 제출 시 잠김. 수정하려면 사유 필수, 변경 전/후를 `audit_log`에 기록.
2. `approved` 상태에서 수정되면 `submitted`로 되돌아가 팀장 재확인 필요.
3. 체크 안 된 항목이 하나라도 있으면 비고 필수, `has_issue = 1`("이상"으로 표시).
4. 삭제 기능 없음. 사용자·시스템·항목은 `active = 0` 처리만.
5. 항목 추가/미사용, 사용자·시스템 변경도 `audit_log`에 기록.
6. `audit_log`는 추가만 한다(앱에 수정·삭제 경로 없음).
7. 시각은 서버 시간 기준.

## 8. 보안

- 비밀번호 해시 저장(`generate_password_hash` / `check_password_hash`).
- 세션 쿠키: `HttpOnly`, `SameSite=Lax`.
- 모든 POST 폼에 세션 기반 CSRF 토큰.
- 모든 SQL은 파라미터 바인딩.
- 역할 검사는 서버에서 라우트마다 수행.
- 최초 관리자 계정은 초기화 명령으로 생성(`python app.py init-admin`), 첫 로그인 시 비밀번호 변경.

## 9. 에러 처리

- 같은 (날짜, 시스템)을 두 명이 동시에 작성: `UNIQUE` 제약 + 저장 시 상태 재확인,
  이미 제출된 경우 "다른 사용자가 먼저 제출했습니다" 안내.
- 필수값 누락(비고, 수정 사유): 저장 거부 + 화면에 메시지, 입력값 유지.

## 10. 테스트

- `test_app.py` 1개(표준 `unittest` + Flask 테스트 클라이언트, 임시 DB):
  - 로그인 성공/실패/잠김
  - 체크 누락 시 비고 없이 제출 거부
  - 제출 후 사유 없이 수정 거부, 사유 입력 시 `audit_log` 기록
  - 확인 후 수정 시 `submitted`로 복귀
  - member가 팀장 확인 요청 시 거부
  - 항목 추가 후 다음 점검표에 표시, 기존 제출 기록 불변

## 11. 범위 외 (추후)

- 휴일 달력, 메일/메신저 알림, 이상 건 처리 추적, AD/SSO 연동, HTTPS(사내 리버스 프록시 사용 시 거기서 처리).
