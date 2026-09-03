# 고려대학교 장학금 공지 알리미

고려대학교의 두 게시판을 주기적으로 확인해서 새 공지가 올라오면 자동으로 정적 사이트
(HTML + RSS)를 갱신하는 GitHub Actions 기반 모니터입니다.

- [장학금 공지사항 (568)](https://www.korea.ac.kr/ko/568/subview.do) — 전체 공지
- [학사공지 (566)](https://www.korea.ac.kr/ko/566/subview.do) — 제목에 **"근로장학"**이
  포함된 공지만 골라서 추적 (그 외 공지는 가져오기만 하고 버립니다)

## 동작 방식

1. `.github/workflows/scholarship-watch.yml`이 30분마다(및 수동 실행 시) `scripts/check_scholarship.py`를 실행합니다.
2. 이 스크립트는 `scripts/check_scholarship.py`의 `SOURCES` 목록에 있는 게시판을 각각
   가져와 목록(번호/카테고리/제목/날짜)을 파싱하고, 이전 실행 때 저장한 `data/seen.json`과
   비교해 새로 생긴 항목을 찾습니다. `keyword`가 설정된 게시판(566)은 파싱한 뒤 제목에 그
   키워드가 포함된 것만 남기고 나머지는 버립니다. 확인 주기 사이에 10건(한 페이지 분량)
   넘게 올라올 경우를 대비해 게시판마다 `?page=2`, `?page=3`도 이어서 가져와 최대
   `PAGES_PER_RUN`(기본 3)페이지까지 확인합니다 — 사이트가 페이지네이션 쿼리를 다르게
   처리하면 추가 페이지가 그냥 1페이지와 동일하게 와서 자동으로 멈추므로 안전합니다.
3. 결과로 `docs/index.html`(사람이 보는 페이지)과 `docs/feed.xml`(RSS 피드)을 다시 생성합니다.
   두 게시판의 공지가 날짜순으로 한 목록에 합쳐지고, 566에서 온 공지는 원래 제목에 대괄호
   카테고리가 없으면 "근로장학" 태그가 자동으로 붙습니다.
4. 변경 사항이 있으면 저장소에 커밋/푸시하고, `docs/` 폴더를 GitHub Pages로 배포합니다.

**게시판을 더 추가하거나 키워드를 바꾸려면** `scripts/check_scholarship.py`의 `SOURCES`
리스트에 `{"key": "...", "name": "...", "url": "...", "keyword": "..." 또는 None}` 형태로
항목을 추가/수정하면 됩니다. `key`는 다른 게시판과 겹치지 않는 짧은 문자열이면 됩니다.

새 공지를 "바로" 팔로우업하고 싶다면, 생성되는 `feed.xml`을 RSS 리더(또는 Slack/이메일 RSS
연동 등)에 등록해두는 것을 추천합니다 — 사이트를 직접 열어보지 않아도 새 글이 올라오면
피드에 바로 반영됩니다. 텔레그램 채널로 바로 알림을 받고 싶다면 아래 "텔레그램 알림 연동"을
참고하세요.

## 최초 1회 설정 (필수)

GitHub Pages 배포가 동작하려면 저장소 설정에서 Pages 소스를 한 번 지정해야 합니다:

1. 저장소 **Settings → Pages**로 이동
2. **Source**를 **GitHub Actions**로 선택

이 설정은 API로 자동화할 수 없어(권한상 수동 설정이 필요) 직접 한 번 눌러주셔야 합니다.
설정 후에는 워크플로가 실행될 때마다 자동으로 배포됩니다.

배포 후 사이트 주소는 기본적으로 `https://sanitykorea.github.io/koreauniv/` 형태입니다
(커스텀 도메인을 쓰신다면 `scripts/check_scholarship.py`의 `SITE_BASE_URL`을 맞춰 수정해주세요).

## 알아두어야 할 제한 사항

- 이 저장소를 구성한 환경(샌드박스)은 조직 네트워크 정책상 `korea.ac.kr`에 직접 접속할 수 없습니다.
  대신 실제 GitHub Actions 실행 로그에 찍힌 HTML을 읽어 파싱 로직(`parse_korea_portal_board`,
  `scripts/check_scholarship.py`)을 568번 게시판 마크업에 맞게 작성/검증했습니다. 566번
  게시판도 같은 사이트의 같은 CMS(portalBoard 위젯)를 쓸 가능성이 높아 같은 파서를 그대로
  적용했지만, 실제로 잘 맞는지는 배포 후 Actions 로그에서 `[근로장학]` 관련 줄을 확인해보세요
  — `[warn] [근로장학] 목록 구조를 인식하지 못했습니다`가 뜨면 아래 fallback 항목을 참고하세요.
- 이 게시판들(고려대 portalBoard 위젯)은 각 글 제목의 `<a href>`가 전부 동일한 더미 값(`#1`)이고
  실제 이동은 `onclick="jf_view('articleId','3','ko')"` JS로 폼을 제출하는 방식이라, 브라우저
  주소창에 바로 넣을 수 있는 개별 게시글 URL이 없습니다. 그래서 목록/피드의 링크는 게시글
  상세 페이지가 아니라 **해당 공지가 속한 게시판 페이지 자체**로 연결됩니다 — 새 글은 보통
  목록 맨 위에 있으므로 클릭하면 바로 확인할 수 있습니다. 새 글 식별은 onclick 안의
  articleId(없으면 게시글 번호)로 합니다.
- 만약 특정 게시판의 구조가 바뀌어(또는 566이 애초에 다른 구조라서) 목록이 인식되지 않으면,
  그 게시판만 전체 페이지 텍스트 변경 여부를 추적하는 fallback 모드로 동작하고(다른 게시판은
  평소대로 계속 동작), 생성된 페이지 상단에 경고 문구가 뜹니다. 이 경우
  `scripts/check_scholarship.py`의 `parse_korea_portal_board`/`extract_portal_board_rows`
  함수를 바뀐 페이지 구조에 맞게 조정해주세요. (구조 인식에 실패하면 워크플로 로그에 실제
  페이지 스니펫을 자동으로 출력하도록 `print_diagnostics`가 이미 붙어 있습니다.)
- 첫 실행에서는 기존에 올라와 있던 공지들을 전부 "새 글"로 표시하지 않고 기준선(baseline)으로만
  저장합니다. 그 다음 실행부터 새로 생긴 공지만 감지합니다.

## 텔레그램 알림 연동 (선택)

새 공지가 감지되면 텔레그램 채널/채팅으로 바로 메시지를 보낼 수 있습니다. 공지 1건이면
개별 메시지로, 한 번에 9건 넘게 감지되면(예: 오랫동안 워크플로가 멈춰 있었던 경우) 채널이
도배되지 않도록 하나로 묶은 요약 메시지로 보냅니다. 아무 것도 설정하지 않으면 이 기능은
그냥 조용히 꺼진 상태로 남습니다.

**1. 봇 만들기**
텔레그램에서 [@BotFather](https://t.me/BotFather)와 대화를 시작해 `/newbot`을 보내고
안내에 따라 봇 이름을 정하면, 아래에서 쓸 **봇 토큰**(`123456:ABC-DEF...` 형태)을 줍니다.

**2. 채널에 봇 추가**
알림을 받을 채널의 관리자 설정에서 방금 만든 봇을 **관리자(admin)**로 추가합니다
(메시지를 올리려면 관리자 권한이 필요합니다).

**3. chat_id 확인**
- **공개 채널**이면 채널의 `@사용자명`을 그대로 chat_id로 쓰면 됩니다 (예: `@my_scholarship_channel`).
- **비공개 채널**이면 숫자로 된 chat_id가 필요합니다. 채널에 아무 메시지나 하나 올린 뒤,
  브라우저로 `https://api.telegram.org/bot<봇토큰>/getUpdates`에 접속하면 응답 JSON의
  `"chat":{"id": -100...}` 값을 확인할 수 있습니다.

**4. 저장소에 시크릿 등록**
저장소 **Settings → Secrets and variables → Actions → New repository secret**에서 아래
두 개를 추가합니다.
- `TELEGRAM_BOT_TOKEN`: 위에서 받은 봇 토큰
- `TELEGRAM_CHAT_ID`: 위에서 확인한 `@채널명` 또는 숫자 chat_id

다음 워크플로 실행부터 새 공지가 감지되면 자동으로 텔레그램 메시지가 발송됩니다. 목록 구조
인식에 실패해 전체 페이지 변경만 감지된 경우에도 확인을 요청하는 경고 메시지를 보냅니다.

## 디자인

고려대학교 상징색인 크림슨(`#8b0029`)과 아이보리(`#d6cabc`)를 기반으로 한 전용 디자인을
적용했습니다. 게시판이 이미 제목 앞에 붙이는 `[국가근로]`, `[교외-9/22]` 같은 카테고리
표기를 그대로 뽑아내 태그로 보여주고, 게시글 번호는 도장처럼 크림슨 배지로 표시합니다.
폰트는 Google Fonts의 Noto Serif KR(제목) / Noto Sans KR(본문)을 사용합니다.

## 로컬에서 테스트하기

```bash
pip install -r requirements.txt
python scripts/check_scholarship.py
```

## 설정 조정

- 확인 주기: `.github/workflows/scholarship-watch.yml`의 `cron` 값 수정 (기본: 30분마다)
- NEW 배지 표시 기간: `scripts/check_scholarship.py`의 `NEW_BADGE_HOURS`
- 보관할 최대 공지 수: `MAX_ITEMS_KEPT`
