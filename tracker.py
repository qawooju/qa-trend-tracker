"""QA 트렌드 & AI in QA 리포트 — 블로그/뉴스 수집 후 Slack 발송."""

import json
import os
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone, timedelta
from html import unescape
from pathlib import Path

# ── 경로 / 환경변수 ─────────────────────────────────────────────────────
ROOT = Path(__file__).parent
STATE_PATH = ROOT / "states" / "tracker_state.json"

SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN", "")
SLACK_CHANNEL = os.environ.get("SLACK_CHANNEL", "C0AJZJLSXMH")

KST = timezone(timedelta(hours=9))

# ── QA 키워드 (제목 필터링용) ────────────────────────────────────────────
QA_KEYWORDS = [
    "qa", "qe", "test", "testing", "quality", "automation",
    "자동화", "테스트", "품질", "검증", "QA", "릴리즈", "배포",
    "e2e", "unit test", "integration test", "regression",
    "selenium", "playwright", "cypress", "appium",
    "bug", "defect", "결함", "장애",
]

AI_KEYWORDS = [
    "ai", "llm", "gpt", "copilot", "claude", "gemini", "chatgpt",
    "machine learning", "ml", "인공지능", "생성형",
    "ai test", "ai qa", "ai 테스트", "ai 자동화", "ai 품질",
    "autonomous testing", "self-healing", "visual ai",
    "ai-driven", "ai-powered", "ai agent",
]


# ── 공통 헬퍼 ────────────────────────────────────────────────────────────
def load_state():
    if STATE_PATH.exists():
        with open(STATE_PATH, encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_state(state):
    STATE_PATH.parent.mkdir(exist_ok=True)
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)


def fetch_rss(url, timeout=15):
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0",
        "Accept": "application/rss+xml, application/xml, text/xml",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", errors="ignore")
    except Exception as e:
        print(f"  RSS 오류: {e} - {url}")
        return ""


def parse_rss_items(rss_text, max_items=20):
    """RSS/Atom 피드에서 항목 파싱"""
    articles = []

    # RSS <item> 파싱
    items = re.findall(r"<item>(.*?)</item>", rss_text, re.DOTALL)
    for item in items[:max_items]:
        title_m = re.search(r"<title[^>]*>(.*?)</title>", item, re.DOTALL)
        link_m = re.search(r"<link[^>]*>(https?://[^<\s]+)</link>", item)
        if not link_m:
            link_m = re.search(r"<link[^>]*href=[\"']([^\"']+)[\"']", item)
        if not title_m or not link_m:
            continue
        title = unescape(re.sub(r"<!\[CDATA\[(.*?)\]\]>", r"\1", title_m.group(1))).strip()
        url = unescape(link_m.group(1)).strip()
        if title and len(title) >= 5:
            articles.append({"title": title, "url": url})

    # Atom <entry> 파싱
    if not articles:
        entries = re.findall(r"<entry>(.*?)</entry>", rss_text, re.DOTALL)
        for entry in entries[:max_items]:
            title_m = re.search(r"<title[^>]*>(.*?)</title>", entry, re.DOTALL)
            link_m = re.search(r"<link[^>]*href=[\"']([^\"']+)[\"']", entry)
            if not title_m or not link_m:
                continue
            title = unescape(re.sub(r"<!\[CDATA\[(.*?)\]\]>", r"\1", title_m.group(1))).strip()
            url = unescape(link_m.group(1)).strip()
            if title and len(title) >= 5:
                articles.append({"title": title, "url": url})

    return articles


def matches_keywords(title, keywords):
    title_lower = title.lower()
    return any(kw.lower() in title_lower for kw in keywords)


def slack_post(blocks, thread_ts=None):
    payload = {
        "channel": SLACK_CHANNEL,
        "blocks": blocks,
        "unfurl_links": False,
        "unfurl_media": False,
    }
    if thread_ts:
        payload["thread_ts"] = thread_ts

    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        "https://slack.com/api/chat.postMessage",
        data=data,
        headers={
            "Content-Type": "application/json; charset=utf-8",
            "Authorization": f"Bearer {SLACK_BOT_TOKEN}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = json.loads(resp.read().decode())
            if not body.get("ok"):
                print(f"  Slack API 오류: {body.get('error')}")
                return None
            return body.get("ts")
    except urllib.error.HTTPError as e:
        print(f"  Slack 오류: {e.code} {e.reason}")
        return None


def lines_to_blocks(lines):
    blocks = []
    chunk = []
    chunk_len = 0
    for line in lines:
        # Slack section 블록 텍스트 제한 2900자 (여유분 확보)
        if chunk and chunk_len + len(line) + 1 > 2900:
            blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(chunk)}})
            chunk = []
            chunk_len = 0
        chunk.append(line)
        chunk_len += len(line) + 1
    if chunk:
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(chunk)}})
    return blocks


# ── 데이터 소스 정의 ─────────────────────────────────────────────────────
# QA/Testing 전문 소스 (제목 필터링 없이 전체 수집)
QA_DEDICATED_SOURCES = [
    ("Dev.to #testing", "https://dev.to/feed/tag/testing"),
    ("Dev.to #qa", "https://dev.to/feed/tag/qa"),
    ("Medium QA", "https://medium.com/feed/tag/qa"),
    ("Medium Test Automation", "https://medium.com/feed/tag/test-automation"),
    ("Google Testing Blog", "https://testing.googleblog.com/feeds/posts/default?alt=rss"),
]

# AI + QA/Testing 전문 소스
AI_QA_SOURCES = [
    ("Dev.to #aitesting", "https://dev.to/feed/tag/aitesting"),
]

# 국내 테크 블로그 (QA/AI 키워드로 필터링)
KR_TECH_BLOGS = [
    ("토스 기술블로그", "https://toss.tech/rss.xml"),
    ("카카오 기술블로그", "https://tech.kakao.com/feed/"),
    ("라인 기술블로그", "https://engineering.linecorp.com/ko/feed/"),
    ("우아한형제들 기술블로그", "https://techblog.woowahan.com/feed/"),
    ("네이버 D2", "https://d2.naver.com/d2.atom"),
    ("쿠팡 기술블로그", "https://medium.com/feed/coupang-engineering"),
    ("당근 기술블로그", "https://medium.com/feed/daangn"),
    ("NHN Cloud", "https://meetup.nhncloud.com/rss"),
]


# ── 수집 함수 ─────────────────────────────────────────────────────────────
def collect_qa_articles():
    """QA/Testing 전문 소스에서 아티클 수집"""
    articles = []
    for name, url in QA_DEDICATED_SOURCES:
        rss = fetch_rss(url)
        if not rss:
            continue
        items = parse_rss_items(rss, max_items=10)
        for item in items:
            item["source"] = name
        articles.extend(items)
        print(f"  {name}: {len(items)}건")
    return articles


def collect_ai_qa_articles():
    """AI + QA 관련 아티클 수집"""
    articles = []

    # 1) AI Testing 전문 소스
    for name, url in AI_QA_SOURCES:
        rss = fetch_rss(url)
        if not rss:
            continue
        items = parse_rss_items(rss, max_items=10)
        for item in items:
            item["source"] = name
        articles.extend(items)
        print(f"  {name}: {len(items)}건")

    # 2) QA 전문 소스에서 AI 키워드 필터링
    for name, url in QA_DEDICATED_SOURCES:
        rss = fetch_rss(url)
        if not rss:
            continue
        items = parse_rss_items(rss, max_items=15)
        for item in items:
            if matches_keywords(item["title"], AI_KEYWORDS):
                item["source"] = name
                articles.append(item)

    return articles


def collect_kr_tech_articles():
    """국내 테크 블로그에서 QA/AI 관련 아티클 필터링"""
    qa_articles = []
    ai_articles = []

    for name, url in KR_TECH_BLOGS:
        rss = fetch_rss(url)
        if not rss:
            continue
        items = parse_rss_items(rss, max_items=20)
        qa_count = 0
        ai_count = 0
        for item in items:
            item["source"] = name
            is_qa = matches_keywords(item["title"], QA_KEYWORDS)
            is_ai = matches_keywords(item["title"], AI_KEYWORDS)
            if is_qa:
                qa_articles.append(item)
                qa_count += 1
            if is_ai:
                ai_articles.append(item)
                ai_count += 1
        total = len(items)
        print(f"  {name}: {total}건 중 QA {qa_count}건, AI {ai_count}건")

    return qa_articles, ai_articles


# ── 메시지 빌드 ───────────────────────────────────────────────────────────
def build_header():
    now = datetime.now(KST).strftime("%Y-%m-%d %H:%M KST")
    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": "QA 트렌드 & AI in QA 리포트"}},
        {"type": "context", "elements": [{"type": "mrkdwn", "text": now}]},
        {"type": "divider"},
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    "QA/테스트 관련 최신 아티클과 AI 활용 사례를 매일 정리합니다.\n"
                    "상세 내역은 스레드를 확인해주세요."
                ),
            },
        },
    ]
    return blocks


def build_article_thread(title, articles, seen_urls):
    """아티클 목록을 스레드 블록으로 변환 (중복 제거)"""
    new_articles = [a for a in articles if a["url"] not in seen_urls]

    # URL 중복 제거
    unique = {}
    for a in new_articles:
        if a["url"] not in unique:
            unique[a["url"]] = a
    new_articles = list(unique.values())

    lines = [f"*{title}*\n"]

    if not new_articles:
        lines.append("새로운 아티클 없음")
        return lines_to_blocks(lines), []

    for i, a in enumerate(new_articles[:15], 1):
        source = a.get("source", "")
        lines.append(f"{i}. <{a['url']}|{a['title']}> ({source})")

    return lines_to_blocks(lines), [a["url"] for a in new_articles[:15]]


# ── 메인 ─────────────────────────────────────────────────────────────────
def main():
    now_kst = datetime.now(KST)

    if not SLACK_BOT_TOKEN:
        print("SLACK_BOT_TOKEN 미설정.")
        sys.exit(1)

    print(f"QA 트렌드 수집 시작 ({now_kst.strftime('%Y-%m-%d %H:%M')} KST)...")

    # 데이터 수집
    print("\n[QA/Testing 전문 소스]")
    qa_articles = collect_qa_articles()

    print("\n[AI + QA 소스]")
    ai_qa_articles = collect_ai_qa_articles()

    print("\n[국내 테크 블로그]")
    kr_qa_articles, kr_ai_articles = collect_kr_tech_articles()

    print(f"\n수집 결과: QA {len(qa_articles)}건, AI+QA {len(ai_qa_articles)}건, "
          f"한국 QA {len(kr_qa_articles)}건, 한국 AI {len(kr_ai_articles)}건")

    # 이전 상태 로드
    prev_state = load_state()
    seen_urls = set(prev_state.get("seen_urls", []))

    # 1) 메인 메시지
    header_blocks = build_header()
    ts = slack_post(header_blocks)
    if not ts:
        print("메인 메시지 전송 실패. 종료.")
        return
    print(f"\n메인 메시지 전송 (ts={ts})")

    all_new_urls = []

    # 2) 국내 테크 블로그 QA 아티클
    blocks, new_urls = build_article_thread(
        "[국내 테크 블로그 — QA/테스트]", kr_qa_articles, seen_urls
    )
    slack_post(blocks, thread_ts=ts)
    all_new_urls.extend(new_urls)
    print(f"  스레드: 한국 QA ({len(new_urls)}건)")

    # 3) 국내 테크 블로그 AI 아티클
    blocks, new_urls = build_article_thread(
        "[국내 테크 블로그 — AI 활용]", kr_ai_articles, seen_urls
    )
    slack_post([{"type": "divider"}] + blocks, thread_ts=ts)
    all_new_urls.extend(new_urls)
    print(f"  스레드: 한국 AI ({len(new_urls)}건)")

    # 4) 글로벌 QA/Testing 아티클
    blocks, new_urls = build_article_thread(
        "[글로벌 QA/Testing 아티클]", qa_articles, seen_urls
    )
    slack_post([{"type": "divider"}] + blocks, thread_ts=ts)
    all_new_urls.extend(new_urls)
    print(f"  스레드: 글로벌 QA ({len(new_urls)}건)")

    # 5) AI in QA 아티클
    blocks, new_urls = build_article_thread(
        "[AI in QA — AI 활용 테스트 사례]", ai_qa_articles, seen_urls
    )
    slack_post([{"type": "divider"}] + blocks, thread_ts=ts)
    all_new_urls.extend(new_urls)
    print(f"  스레드: AI+QA ({len(new_urls)}건)")

    # 상태 저장
    all_seen = list(seen_urls | set(all_new_urls))
    # 최근 200개만 유지
    new_state = {
        "updated": now_kst.isoformat(),
        "seen_urls": all_seen[-200:],
    }
    save_state(new_state)
    print("\n상태 저장 완료")
    print("완료.")


if __name__ == "__main__":
    main()
