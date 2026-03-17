import os
import feedparser
from datetime import datetime, timezone, timedelta
import anthropic
import requests
import json

# ─────────────────────────────────────────
# 설정
# ─────────────────────────────────────────
CLAUDE_API_KEY  = os.environ["CLAUDE_API_KEY"]
WEBEX_BOT_TOKEN = os.environ["WEBEX_BOT_TOKEN"]
WEBEX_ROOM_ID   = os.environ["WEBEX_ROOM_ID"]

HOURS_LIMIT      = 24   # 수집 범위: 24시간 이내
MAX_PER_CATEGORY = 3    # 카테고리별 최종 선별 기사 수

# ─────────────────────────────────────────
# RSS 피드 목록
# ─────────────────────────────────────────
RSS_FEEDS = [
    ("전자신문",     "https://www.etnews.com/rss/section/144"),
    ("AI타임스",     "https://www.aitimes.com/rss/allArticle.xml"),
    ("ZDNet Korea",  "https://zdnet.co.kr/rss/feed/zdnet_news_all.xml"),
    ("한국경제IT",   "https://www.hankyung.com/feed/it"),
    ("매일경제IT",   "https://www.mk.co.kr/rss/30200030/"),
    ("디지털투데이", "https://www.digitaltoday.co.kr/rss/allArticle.xml"),
    ("IT조선",       "https://it.chosun.com/rss/feed/"),
    ("TechCrunch",   "https://techcrunch.com/feed/"),
    ("VentureBeat",  "https://venturebeat.com/feed/"),
    ("MIT Tech",     "https://www.technologyreview.com/feed/"),
    ("The Verge",    "https://www.theverge.com/rss/index.xml"),
    ("Wired AI",     "https://www.wired.com/feed/tag/ai/latest/rss"),
]

# AI 관련 키워드 (수집 단계에서 완전히 무관한 기사만 제외 — 최대한 넓게)
AI_KEYWORDS = [
    "AI", "인공지능", "LLM", "에이전트", "agent", "생성형", "generative",
    "GPT", "Claude", "Gemini", "딥러닝", "머신러닝", "chatbot", "챗봇",
    "자동화", "automation", "foundation model", "RAG", "machine learning",
    "deep learning", "neural", "모델", "데이터",
]


# ═══════════════════════════════════════════
# 1단계: 수집 — 24시간 이내 AI 관련 기사 전부
# ═══════════════════════════════════════════
def step1_collect():
    now    = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=HOURS_LIMIT)
    pool   = []

    print("=" * 50)
    print("1단계: 기사 수집")
    print("=" * 50)

    for name, url in RSS_FEEDS:
        try:
            feed  = feedparser.parse(url)
            count = 0
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

                title   = entry.get("title", "").strip()
                summary = entry.get("summary", "")[:500].strip()
                text    = (title + " " + summary).lower()

                # AI 키워드 없으면 제외 (완전 무관 기사 차단)
                if not any(kw.lower() in text for kw in AI_KEYWORDS):
                    continue

                pool.append({
                    "title":     title,
                    "summary":   summary,
                    "link":      entry.get("link", ""),
                    "source":    name,
                    "pub_str":   published.strftime("%Y-%m-%d %H:%M UTC"),
                })
                count += 1

            print(f"  [{name}] {count}건")

        except Exception as e:
            print(f"  [{name}] 오류: {e}")

    print(f"\n  → 총 수집: {len(pool)}건\n")
    return pool


# ═══════════════════════════════════════════
# 2단계: 중복 제거 — 제목 유사도 기반
# ═══════════════════════════════════════════
def step2_deduplicate(pool):
    print("=" * 50)
    print("2단계: 중복 제거")
    print("=" * 50)

    seen   = []
    result = []

    for article in pool:
        title_key = article["title"].lower()
        # 앞 40자가 기존 기사와 겹치면 중복으로 판단
        is_dup = any(
            title_key[:40] in seen_title or seen_title[:40] in title_key
            for seen_title in seen
        )
        if not is_dup:
            seen.append(title_key)
            result.append(article)

    print(f"  중복 제거 전: {len(pool)}건")
    print(f"  중복 제거 후: {len(result)}건\n")
    return result


# ═══════════════════════════════════════════
# 3단계: 선별 — Claude가 관련성 판단
# (제목+요약만 넘겨서 토큰 절약)
# ═══════════════════════════════════════════
def step3_select(pool):
    print("=" * 50)
    print("3단계: Claude 선별")
    print("=" * 50)

    client = anthropic.Anthropic(api_key=CLAUDE_API_KEY)

    # 제목+출처만 간략하게 전달 (토큰 절약)
    article_list = "\n".join(
        f"[{i}] [{a['source']}] {a['title']}"
        for i, a in enumerate(pool, 1)
    )

    prompt = f"""당신은 롯데멤버스 AI 비즈니스 전략팀 담당자입니다.
롯데멤버스 주요 사업: 리테일 미디어 네트워크(RMN), 광고 플랫폼, 개인화 마케팅, 데이터 사업, 멤버십.

아래 기사 목록에서 롯데멤버스 담당자가 반드시 알아야 할 기사를 선별하세요.

선별 기준:
- 광고·마케팅·커머스·데이터·개인화·멤버십과 직접 관련된 기사 우선
- AI 규제·법안·윤리 이슈 포함
- 경쟁사 동향, 글로벌 AI 플랫폼 전략 포함
- 순수 자율주행·의료·반도체 제조·하드웨어 스펙 등 무관한 기사 제외

각 카테고리별로 최대 {MAX_PER_CATEGORY}개 기사 번호를 선택하세요.

=== 기사 목록 ===
{article_list}

=== 응답 형식 (JSON만, 다른 텍스트 없이) ===
{{
  "솔루션/서비스": [1, 5, 12],
  "기술/트렌드": [3, 8],
  "법규/규제/리스크": [7]
}}"""

    message = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=500,
        messages=[{"role": "user", "content": prompt}],
    )

    raw = message.content[0].text.strip()
    # JSON 파싱
    try:
        selected = json.loads(raw)
    except json.JSONDecodeError:
        # 혹시 마크다운 코드블록으로 감싸져 있으면 제거
        raw = raw.replace("```json", "").replace("```", "").strip()
        selected = json.loads(raw)

    # 선별된 기사 객체로 변환
    result = {}
    for category, indices in selected.items():
        result[category] = []
        for idx in indices:
            if 1 <= idx <= len(pool):
                result[category].append(pool[idx - 1])

    total = sum(len(v) for v in result.values())
    print(f"  선별 완료: {total}건")
    for cat, arts in result.items():
        print(f"    [{cat}] {len(arts)}건")
    print()

    return result


# ═══════════════════════════════════════════
# 4단계: 요약 — 선별된 기사만 브리핑 작성
# ═══════════════════════════════════════════
def step4_summarize(selected):
    print("=" * 50)
    print("4단계: 브리핑 작성")
    print("=" * 50)

    client  = anthropic.Anthropic(api_key=CLAUDE_API_KEY)
    kst_now = datetime.now(timezone(timedelta(hours=9))).strftime("%Y-%m-%d")

    # 선별된 기사를 카테고리별로 정리
    context_parts = []
    for category, articles in selected.items():
        if not articles:
            continue
        context_parts.append(f"[{category}]")
        for a in articles:
            context_parts.append(
                f"- 제목: {a['title']}\n"
                f"  출처: {a['source']} ({a['pub_str']})\n"
                f"  내용: {a['summary']}\n"
                f"  링크: {a['link']}"
            )
    context = "\n\n".join(context_parts)

    prompt = f"""아래 선별된 기사를 바탕으로 롯데멤버스 AI 비즈니스 브리핑을 작성하세요.

규칙:
1. 제공된 기사 내용만 사용. 외부 지식·추측 금지.
2. 인사이트는 롯데멤버스 실무에 바로 적용 가능한 내용으로.
3. 카테고리별로 구분해서 작성.
4. 해당 기사 없는 카테고리는 생략.

=== 선별된 기사 ===
{context}

=== 출력 형식 ===
📅 {kst_now} 롯데멤버스 AI 비즈니스 브리핑

━━━━━━━━━━━━━━━━━━━━━━
🛠️ 솔루션/서비스
━━━━━━━━━━━━━━━━━━━━━━

• 기사 제목 (출처)
  요약: 2문장
  인사이트: [태그] 1문장

━━━━━━━━━━━━━━━━━━━━━━
📡 기술/트렌드
━━━━━━━━━━━━━━━━━━━━━━

• 기사 제목 (출처)
  요약: 2문장
  인사이트: [태그] 1문장

━━━━━━━━━━━━━━━━━━━━━━
⚖️ 법규/규제/리스크
━━━━━━━━━━━━━━━━━━━━━━

• 기사 제목 (출처)
  요약: 2문장
  인사이트: [태그] 1문장

━━━━━━━━━━━━━━━━━━━━━━
🎯 오늘의 액션 아이템
━━━━━━━━━━━━━━━━━━━━━━
- 항목 1
- 항목 2
- 항목 3

인사이트 태그: [광고플랫폼] [데이터전략] [RMN] [리스크관리] [파트너십] 중 선택"""

    message = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=4000,
        messages=[{"role": "user", "content": prompt}],
    )

    briefing = message.content[0].text
    print("  브리핑 작성 완료\n")
    return briefing


# ═══════════════════════════════════════════
# 5단계: Webex 전송 (길이 초과 시 자동 분할)
# ═══════════════════════════════════════════
def step5_send(text):
    print("=" * 50)
    print("5단계: Webex 전송")
    print("=" * 50)

    url     = "https://webexapis.com/v1/messages"
    headers = {
        "Authorization": f"Bearer {WEBEX_BOT_TOKEN}",
        "Content-Type":  "application/json",
    }

    LIMIT  = 7000
    chunks = []
    while len(text) > LIMIT:
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
            print(f"  ✅ 전송 성공 ({i}/{len(chunks)})")
        else:
            print(f"  ❌ 전송 실패: {resp.status_code} {resp.text}")


# ═══════════════════════════════════════════
# 실행
# ═══════════════════════════════════════════
def main():
    # 1. 수집
    pool = step1_collect()
    if not pool:
        step5_send("⚠️ 오늘은 수집된 AI 관련 기사가 없습니다.")
        return

    # 2. 중복 제거
    pool = step2_deduplicate(pool)

    # 3. Claude 선별
    selected = step3_select(pool)
    if not any(selected.values()):
        step5_send("⚠️ 오늘은 롯데멤버스 관련 기사가 없습니다.")
        return

    # 4. 브리핑 작성
    briefing = step4_summarize(selected)

    # 5. Webex 전송
    step5_send(briefing)


if __name__ == "__main__":
    main()
