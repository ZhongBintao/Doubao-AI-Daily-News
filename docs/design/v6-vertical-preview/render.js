// Render every frame of the v6 vertical preview to PNG (1080x1920) and report overflow/fit results.
// Usage: node docs/design/v6-vertical-preview/render.js <output-dir>
const path = require('path');
const { chromium } = require('playwright');
(async () => {
  const outdir = path.resolve(process.argv[2] || '.');
  const file = path.join(__dirname, 'template.html');
  const browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width: 1080, height: 1920 }, deviceScaleFactor: 1 });
  await page.goto('file://' + file, { waitUntil: 'networkidle' });
  await page.waitForFunction(() => document.body.dataset.ready === '1');
  const frames = await page.locator('section.frame').all();
  const kinds = ['hook', 'story', 'flash'];
  for (let i = 0; i < frames.length; i++) {
    await frames[i].screenshot({ path: path.join(outdir, `s${Math.floor(i / 3) + 1}-${kinds[i % 3]}.png`) });
  }
  const overflow = await page.evaluate(() => [...document.querySelectorAll('.fit, .card p, .keyfact p, .flash-card p')]
    .filter((e) => e.scrollWidth > e.clientWidth + 1).map((e) => e.textContent.slice(0, 20)));
  const fit = await page.evaluate(() => window.__fitLog);
  console.log(JSON.stringify({ frames: frames.length, overflow, fit }));
  await browser.close();
})();
