import os
import feedparser
from datetime import datetime, timezone, timedelta
import anthropic
import requests

# ─────────────────────────────────────────
# 설정
# ─────────────────────────────────────────
CLAUDE_API_KEY  = os.environ["CLAUDE_API_KEY"]
WEBEX_BOT_TOKEN = os.environ["WEBEX_BOT_TOKEN"]
WEBEX_ROOM_ID   = os.environ["WEBEX_ROOM_ID"]

HOURS_LIMIT       = 24   # 24시간 이내 기사만
MAX_PER_CATEGORY  = 3    # 카테고리별 최대 기사 수
MAX_TOTAL         = 20   # 전체 수집 풀 (이 중에서 관련 기사 선별)

# ─────────────────────────────────────────
# RSS 피드 (최대한 많이 — 동작 안 하는 건 로그로 확인 후 제거)
# ─────────────────────────────────────────
RSS_FEEDS = [
    ("전자신문",      "https://www.etnews.com/rss/section/144"),
    ("AI타임스",      "https://www.aitimes.com/rss/allArticle.xml"),
    ("ZDNet Korea",   "https://zdnet.co.kr/rss/feed/zdnet_news_all.xml"),
    ("한국경제IT",    "https://www.hankyung.com/feed/it"),
    ("매일경제IT",    "https://www.mk.co.kr/rss/30200030/"),
    ("디지털투데이",  "https://www.digitaltoday.co.kr/rss/allArticle.xml"),
    ("IT조선",        "https://it.chosun.com/rss/feed/"),
    ("TechCrunch",    "https://techcrunch.com/feed/"),
    ("VentureBeat",   "https://venturebeat.com/feed/"),
    ("MIT Tech",      "https://www.technologyreview.com/feed/"),
    ("The Verge",     "https://www.theverge.com/rss/index.xml"),
    ("Wired AI",      "https://www.wired.com/feed/tag/ai/latest/rss"),
]

# ─────────────────────────────────────────
# AI 관련 키워드 (1차 필터 — 넓게)
# ─────────────────────────────────────────
AI_KEYWORDS = [
    "AI", "인공지능", "LLM", "에이전트", "agent",
    "생성형", "generative", "GPT", "Claude", "Gemini",
    "딥러닝", "머신러닝", "chatbot", "챗봇", "자동화",
    "automation", "foundation model", "RAG",
]

# ─────────────────────────────────────────
# 광고·마케팅 관련 키워드 (관련성 점수용)
# ─────────────────────────────────────────
MARKETING_KEYWORDS = [
    "광고", "마케팅", "리테일", "커머스", "쇼핑",
    "개인화", "추천", "타겟팅", "ROAS", "CRM",
    "멤버십", "loyalty", "결제", "플랫폼", "미디어",
    "콘텐츠", "브랜드", "캠페인", "retail", "commerce",
    "advertising", "personalization", "recommendation",
    "martech", "adtech", "데이터",
]


# ─────────────────────────────────────────
# 1단계: 전체 기사 수집 (풀 구성)
# ─────────────────────────────────────────
def fetch_all_articles():
    now    = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=HOURS_LIMIT)
    pool   = []
    seen_titles = set()  # 중복 제거용

    for name, url in RSS_FEEDS:
        try:
            feed    = feedparser.parse(url)
            count   = 0
            for entry in feed.entries:

                # 날짜 파싱 — 없으면 제외
                published = None
                for attr in ("published_parsed", "updated_parsed"):
                    val = getattr(entry, attr, None)
                    if val:
                        try:
                            published = datetime(*val[:6], tzinfo=timezone.utc)
                            break
                        except Exception:
                            pass
                if published is None or published < cutoff:
                    continue

                # 제목 기반 중복 제거
                title = entry.get("title", "").strip()
                title_key = title.lower()[:50]  # 앞 50자로 비교
                if title_key in seen_titles:
                    continue
                seen_titles.add(title_key)

                summary = entry.get("summary", "")[:400].strip()
                text    = (title + " " + summary).lower()

                # AI 키워드 없으면 제외
                if not any(kw.lower() in text for kw in AI_KEYWORDS):
                    continue

                # 광고/마케팅 관련성 점수
                score = sum(1 for kw in MARKETING_KEYWORDS if kw.lower() in text)

                pool.append({
                    "title":     title,
                    "summary":   summary,
                    "link":      entry.get("link", ""),
                    "source":    name,
                    "published": published,
                    "pub_str":   published.strftime("%Y-%m-%d %H:%M UTC"),
                    "score":     score,
                })
                count += 1

            print(f"  [{name}] {count}건 수집")

        except Exception as e:
            print(f"  [{name}] 오류: {e}")

    # 관련성 높은 순 → 최신순 정렬
    pool.sort(key=lambda x: (x["score"], x["published"]), reverse=True)
    print(f"\n  전체 풀: {len(pool)}건 (중복 제거 후)")
    return pool[:MAX_TOTAL]


# ─────────────────────────────────────────
# 2단계: Claude가 카테고리 분류 + 요약
# ─────────────────────────────────────────
def build_context(articles):
    lines = []
    for i, a in enumerate(articles, 1):
        tag = " ★" if a["score"] > 0 else ""
        lines.append(
            f"[{i}]{tag} [{a['source']}] {a['pub_str']}\n"
            f"제목: {a['title']}\n"
            f"내용: {a['summary']}\n"
            f"링크: {a['link']}"
        )
    return "\n\n".join(lines)


def summarize_with_claude(context, article_count):
    client  = anthropic.Anthropic(api_key=CLAUDE_API_KEY)
    kst_now = datetime.now(timezone(timedelta(hours=9))).strftime("%Y-%m-%d")

    system_prompt = f"""당신은 롯데멤버스 AI 비즈니스 전략팀 브리핑 작성 전문가입니다.
롯데멤버스 주요 사업: 리테일 미디어 네트워크(RMN), 광고 플랫폼, 개인화 마케팅, 데이터 사업, 4300만 회원 멤버십.

규칙:
1. 제공된 기사만 사용. 외부 지식·추측 금지.
2. 각 카테고리에서 롯데멤버스 관련성 높은 기사 최대 {MAX_PER_CATEGORY}건만 선택.
3. 관련성 낮은 기사(순수 자율주행·의료·반도체 제조 등)는 과감히 제외.
4. 인사이트는 롯데멤버스 실무에 바로 쓸 수 있는 내용으로."""

    user_prompt = f"""아래 {article_count}건 기사를 3개 카테고리로 분류해 브리핑을 작성하세요.
각 카테고리당 최대 {MAX_PER_CATEGORY}건, 관련성 높은 것만 선택하세요.

=== 기사 목록 ===
{context}

=== 출력 형식 ===
📅 {kst_now} 롯데멤버스 AI 비즈니스 브리핑

━━━━━━━━━━━━━━━━━━━━━━
🛠️ 솔루션/서비스
━━━━━━━━━━━━━━━━━━━━━━
(AI 제품·서비스 출시, 플랫폼, 파트너십)

• 기사제목 (출처)
  요약: 2문장
  인사이트: [태그] 1문장

━━━━━━━━━━━━━━━━━━━━━━
📡 기술/트렌드
━━━━━━━━━━━━━━━━━━━━━━
(기술 동향, 연구, 시장 흐름)

• 기사제목 (출처)
  요약: 2문장
  인사이트: [태그] 1문장

━━━━━━━━━━━━━━━━━━━━━━
⚖️ 법규/규제/리스크
━━━━━━━━━━━━━━━━━━━━━━
(법안, 규제, 윤리, 리스크)

• 기사제목 (출처)
  요약: 2문장
  인사이트: [태그] 1문장

━━━━━━━━━━━━━━━━━━━━━━
🎯 오늘의 액션 아이템
━━━━━━━━━━━━━━━━━━━━━━
- 항목 1
- 항목 2
- 항목 3

인사이트 태그: [광고플랫폼] [데이터전략] [RMN] [리스크관리] [파트너십] 중 선택
해당 기사 없는 카테고리는 "오늘 해당 기사 없음"으로 표기"""

    message = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=4000,
        messages=[{"role": "user", "content": user_prompt}],
        system=system_prompt,
    )
    return message.content[0].text


# ─────────────────────────────────────────
# 3단계: Webex 전송 (길이 초과 시 분할)
# ─────────────────────────────────────────
def send_to_webex(text):
    url     = "https://webexapis.com/v1/messages"
    headers = {
        "Authorization": f"Bearer {WEBEX_BOT_TOKEN}",
        "Content-Type":  "application/json",
    }

    # Webex 메시지 최대 7,439자 → 7,000자 기준으로 분할
    LIMIT = 7000
    chunks = []
    while len(text) > LIMIT:
        # 7000자 이전 마지막 줄바꿈 위치에서 자르기
        cut = text.rfind("\n", 0, LIMIT)
        if cut == -1:
            cut = LIMIT
        chunks.append(text[:cut])
        text = text[cut:].lstrip("\n")
    chunks.append(text)

    for i, chunk in enumerate(chunks, 1):
        if len(chunks) > 1:
            chunk = f"[{i}/{len(chunks)}]\n\n" + chunk
        resp = requests.post(url, headers=headers, json={
            "roomId": WEBEX_ROOM_ID,
            "text":   chunk,
        })
        if resp.status_code == 200:
            print(f"✅ Webex 전송 성공 ({i}/{len(chunks)})")
        else:
            print(f"❌ Webex 전송 실패: {resp.status_code} {resp.text}")


# ─────────────────────────────────────────
# 실행
# ─────────────────────────────────────────
def main():
    print("🔍 전체 기사 수집 중...")
    articles = fetch_all_articles()
    print(f"✅ 최종 풀: {len(articles)}건\n")

    if not articles:
        send_to_webex("⚠️ 오늘은 조건에 맞는 AI 관련 기사가 없습니다.")
        return

    context = build_context(articles)

    print("🤖 Claude 브리핑 생성 중...")
    briefing = summarize_with_claude(context, len(articles))
    print("✅ 브리핑 생성 완료\n")

    print("📨 Webex 전송 중...")
    send_to_webex(briefing)


if __name__ == "__main__":
    main()
