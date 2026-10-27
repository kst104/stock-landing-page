#!/usr/bin/env node
/* 정답을 뺀 '블라인드' 복사본을 만든다. 독립 검증자가 정답을 모른 채 풀어 보게 하기 위한 것.
   사용: node tools/make-blind.js <출력 폴더> [sets/2026-10-28.js ...]   (파일을 안 주면 sets/ 전체) */
'use strict';
const fs = require('fs');
const path = require('path');
const ROOT = path.resolve(__dirname, '..');
const [outDir, ...given] = process.argv.slice(2);
if (!outDir) { console.error('출력 폴더를 지정하세요.'); process.exit(2); }
fs.mkdirSync(outDir, { recursive: true });
let files = given;
if (!files.length) {
  const dir = path.join(ROOT, 'sets');
  files = fs.readdirSync(dir).filter((f) => /^\d{4}-\d{2}-\d{2}\.js$/.test(f)).sort().map((f) => path.join(dir, f));
}
for (const f of files) {
  const win = {};
  new Function('window', fs.readFileSync(f, 'utf8'))(win);
  const s = win.CSAT_SET;
  const blind = {
    date: s.date,
    questions: s.questions.map((q) => ({
      n: q.n, type: q.type, stem: q.stem || null, passage: q.passage || null, given: q.given || null,
      parts: q.parts || null, options: q.options
    }))
  };
  const out = path.join(outDir, s.date + '.json');
  fs.writeFileSync(out, JSON.stringify(blind, null, 2));
  console.log(out);
}
