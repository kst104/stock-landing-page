#!/usr/bin/env node
/* 독립 검증자의 답안(JSON)을 세트의 정답 키와 대조한다.
   사용: node tools/compare.js <답안 폴더>
   답안 파일: <폴더>/YYYY-MM-DD.json = { "date": "...", "answers": [ { "n": 1, "answer": 3, "note": "..." }, ... ] } */
'use strict';
const fs = require('fs');
const path = require('path');
const ROOT = path.resolve(__dirname, '..');
const dir = process.argv[2];
if (!dir) { console.error('답안 폴더를 지정하세요.'); process.exit(2); }
let mismatches = 0, total = 0, missing = 0;
const files = fs.readdirSync(dir).filter((f) => /^\d{4}-\d{2}-\d{2}\.json$/.test(f)).sort();
for (const f of files) {
  const ans = JSON.parse(fs.readFileSync(path.join(dir, f), 'utf8'));
  const setFile = path.join(ROOT, 'sets', f.replace(/\.json$/, '.js'));
  if (!fs.existsSync(setFile)) { console.log(`?? ${f}: 세트 파일 없음`); continue; }
  const win = {};
  new Function('window', fs.readFileSync(setFile, 'utf8'))(win);
  const key = win.CSAT_SET.questions;
  const lines = [];
  for (const q of key) {
    total += 1;
    const a = (ans.answers || []).find((x) => x.n === q.n);
    if (!a) { missing += 1; lines.push(`  Q${q.n}(${q.type}): 검증자 답 없음`); continue; }
    if (a.answer !== q.answer) {
      mismatches += 1;
      lines.push(`  Q${q.n}(${q.type}): 키 ${q.answer} ≠ 검증자 ${a.answer}${a.note ? ' — ' + a.note : ''}`);
    } else if (a.flag) {
      lines.push(`  Q${q.n}(${q.type}): 일치하지만 검증자 표시 — ${a.flag}`);
    }
  }
  console.log((lines.length ? 'DIFF ' : 'OK   ') + f.replace(/\.json$/, ''));
  lines.forEach((l) => console.log(l));
}
console.log(`\n문항 ${total}개 중 불일치 ${mismatches}, 답 없음 ${missing}`);
process.exit(mismatches || missing ? 1 : 0);
