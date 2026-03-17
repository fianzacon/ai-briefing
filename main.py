import os
import feedparser
from datetime import datetime, timezone, timedelta
import anthropic
import requests

# ─────────────────────────────────────────
# 설정 (GitHub Secrets에서 자동으로 읽어옴)
# ─────────────────────────────────────────
CLAUDE_API_KEY  = os.environ["CLAUDE_API_KEY"]
WEBEX_BOT_TOKEN = os.environ["WEBEX_BOT_TOKEN"]
WEBEX_ROOM_ID   = os.environ["WEBEX_ROOM_ID"]

# ─────────────────────────────────────────
# RSS 피드 목록 (동작 확인된 피드만)
# ─────────────────────────────────────────
RSS_FEEDS = [
    # 국내
    "https://www.etnews.com/rss/section/144",              # 전자신문 AI/SW
    "https://www.aitimes.com/rss/allArticle.xml",          # AI타임스
    "https://zdnet.co.kr/rss/feed/zdnet_news_all.xml",    # ZDNet Korea
    "https://www.hankyung.com/feed/it",                    # 한국경제 IT
    "https://www.mk.co.kr/rss/30200030/",                  # 매일경제 IT
    # 해외
    "https://techcrunch.com/feed/",                        # TechCrunch
    "https://venturebeat.com/feed/",                       # VentureBeat
    "https://www.technologyreview.com/feed/",              # MIT Tech Review
    "https://feeds.feedburner.com/oreilly/radar/atom",     # O'Reilly Radar
]

# ─────────────────────────────────────────
# 1차 필터: AI 관련 키워드 (광범위하게)
# ─────────────────────────────────────────
AI_KEYWORDS = [
    "AI", "인공지능", "LLM", "에이전트", "agent",
    "생성형", "generative", "GPT", "Claude", "Gemini",
    "딥러닝", "머신러닝", "chatbot", "챗봇", "자동화",
    "automation", "foundation model", "RAG", "파운데이션",
]

# ─────────────────────────────────────────
# 2차 필터: 광고·마케팅·커머스 관련 키워드
# 이 키워드가 포함된 기사를 우선 선별
# ─────────────────────────────────────────
MARKETING_KEYWORDS = [
    "광고", "마케팅", "리테일", "커머스", "쇼핑",
    "개인화", "추천", "타겟팅", "ROAS", "CRM",
    "멤버십", "loyalty", "데이터", "결제", "핀테크",
    "플랫폼", "미디어", "콘텐츠", "브랜드", "캠페인",
    "retail", "commerce", "advertising", "personalization",
    "recommendation", "martech", "adtech",
]

HOURS_LIMIT  = 24  # 24시간 이내 기사만
MAX_ARTICLES = 15  # Claude에 넘길 최대 기사 수


def fetch_recent_articles():
    """RSS 피드에서 24시간 이내 + AI 키워드 포함 기사 수집"""
    now    = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=HOURS_LIMIT)
    articles = []

    for url in RSS_FEEDS:
        try:
            feed = feedparser.parse(url)
            for entry in feed.entries:

                # ── 날짜 파싱: 날짜 없는 기사는 무조건 제외 ──
                published = None
                if hasattr(entry, "published_parsed") and entry.published_parsed:
                    try:
                        published = datetime(*entry.published_parsed[:6], tzinfo=timezone.utc)
                    except Exception:
                        pass
                if published is None and hasattr(entry, "updated_parsed") and entry.updated_parsed:
                    try:
                        published = datetime(*entry.updated_parsed[:6], tzinfo=timezone.utc)
                    except Exception:
                        pass

                if published is None:
                    continue  # 날짜 확인 불가 → 제외

                if published < cutoff:
                    continue  # 24시간 초과 → 제외

                # ── 1차 AI 키워드 필터 ──
                title   = entry.get("title", "")
                summary = entry.get("summary", "")
                text    = (title + " " + summary).lower()

                if not any(kw.lower() in text for kw in AI_KEYWORDS):
                    continue

                # ── 광고/마케팅 관련성 점수 계산 (2차 필터용) ──
                relevance_score = sum(
                    1 for kw in MARKETING_KEYWORDS if kw.lower() in text
                )

                articles.append({
                    "title":     title.strip(),
                    "summary":   summary[:400].strip(),
                    "link":      entry.get("link", ""),
                    "source":    feed.feed.get("title", url),
                    "published": published,
                    "pub_str":   published.strftime("%Y-%m-%d %H:%M UTC"),
                    "relevance": relevance_score,
                })

        except Exception as e:
            print(f"[RSS 오류] {url}: {e}")

    # ── 정렬: 광고/마케팅 관련성 높은 것 우선, 동점이면 최신순 ──
    articles.sort(key=lambda x: (x["relevance"], x["published"]), reverse=True)

    print(f"  - 수집된 총 기사: {len(articles)}건")
    print(f"  - 광고/마케팅 관련 기사: {sum(1 for a in articles if a['relevance'] > 0)}건")

    return articles[:MAX_ARTICLES]


def build_context(articles):
    """Claude에게 넘길 기사 컨텍스트 문자열 생성"""
    if not articles:
        return None
    lines = []
    for i, a in enumerate(articles, 1):
        relevance_tag = "★ 광고/마케팅 관련" if a["relevance"] > 0 else ""
        lines.append(
            f"[{i}] [{a['source']}] {a['pub_str']} {relevance_tag}\n"
            f"제목: {a['title']}\n"
            f"요약: {a['summary']}\n"
            f"링크: {a['link']}"
        )
    return "\n\n".join(lines)


def summarize_with_claude(context, article_count):
    """Claude API로 브리핑 생성"""
    client  = anthropic.Anthropic(api_key=CLAUDE_API_KEY)
    kst_now = datetime.now(timezone(timedelta(hours=9))).strftime("%Y-%m-%d")

    system_prompt = """당신은 롯데멤버스 AI 비즈니스 전략팀의 브리핑 작성 전문가입니다.
롯데멤버스는 4,300만 회원을 보유한 멤버십·광고·데이터 플랫폼 기업입니다.
주요 사업: 리테일 미디어 네트워크(RMN), 광고 플랫폼, 개인화 마케팅, 데이터 사업.

반드시 지킬 규칙:
1. 제공된 기사 원문만 참고하세요. 외부 지식이나 추측을 절대 사용하지 마세요.
2. 광고·마케팅·데이터·커머스와 직접 연관된 기사를 최우선으로 다루세요.
3. 연관성이 낮은 기사(예: 순수 자율주행, 의료AI, 반도체 제조 등)는 간략히 언급하거나 생략하세요.
4. 각 기사의 인사이트는 반드시 롯데멤버스 실무에 적용 가능한 내용으로 작성하세요.
5. 브리핑 마지막에 오늘의 핵심 액션 아이템 2~3개를 작성하세요."""

    user_prompt = f"""아래 {article_count}건의 기사를 바탕으로 오늘의 AI 비즈니스 브리핑을 작성해주세요.

=== 오늘의 기사 ===
{context}

=== 출력 형식 ===
📅 {kst_now} 롯데멤버스 AI 비즈니스 브리핑

기사를 아래 3개 카테고리로 분류하고, 카테고리 순서대로 출력하세요.
카테고리가 없으면 해당 섹션은 생략하세요.

━━━━━━━━━━━━━━━━━━━━━━━━
🛠️ 솔루션/서비스
━━━━━━━━━━━━━━━━━━━━━━━━
(AI 제품·서비스 출시, 플랫폼 업데이트, 파트너십 등)

• 제목 (출처)
  핵심 요약: 2문장
  인사이트: [태그] 롯데멤버스 실무 적용 관점 1문장

━━━━━━━━━━━━━━━━━━━━━━━━
📡 기술/트렌드
━━━━━━━━━━━━━━━━━━━━━━━━
(AI 기술 동향, 연구, 시장 트렌드 등)

• 제목 (출처)
  핵심 요약: 2문장
  인사이트: [태그] 롯데멤버스 실무 적용 관점 1문장

━━━━━━━━━━━━━━━━━━━━━━━━
⚖️ 법규/규제/리스크
━━━━━━━━━━━━━━━━━━━━━━━━
(법안, 규제, 윤리, 리스크 이슈 등)

• 제목 (출처)
  핵심 요약: 2문장
  인사이트: [태그] 롯데멤버스 실무 적용 관점 1문장

━━━━━━━━━━━━━━━━━━━━━━━━
🎯 오늘의 액션 아이템
━━━━━━━━━━━━━━━━━━━━━━━━
- 항목 1
- 항목 2
- 항목 3

인사이트 태그는 [광고플랫폼], [데이터전략], [RMN], [리스크관리], [파트너십] 중 선택"""

    message = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=4000,  # 잘림 방지: 2000 → 4000
        messages=[{"role": "user", "content": user_prompt}],
        system=system_prompt,
    )
    return message.content[0].text


def send_to_webex(message_text):
    """Webex 방에 메시지 전송"""
    url     = "https://webexapis.com/v1/messages"
    headers = {
        "Authorization": f"Bearer {WEBEX_BOT_TOKEN}",
        "Content-Type":  "application/json",
    }
    payload = {
        "roomId": WEBEX_ROOM_ID,
        "text":   message_text,
    }
    response = requests.post(url, headers=headers, json=payload)
    if response.status_code == 200:
        print("✅ Webex 전송 성공")
    else:
        print(f"❌ Webex 전송 실패: {response.status_code} {response.text}")


def main():
    print("🔍 기사 수집 중...")
    articles = fetch_recent_articles()
    print(f"✅ 최종 {len(articles)}건 선별 완료")

    if not articles:
        send_to_webex("⚠️ 오늘은 조건에 맞는 AI 관련 기사가 없습니다.")
        return

    context = build_context(articles)

    print("🤖 Claude로 브리핑 생성 중...")
    briefing = summarize_with_claude(context, len(articles))
    print("✅ 브리핑 생성 완료")

    print("📨 Webex 전송 중...")
    send_to_webex(briefing)


if __name__ == "__main__":
    main()
