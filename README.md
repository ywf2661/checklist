# 일일 시스템 점검

종이 점검표를 대신하는 사내 웹 체크리스트.

## 설치 (인터넷 없는 서버)

1. 인터넷 되는 PC에서 **서버와 같은 OS·같은 파이썬 버전**으로 패키지를 받는다.
   ```
   pip download -r requirements.txt -d wheels
   ```
2. 이 폴더 전체와 `wheels/`를 서버로 옮긴다.
3. 서버에서:
   ```
   python -m venv venv
   venv/bin/pip install --no-index --find-links wheels -r requirements.txt   # 윈도우: venv\Scripts\pip
   venv/bin/python app.py init-admin      # 관리자 계정 생성
   venv/bin/python app.py                 # http://서버주소:8080
   ```
   포트·주소 변경: 환경변수 `CHECKLIST_PORT`, `CHECKLIST_HOST`. DB 위치 변경: `CHECKLIST_DB`.

## 처음 쓸 때

1. 관리자로 로그인 → 비밀번호 변경.
2. 관리 화면에서 부서원 계정 추가(임시 비밀번호 전달) → 시스템 추가·담당자 지정.
3. 각 시스템 점검표 화면에서 "+ 항목 추가"로 종이 점검표 항목을 옮겨 적는다.

## 운영

- **자동 실행**: 리눅스는 systemd, 윈도우는 작업 스케줄러("컴퓨터 시작 시")에 `venv/bin/python app.py` 등록.
- **백업**: 매일 한 번 `venv/bin/python app.py backup /백업/경로` 를 스케줄러(cron 등)에 등록.
  사용 중에도 안전하게 복사된다. 복구는 서버를 멈추고 백업 파일을 `checklist.db`로 바꿔 넣으면 된다.
- **지우면 안 되는 파일**: `checklist.db`(모든 기록), `secret.key`(없어지면 모두 다시 로그인해야 함).
- **HTTPS**: 필요하면 사내 리버스 프록시(웹서버)에서 처리한다.
- **테스트**: `venv/bin/python -m unittest -v`
