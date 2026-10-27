/* 날짜별 학습 계획. index.html, 각 세트 페이지, tools/validate.js가 함께 읽는다.
   types: 그날 세트의 문항 유형을 출제 순서대로 적은 것. three: 3점 문항 수. */
window.CSAT_PLAN = {
  examDate: "2026-11-19",
  examTime: "3교시 13:10~14:20 (70분, 듣기 포함 45문항)",
  defaultTimeLimitMin: 20,
  weeks: {
    1: "1주차 · 진단 — 모든 유형을 고르게",
    2: "2주차 · 고난도 유형 집중",
    3: "3주차 · 실전 혼합 — 시간 압박",
    4: "마무리 — 가볍게, 컨디션 유지"
  },
  days: [
    { date: "2026-10-27", dday: 23, week: 1, focus: "빈칸 추론 (진단 ①)",
      types: ["purpose", "gist", "vocab", "blank", "blank", "irrelevant", "order", "insert", "summary"], three: 3 },
    { date: "2026-10-28", dday: 22, week: 1, focus: "글의 순서·문장 삽입 (진단 ②)",
      types: ["claim", "title", "grammar", "blank", "blank", "order", "order", "insert", "summary"], three: 3 },
    { date: "2026-10-29", dday: 21, week: 1, focus: "어법·어휘",
      types: ["mood", "topic", "notice", "grammar", "vocab", "blank", "blank", "order", "insert"], three: 3 },
    { date: "2026-10-30", dday: 20, week: 1, focus: "요지·주제·제목",
      types: ["implied", "gist", "topic", "title", "blank", "blank", "order", "insert", "summary"], three: 3 },
    { date: "2026-10-31", dday: 19, week: 1, focus: "함축 의미",
      types: ["claim", "implied", "implied", "grammar", "blank", "blank", "irrelevant", "insert", "summary"], three: 3 },
    { date: "2026-11-01", dday: 18, week: 1, focus: "요약문",
      types: ["purpose", "match", "vocab", "blank", "blank", "order", "insert", "summary", "summary"], three: 3 },
    { date: "2026-11-02", dday: 17, week: 1, focus: "1주차 점검 (혼합)",
      types: ["title", "implied", "grammar", "blank", "blank", "blank", "order", "insert", "summary"], three: 3 },

    { date: "2026-11-03", dday: 16, week: 2, focus: "빈칸 집중 ①",
      types: ["gist", "vocab", "blank", "blank", "blank", "blank", "order", "insert", "summary"], three: 3 },
    { date: "2026-11-04", dday: 15, week: 2, focus: "순서·삽입 집중",
      types: ["title", "grammar", "blank", "blank", "irrelevant", "order", "order", "insert", "insert"], three: 3 },
    { date: "2026-11-05", dday: 14, week: 2, focus: "어법·어휘 집중",
      types: ["grammar", "grammar", "vocab", "vocab", "blank", "blank", "order", "insert", "summary"], three: 3 },
    { date: "2026-11-06", dday: 13, week: 2, focus: "빈칸 집중 ②",
      types: ["claim", "implied", "blank", "blank", "blank", "blank", "order", "insert", "summary"], three: 3 },
    { date: "2026-11-07", dday: 12, week: 2, focus: "요약문 집중",
      types: ["title", "grammar", "blank", "blank", "irrelevant", "order", "insert", "summary", "summary"], three: 3 },
    { date: "2026-11-08", dday: 11, week: 2, focus: "무관한 문장·순서·삽입",
      types: ["vocab", "blank", "blank", "irrelevant", "irrelevant", "order", "order", "insert", "insert"], three: 3 },
    { date: "2026-11-09", dday: 10, week: 2, focus: "2주차 점검 (혼합)",
      types: ["purpose", "implied", "grammar", "blank", "blank", "blank", "order", "insert", "summary"], three: 3 },

    { date: "2026-11-10", dday: 9, week: 3, focus: "실전 혼합 ①",
      types: ["grammar", "vocab", "blank", "blank", "blank", "irrelevant", "order", "insert", "summary"], three: 3 },
    { date: "2026-11-11", dday: 8, week: 3, focus: "실전 혼합 ②",
      types: ["implied", "match", "grammar", "blank", "blank", "blank", "order", "insert", "summary"], three: 3 },
    { date: "2026-11-12", dday: 7, week: 3, focus: "실전 혼합 ③",
      types: ["title", "vocab", "blank", "blank", "blank", "order", "order", "insert", "summary"], three: 3 },
    { date: "2026-11-13", dday: 6, week: 3, focus: "실전 혼합 ④",
      types: ["claim", "grammar", "blank", "blank", "blank", "irrelevant", "insert", "insert", "summary"], three: 3 },
    { date: "2026-11-14", dday: 5, week: 3, focus: "실전 혼합 ⑤",
      types: ["gist", "implied", "vocab", "blank", "blank", "blank", "order", "insert", "summary"], three: 3 },
    { date: "2026-11-15", dday: 4, week: 3, focus: "실전 혼합 ⑥",
      types: ["topic", "notice", "grammar", "blank", "blank", "blank", "order", "insert", "summary"], three: 3 },
    { date: "2026-11-16", dday: 3, week: 3, focus: "3주차 점검 (혼합)",
      types: ["purpose", "title", "grammar", "blank", "blank", "blank", "order", "insert", "summary"], three: 3 },

    { date: "2026-11-17", dday: 2, week: 4, focus: "가벼운 정리 (전부 2점)",
      types: ["purpose", "mood", "gist", "title", "vocab", "blank", "blank", "order", "insert"], three: 0 },
    { date: "2026-11-18", dday: 1, week: 4, focus: "컨디션 세트 (6문항, 15분)",
      types: ["purpose", "claim", "topic", "blank", "insert", "summary"], three: 1, timeLimitMin: 15 }
  ]
};
