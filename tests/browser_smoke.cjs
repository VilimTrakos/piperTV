// Optional developer check. See docs/implementation-plan.md for installation.
const playwright = process.env.PIPERTV_PLAYWRIGHT || 'playwright';
const { chromium } = require(playwright);
const { expect } = require(`${playwright}/test`);
const { spawn } = require('node:child_process');
const { resolve } = require('node:path');
const root = resolve(__dirname, '..');
const server = spawn(process.env.PIPERTV_PYTHON || resolve(root, '.venv/bin/python'), [resolve(__dirname, 'browser_server.py')], {
  cwd:root, env:{...process.env, PYTHONPATH:root}, stdio:['ignore','pipe','pipe']
});
let log=''; server.stdout.on('data',b=>log+=b); server.stderr.on('data',b=>log+=b);
server.on('error', error => { log += error.message; });
let browser;
(async()=>{
  let port;
  for (let n=0;n<100;n++) {
    port = log.match(/PIPERTV_TEST_PORT=(\d+)/)?.[1];
    if (port) break;
    if (server.exitCode !== null) throw Error(log);
    await new Promise(r=>setTimeout(r,50));
  }
  if (!port) throw Error(`Test server did not start: ${log}`);
  const base=`http://127.0.0.1:${port}`;
  browser=await chromium.launch({headless:true,
    ...(process.env.PIPERTV_CHROMIUM ? {executablePath:process.env.PIPERTV_CHROMIUM} : {})});
  const page=await browser.newPage({viewport:{width:1440,height:1100}});
  const errors=[]; page.on('pageerror', e=>errors.push(e.message));
  await page.goto(base);
  await expect(page.locator('.remote-key')).toHaveCount(49);
  await expect(page.locator('#control-confirm')).toBeVisible();
  await expect(page.locator('#control-modes')).toBeHidden();
  await page.locator('#record-button').click();
  await expect(page.locator('#sample-count')).toHaveText('1');
  await expect(page.locator('#record-button')).toBeEnabled();
  const source=async(state)=>page.request.post(base+'/test/source',{data:{state}});
  await source('active');
  await expect(page.locator('#mode-pointer')).toBeVisible({timeout:6000});
  await page.locator('#mode-pointer').click();
  await expect(page.locator('#control-badge')).toContainText('On · pointer');
  await page.locator('#control-stop').click();
  await expect(page.locator('#control-confirm')).toBeVisible();
  await page.waitForTimeout(3000);
  await expect(page.locator('#control-modes')).toBeHidden();
  await source('inactive'); await source('active');
  await expect(page.locator('#mode-snapping')).toBeVisible({timeout:6000});
  await page.locator('#mode-snapping').click();
  await expect(page.locator('#control-badge')).toContainText('On · snapping');
  await source('unknown');
  await expect(page.locator('#control-confirm')).toBeVisible({timeout:6000});
  await page.locator('#control-confirm').click();
  await expect(page.locator('#control-detail')).toContainText('Manual confirmation');
  await page.locator('#mode-pointer').click();
  await expect(page.locator('#control-badge')).toContainText('Manual · pointer');
  await page.route('**/api/control/mode', route=>route.fulfill({status:409, contentType:'application/json',body:JSON.stringify({error:'Choose a mode again for the current visit.'})}));
  await page.locator('#control-stop').click();
  await page.locator('#control-confirm').click();
  await page.locator('#mode-pointer').click();
  await expect(page.locator('#control-error')).toContainText('Choose a mode again');
  await page.waitForTimeout(2800);
  await expect(page.locator('#control-error')).toBeVisible();
  await page.unroute('**/api/control/mode');
  await page.locator('#rename-button').click();
  await page.locator('#button-label').fill('Standby');
  await page.locator('#rename-form button[type=submit]').click();
  await expect(page.locator('#selected-label')).toHaveText('Standby');
  const exported=await (await page.request.get(base+'/api/export')).json();
  if (exported.recordings.power.label !== 'Standby') throw Error('Export did not preserve renamed recording');
  await page.reload();
  await expect(page.locator('#selected-label')).toHaveText('Standby');
  await expect(page.locator('#sample-count')).toHaveText('1');
  await page.setViewportSize({width:390,height:844});
  if (!(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth))) throw Error('Mobile horizontal overflow');

  // The TV page must not replay old presses or navigate in desktop mode.
  await page.setViewportSize({width:1920,height:1080});
  await page.locator('#mode-piper').click();
  await expect(page.locator('#control-badge')).toContainText('Manual · piper');
  const tv = await browser.newPage({viewport:{width:1920,height:1080}});
  tv.on('pageerror',e=>errors.push(e.message));
  const send = button=>page.request.post(base+'/test/press', {data:{button}});
  await send('right'); // This press predates the page and must be discarded.
  await tv.goto(base+'/tv?boot=0');
  await expect(tv.locator('#home-source')).toHaveText('remote connected');
  await expect(tv.locator('#focus-name')).toHaveText('Netflix');
  await send('right');
  await expect(tv.locator('#focus-name')).toHaveText('YouTube');
  await tv.reload();
  await expect(tv.locator('#home-source')).toHaveText('remote connected');
  await expect(tv.locator('#focus-name')).toHaveText('Netflix');
  let state=await (await page.request.get(base+'/api/control')).json();
  await page.request.post(base+'/api/control/mode',{data:{mode:'pointer',session_id:state.session.id}});
  await send('right');
  await expect(tv.locator('#home-source')).toHaveText('desktop control selected');
  await expect(tv.locator('#focus-name')).toHaveText('Netflix');
  await source('inactive');
  await send('right');
  await expect(tv.locator('#home-source')).toHaveText('remote control off');
  await expect(tv.locator('#focus-name')).toHaveText('Netflix');
  await tv.keyboard.press('ArrowRight');
  await expect(tv.locator('#focus-name')).toHaveText('YouTube');
  await tv.keyboard.press('Enter');
  await expect(tv.locator('#tv-notice')).toContainText('not implemented yet');
  if (errors.length) throw Error(errors.join('\n'));
  console.log('Browser checks passed: recording/exports, per-visit modes, manual/stop, visible errors, mobile layout, and TV event gating/reload/keyboard navigation.');
})().catch(e=>{console.error(e); console.error(log); process.exitCode=1;}).finally(async()=>{
  if(browser) await browser.close();
  server.kill('SIGTERM');
  await new Promise(r=>server.exitCode !== null ? r() : server.once('exit',r));
});
