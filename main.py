import os
import feedparser
from datetime import datetime, timezone, timedelta
import anthropic
import requests

# ─────────────────────────────────────────
# 설정 (GitHub Secrets에서 자동으로 읽어옴)
# ─────────────────────────────────────────
CLAUDE_API_KEY   = os.environ["CLAUDE_API_KEY"]
WEBEX_BOT_TOKEN  = os.environ["WEBEX_BOT_TOKEN"]
WEBEX_ROOM_ID    = os.environ["WEBEX_ROOM_ID"]

# ─────────────────────────────────────────
# RSS 피드 목록 (원하는 것 추가/삭제 가능)
# ─────────────────────────────────────────
RSS_FEEDS = [
    # 국내
    "https://www.etnews.com/rss/section/144",          # 전자신문 AI
    "https://www.aitimes.com/rss/allArticle.xml",      # AI타임스
    "https://feeds.feedburner.com/zdkorea",            # ZDNet Korea
    # 해외
    "https://techcrunch.com/feed/",                    # TechCrunch
    "https://feeds.feedburner.com/venturebeat/SZYF",   # VentureBeat AI
    "https://www.technologyreview.com/feed/",          # MIT Tech Review
]

# ─────────────────────────────────────────
# AI 관련 키워드 필터 (한/영)
# ─────────────────────────────────────────
KEYWORDS = [
    "AI", "인공지능", "LLM", "에이전트", "agent",
    "생성형", "GPT", "Claude", "Gemini", "딥러닝",
    "머신러닝", "챗봇", "자동화", "데이터", "모델",
]

HOURS_LIMIT = 24  # 최근 몇 시간 이내 기사만 사용


def fetch_recent_articles():
    """RSS 피드에서 24시간 이내 + 키워드 포함 기사 수집"""
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=HOURS_LIMIT)
    articles = []

    for url in RSS_FEEDS:
        try:
            feed = feedparser.parse(url)
            for entry in feed.entries:
                # 발행 시각 파싱
                published = None
                if hasattr(entry, "published_parsed") and entry.published_parsed:
                    published = datetime(*entry.published_parsed[:6], tzinfo=timezone.utc)
                elif hasattr(entry, "updated_parsed") and entry.updated_parsed:
                    published = datetime(*entry.updated_parsed[:6], tzinfo=timezone.utc)

                if not published or published < cutoff:
                    continue  # 24시간 초과 기사 제외

                # 키워드 필터
                text = (entry.get("title", "") + " " + entry.get("summary", "")).lower()
                if not any(kw.lower() in text for kw in KEYWORDS):
                    continue

                articles.append({
                    "title":   entry.get("title", "").strip(),
                    "summary": entry.get("summary", "")[:300].strip(),
                    "link":    entry.get("link", ""),
                    "source":  feed.feed.get("title", url),
                    "published": published.strftime("%Y-%m-%d %H:%M UTC"),
                })
        except Exception as e:
            print(f"[RSS 오류] {url}: {e}")

    # 최신순 정렬, 최대 20건
    articles.sort(key=lambda x: x["published"], reverse=True)
    return articles[:20]


def build_context(articles):
    """Claude에게 넘길 기사 컨텍스트 문자열 생성"""
    if not articles:
        return None
    lines = []
    for i, a in enumerate(articles, 1):
        lines.append(
            f"[{i}] [{a['source']}] {a['published']}\n"
            f"제목: {a['title']}\n"
            f"요약: {a['summary']}\n"
            f"링크: {a['link']}"
        )
    return "\n\n".join(lines)


def summarize_with_claude(context):
    """Claude API로 브리핑 생성 (제공된 기사만 참고, 외부 지식 사용 안 함)"""
    client = anthropic.Anthropic(api_key=CLAUDE_API_KEY)

    system_prompt = """당신은 롯데멤버스 AI 비즈니스 전략팀의 브리핑 작성 전문가입니다.
아래 규칙을 반드시 따르세요:
1. 제공된 기사 원문만 참고하세요. 외부 지식이나 추측을 절대 사용하지 마세요.
2. 각 기사를 [카테고리], 핵심 요약, 롯데멤버스 관점의 인사이트 순으로 작성하세요.
3. 카테고리는 [법규/리스크], [솔루션/서비스], [기술/트렌드], [규제/윤리] 중 선택하세요.
4. 인사이트는 반드시 롯데멤버스 광고·데이터·리테일 비즈니스와 연결하세요.
5. 기사가 없으면 "오늘은 해당 기사가 없습니다"라고 답하세요."""

    user_prompt = f"""아래 기사들을 바탕으로 오늘의 AI 비즈니스 브리핑을 작성해주세요.

=== 오늘의 기사 ({len(context.splitlines())}건) ===
{context}

=== 출력 형식 ===
📅 {datetime.now(timezone(timedelta(hours=9))).strftime('%Y-%m-%d')} 롯데멤버스 AI 비즈니스 브리핑

📂 [카테고리] 제목 (출처, 날짜)
핵심 요약: 2~3문장
인사이트: 롯데멤버스 관점 1~2문장

(위 형식을 기사 수만큼 반복)

🛠️ 오늘의 액션 아이템
- 항목 1
- 항목 2"""

    message = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=2000,
        messages=[{"role": "user", "content": user_prompt}],
        system=system_prompt,
    )
    return message.content[0].text


def send_to_webex(message_text):
    """Webex 방에 메시지 전송"""
    url = "https://webexapis.com/v1/messages"
    headers = {
        "Authorization": f"Bearer {WEBEX_BOT_TOKEN}",
        "Content-Type": "application/json",
    }
    payload = {
        "roomId": WEBEX_ROOM_ID,
        "text": message_text,
    }
    response = requests.post(url, headers=headers, json=payload)
    if response.status_code == 200:
        print("✅ Webex 전송 성공")
    else:
        print(f"❌ Webex 전송 실패: {response.status_code} {response.text}")


def main():
    print("🔍 기사 수집 중...")
    articles = fetch_recent_articles()
    print(f"✅ {len(articles)}건 수집 완료")

    if not articles:
        send_to_webex("⚠️ 오늘은 조건에 맞는 AI 관련 기사가 없습니다.")
        return

    context = build_context(articles)

    print("🤖 Claude로 브리핑 생성 중...")
    briefing = summarize_with_claude(context)
    print("✅ 브리핑 생성 완료")

    print("📨 Webex 전송 중...")
    send_to_webex(briefing)


if __name__ == "__main__":
    main()
