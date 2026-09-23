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
2. 관리 화면에서 부서원 계정 추가(임시 비밀번호 전달).
3. 항목 관리 아래 **들여쓰기 붙여넣기**에 종이 점검표를 옮겨 적는다. 들여쓰기로 계층, 줄 끝 `(담당자ID)`가 있으면 점검 항목.
   ```
   네트워크 및 시스템 점검
       네트워크상태
           백본 및 각 층 네트워크 상태 (kim)
           인터넷 방화벽,스위치 상태 (kim)
   ```
   이후 추가·순서 변경은 항목 관리 화면의 **+하위**, **↑↓**, **수정** 버튼으로 한다.

> 이전 버전으로 만든 `checklist.db`가 있으면 실행 시 안내가 나온다. 기록이 없다면 파일을 지우고 다시 실행하면 된다.

## 운영

- **자동 실행**: 리눅스는 systemd, 윈도우는 작업 스케줄러("컴퓨터 시작 시")에 `venv/bin/python app.py` 등록.
- **백업**: 매일 한 번 `venv/bin/python app.py backup /백업/경로` 를 스케줄러(cron 등)에 등록.
  사용 중에도 안전하게 복사된다. 복구는 서버를 멈추고 백업 파일을 `checklist.db`로 바꿔 넣으면 된다.
- **지우면 안 되는 파일**: `checklist.db`(모든 기록), `secret.key`(없어지면 모두 다시 로그인해야 함).
- **HTTPS**: 필요하면 사내 리버스 프록시(웹서버)에서 처리한다.
- **테스트**: `venv/bin/python -m unittest -v`
