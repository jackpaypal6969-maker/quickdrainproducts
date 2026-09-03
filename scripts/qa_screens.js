// Full-page 390px screenshots that scroll through the page first, so
// IntersectionObserver reveals have fired and nothing is captured mid-fade.
// Usage: node scripts/qa_screens.js <base_url> <out_dir>
const path = require('path');
const { chromium } = require(path.join(__dirname, '..', 'node_modules', 'playwright'));
const base = process.argv[2] || 'http://127.0.0.1:8006';
const out = process.argv[3] || path.join(__dirname, '..', 'docs', 'qa');
const PAGES = [
  ['home', '/'],
  ['product', '/products/drain-shot'],
  ['cart-empty', '/cart'],
  ['checkout-pending', '/checkout/success?session_id=demo'],
  ['contact', '/contact'],
  ['account-login', '/account/login'],
];
(async () => {
  const browser = await chromium.launch({ executablePath: process.env.MOBILE_QA_CHROMIUM || '/opt/pw-browsers/chromium', args: ['--no-sandbox'] });
  const ctx = await browser.newContext({ viewport: { width: 390, height: 844 }, deviceScaleFactor: 2, isMobile: true, hasTouch: true });
  const page = await ctx.newPage();
  const errors = [];
  page.on('pageerror', (e) => errors.push(e.message));
  for (const [name, url] of PAGES) {
    await page.goto(base + url, { waitUntil: 'networkidle' });
    await page.evaluate(async () => { const h = document.body.scrollHeight; for (let y = 0; y < h; y += 400) { window.scrollTo({ top: y, behavior: 'instant' }); await new Promise((r) => setTimeout(r, 50)); } window.scrollTo({ top: 0, behavior: 'instant' }); });
    await page.waitForTimeout(400);
    const width = await page.evaluate(() => document.documentElement.scrollWidth);
    await page.screenshot({ path: path.join(out, `${name}-390.png`), fullPage: true });
    console.log(`${name}: ${url} scrollWidth=${width}${width > 390 ? ' HORIZONTAL OVERFLOW' : ''}`);
  }
  // cart with a subscription line, then the drawer
  await page.goto(base + '/products/drain-shot', { waitUntil: 'networkidle' });
  const sub = await page.$('input[name=delivery][value=sub]');
  if (sub) { await sub.click(); await page.click('input[name=variant_id][value="2"]'); }
  await page.click('[data-add-button-main]');
  await page.waitForTimeout(900);
  await page.screenshot({ path: path.join(out, 'cart-drawer-390.png') });
  await page.goto(base + '/cart', { waitUntil: 'networkidle' });
  await page.screenshot({ path: path.join(out, 'cart-with-subscription-390.png'), fullPage: true });
  console.log('cart-with-subscription captured; js errors:', errors.length ? errors : 'none');
  await browser.close();
})().catch((e) => { console.error(e); process.exit(1); });
