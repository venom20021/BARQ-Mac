const { chromium } = require('playwright');
(async () => {
  const browser = await chromium.launch({
    headless: true,
    executablePath: 'C:/Users/Sai Prabhat/AppData/Local/ms-playwright/chromium-1234/chrome-win64/chrome.exe',
    args: ['--no-sandbox', '--enable-unsafe-swiftshader', '--disable-gpu']
  });
  const page = await browser.newPage({ viewport: { width: 1600, height: 1000 } });
  await page.goto('http://127.0.0.1:5188/', { waitUntil: 'load', timeout: 60000 });
  try {
    await page.waitForSelector('text=/SKIP/i', { timeout: 25000 });
    await page.click('text=/SKIP/i');
    console.log('CLICKED-SKIP');
  } catch (e) { console.log('NO-SKIP:', (e.message || '').slice(0, 100)); }
  await page.waitForTimeout(9000);
  const txt = await page.evaluate(() => document.body.innerText.replace(/\n+/g, ' | ').slice(0, 350));
  console.log('TEXT:', txt);
  await page.screenshot({ path: 'C:/Users/Sai Prabhat/AppData/Local/Temp/barq-final.png' });
  await browser.close();
  console.log('DONE');
})().catch(e => { console.error('FATAL', (e.message || '').slice(0, 200)); process.exit(1); });
