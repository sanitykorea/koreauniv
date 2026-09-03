# 고려대학교 장학금 공지 알리미

고려대학교 [장학금 공지사항 페이지](https://www.korea.ac.kr/ko/568/subview.do)를 주기적으로 확인해서
새 공지가 올라오면 자동으로 정적 사이트(HTML + RSS)를 갱신하는 GitHub Actions 기반 모니터입니다.

## 동작 방식

1. `.github/workflows/scholarship-watch.yml`이 30분마다(및 수동 실행 시) `scripts/check_scholarship.py`를 실행합니다.
2. 이 스크립트는 공지 페이지를 가져와 목록(제목/링크/날짜)을 파싱하고, 이전 실행 때 저장한
   `data/seen.json`과 비교해 새로 생긴 항목을 찾습니다.
3. 결과로 `docs/index.html`(사람이 보는 페이지)과 `docs/feed.xml`(RSS 피드)을 다시 생성합니다.
4. 변경 사항이 있으면 저장소에 커밋/푸시하고, `docs/` 폴더를 GitHub Pages로 배포합니다.

새 공지를 "바로" 팔로우업하고 싶다면, 생성되는 `feed.xml`을 RSS 리더(또는 Slack/이메일 RSS
연동 등)에 등록해두는 것을 추천합니다 — 사이트를 직접 열어보지 않아도 새 글이 올라오면
피드에 바로 반영됩니다.

## 최초 1회 설정 (필수)

GitHub Pages 배포가 동작하려면 저장소 설정에서 Pages 소스를 한 번 지정해야 합니다:

1. 저장소 **Settings → Pages**로 이동
2. **Source**를 **GitHub Actions**로 선택

이 설정은 API로 자동화할 수 없어(권한상 수동 설정이 필요) 직접 한 번 눌러주셔야 합니다.
설정 후에는 워크플로가 실행될 때마다 자동으로 배포됩니다.

배포 후 사이트 주소는 기본적으로 `https://sanitykorea.github.io/koreauniv/` 형태입니다
(커스텀 도메인을 쓰신다면 `scripts/check_scholarship.py`의 `SITE_BASE_URL`을 맞춰 수정해주세요).

## 알아두어야 할 제한 사항

- 이 저장소를 구성한 환경(샌드박스)은 조직 네트워크 정책상 `korea.ac.kr`에 직접 접속할 수 없어,
  실제 공지 페이지의 HTML 구조를 직접 확인하며 파싱 로직을 검증하지는 못했습니다. 대신
  특정 CSS 선택자에 최대한 덜 의존하는 범용 게시판 파싱 로직(표/목록에서 가장 긴 텍스트의
  링크를 제목으로 추출)으로 작성했습니다.
- GitHub Actions 러너는 이 샌드박스와 별개로 정상적인 인터넷 접근 권한이 있으므로, 실제 배포
  후 **Actions 탭에서 `scholarship-watch` 워크플로의 첫 실행 로그**를 확인해 공지가 몇 건
  파싱됐는지, 제목이 정상적으로 나오는지 점검해보시길 권장합니다.
- 만약 목록이 제대로 인식되지 않으면(로그에 "fallback-hash 모드"로 표시됨), 생성된 페이지 상단에
  경고 문구가 뜨고 전체 페이지 텍스트 변경 여부만 추적하는 방식으로 동작합니다. 이 경우
  `scripts/check_scholarship.py`의 `CONTAINER_SELECTORS`나 `parse_structured` 함수를
  실제 페이지 구조에 맞게 조정해주세요.
- 첫 실행에서는 기존에 올라와 있던 공지들을 전부 "새 글"로 표시하지 않고 기준선(baseline)으로만
  저장합니다. 그 다음 실행부터 새로 생긴 공지만 감지합니다.

## 로컬에서 테스트하기

```bash
pip install -r requirements.txt
python scripts/check_scholarship.py
```

## 설정 조정

- 확인 주기: `.github/workflows/scholarship-watch.yml`의 `cron` 값 수정 (기본: 30분마다)
- NEW 배지 표시 기간: `scripts/check_scholarship.py`의 `NEW_BADGE_HOURS`
- 보관할 최대 공지 수: `MAX_ITEMS_KEPT`
