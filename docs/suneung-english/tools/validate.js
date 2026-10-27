#!/usr/bin/env node
/* 세트 파일 검증기.
   사용: node tools/validate.js sets/2026-10-28.js [sets/...]   또는   node tools/validate.js --all
   assets/SET-FORMAT.md 의 규칙을 기계적으로 확인한다. 통과하면 파일마다 "OK"를 출력하고 종료 코드 0. */
'use strict';
const fs = require('fs');
const path = require('path');

const ROOT = path.resolve(__dirname, '..');
const CIRCLED = ['①', '②', '③', '④', '⑤'];
const ORDER_OPTIONS = ['(A) - (C) - (B)', '(B) - (A) - (C)', '(B) - (C) - (A)', '(C) - (A) - (B)', '(C) - (B) - (A)'];
const MARK_TYPES = { grammar: 1, vocab: 1, irrelevant: 1 };
const KO_TYPES = { purpose: 1, mood: 0, claim: 1, gist: 1, match: 1, notice: 1 };

function loadGlobal(file, name) {
  const code = fs.readFileSync(file, 'utf8');
  const win = {};
  new Function('window', code)(win);
  if (!win[name]) throw new Error(file + ': window.' + name + ' 이(가) 정의되지 않음');
  return win[name];
}

const PLAN = loadGlobal(path.join(ROOT, 'assets', 'plan.js'), 'CSAT_PLAN');

function stripTokens(s) {
  return String(s || '')
    .replace(/\[BLANK\]/g, ' ')
    .replace(/\[[1-5]\]\{([^}]*)\}/g, '$1')
    .replace(/\[P[1-5]\]/g, ' ')
    .replace(/\[U\]\{([^}]*)\}/g, '$1');
}
function words(s) { return stripTokens(s).split(/\s+/).filter(Boolean).length; }
function count(s, re) { return (String(s || '').match(re) || []).length; }
function hasHangul(s) { return /[가-힣]/.test(s || ''); }

function validate(file) {
  const errs = [];
  const warn = [];
  const E = (m) => errs.push(m);
  const W = (m) => warn.push(m);
  let set;
  try { set = loadGlobal(file, 'CSAT_SET'); } catch (e) { return { errs: [e.message], warn }; }

  const base = path.basename(file, '.js');
  if (set.date !== base) E(`date "${set.date}" 가 파일 이름 ${base} 과 다름`);
  const day = PLAN.days.find((d) => d.date === set.date);
  if (!day) { E(`plan.js에 ${set.date} 가 없음`); return { errs, warn }; }
  if (set.dday !== day.dday) E(`dday ${set.dday} ≠ plan ${day.dday}`);
  if (set.week !== day.week) E(`week ${set.week} ≠ plan ${day.week}`);
  if (set.focus !== day.focus) E(`focus "${set.focus}" ≠ plan "${day.focus}"`);
  if (day.timeLimitMin && set.timeLimitMin !== day.timeLimitMin) E(`timeLimitMin 은 ${day.timeLimitMin} 이어야 함`);
  if (!set.intro || !hasHangul(set.intro)) E('intro 가 비었거나 한국어가 아님');
  if (!set.tip || !hasHangul(set.tip)) E('tip 이 비었거나 한국어가 아님');

  const qs = Array.isArray(set.questions) ? set.questions : [];
  if (qs.length !== day.types.length) E(`문항 수 ${qs.length} ≠ plan ${day.types.length}`);
  const typeSeq = qs.map((q) => q.type);
  if (typeSeq.join(',') !== day.types.join(',')) E(`유형 순서 불일치\n    세트: ${typeSeq.join(',')}\n    plan: ${day.types.join(',')}`);

  let three = 0;
  const answers = [];
  qs.forEach((q, i) => {
    const tag = `Q${q.n}(${q.type})`;
    if (q.n !== i + 1) E(`${tag}: n 은 ${i + 1} 이어야 함`);
    if (![2, 3].includes(q.points)) E(`${tag}: points 는 2 또는 3`);
    if (q.points === 3) three += 1;
    if (!Array.isArray(q.options) || q.options.length !== 5 || q.options.some((o) => typeof o !== 'string' || !o.trim())) E(`${tag}: options 는 비어 있지 않은 문자열 5개`);
    if (!Number.isInteger(q.answer) || q.answer < 1 || q.answer > 5) E(`${tag}: answer 는 1~5`);
    answers.push(q.answer);
    if (!q.explanation || !hasHangul(q.explanation) || q.explanation.length < 40) E(`${tag}: explanation 은 한국어 40자 이상`);
    if (!Array.isArray(q.vocab) || q.vocab.length < 3 || q.vocab.length > 8) E(`${tag}: vocab 은 3~8개`);
    else q.vocab.forEach((v) => { if (!/ — /.test(v)) W(`${tag}: vocab "${v}" 는 "단어 — 뜻" 형식 권장`); });

    const p = q.passage || '';
    const opts = q.options || [];
    const isCircled = opts.join('') === CIRCLED.join('');

    if (q.type === 'order') {
      if (p) W(`${tag}: order 는 passage 대신 given/parts 를 씀`);
      if (!q.given) E(`${tag}: given(주어진 글) 필요`);
      if (!q.parts || !['A', 'B', 'C'].every((k) => q.parts[k])) E(`${tag}: parts.A/B/C 필요`);
      if (opts.join('|') !== ORDER_OPTIONS.join('|')) E(`${tag}: options 는 고정된 순서 선택지 5개`);
      const total = words(q.given) + ['A', 'B', 'C'].reduce((a, k) => a + words(q.parts && q.parts[k]), 0);
      if (total < 120 || total > 190) E(`${tag}: given+parts 단어 수 ${total} (120~190)`);
    } else {
      const wc = words(p);
      const lo = q.type === 'notice' ? 60 : 100, hi = q.type === 'notice' ? 140 : 170;
      if (wc < lo || wc > hi) E(`${tag}: 지문 단어 수 ${wc} (${lo}~${hi})`);
    }
    if (/<[a-z/]/i.test(p + (q.given || ''))) E(`${tag}: 지문에 HTML 태그 사용 금지`);

    if (q.type === 'blank') {
      if (count(p, /\[BLANK\]/g) !== 1) E(`${tag}: [BLANK] 정확히 1개`);
      if (isCircled) E(`${tag}: 빈칸 선택지는 영어 구/절`);
    } else if (count(p, /\[BLANK\]/g)) E(`${tag}: [BLANK] 는 blank 유형에만`);

    if (MARK_TYPES[q.type]) {
      for (let k = 1; k <= 5; k++) if (count(p, new RegExp(`\\[${k}\\]\\{`, 'g')) !== 1) E(`${tag}: [${k}]{...} 정확히 1개`);
      if (!isCircled) E(`${tag}: options 는 ["①","②","③","④","⑤"]`);
    } else if (/\[[1-5]\]\{/.test(p)) E(`${tag}: [n]{...} 는 grammar/vocab/irrelevant 에만`);

    if (q.type === 'insert') {
      if (!q.given) E(`${tag}: given(주어진 문장) 필요`);
      for (let k = 1; k <= 5; k++) if (count(p, new RegExp(`\\[P${k}\\]`, 'g')) !== 1) E(`${tag}: [P${k}] 정확히 1개`);
      if (!isCircled) E(`${tag}: options 는 ["①","②","③","④","⑤"]`);
    } else if (/\[P[1-5]\]/.test(p)) E(`${tag}: [Pn] 는 insert 에만`);

    if (q.type === 'implied') {
      if (count(p, /\[U\]\{/g) !== 1) E(`${tag}: [U]{...} 정확히 1개`);
    } else if (/\[U\]\{/.test(p)) E(`${tag}: [U]{...} 는 implied 에만`);

    if (q.type === 'summary') {
      if (!q.given || !/\(A\)/.test(q.given) || !/\(B\)/.test(q.given)) E(`${tag}: given 요약문에 (A), (B) 필요`);
      if (!opts.every((o) => / …… /.test(o))) E(`${tag}: 선택지는 "word …… word" 형식`);
    }
    if ((q.type === 'match' || q.type === 'notice') && !q.stem) E(`${tag}: stem(발문)에 이름/행사명을 넣어야 함`);
    if (q.type === 'mood' && !opts.every((o) => /→/.test(o))) E(`${tag}: 심경 선택지는 "A → B" 형식`);
    if (KO_TYPES[q.type] === 1 && !opts.every(hasHangul)) E(`${tag}: 선택지는 한국어`);
    if (['topic', 'title', 'implied', 'blank'].includes(q.type) && opts.some(hasHangul)) E(`${tag}: 선택지는 영어`);

    if (!isCircled && q.type !== 'order') {
      const lens = opts.map((o) => o.length);
      const ratio = Math.max(...lens) / Math.max(1, Math.min(...lens));
      if (ratio > 1.8) W(`${tag}: 선택지 길이 편차 큼 (최장/최단 = ${ratio.toFixed(2)})`);
    }
    const stray = (p + (q.given || '')).match(/\[(?!BLANK\]|[1-5]\]\{|P[1-5]\]|U\]\{)[^\]]*\]/g);
    if (stray) E(`${tag}: 알 수 없는 토큰 ${stray.join(' ')}`);
  });

  if (three !== day.three) E(`3점 문항 수 ${three} ≠ plan ${day.three}`);
  const dist = [1, 2, 3, 4, 5].map((a) => answers.filter((x) => x === a).length);
  const maxAllowed = qs.length >= 9 ? 3 : 2;
  if (qs.length >= 9 && dist.some((c) => c === 0)) E(`정답 번호 분포 ${dist.join('/')}: ①~⑤ 모두 최소 1번`);
  if (dist.some((c) => c > maxAllowed)) E(`정답 번호 분포 ${dist.join('/')}: 한 번호가 ${maxAllowed}번 초과`);

  return { errs, warn, set };
}

const args = process.argv.slice(2);
let files = args.filter((a) => !a.startsWith('--'));
if (args.includes('--all') || !files.length) {
  const dir = path.join(ROOT, 'sets');
  files = fs.existsSync(dir) ? fs.readdirSync(dir).filter((f) => /^\d{4}-\d{2}-\d{2}\.js$/.test(f)).sort().map((f) => path.join(dir, f)) : [];
}

let failed = 0;
const fingerprints = new Map();
for (const f of files) {
  const { errs, warn, set } = validate(path.resolve(f));
  const name = path.relative(ROOT, path.resolve(f));
  if (set) {
    for (const q of set.questions || []) {
      const key = stripTokens(q.passage || (q.given || '') + (q.parts ? q.parts.A : '')).toLowerCase().replace(/[^a-z ]/g, '').slice(0, 80);
      if (key.length > 40) {
        if (fingerprints.has(key)) errs.push(`Q${q.n}: 지문이 ${fingerprints.get(key)} 와 중복`);
        else fingerprints.set(key, `${name} Q${q.n}`);
      }
    }
  }
  if (errs.length) { failed += 1; console.log(`FAIL ${name}`); errs.forEach((e) => console.log('  - ' + e)); }
  else console.log(`OK   ${name}`);
  warn.forEach((w) => console.log('  ~ ' + w));
}
if (!files.length) { console.log('검사할 세트 파일이 없습니다.'); }
process.exit(failed ? 1 : 0);
