// 微信读书文章列表 —— 单页抓取原语
//
// 用法：node weread_page.mjs <bookId> <offset>
//
// 输出（stdout，恰好一行 JSON）：
//   {"ok":true,"offset":0,"reviews":19,"items":[{t,title,url,rid}],"oldest":...}
//   {"ok":false,"error":"...","hint":"..."}
//
// ## 为什么要单独做这个
//
// weread-mp-fetcher 只能「从 offset=0 开始翻 N 页」，没有「从 offset=M 继续」。
// 而全量抓取必须能**从中断处续跑**，否则每次都要从头再来 —— 既慢又费额度。
//
// 这里是纯粹的「取一页」原语：
//   · 不做循环、不做重试、不做限频 —— 那些交给 Python 侧（便于测试和审计）
//   · 不管配额 —— 配额由 Python 侧的账本统一管
//
// ⚠️ 必须在微信读书阅读器页的上下文里请求，否则返回 -2041（页面上下文校验）。
//    所以需要那个带调试端口的 Chrome 开着。

const CDP_PORT = Number(process.env.WEREAD_CDP_PORT || 9333);

const [, , bookId, offsetArg] = process.argv;
const offset = Number(offsetArg || 0);

if (!bookId) {
  console.log(JSON.stringify({ ok: false, error: '缺少 bookId' }));
  process.exit(2);
}

function fail(error, hint) {
  console.log(JSON.stringify({ ok: false, error, hint }));
  process.exit(0);   // 用 stdout 传业务失败，退出码保持 0，避免调用方把「业务失败」当「进程崩溃」
}

// ---------------------------------------------------------------- 找阅读器页

let tabs;
try {
  const res = await fetch(`http://127.0.0.1:${CDP_PORT}/json/list`, { signal: AbortSignal.timeout(5000) });
  tabs = await res.json();
} catch (e) {
  fail(
    `连不上专用 Chrome（端口 ${CDP_PORT}）`,
    '先启动：bash ~/.weread-mp-fetcher/start-chrome.sh'
  );
}

const pages = (tabs || []).filter((t) => t.type === 'page');
const reader = pages.find((p) => p.url.includes('/web/mp/reader/'));

if (!reader) {
  fail(
    '没有找到微信读书阅读器页',
    '请在专用 Chrome 里打开任意公众号的阅读页（或重跑 start-chrome.sh）'
  );
}

// ---------------------------------------------------------------- 取一页

const ws = new WebSocket(reader.webSocketDebuggerUrl);
let msgId = 0;
const send = (method, params) => new Promise((resolve) => {
  const myId = ++msgId;
  const handler = (ev) => {
    const d = JSON.parse(ev.data);
    if (d.id === myId) { ws.removeEventListener('message', handler); resolve(d); }
  };
  ws.addEventListener('message', handler);
  ws.send(JSON.stringify({ id: myId, method, params }));
});

const opened = await new Promise((resolve) => {
  const timer = setTimeout(() => resolve(false), 8000);
  ws.addEventListener('open', () => { clearTimeout(timer); resolve(true); });
  ws.addEventListener('error', () => { clearTimeout(timer); resolve(false); });
});
if (!opened) fail('WebSocket 连接失败', 'Chrome 可能已退出，重跑 start-chrome.sh');

// 页内脚本：调接口 → 展平 → 只回结构化数据
// ⚠️ 一次群发 = 一个 reviews 条目，里面的 subReviews 才是一篇篇文章。
//    只取 subReviews[0] 会丢掉同一次群发的其余文章。
const expr = `(function(){
  function flatten(o){
    var out = [];
    (o.reviews || []).forEach(function(grp){
      (grp.subReviews || []).forEach(function(s){
        var r = s.review || {}, mi = r.mpInfo || {};
        if (!mi.title) return;
        out.push({
          t: r.createTime || grp.createTime || 0,
          title: mi.title,
          // WR1 IDs are opaque reader IDs, not public WeChat short links.
          url: mi.originalId ? (mi.originalId.startsWith('WR1')
            ? ('weread:' + r.reviewId)
            : ('https://mp.weixin.qq.com/s/' + mi.originalId.replace(/~/g,'_'))) : '',
          rid: r.reviewId || ''
        });
      });
    });
    out.sort(function(a,b){ return b.t - a.t; });
    return out;
  }
  return fetch('/web/mp/articles?bookId=' + encodeURIComponent(${JSON.stringify(String(bookId))}) + '&offset=' + ${offset}, {credentials:'include'})
    .then(function(r){ return r.json(); })
    .then(function(o){
      if (o.errCode) return JSON.stringify({ok:false, errCode:o.errCode});
      var items = flatten(o);
      var ts = items.map(function(i){ return i.t; }).filter(Boolean);
      return JSON.stringify({
        ok: true,
        offset: ${offset},
        reviews: (o.reviews || []).length,
        items: items,
        oldest: ts.length ? Math.min.apply(null, ts) : 0
      });
    })
    .catch(function(e){ return JSON.stringify({ok:false, err:String(e).slice(0,120)}); });
})()`;

const r = await send('Runtime.evaluate', { expression: expr, awaitPromise: true, returnByValue: true });
ws.close();

let payload;
try {
  payload = JSON.parse(r.result.result.value);
} catch (e) {
  fail('页面返回了无法解析的结果', JSON.stringify(r).slice(0, 200));
}

if (!payload.ok) {
  const code = payload.errCode;
  if (code === -2041) {
    fail('微信读书返回 -2041（页面上下文校验失败或触发风控）',
         '风控通常 20-30 分钟自动恢复；稍后重试，不要连发请求');
  }
  if (code === -2010) {
    // 工具的官方技术文档把 -2010 记为「登录失效」（docs/HOW-IT-WORKS.md）。
    // 但别只凭一个错误码就让人去扫码 —— 先确认是不是风控（见 -2012 分支）。
    fail('微信读书返回 -2010',
         '通常是登录态失效，需要在专用 Chrome 里重新扫码。' +
         '若刚发生过大量请求，也可能是风控，先等 15 分钟再试一次确认');
  }
  if (code === -2012) {
    // ⚠️ 别把这个当成「登录失效」—— 实测它几乎总是短时风控：
    //   所有接口（含书架）一起失败，等十几分钟自愈，登录 cookie 其实好好的。
    //
    // 踩过的坑：曾用 `document.cookie` 查 wr_skey，发现「不见了」就判定登录失效，
    // 让用户白白重新扫码。实际上 wr_skey 是 HttpOnly 的，JS 永远读不到，
    // 但浏览器发请求时会自动带上 —— 那个判断方法从根上就是错的。
    //
    // 正确判据：拿一个**已知能抓**的号对照。
    //   · 它也 -2012  → 全站风控，等 15-30 分钟
    //   · 只有目标号  → 该号未被微信读书收录
    fail('微信读书返回 -2012（多为短时风控）',
         '停手等 15-30 分钟再试，不要连发请求。' +
         '判断方法：拿一个已知能抓的号对照 —— 它也 -2012 就是风控；' +
         '只有目标号失败，才是该号未被微信读书收录。' +
         '注意：这不代表登录失效，别急着重扫码');
  }
  fail(`微信读书返回错误 ${code ?? ''}`, payload.err || '');
}

payload.pageMeta = { offset, reviews: payload.reviews, items: payload.items.length, oldest: payload.oldest };
console.log(JSON.stringify(payload));
