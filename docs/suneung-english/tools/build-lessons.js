#!/usr/bin/env node
/* assets/plan.js 의 날짜마다 lessons/YYYY-MM-DD.html 을 만든다. 이미 있으면 덮어쓴다 (얇은 페이지라 손으로 고칠 일이 없다). */
'use strict';
const fs = require('fs');
const path = require('path');
const ROOT = path.resolve(__dirname, '..');
const win = {};
new Function('window', fs.readFileSync(path.join(ROOT, 'assets', 'plan.js'), 'utf8'))(win);
const plan = win.CSAT_PLAN;
const dir = path.join(ROOT, 'lessons');
fs.mkdirSync(dir, { recursive: true });

for (const d of plan.days) {
  const [y, m, dd] = d.date.split('-');
  const html = `<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>D-${d.dday} 수능영어 세트 (${Number(m)}/${Number(dd)})</title>
<link rel="stylesheet" href="../assets/style.css">
</head>
<body>
<main id="app"><noscript><p>이 페이지는 JavaScript가 필요합니다.</p></noscript></main>
<script src="../assets/plan.js"></script>
<script src="../sets/${d.date}.js"></script>
<script src="../assets/set-runner.js"></script>
</body>
</html>
`;
  fs.writeFileSync(path.join(dir, d.date + '.html'), html);
}
console.log(`lessons/ 에 ${plan.days.length}개 페이지 생성`);
