/* set-runner.js — 수능형 영어 연습 세트 실행기
   입력: window.CSAT_SET (sets/YYYY-MM-DD.js), window.CSAT_PLAN (assets/plan.js)
   동작: 시작 버튼으로 타이머를 켜고 문항을 보여 준다. 제출하면 채점, 해설, 유형별 정답률을 보여 주고
         localStorage('csat-en:'+날짜)에 결과를 저장한다. "결과 복사"는 대화창에 붙여 넣을 요약문을 만든다. */
(function () {
  'use strict';

  var CIRCLED = ['①', '②', '③', '④', '⑤'];
  var TYPE_LABEL = {
    purpose: '목적', mood: '심경 변화', claim: '주장', implied: '함축 의미', gist: '요지', topic: '주제',
    title: '제목', match: '내용 일치', notice: '안내문', grammar: '어법', vocab: '어휘', blank: '빈칸 추론',
    irrelevant: '무관한 문장', order: '글의 순서', insert: '문장 삽입', summary: '요약문'
  };
  var STEM = {
    purpose: '다음 글의 목적으로 가장 적절한 것은?',
    mood: '다음 글에 드러난 인물의 심경 변화로 가장 적절한 것은?',
    claim: '다음 글에서 필자가 주장하는 바로 가장 적절한 것은?',
    implied: '밑줄 친 부분이 다음 글에서 의미하는 바로 가장 적절한 것은?',
    gist: '다음 글의 요지로 가장 적절한 것은?',
    topic: '다음 글의 주제로 가장 적절한 것은?',
    title: '다음 글의 제목으로 가장 적절한 것은?',
    match: '다음 글의 내용과 일치하지 않는 것은?',
    notice: '다음 안내문의 내용과 일치하지 않는 것은?',
    grammar: '다음 글의 밑줄 친 부분 중, 어법상 틀린 것은?',
    vocab: '다음 글의 밑줄 친 부분 중, 문맥상 낱말의 쓰임이 적절하지 않은 것은?',
    blank: '다음 빈칸에 들어갈 말로 가장 적절한 것은?',
    irrelevant: '다음 글에서 전체 흐름과 관계 없는 문장은?',
    order: '주어진 글 다음에 이어질 글의 순서로 가장 적절한 것을 고르시오.',
    insert: '글의 흐름으로 보아, 주어진 문장이 들어가기에 가장 적절한 곳을 고르시오.',
    summary: '다음 글의 내용을 한 문장으로 요약하고자 한다. 빈칸 (A), (B)에 들어갈 말로 가장 적절한 것은?'
  };
  var ORDER_OPTIONS = ['(A) - (C) - (B)', '(B) - (A) - (C)', '(B) - (C) - (A)', '(C) - (A) - (B)', '(C) - (B) - (A)'];
  var KO_OPTION_TYPES = { purpose: 1, mood: 1, claim: 1, gist: 1, match: 1, notice: 1 };

  var app = document.getElementById('app');
  var SET = window.CSAT_SET;
  var PLAN = window.CSAT_PLAN || { days: [], defaultTimeLimitMin: 20 };

  function esc(s) {
    return String(s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }
  function el(tag, cls, html) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (html != null) n.innerHTML = html;
    return n;
  }
  function pad(n) { return (n < 10 ? '0' : '') + n; }
  function mmss(sec) { sec = Math.max(0, Math.round(sec)); return pad(Math.floor(sec / 60)) + ':' + pad(sec % 60); }
  function storageKey() { return 'csat-en:' + SET.date; }
  function loadResult() { try { return JSON.parse(localStorage.getItem(storageKey())); } catch (e) { return null; } }
  function saveResult(r) { try { localStorage.setItem(storageKey(), JSON.stringify(r)); return true; } catch (e) { return false; } }

  /* 지문 토큰 → HTML. 먼저 이스케이프한 뒤 토큰만 바꾼다.
     [BLANK] 빈칸 · [n]{text} 번호 밑줄 · [Pn] 삽입 위치 · [U]{text} 밑줄 · 줄바꿈은 단락 */
  function renderText(s) {
    var h = esc(s);
    h = h.replace(/\[BLANK\]/g, '<span class="blank" aria-label="빈칸">&nbsp;</span>');
    h = h.replace(/\[([1-5])\]\{([^}]*)\}/g, function (_, n, t) { return '<span class="mk">' + CIRCLED[n - 1] + '</span><u>' + t + '</u>'; });
    h = h.replace(/\[P([1-5])\]/g, function (_, n) { return '<span class="ins">( ' + CIRCLED[n - 1] + ' )</span>'; });
    h = h.replace(/\[U\]\{([^}]*)\}/g, '<u>$1</u>');
    return h.split(/\n+/).map(function (p) { return '<p>' + p + '</p>'; }).join('');
  }

  if (!SET || !SET.questions) {
    app.innerHTML = '<p class="eyebrow">수능영어 연습 세트</p><h1>이 날짜의 세트가 아직 없습니다</h1>' +
      '<p>sets/ 폴더에 세트 파일이 없거나 아직 만들어지지 않았습니다. <a href="../index.html">달력으로 돌아가기</a></p>';
    return;
  }

  var dayIdx = -1;
  for (var i = 0; i < PLAN.days.length; i++) if (PLAN.days[i].date === SET.date) dayIdx = i;
  var day = dayIdx >= 0 ? PLAN.days[dayIdx] : { dday: SET.dday, focus: SET.focus, week: SET.week };
  var prevDay = dayIdx > 0 ? PLAN.days[dayIdx - 1] : null;
  var nextDay = dayIdx >= 0 && dayIdx < PLAN.days.length - 1 ? PLAN.days[dayIdx + 1] : null;
  var limitSec = (SET.timeLimitMin || day.timeLimitMin || PLAN.defaultTimeLimitMin || 20) * 60;
  var totalPoints = SET.questions.reduce(function (a, q) { return a + (q.points || 2); }, 0);

  function optionsOf(q) {
    if (q.options && q.options.length === 5) return q.options;
    if (q.type === 'order') return ORDER_OPTIONS;
    return CIRCLED;
  }
  function stemOf(q) { return q.stem || STEM[q.type] || ''; }

  /* ---------- 상단 바 ---------- */
  var bar = el('div', 'bar');
  bar.innerHTML = '<span><strong>D-' + esc(day.dday) + '</strong> · ' + esc(SET.date) + '</span>' +
    '<span class="muted">' + esc(day.focus || '') + '</span><span class="spacer"></span>' +
    '<span class="timer" aria-live="off">' + mmss(limitSec) + '</span>';
  var submitBtn = el('button', 'btn btn-primary', '제출하고 채점');
  submitBtn.type = 'button'; submitBtn.hidden = true;
  bar.appendChild(submitBtn);
  app.appendChild(bar);
  var timerEl = bar.querySelector('.timer');

  /* ---------- 머리말 ---------- */
  var head = el('div', 'intro');
  head.innerHTML = '<p class="eyebrow">수능영어 연습 세트 · ' + esc(PLAN.weeks ? PLAN.weeks[day.week] || '' : '') + '</p>' +
    '<h1>D-' + esc(day.dday) + ' · ' + esc(day.focus || '') + '</h1>' +
    '<p>' + esc(SET.intro || '') + '</p>' +
    (SET.tip ? '<div class="callout"><p><strong>오늘의 전략 — ' + esc(day.focus || '') + '</strong></p><p>' + esc(SET.tip) + '</p></div>' : '') +
    '<p class="muted small">독해 ' + SET.questions.length + '문항 · ' + totalPoints + '점 · 제한 ' + Math.round(limitSec / 60) + '분. ' +
    '시작을 누르면 시간이 갑니다. 모르는 문항은 표시만 하고 넘어간 뒤 마지막에 돌아오세요. 지문과 선택지는 AI가 새로 쓴 창작 문항이며 평가원 문제가 아닙니다.</p>';
  app.appendChild(head);

  var prior = loadResult();
  var priorBox = el('div', 'callout');
  if (prior) {
    priorBox.innerHTML = '<p><strong>지난 기록</strong> ' + esc(prior.correct) + '/' + esc(prior.count) + ' 정답 · ' +
      esc(prior.score) + '/' + esc(prior.total) + '점 · ' + mmss(prior.seconds) + (prior.overtime ? ' (시간 초과)' : '') +
      ' · ' + esc(prior.at || '') + '</p><p class="small muted">다시 풀면 기록이 덮어써집니다.</p>';
    head.appendChild(priorBox);
  }

  var startBtn = el('button', 'btn btn-primary', prior ? '다시 풀기 (타이머 시작)' : '시작 (타이머 시작)');
  startBtn.type = 'button';
  var startRow = el('div', 'actions');
  startRow.appendChild(startBtn);
  head.appendChild(startRow);

  /* ---------- 문항 ---------- */
  var form = el('form');
  form.hidden = true;
  form.addEventListener('submit', function (e) { e.preventDefault(); });

  SET.questions.forEach(function (q, qi) {
    var fs = el('fieldset', 'q');
    fs.id = 'q' + q.n;
    var legend = el('legend', null,
      esc(q.n) + '. ' + esc(stemOf(q)) + ' <span class="muted">[' + esc(q.points || 2) + '점]</span>' +
      '<span class="badge">' + esc(TYPE_LABEL[q.type] || q.type) + '</span>');
    fs.appendChild(legend);

    if (q.type === 'insert' && q.given) {
      fs.appendChild(el('div', 'box passage', '<span class="lbl">주어진 문장</span>' + renderText(q.given)));
    }
    if (q.type === 'order') {
      fs.appendChild(el('div', 'box passage', '<span class="lbl">주어진 글</span>' + renderText(q.given || '')));
      var parts = el('div', 'passage parts');
      ['A', 'B', 'C'].forEach(function (k) {
        parts.appendChild(el('p', null, '<b>(' + k + ')</b> ' + renderText((q.parts && q.parts[k]) || '').replace(/^<p>|<\/p>$/g, '')));
      });
      fs.appendChild(parts);
    } else {
      fs.appendChild(el('div', 'passage', renderText(q.passage || '')));
    }
    if (q.type === 'summary' && q.given) {
      fs.appendChild(el('div', 'box passage', '<span class="lbl">요약문</span>' + renderText(q.given).replace(/\((A|B)\)/g, '<b>($1)</b>')));
    }
    if (q.type !== 'insert' && q.type !== 'order' && q.type !== 'summary' && q.given) {
      fs.appendChild(el('div', 'box passage', renderText(q.given)));
    }

    var opts = el('div', 'opts');
    optionsOf(q).forEach(function (text, oi) {
      var label = el('label', KO_OPTION_TYPES[q.type] ? 'ko' : '');
      var input = document.createElement('input');
      input.type = 'radio'; input.name = 'q' + q.n; input.value = String(oi + 1);
      label.appendChild(input);
      var isCircled = text === CIRCLED[oi];
      label.appendChild(el('span', null, isCircled ? esc(text) : esc(CIRCLED[oi]) + ' ' + esc(text)));
      opts.appendChild(label);
    });
    fs.appendChild(opts);
    form.appendChild(fs);
  });
  app.appendChild(form);

  var bottom = el('div', 'actions');
  bottom.hidden = true;
  var submitBtn2 = el('button', 'btn btn-primary', '제출하고 채점');
  submitBtn2.type = 'button';
  bottom.appendChild(submitBtn2);
  app.appendChild(bottom);

  var resultBox = el('section', 'result');
  resultBox.hidden = true;
  app.appendChild(resultBox);

  var nav = el('nav', 'footer-nav');
  nav.setAttribute('aria-label', '이동');
  nav.innerHTML = '<a href="../index.html">달력</a>' +
    (prevDay ? '<a href="' + esc(prevDay.date) + '.html">← D-' + esc(prevDay.dday) + '</a>' : '') +
    (nextDay ? '<a href="' + esc(nextDay.date) + '.html">D-' + esc(nextDay.dday) + ' →</a>' : '') +
    '<a href="../reference/question-types.html">유형별 접근법</a>';
  app.appendChild(nav);

  /* ---------- 타이머 ---------- */
  var startedAt = null, tick = null, overtime = false;
  function updateTimer() {
    var elapsed = (Date.now() - startedAt) / 1000;
    var remain = limitSec - elapsed;
    if (remain <= 0) {
      if (!overtime) { overtime = true; timerEl.classList.add('over'); }
      timerEl.textContent = '+' + mmss(-remain);
    } else {
      timerEl.textContent = mmss(remain);
    }
  }
  startBtn.addEventListener('click', function () {
    startedAt = Date.now(); overtime = false; timerEl.classList.remove('over');
    head.hidden = true; form.hidden = false; bottom.hidden = false; submitBtn.hidden = false; resultBox.hidden = true;
    Array.prototype.forEach.call(form.querySelectorAll('input[type=radio]'), function (r) { r.checked = false; r.disabled = false; });
    Array.prototype.forEach.call(form.querySelectorAll('.is-correct, .is-wrong'), function (n) { n.classList.remove('is-correct', 'is-wrong'); });
    Array.prototype.forEach.call(form.querySelectorAll('.verdict, .expl, .vocab'), function (n) { n.parentNode.removeChild(n); });
    updateTimer();
    tick = setInterval(updateTimer, 1000);
    window.scrollTo(0, 0);
  });

  /* ---------- 채점 ---------- */
  function submit() {
    var unanswered = SET.questions.filter(function (q) { return !form.querySelector('input[name=q' + q.n + ']:checked'); });
    if (unanswered.length && !window.confirm('아직 답하지 않은 문항이 ' + unanswered.length + '개 있습니다. 그대로 제출할까요? (미답은 오답 처리)')) return;
    clearInterval(tick);
    var seconds = Math.round((Date.now() - startedAt) / 1000);
    var correct = 0, score = 0, perType = {}, rows = [];

    SET.questions.forEach(function (q) {
      var picked = form.querySelector('input[name=q' + q.n + ']:checked');
      var sel = picked ? parseInt(picked.value, 10) : 0;
      var ok = sel === q.answer;
      var pts = q.points || 2;
      if (ok) { correct += 1; score += pts; }
      var t = perType[q.type] || (perType[q.type] = { n: 0, ok: 0 });
      t.n += 1; if (ok) t.ok += 1;
      rows.push({ n: q.n, type: q.type, sel: sel, ans: q.answer, ok: ok, pts: pts });

      var fs = document.getElementById('q' + q.n);
      var labels = fs.querySelectorAll('.opts label');
      Array.prototype.forEach.call(labels, function (lab, i) {
        lab.querySelector('input').disabled = true;
        if (i + 1 === q.answer) lab.classList.add('is-correct');
        if (!ok && i + 1 === sel) lab.classList.add('is-wrong');
      });
      var v = el('p', 'verdict ' + (ok ? 'ok' : 'bad'),
        (ok ? '정답' : (sel ? '오답' : '미답')) + ' · 정답 ' + CIRCLED[q.answer - 1] + (sel && !ok ? ', 선택 ' + CIRCLED[sel - 1] : ''));
      fs.appendChild(v);
      fs.appendChild(el('p', 'expl', esc(q.explanation || '')));
      if (q.vocab && q.vocab.length) {
        var ul = el('ul', 'vocab');
        q.vocab.forEach(function (w) { ul.appendChild(el('li', null, esc(w))); });
        fs.appendChild(ul);
      }
    });

    var at = new Date();
    var atStr = at.getFullYear() + '-' + pad(at.getMonth() + 1) + '-' + pad(at.getDate()) + ' ' + pad(at.getHours()) + ':' + pad(at.getMinutes());
    var result = { date: SET.date, dday: day.dday, focus: day.focus, correct: correct, count: SET.questions.length,
      score: score, total: totalPoints, seconds: seconds, limit: limitSec, overtime: seconds > limitSec, perType: perType, rows: rows, at: atStr };
    var saved = saveResult(result);

    var typeLines = Object.keys(perType).map(function (k) { return TYPE_LABEL[k] + ' ' + perType[k].ok + '/' + perType[k].n; });
    var wrong = rows.filter(function (r) { return !r.ok; }).map(function (r) {
      return r.n + '번(' + TYPE_LABEL[r.type] + ', ' + (r.sel ? '선택 ' + CIRCLED[r.sel - 1] : '미답') + '/정답 ' + CIRCLED[r.ans - 1] + ')';
    });
    var summaryText = '[수능영어 연습 세트] ' + SET.date + ' (D-' + day.dday + ') ' + (day.focus || '') + '\n' +
      '정답 ' + correct + '/' + SET.questions.length + ' · 점수 ' + score + '/' + totalPoints + ' · 시간 ' + mmss(seconds) +
      ' (제한 ' + mmss(limitSec) + (seconds > limitSec ? ', 초과' : '') + ')\n' +
      '유형별: ' + typeLines.join(', ') + '\n' +
      '틀린 문항: ' + (wrong.length ? wrong.join(', ') : '없음') + '\n' +
      '틀린 이유(직접 적기): ';

    resultBox.innerHTML = '<h2>결과</h2>' +
      '<p class="big">' + correct + ' / ' + SET.questions.length + ' 정답 · ' + score + ' / ' + totalPoints + '점</p>' +
      '<p>소요 시간 ' + mmss(seconds) + (seconds > limitSec ? ' <span class="verdict bad" style="display:inline">(제한 시간 초과)</span>' : '') +
      (saved ? '' : ' · <span class="muted small">이 브라우저에는 기록이 저장되지 않았습니다.</span>') + '</p>' +
      '<table><thead><tr><th>유형</th><th class="num">정답/문항</th></tr></thead><tbody>' +
      Object.keys(perType).map(function (k) { return '<tr><td>' + esc(TYPE_LABEL[k]) + '</td><td class="num">' + perType[k].ok + '/' + perType[k].n + '</td></tr>'; }).join('') +
      '</tbody></table>' +
      '<p><strong>다음 단계.</strong> 아래 요약을 복사해 Claude 대화창에 붙여 넣고, 틀린 문항마다 이유를 한 줄로 적어 주세요 (어휘 모름 / 흐름 놓침 / 시간 부족 / 선지 함정). 그 기록으로 다음 주 세트의 유형 비중을 조정합니다.</p>' +
      '<textarea readonly aria-label="결과 요약"></textarea>';
    var ta = resultBox.querySelector('textarea');
    ta.value = summaryText;
    var acts = el('div', 'actions');
    var copyBtn = el('button', 'btn btn-primary', '결과 복사');
    copyBtn.type = 'button';
    copyBtn.addEventListener('click', function () {
      var done = function () { copyBtn.textContent = '복사됨'; };
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(ta.value).then(done, function () { ta.select(); });
      } else { ta.select(); try { document.execCommand('copy'); done(); } catch (e) { /* 선택만 */ } }
    });
    acts.appendChild(copyBtn);
    var topBtn = el('a', 'btn', '해설 보기 (1번으로)');
    topBtn.href = '#q' + SET.questions[0].n;
    acts.appendChild(topBtn);
    if (nextDay) { var nb = el('a', 'btn', '다음 날 세트 →'); nb.href = nextDay.date + '.html'; acts.appendChild(nb); }
    resultBox.appendChild(acts);

    resultBox.hidden = false; bottom.hidden = true; submitBtn.hidden = true;
    resultBox.scrollIntoView({ behavior: 'smooth', block: 'start' });
  }
  submitBtn.addEventListener('click', submit);
  submitBtn2.addEventListener('click', submit);
})();
