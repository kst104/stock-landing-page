window.CSAT_SET = {
  date: "2026-11-04",
  dday: 15,
  week: 2,
  focus: "순서·삽입 집중",
  intro: "순서·삽입 집중일입니다. 순서 두 문항과 삽입 두 문항이 6번부터 9번까지 몰려 있으니, 앞의 제목·어법·빈칸·무관한 문장을 11분 안에 끝내고 남은 시간을 뒤에 쓰세요.",
  tip: "순서 문항은 (A)(B)(C)를 읽기 전에 주어진 글의 마지막 문장에서 다음에 올 내용을 예측하세요. 그다음 각 단락의 첫 어구(this, such, for example, however, 대명사)가 무엇을 받는지 표시하고, 받을 대상이 아직 나오지 않은 단락은 첫 자리에서 지웁니다. 삽입 문항은 주어진 문장의 지시어와 연결어에 먼저 밑줄을 긋고, 지문에서 앞 문장과 뒤 문장이 매끄럽게 이어지지 않는 틈을 찾은 뒤, 넣고 나서 앞뒤 문장을 한 번 더 읽어 확인하세요.",
  questions: [
    {
      n: 1,
      type: "title",
      points: 2,
      passage: "When we save a photo to the cloud, the phrase invites us to picture something light and distant, a place without weight or location. The picture is misleading. Every file sent online travels as pulses of light through glass cables, many of them lying on the ocean floor, where a dragging anchor can cut off a region's connection for days. At the other end, the file lands in a data center: a building full of machines that run day and night, draw as much electricity as a small town, and must be cooled constantly, often with water taken from nearby rivers. These buildings are placed not where users live but where land and power are cheap. The cloud, in short, is made of cable, concrete, and electricity. Calling it a cloud does not change what it is; it only makes its costs easier to overlook.",
      options: [
        "How Undersea Cables Carry the World's Data",
        "The Cloud Is Heavier Than Its Name Suggests",
        "Why Data Centers Belong Close to Their Users",
        "Saving Energy by Keeping Fewer Files Online",
        "A Metaphor That Made the Internet Popular"
      ],
      answer: 2,
      explanation: "글은 '클라우드'라는 말이 가볍고 장소 없는 것을 연상시키지만 \"The picture is misleading\"이라며 해저 케이블, 전기와 물을 쓰는 데이터 센터를 들어 인터넷이 물리적 실체임을 보입니다. 결론 \"The cloud, in short, is made of cable, concrete, and electricity\"를 제목으로 옮긴 ②가 정답입니다. ①은 케이블이라는 한 가지 예에만 머물고, ③은 \"placed not where users live but where land and power are cheap\"과 어긋납니다.",
      vocab: ["misleading — 오해를 부르는", "pulse — 파동, 맥박", "anchor — 닻", "data center — 데이터 센터", "draw — (전기·물을) 끌어 쓰다", "overlook — 간과하다"]
    },
    {
      n: 2,
      type: "grammar",
      points: 2,
      passage: "For decades, companies that sold machines had to keep warehouses full of spare parts, many of [1]{which} would never be ordered. A part for a model sold twenty years ago still had to be stored somewhere, just in case a customer called. Three-dimensional printing has begun to change this calculation. Instead of storing the part itself, a company can store a digital file [2]{describing} it and print the part only when a customer actually needs it. The savings are not limited to shelf space. Shipping a file costs almost nothing, so a replacement can be produced close to the customer rather than [3]{sent} across the world. There are limits, of course: printed parts are not yet as strong as those made by traditional methods, and a company must make sure that the files it stores [4]{is} protected from being copied. Still, the idea of a warehouse that holds information rather than objects [5]{seems} likely to spread far beyond the machinery business.",
      options: ["①", "②", "③", "④", "⑤"],
      answer: 4,
      explanation: "④의 주어는 바로 앞의 stores가 아니라 \"the files (it stores)\"이므로 복수 동사 are protected로 고쳐야 합니다. 관계절 it stores가 주어와 동사 사이에 끼어 있어 수일치를 놓치기 쉽습니다. ②는 '~을 설명하는 파일'이라는 능동 의미의 현재분사로 맞고, ③은 \"be produced ... rather than (be) sent\"의 병렬 구조이므로 과거분사가 맞습니다. ⑤의 주어는 objects가 아니라 the idea이므로 단수 동사 seems가 맞습니다.",
      vocab: ["spare part — 예비 부품", "warehouse — 창고", "calculation — 계산, 셈법", "replacement — 교체품", "three-dimensional — 3차원의", "machinery — 기계류"]
    },
    {
      n: 3,
      type: "blank",
      points: 2,
      passage: "For more than a century, a photograph was accepted as proof. A picture of a flooded street or a crowded square could be printed in a newspaper, and readers took it as a record of something that had actually happened, because a camera was understood to capture only what was in front of it. Painters could invent; cameras could not. That understanding has quietly collapsed. Tools that once required a skilled specialist can now add a person to a crowd, remove a building from a skyline, or produce an entire scene that was never photographed at all, and the result is often impossible to distinguish from the real thing by looking. The image itself no longer settles the question of what happened. What matters now is who took the picture, when, and whether it has passed unchanged through every hand since. A photograph, in other words, is now only as trustworthy as [BLANK].",
      options: [
        "the account of where it came from",
        "the quality of the camera that took it",
        "the number of people who have shared it",
        "the skill of the painter who inspired it",
        "the newspaper that chose to print it"
      ],
      answer: 1,
      explanation: "빈칸 문장은 in other words로 바로 앞 문장을 되풀이합니다. 앞에서 \"The image itself no longer settles the question\"이라 하고 \"What matters now is who took the picture, when, and whether it has passed unchanged through every hand since\"라고 했으므로, 사진의 신뢰도는 그 출처와 경로에 대한 설명에 달려 있다는 ①이 정답입니다. ②는 카메라 성능이 아니라 '편집 가능성'이 문제라는 글의 논지와 맞지 않고, ⑤는 신문에 실렸다는 사실이 이제는 증거가 되지 못한다는 흐름과 어긋납니다.",
      vocab: ["proof — 증거", "capture — 포착하다", "collapse — 무너지다", "skyline — 스카이라인, 건물들이 하늘과 맞닿은 윤곽", "distinguish A from B — A를 B와 구별하다", "account — 설명, 내력"]
    },
    {
      n: 4,
      type: "blank",
      points: 3,
      passage: "Automated systems are usually introduced with a reassuring arrangement: the machine does the routine work, and a human operator watches over it, ready to step in if something goes wrong. The arrangement sounds safe, but it contains a hidden contradiction. The better the machine performs, the less often the operator has to intervene, and the less often a skill is used, the faster it fades. An operator who once flew the aircraft or ran the plant by hand gradually becomes someone who has only watched it being done. Meanwhile, the situations in which the machine finally fails are, almost by definition, the unusual and difficult ones that the machine could not handle. At precisely that moment, the system hands control to a person whose ability to cope has been quietly eroded by years of not being needed. The design therefore guarantees that [BLANK].",
      options: [
        "the machine will eventually learn to handle every unusual case",
        "operators will be replaced as soon as the machine proves reliable",
        "the human is least prepared exactly when the human is most needed",
        "routine work will become more demanding than it was before automation",
        "the operator's trust in the machine will decline with every failure"
      ],
      answer: 3,
      explanation: "빈칸 문장은 therefore로 시작하는 결론입니다. 앞에서 기계가 잘 돌아갈수록 조작자의 기술은 \"fades\"하고, 기계가 실패하는 순간은 \"the unusual and difficult ones\"이며, 바로 그때 \"a person whose ability to cope has been quietly eroded\"에게 통제가 넘어간다고 했습니다. 즉 사람이 가장 필요한 순간에 가장 준비가 안 되어 있다는 ③이 정답입니다. ⑤는 신뢰의 감소를 말하는데, 글의 문제는 신뢰가 아니라 쓰지 않아 사라진 '능력'입니다.",
      vocab: ["reassuring — 안심시키는", "intervene — 개입하다", "fade — 희미해지다, 사라지다", "by definition — 정의상, 당연히", "cope — 대처하다", "erode — 서서히 약화시키다"]
    },
    {
      n: 5,
      type: "irrelevant",
      points: 2,
      passage: "Many restaurants have replaced their printed menus with a small square code on the table that customers scan with a phone. [1]{For the owner, the advantages are obvious: prices can be changed in minutes, a dish that has sold out can be removed instantly, and there are no stained or torn menus to reprint.} [2]{Customers who are comfortable with their phones often find the system faster as well, since they can order and pay without waiting for a server.} [3]{Yet for diners without a smartphone, or with one whose camera or battery has failed, the same system turns a simple meal into an awkward request for help.} [4]{The restaurant industry is one of the largest employers of young people taking their first job, and turnover among its staff is unusually high.} [5]{A technology that saves effort for most people can, without anyone intending it, quietly exclude the rest.}",
      options: ["①", "②", "③", "④", "⑤"],
      answer: 4,
      explanation: "글은 코드 메뉴의 장점(①②)과 그것이 일부 손님을 배제한다는 문제(③⑤)를 다룹니다. ④는 식당 업계의 고용과 이직률 이야기라 소재(식당)만 같을 뿐 메뉴 기술과 무관합니다. ⑤의 \"A technology that saves effort for most people\"이 ③의 '대부분에게는 편하지만 일부에게는 곤란한' 상황을 바로 받아 결론을 내린다는 점도 ④가 끼어든 문장임을 보여 줍니다.",
      vocab: ["scan — 스캔하다, 읽어 들이다", "stained — 얼룩진", "reprint — 다시 인쇄하다", "server — (식당의) 종업원", "turnover — 이직률", "exclude — 배제하다"]
    },
    {
      n: 6,
      type: "order",
      points: 2,
      given: "In the earliest libraries, books were simply placed on the shelves in the order in which they arrived. Each new volume went to the end of the row, and a list recorded where it had been put.",
      parts: {
        A: "The solution was to abandon the order of arrival altogether and to arrange books by what they were about. Under such a system, a book's place on the shelf said something about its contents, and a stranger could find it without asking anyone.",
        B: "As collections grew into the thousands, however, memory failed and the lists became unmanageable. Two books on the same subject might sit at opposite ends of the building, and finding everything the library held on a single topic could take days.",
        C: "This method worked well enough while a collection was small. A librarian who had handled every book could remember roughly where each one stood, and a visitor could simply ask."
      },
      options: ["(A) - (C) - (B)", "(B) - (A) - (C)", "(B) - (C) - (A)", "(C) - (A) - (B)", "(C) - (B) - (A)"],
      answer: 5,
      explanation: "(C)의 \"This method\"는 주어진 글의 '도착한 순서대로 꽂는 방식'을 받고, 그 방식이 작은 장서에서는 통했다고 합니다. (B)는 \"however\"로 장서가 커지자 (C)에서 말한 기억(\"could remember\")이 통하지 않게 된 문제를 제시하고, (A)의 \"The solution\"은 그 문제의 해결책으로 도착 순서를 버리고 주제별로 배열했다고 하므로 (C)-(B)-(A), 즉 ⑤입니다. (B)를 먼저 두면 '잘 통하던 시절'이 나오기 전에 however로 반전하게 되어 어색하고, (A)를 먼저 두면 'The solution'이 풀 문제가 아직 없습니다.",
      vocab: ["volume — (책의) 한 권", "collection — 소장품, 장서", "roughly — 대략", "unmanageable — 감당할 수 없는", "abandon — 버리다", "arrange — 배열하다"]
    },
    {
      n: 7,
      type: "order",
      points: 3,
      given: "Headphones that promise to cancel noise do not simply block sound with thick padding, as earmuffs do. They fight sound with sound.",
      parts: {
        A: "When the two waves meet in the ear, they cancel each other out, and the listener hears almost nothing. This trick works best against sounds that are steady and low, such as the hum of an aircraft engine or the rumble of a train, because such sounds change slowly enough for the chip to keep up.",
        B: "A tiny microphone on the outside of each earcup listens to the noise approaching the ear. A chip inside then produces a second sound wave that is the exact mirror image of the first, rising where the noise falls and falling where it rises.",
        C: "Sudden or high-pitched sounds are a different matter. A voice or a slamming door changes too quickly to be mirrored in time, which is why even the best of these headphones still rely on ordinary padding for part of their effect."
      },
      options: ["(A) - (C) - (B)", "(B) - (A) - (C)", "(B) - (C) - (A)", "(C) - (A) - (B)", "(C) - (B) - (A)"],
      answer: 2,
      explanation: "주어진 글의 \"They fight sound with sound\"를 구체적으로 설명하는 (B)가 먼저 와서 마이크와 '거울상 음파'를 소개합니다. (A)의 \"the two waves\"는 (B)의 소음과 두 번째 음파를 받고, (C)의 \"Sudden or high-pitched sounds are a different matter\"는 (A)에서 말한 '일정하고 낮은 소리'와 대비되며, 마지막의 \"still rely on ordinary padding\"이 주어진 글의 padding으로 돌아가 마무리합니다. 따라서 (B)-(A)-(C), 즉 ②입니다. (C)를 (A)보다 앞에 두면 'a different matter'가 무엇과 다른지 알 수 없습니다.",
      vocab: ["padding — 완충재, 쿠션", "earmuffs — 귀마개", "earcup — (헤드폰의) 귀 덮개", "mirror image — 거울상, 정반대 형태", "hum — 웅웅거리는 소리", "high-pitched — 높은 음의"]
    },
    {
      n: 8,
      type: "insert",
      points: 2,
      given: "That assumption, it turned out, was mistaken.",
      passage: "When video platforms first added captions to their programs, the goal was narrow and clear: viewers who could not hear should be able to follow what was said. [P1] Producing captions was expensive and slow. [P2] Some companies therefore treated the requirement as a burden, assuming that only a small minority would ever benefit, and did only the minimum the law demanded. [P3] Within a few years, usage data showed that commuters on noisy trains were switching captions on to watch without sound, that language learners were using them to catch words they would otherwise miss, and that many people simply found fast dialogue easier to follow when they could read it as well as hear it. [P4] In some surveys, most of the people who used captions reported no hearing difficulty at all. [P5] A feature built for a few had quietly become a feature for everyone, and designers began to treat accessibility not as a side obligation but as a source of ideas.",
      options: ["①", "②", "③", "④", "⑤"],
      answer: 3,
      explanation: "주어진 문장의 \"That assumption\"은 '소수만 혜택을 볼 것'이라는 회사들의 가정(\"assuming that only a small minority would ever benefit\")을 가리키므로 그 문장 바로 뒤에 와야 하고, \"it turned out\" 뒤에는 가정이 틀렸음을 보여 주는 이용 자료(\"usage data showed that commuters ...\")가 이어져야 합니다. 따라서 ③이 정답입니다. ②는 비용이 많이 든다는 사실만 나온 자리라 가리킬 '가정'이 없고, ④는 자료가 이미 제시된 뒤라 'it turned out'이 늦습니다.",
      vocab: ["caption — 자막", "burden — 부담", "minority — 소수", "commuter — 통근자", "dialogue — 대화", "accessibility — 접근성", "obligation — 의무"]
    },
    {
      n: 9,
      type: "insert",
      points: 3,
      given: "By then, however, the first version has usually done its work.",
      passage: "When a major event breaks, the first reports are often wrong in some detail, and news organizations correct them within hours as better information arrives. [P1] Readers who saw the early report have already formed a picture of what happened, and that picture is surprisingly resistant to revision. [P2] A correction, published quietly and read by far fewer people, rarely reaches everyone who saw the original, and even those who do read it tend to remember the vivid first account rather than the dull later one. [P3] Experiments confirm this: when people are told a fact and then told that it was mistaken, traces of the first fact continue to shape their judgments long afterward. [P4] For newsrooms, this means that speed carries a cost that is not paid by the newsroom itself but by its audience. [P5] The question is not only whether a story will be corrected but how many minds will have been made up before the correction arrives.",
      options: ["①", "②", "③", "④", "⑤"],
      answer: 1,
      explanation: "주어진 문장의 \"By then\"은 정정 보도가 나오는 시점, 즉 첫 문장의 \"correct them within hours\"를 받고, \"however\"는 '정정하면 된다'는 흐름을 꺾습니다. 또 \"done its work\"가 무슨 뜻인지는 바로 뒤의 \"Readers who saw the early report have already formed a picture of what happened\"가 설명하므로 ①이 정답입니다. ③에 넣으면 앞 문장에 by then이 받을 시점 표현이 없고, 뒤의 \"Experiments confirm this\"가 가리킬 구체적 주장(첫 보도를 더 잘 기억한다)과도 떨어지게 됩니다.",
      vocab: ["break — (뉴스가) 터지다, 알려지다", "resistant to — ~에 저항하는, 잘 바뀌지 않는", "revision — 수정", "vivid — 생생한", "trace — 흔적", "make up one's mind — 마음을 정하다"]
    }
  ]
};
