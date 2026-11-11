#!/usr/bin/env node
// record.js — deterministic PoC video recorder for the hackbot harness.
//
// The poc-builder agent only supplies a storyboard (captions + browser steps); this code does the
// recording, so a model never drives an unbounded browser. Every browser request to a host outside
// the engagement scope is aborted in code (route interception), mirroring scope_guard.py.
//
// Usage: node record.js <job.json>      (playwright resolved via NODE_PATH, set by engine/video.py)
// job.json: { out, title, subtitle, scope:{in:[],out:[]}, proxy, cookies:[{name,value,url}],
//             hide_selectors:[], steps:[{caption, action, url|selector|text|key|ms, hold}],
//             run_output, poc_command, summary:[] }
// Actions: goto(url) · type(selector,text) · fill(selector,text) · click(selector) · press(key)
//          · waitFor(selector) · wait(ms) · scroll(y) · caption (caption only)
const { chromium } = require('playwright');
const fs = require('fs');
const path = require('path');

const job = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const esc = (s) => String(s ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');

function hostMatch(pat, host) {
  pat = pat.toLowerCase(); host = host.toLowerCase();
  if (pat.startsWith('*.')) return host === pat.slice(2) || host.endsWith(pat.slice(1));
  return host === pat;
}
function inScope(urlStr) {
  let u; try { u = new URL(urlStr); } catch { return false; }
  if (['data:', 'about:', 'blob:'].includes(u.protocol)) return true;
  const h = u.hostname.replace(/^\[|\]$/g, '');
  if ((job.scope.out || []).some((p) => hostMatch(p, h))) return false;
  return (job.scope.in || []).some((p) => hostMatch(p, h));
}

const OVERLAY_JS = (hide) => `
document.addEventListener('DOMContentLoaded', () => {
  const st = document.createElement('style');
  st.textContent = ${JSON.stringify((hide || []).join(',') + (hide && hide.length ? '{display:none !important}' : ''))};
  document.head.appendChild(st);
});
window.__cap = (step, text) => {
  let el = document.getElementById('__poc_cap');
  if (!el) {
    el = document.createElement('div'); el.id = '__poc_cap';
    el.style.cssText = 'position:fixed;left:0;right:0;bottom:0;z-index:2147483647;background:rgba(10,10,10,.88);' +
      'color:#fff;font:600 20px/1.35 -apple-system,Segoe UI,Helvetica,Arial,sans-serif;padding:14px 22px;' +
      'border-top:3px solid #e5484d;pointer-events:none';
    document.documentElement.appendChild(el);
  }
  el.innerHTML = '<span style="color:#e5484d;margin-right:10px">' + step + '</span>' + text;
};`;

const CARD = (inner) => `<html><body style="margin:0;background:#111;color:#eee;font-family:-apple-system,Helvetica,Arial,sans-serif;
  display:flex;align-items:center;justify-content:center;height:100vh"><div style="max-width:1080px;padding:40px">${inner}</div></body></html>`;

(async () => {
  const launch = {};
  if (job.proxy) launch.proxy = { server: job.proxy };
  const browser = await chromium.launch(launch);
  const ctx = await browser.newContext({ viewport: { width: 1280, height: 720 }, ignoreHTTPSErrors: true,
    recordVideo: { dir: path.dirname(job.out), size: { width: 1280, height: 720 } } });
  const blocked = new Set();
  await ctx.route('**/*', (route) => {
    const u = route.request().url();
    const m = route.request().method();
    if (!job.intrusive && ['PUT', 'PATCH', 'DELETE'].includes(m)) { blocked.add(`${m} (read-only run)`); return route.abort(); }
    if (inScope(u)) return route.continue();
    try { blocked.add(new URL(u).hostname); } catch {}
    return route.abort();
  });
  await ctx.addInitScript(OVERLAY_JS(job.hide_selectors));
  if (job.cookies && job.cookies.length) await ctx.addCookies(job.cookies);
  const page = await ctx.newPage();
  const result = { out: job.out, steps: [], blocked_hosts: [] };

  // 1. title card
  await page.setContent(CARD(`<div style="color:#e5484d;font-weight:700;letter-spacing:.08em">PROOF OF CONCEPT</div>
    <h1 style="font-size:38px;margin:14px 0">${esc(job.title)}</h1><p style="font-size:21px;color:#bbb">${esc(job.subtitle)}</p>`));
  await sleep(4000);

  // 2. storyboard (browser part)
  let n = 0;
  for (const s of job.steps || []) {
    n += 1;
    const rec = { n, action: s.action, ok: true };
    try {
      if (s.action === 'goto') {
        if (!inScope(s.url)) throw new Error(`out-of-scope url ${s.url}`);
        await page.goto(s.url, { waitUntil: 'domcontentloaded', timeout: 20000 });
        await sleep(800);
      }
      if (s.caption) { await page.evaluate(([a, b]) => window.__cap && window.__cap(a, b), [s.label || String(n), s.caption]); await sleep(1500); }
      switch (s.action) {
        case 'type': await page.type(s.selector, s.text ?? '', { delay: 65, timeout: 10000 }); break;
        case 'fill': await page.fill(s.selector, s.text ?? '', { timeout: 10000 }); break;
        case 'click': await page.click(s.selector, { timeout: 10000 }); break;
        case 'press': await page.keyboard.press(s.key || 'Enter'); break;
        case 'waitFor': await page.waitForSelector(s.selector, { timeout: 10000 }); break;
        case 'wait': await sleep(Math.min(s.ms || 1000, 10000)); break;
        case 'scroll': await page.mouse.wheel(0, s.y || 400); break;
        default: break;   // goto (done above) / caption
      }
      await sleep(Math.min(s.hold ?? 1500, 8000));
    } catch (e) {
      rec.ok = false; rec.error = String(e.message || e).split('\n')[0];
    }
    result.steps.push(rec);
  }

  // 3. automated PoC replay (always — makes API-only bugs filmable too)
  if (job.run_output) {
    const lines = job.run_output.split('\n').slice(0, 40).map(esc);
    await page.setContent(CARD(`<div style="color:#e5484d;font-weight:700;letter-spacing:.08em">AUTOMATED POC · RETEST</div>
      <p style="font-size:18px;color:#bbb;margin:8px 0 14px"><code>${esc(job.poc_command || 'python3 poc.py')}</code></p>
      <pre id="t" style="background:#000;color:#7ee787;font-size:13px;line-height:1.35;padding:18px;border-radius:6px;
        min-height:420px;max-height:540px;overflow:hidden;white-space:pre-wrap"></pre>
      <script>const L=${JSON.stringify(lines)};let i=0;const t=document.getElementById('t');
        const iv=setInterval(()=>{if(i>=L.length){clearInterval(iv);return}t.innerHTML+=L[i++]+'\\n'},350);</script>`));
    await sleep(lines.length * 350 + 3500);
  }

  // 4. summary card
  if (job.summary && job.summary.length) {
    await page.setContent(CARD(`<h1 style="font-size:34px">Summary</h1><ul style="font-size:21px;line-height:1.7;color:#ddd">
      ${job.summary.map((b) => `<li>${esc(b)}</li>`).join('')}</ul>`));
    await sleep(4500);
  }

  const vpath = await page.video().path();
  await ctx.close(); await browser.close();
  fs.renameSync(vpath, job.out);
  result.blocked_hosts = [...blocked].sort();
  console.log(JSON.stringify(result));
})().catch((e) => { console.error(String(e.stack || e)); process.exit(1); });
