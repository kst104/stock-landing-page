window.CSAT_SET = {
  date: "2026-11-09",
  dday: 10,
  week: 2,
  focus: "2주차 점검 (혼합)",
  intro: "2주차를 마무리하는 점검 세트입니다. 빈칸 세 문항이 연달아 나오고 빈칸 하나·삽입·요약문이 3점이므로, 앞의 목적·함축·어법 세 문항은 6분 안에 끝내고 뒤에 시간을 남기세요.",
  tip: "유형이 바뀔 때마다 발문을 먼저 읽고 무엇을 찾아야 하는지 정한 뒤 지문으로 들어가세요. 빈칸은 빈칸이 든 문장이 어느 문장을 다시 말하는지 표시하고, 삽입은 주어진 문장의 지시어와 연결어가 가리키는 대상을 지문에서 찾습니다. 한 문항에 3분을 넘기면 답을 하나 골라 표시해 두고 다음으로 넘어간 뒤 마지막에 돌아오세요.",
  questions: [
    {
      n: 1,
      type: "purpose",
      points: 2,
      passage: "Dear Volunteers,\nThank you for taking part in this year's Riverside Butterfly Count. Over the summer, more than two hundred of you walked your assigned routes every week and recorded what you saw. The counting season ended on October 31, but our work is not finished. Our research team can begin analyzing the data only after every log has been entered, and as of today about one third of the logs have not yet been submitted. Please go to the project website and enter your weekly records by November 15. If you kept your notes on paper, you may also mail them to our office, and we will enter them for you. Every missing log weakens the final result, so even a route with few sightings matters. Thank you again for your time and care.\nSincerely,\nEllen Marsh\nProject Coordinator, Riverside Butterfly Count",
      options: [
        "나비 관찰 자원봉사자를 새로 모집하려고",
        "관찰 경로 배정이 변경되었음을 알리려고",
        "관찰 기록을 기한 내에 제출하도록 요청하려고",
        "올해 나비 개체 수 조사 결과를 공유하려고",
        "자원봉사자 감사 행사 참석을 안내하려고"
      ],
      answer: 3,
      explanation: "핵심 문장은 \"Please go to the project website and enter your weekly records by November 15\"이며, 아직 제출되지 않은 기록이 있어 분석을 시작할 수 없다는 사정을 설명한 뒤 기한을 못 박고 있으므로 ③이 정답입니다. ④는 결과 공유가 아니라 \"can begin analyzing the data only after every log has been entered\"처럼 분석을 아직 시작하지도 못했다는 내용과 어긋나고, ①은 이미 참여한 사람들에게 보내는 글이라 맞지 않습니다.",
      vocab: ["take part in — ~에 참여하다", "assigned — 배정된", "log — 기록(지)", "as of today — 오늘 기준으로", "sighting — 목격, 관찰"]
    },
    {
      n: 2,
      type: "implied",
      points: 2,
      passage: "Scientific journals are full of experiments that worked. Open almost any issue and you will find a new effect discovered, a prediction confirmed, a hypothesis supported. What you will rarely find is the far larger number of experiments that found nothing: the treatment that made no difference, the correlation that disappeared, the effect that could not be repeated. Because such results are seldom published, each new researcher who approaches a question starts with an incomplete picture of what has already been tried. Teams in different cities repeat the same failed experiment, each believing it to be new, and the field as a whole spends years learning the same lesson over and over. A negative result may not make a headline, but [U]{a map of dead ends is still a map}. Knowing where a road does not lead saves travelers from walking it again, and a discipline that throws such knowledge away is choosing to get lost.",
      options: [
        "failed experiments should be repeated until they succeed",
        "scientists must publish only results that make headlines",
        "even careful research cannot avoid taking wrong turns",
        "a field advances fastest when teams compete to be first",
        "knowing what does not work is itself useful knowledge"
      ],
      answer: 5,
      explanation: "글은 효과가 없었던 실험 결과가 발표되지 않아 여러 팀이 같은 실패를 되풀이한다고 지적하고, 밑줄 바로 뒤 문장 \"Knowing where a road does not lead saves travelers from walking it again\"이 비유를 풀어 줍니다. 막다른 길이 표시된 지도처럼, 무엇이 통하지 않는지 아는 것도 쓸모 있는 지식이라는 ⑤가 정답입니다. ①은 실패한 실험을 다시 하는 일이 바로 글이 말하는 낭비이고, ③은 글의 초점이 '실수를 피할 수 없다'가 아니라 '실패의 기록을 버리지 말라'는 데 있으므로 맞지 않습니다.",
      vocab: ["issue — (잡지의) 호", "hypothesis — 가설", "correlation — 상관관계", "seldom — 좀처럼 ~않는", "dead end — 막다른 길", "discipline — 학문 분야"]
    },
    {
      n: 3,
      type: "grammar",
      points: 2,
      passage: "For most of history, the units people used to measure length and weight varied from one town to the next. A 'foot' in one market might be noticeably longer than a 'foot' a day's walk away, and merchants [1]{traveling} between regions had to carry conversion tables or rely on their memory. As trade expanded, this variety became a serious obstacle, since a buyer could never be certain [2]{that} the quantity written in a contract actually meant. Governments gradually responded by defining a single standard for each unit and [3]{requiring} that copies of it [4]{be} kept in public places, where anyone could compare their own tools against it. The point of a standard was not that its length was somehow correct, but that everyone agreed to use the same one. Once that agreement existed, a measurement taken in one city could be trusted in another, [5]{which} made long-distance trade far easier.",
      options: ["①", "②", "③", "④", "⑤"],
      answer: 2,
      explanation: "②에서 뒤의 동사 meant는 목적어가 필요한데 절 안에 목적어가 없으므로, 접속사 that이 아니라 선행사를 포함해 목적어 역할을 하는 관계대명사 what을 써야 합니다(certain what the quantity ... actually meant). ①은 merchants를 수식하는 현재분사, ③은 by 뒤의 defining과 병렬인 동명사, ④는 requiring that 뒤의 요구 내용이므로 동사원형 be가 맞으며, ⑤의 which는 앞 절 전체를 받는 계속적 용법입니다.",
      vocab: ["vary — 다르다, 달라지다", "conversion table — 환산표", "obstacle — 장애물", "standard — 표준(기)", "long-distance — 장거리의"]
    },
    {
      n: 4,
      type: "blank",
      points: 2,
      passage: "Most of us carry around a comfortable feeling of understanding things we have never actually explained. We have seen a kite fly many times, so we feel we know why it stays up; we have used a key every day, so we assume we could describe what happens inside the lock. The feeling survives because it is never tested. The moment we try to explain the kite or the lock to a curious child, the gaps appear: we reach for a step and find it missing, or discover that two of our ideas contradict each other. Teachers know this well, which is why they say that the surest way to learn a subject is to teach it. The act of explaining does not merely transmit what we know; it exposes the difference between knowing and the feeling of knowing. Understanding, it turns out, is often revealed only when we are forced to [BLANK].",
      options: [
        "rely on examples instead of definitions",
        "compare our ideas with those of experts",
        "memorize the steps in the correct order",
        "put what we know into words for someone else",
        "admit how little we actually remember"
      ],
      answer: 4,
      explanation: "\"The feeling survives because it is never tested. The moment we try to explain the kite or the lock to a curious child, the gaps appear\"가 단서입니다. 이해했다는 느낌은 남에게 말로 설명해 보려는 순간에야 검증되므로, 이를 바꿔 말한 ④가 정답입니다. ⑤는 문제가 '기억'이 아니라 '설명'에 있다는 점에서 어긋나고, ②의 전문가와의 비교는 글에 나오지 않습니다.",
      vocab: ["carry around — 지니고 다니다", "contradict — 모순되다", "transmit — 전달하다", "expose — 드러내다", "it turns out — 알고 보니"]
    },
    {
      n: 5,
      type: "blank",
      points: 2,
      passage: "Suppose a regional health office ranks all of its towns by the rate of a certain illness and finds that the towns at the top of the list are small, rural, and far from major hospitals. Officials might be about to conclude that rural life itself carries a risk when someone looks at the bottom of the list. The towns with the lowest rates are also small, rural, and far from hospitals. The explanation has nothing to do with country living. In a town of three hundred people, two or three extra cases in a single year push the rate far above average, while two or three fewer push it far below. In a city of a million, the same handful of cases changes nothing. Small places occupy both ends of any such ranking not because something unusual happens there, but because [BLANK].",
      options: [
        "a few cases carry great weight when totals are small",
        "people in the countryside report illness less often",
        "records are kept more carefully in large cities",
        "their rates are measured against the wrong average",
        "disease travels quickly where neighbors live close"
      ],
      answer: 1,
      explanation: "가장 높은 곳도 가장 낮은 곳도 모두 작은 마을이라는 점이 핵심 단서이며, \"two or three extra cases in a single year push the rate far above average, while two or three fewer push it far below\"와 \"In a city of a million, the same handful of cases changes nothing\"이 그 이유를 말해 줍니다. 전체 수가 작을수록 몇 건의 차이가 비율을 크게 흔든다는 ①이 정답입니다. ②는 시골 마을이 상위와 하위 양쪽에 모두 있다는 사실을 설명하지 못하고, ④의 '잘못된 평균'은 글에 언급이 없습니다.",
      vocab: ["rank — 순위를 매기다", "rural — 시골의", "have nothing to do with — ~와 관계가 없다", "handful — 소수, 한 줌", "occupy — 차지하다"]
    },
    {
      n: 6,
      type: "blank",
      points: 3,
      passage: "Every scientific instrument is also a kind of filter. A thermometer reports temperature and nothing else; a microscope reveals what is small and nearby while remaining silent about what is large and far away. This seems obvious, yet its consequences are easy to miss. Researchers naturally gravitate toward questions that their tools can answer, and over time a field's sense of what is important comes to mirror the capabilities of its equipment. Problems that resist measurement are not declared unimportant; they are simply asked about less often, cited less often, and funded less often, until they fade from view. When a new instrument appears, the effect runs in reverse: phenomena that were always present suddenly become 'discoveries,' and questions that no one thought to ask become urgent. What a discipline regards as its central problems, in other words, is shaped less by nature itself than by [BLANK].",
      options: [
        "the number of researchers competing for funding",
        "the questions the public regards as most urgent",
        "the historical accidents of its early discoveries",
        "the willingness of scientists to admit ignorance",
        "the range of what its instruments can detect"
      ],
      answer: 5,
      explanation: "빈칸 문장은 in other words로 앞 내용을 재진술합니다. \"a field's sense of what is important comes to mirror the capabilities of its equipment\"가 핵심이고, 측정이 어려운 문제는 시야에서 사라지며 새 도구가 나오면 새 질문이 생긴다는 설명이 이를 뒷받침하므로 ⑤가 정답입니다. ③은 '초기 발견의 우연'이라 그럴듯하지만 글이 말하는 것은 도구가 무엇을 감지할 수 있는가이고, ①은 funded라는 단어만 떼어 온 것입니다.",
      vocab: ["filter — 거르는 장치, 필터", "gravitate toward — ~에 끌리다", "mirror — 반영하다", "cite — 인용하다", "fade from view — 시야에서 사라지다", "phenomena — 현상 (phenomenon의 복수)"]
    },
    {
      n: 7,
      type: "order",
      points: 2,
      given: "Stories of great discoveries often feature a lucky accident: a sample left out overnight, a spill, a wrong bottle picked up by mistake.",
      parts: {
        A: "What separated these few from the many was not the accident itself but the response to it. Instead of treating the unexpected result as a nuisance, the researcher paused, asked why it had happened, and designed a second experiment to find out.",
        B: "Told this way, the discovery seems to belong to chance rather than to the scientist. Yet the same accidents happen constantly in laboratories around the world, and almost all of them pass unnoticed, cleaned up and forgotten by the end of the day.",
        C: "That pause is possible only for someone who knows the field well enough to recognize that something does not fit. Luck, in this sense, supplies the raw material, but preparation decides whether it is ever turned into knowledge."
      },
      options: ["(A) - (C) - (B)", "(B) - (A) - (C)", "(B) - (C) - (A)", "(C) - (A) - (B)", "(C) - (B) - (A)"],
      answer: 2,
      explanation: "(B)의 \"Told this way\"는 주어진 글의 '행운의 사고' 이야기 방식을 받고, 같은 사고가 늘 일어나지만 대부분 지나쳐진다고 합니다. (A)의 \"these few from the many\"는 (B)의 '소수의 유명한 사례와 지나쳐진 다수'를 전제로 하며, (C)의 \"That pause\"는 (A)의 \"the researcher paused\"를 받으므로 (B)-(A)-(C), 즉 ②입니다. (A)를 먼저 두면 these few가 가리킬 대상이 없습니다.",
      vocab: ["feature — 특징으로 담다", "spill — 엎지름", "pass unnoticed — 눈에 띄지 않고 지나가다", "nuisance — 성가신 것", "raw material — 원료"]
    },
    {
      n: 8,
      type: "insert",
      points: 3,
      given: "That explanation, however, overlooks a simpler possibility.",
      passage: "A survey of several hundred companies finds that offices with more plants report higher employee productivity. [P1] The finding quickly travels through business magazines, and the explanation seems obvious: greenery reduces stress, and relaxed workers get more done. [P2] Convinced, some managers order plants for every desk. [P3] Companies that spend money on plants are usually companies with money to spend, which means they also tend to offer better pay, newer equipment, and more comfortable working conditions. [P4] Any of these could raise productivity on its own, with the plants doing nothing more than marking which companies are well-off. [P5] To learn whether plants themselves make a difference, one would have to compare offices that are alike in every other respect, or, better still, add plants to some offices chosen at random and see what changes.",
      options: ["①", "②", "③", "④", "⑤"],
      answer: 3,
      explanation: "주어진 문장의 \"That explanation\"은 '식물이 스트레스를 줄여 생산성을 높인다'는 설명을 가리키고, \"however ... a simpler possibility\"는 뒤에 다른 가능성이 제시됨을 예고합니다. ③ 뒤의 \"Companies that spend money on plants are usually companies with money to spend\"가 바로 그 가능성이므로 ③이 정답입니다. ②에 넣으면 \"Convinced, some managers order plants\"가 반박 뒤에 와서 '설득되었다'는 말이 어색해집니다.",
      vocab: ["productivity — 생산성", "greenery — 녹색 식물", "convinced — 확신한, 설득된", "well-off — 부유한", "in every other respect — 다른 모든 면에서", "at random — 무작위로"]
    },
    {
      n: 9,
      type: "summary",
      points: 3,
      passage: "In one experiment, participants were first asked whether they believed a new method of teaching mathematics was effective. They were then shown a table of test results from two schools, one that had adopted the method and one that had not. The numbers were deliberately ambiguous: the school using the method scored slightly higher, but it also had smaller classes and more experienced teachers. Participants who had said the method worked described the table as 'clear evidence' and rarely mentioned the other differences. Those who had doubted the method pointed immediately to class size and teacher experience and judged the table 'useless.' When the researchers swapped the labels so that the higher-scoring school was the one without the method, each group simply switched its reasoning: the former believers now discovered the problems of class size, and the former doubters found the evidence convincing. The data had not changed, but the standards applied to it had.",
      given: "People tend to judge the same evidence as (A) when it supports what they already believe, and to become (B) only when it does not.",
      options: [
        "convincing …… critical",
        "incomplete …… curious",
        "convincing …… generous",
        "outdated …… critical",
        "incomplete …… careless"
      ],
      answer: 1,
      explanation: "두 집단 모두 자기 믿음과 맞는 표는 \"clear evidence\"로 받아들이고, 맞지 않는 표에서는 학급 크기와 교사 경력 같은 문제점을 찾아냈으며, 표의 이름표를 바꾸자 \"each group simply switched its reasoning\"이었습니다. 즉 믿음에 맞으면 설득력 있다고 보고 맞지 않을 때만 비판적이 되므로 (A) convincing, (B) critical인 ①이 정답입니다. ③의 generous는 반대이고, ④의 outdated는 글에 근거가 없습니다.",
      vocab: ["adopt — 채택하다", "deliberately — 일부러", "ambiguous — 모호한", "swap — 맞바꾸다", "former — 이전의", "standard — 기준"]
    }
  ]
};
