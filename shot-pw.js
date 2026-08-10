const { chromium } = require('playwright');
(async () => {
  const browser = await chromium.launch({
    headless: true,
    executablePath: 'C:/Users/Sai Prabhat/AppData/Local/ms-playwright/chromium-1234/chrome-win64/chrome.exe',
    args: ['--no-sandbox', '--enable-unsafe-swiftshader', '--disable-gpu']
  });
  const page = await browser.newPage({ viewport: { width: 1600, height: 1000 } });
  const errs = [];
  page.on('pageerror', e => errs.push('PAGEERROR: ' + e.message.slice(0, 150)));
  await page.goto('http://127.0.0.1:5188/', { waitUntil: 'load', timeout: 60000 });
  try {
    await page.waitForSelector('h1', { timeout: 30000 });
    const h1 = await page.locator('h1').first().innerText();
    console.log('H1:', h1);
  } catch (e) { console.log('NO-H1:', e.message.slice(0, 120)); }
  const info = await page.evaluate(() => {
    const h1 = document.querySelector('h1');
    return {
      bodyBg: getComputedStyle(document.body).backgroundColor,
      h1Color: h1 ? getComputedStyle(h1).color : 'none',
      h1Opacity: h1 ? getComputedStyle(h1).opacity : 'none',
      canvases: document.querySelectorAll('canvas').length,
      bodyText: document.body.innerText.slice(0, 120)
    };
  });
  console.log('STYLE:', JSON.stringify(info));
  await page.waitForTimeout(4000);
  await page.screenshot({ path: 'C:/Users/Sai Prabhat/AppData/Local/Temp/barq-pw.png' });
  console.log('ERRS:', errs.length);
  errs.slice(0, 4).forEach(e => console.log(e));
  await browser.close();
  console.log('PW-DONE');
})().catch(e => { console.error('FATAL', (e.message || '').slice(0, 300)); process.exit(1); });
