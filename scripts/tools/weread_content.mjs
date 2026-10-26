// Fetch HTML through the existing reader context. Python owns rate limiting.
const [, , reviewId] = process.argv;
const port = Number(process.env.WEREAD_CDP_PORT || 9333);
const timeout = setTimeout(() => { console.error('正文请求超时'); process.exit(2); }, 45000);
let ws;
try {
  if (!/^MP_WXS_\d+_/.test(reviewId || '')) throw Error('无效 reviewId');
  const tabs = await (await fetch(`http://127.0.0.1:${port}/json/list`, {signal: AbortSignal.timeout(5000)})).json();
  const reader = tabs.find(t => t.type === 'page' && t.url.includes('/web/mp/reader/'));
  if (!reader) throw Error('没有阅读器页，请运行 weread --probe');
  ws = new WebSocket(reader.webSocketDebuggerUrl);
  await new Promise((resolve, reject) => { ws.onopen = resolve; ws.onerror = reject; });
  const expression = `fetch('/web/mp/content?reviewId='+encodeURIComponent(${JSON.stringify(reviewId)}),{credentials:'include'}).then(r=>r.text())`;
  const result = await new Promise((resolve, reject) => {
    ws.onmessage = e => { const d = JSON.parse(e.data); if (d.id === 1) {
      const value = d.result?.result?.value;
      if (typeof value !== 'string') reject(Error('正文接口返回异常')); else resolve(value);
    }};
    ws.send(JSON.stringify({id:1,method:'Runtime.evaluate',params:{expression,awaitPromise:true,returnByValue:true}}));
  });
  console.log(JSON.stringify({ok:true,html:result}));
} catch (e) { console.log(JSON.stringify({ok:false,error:String(e)})); process.exitCode = 1; }
finally { clearTimeout(timeout); if(ws) ws.close(); }
