// 用专用 Chrome 渲染一篇文章，返回渲染后的正文 HTML
//
// 用法：node render_article.mjs <文章URL>
//
// 输出（stdout，一行 JSON）：
//   {"ok":true,"title":"...","contentHtml":"...","images":["..."],"author":"...","account":"..."}
//   {"ok":false,"error":"...","hint":"..."}
//
// ## 为什么需要这个
//
// 微信公众号正在把文章页改成 **JS 动态渲染**：
//   静态 HTML 里只有壳（「微信扫一扫 使用小程序」之类），
//   正文靠 JS 加载后才出现 —— 抓静态 HTML 会得到「正文为空」。
//
// 判据：静态 HTML 里搜不到真正的 `id="js_content"` 元素
//       （只有 JS 源码里提到这个字符串），就是动态渲染页。
//
// 这类页面必须用真浏览器渲染。我们本来就有个带调试端口的 Chrome
// （微信读书那边在用），复用它即可，不用再开一个。

const CDP_PORT = Number(process.env.WEREAD_CDP_PORT || 9333);
const url = process.argv[2];

if (!url) {
  console.log(JSON.stringify({ ok: false, error: '缺少 URL' }));
  process.exit(2);
}

function fail(error, hint) {
  console.log(JSON.stringify({ ok: false, error, hint }));
  process.exit(0);
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// ---------------------------------------------------------------- CDP 会话

async function version() {
  const res = await fetch(`http://127.0.0.1:${CDP_PORT}/json/version`, { signal: AbortSignal.timeout(5000) });
  return res.json();
}

let ver;
try {
  ver = await version();
} catch {
  fail(`连不上专用 Chrome（端口 ${CDP_PORT}）`,
       '先启动：bash ~/.weread-mp-fetcher/start-chrome.sh');
}

// 用浏览器级 WebSocket（能创建/关闭标签页）
const ws = new WebSocket(ver.webSocketDebuggerUrl);
let msgId = 0;
const pending = new Map();

ws.addEventListener('message', (ev) => {
  const d = JSON.parse(ev.data);
  if (d.id && pending.has(d.id)) {
    const { resolve } = pending.get(d.id);
    pending.delete(d.id);
    resolve(d);
  }
});

const send = (method, params, sessionId) => new Promise((resolve) => {
  const myId = ++msgId;
  pending.set(myId, { resolve });
  ws.send(JSON.stringify({ id: myId, method, params, ...(sessionId ? { sessionId } : {}) }));
});

const opened = await new Promise((resolve) => {
  const t = setTimeout(() => resolve(false), 8000);
  ws.addEventListener('open', () => { clearTimeout(t); resolve(true); });
  ws.addEventListener('error', () => { clearTimeout(t); resolve(false); });
});
if (!opened) fail('WebSocket 连接失败', 'Chrome 可能已退出');

// ---------------------------------------------------------------- 开标签页

const created = await send('Target.createTarget', { url: 'about:blank' });
const targetId = created.result?.targetId;
if (!targetId) fail('无法创建标签页', JSON.stringify(created).slice(0, 200));

const attached = await send('Target.attachToTarget', { targetId, flatten: true });
const sessionId = attached.result?.sessionId;
if (!sessionId) {
  await send('Target.closeTarget', { targetId });
  fail('无法附着到标签页');
}

const ev = async (expression, awaitPromise = true) => {
  const r = await send('Runtime.evaluate', { expression, awaitPromise, returnByValue: true }, sessionId);
  try { return r.result.result.value; } catch { return undefined; }
};

try {
  await send('Page.enable', {}, sessionId);
  await send('Page.navigate', { url }, sessionId);

  // 等正文出现：轮询而不是死等固定时间
  // 判据用「真的有 js_content 元素且有内容」，不是「页面加载完」——
  // 加载完时正文可能还没渲染出来。
  let ready = false;
  for (let i = 0; i < 30; i++) {          // 最多约 15 秒
    await sleep(500);
    const state = await ev(`(function(){
      var el = document.getElementById('js_content');
      return JSON.stringify({
        hasEl: !!el,
        len: el ? el.innerHTML.length : 0,
        title: (document.querySelector('#activity-name')||{}).innerText || document.title || ''
      });
    })()`);
    if (!state) continue;
    try {
      const s = JSON.parse(state);
      if (s.hasEl && s.len > 80) { ready = true; break; }
    } catch { /* 继续轮询 */ }
  }

  // 检测验证/异常页
  const pageText = await ev('document.body ? document.body.innerText.slice(0,300) : ""') || '';
  if (!ready && /环境异常|去验证|请输入验证码|访问过于频繁/.test(pageText)) {
    fail('这篇文章需要验证', `页面提示：${pageText.slice(0, 80)}`);
  }
  if (!ready) {
    fail('等待正文渲染超时', `页面文本：${String(pageText).slice(0, 100)}`);
  }

  // 渲染好了 —— 抓正文 + 元数据
  //
  // ⚠️ 微信的 DOM 选择器改名很频繁，所以每个字段都取多个来源，
  //    取到第一个非空的就用。只依赖单一选择器迟早会失效。
  const payload = await ev(`(function(){
    function pick(){
      for (var i = 0; i < arguments.length; i++) {
        var v = arguments[i];
        if (v && String(v).trim()) return String(v).trim();
      }
      return '';
    }
    function txt(sel){
      var el = document.querySelector(sel);
      return el ? (el.innerText || el.textContent || '') : '';
    }
    function meta(prop){
      var el = document.querySelector('meta[property="' + prop + '"]') || document.querySelector('meta[name="' + prop + '"]');
      return el ? (el.getAttribute('content') || '') : '';
    }

    var content = document.getElementById('js_content');
    // 有些文章用「图片分享页」模板：正文在 js_image_content 里，
    // 外层 js_content 只是个容器。两种都取，取更长的那个。
    var imageContent = document.getElementById('js_image_content');
    var useImage = imageContent && (!content || imageContent.innerHTML.length > content.innerHTML.length);
    if (useImage) content = imageContent;

    // 正文容器里混着赞赏、二维码、推荐位等界面元素 —— 先摘掉再取 innerHTML。
    // （这些不是文章内容，留着会污染 Markdown。）
    var JUNK = [
      '.rich_media_area_extra', '#js_pc_qr_code', '.qr_code_pc', '.rich_media_tool',
      '#js_article_comment', '.discuss_container', '.reward_area', '#js_reward_area',
      '.weui-msg', '#js_preview_reward', '.rich_media_meta', '.rich_media_area_primary_inner > .rich_media_meta_list',
      '.js_unset_tip', '.js_loading', '.function_mod', '.mpda_bottom_container',
      '#js_tags', '.rich_media_extra', '.original_area_primary', '.weapp_text_link'
    ];
    var cleanHtml = '';
    if (content) {
      var clone = content.cloneNode(true);
      JUNK.forEach(function(sel){
        clone.querySelectorAll(sel).forEach(function(el){ el.remove(); });
      });
      // 去掉 script / style / iframe
      clone.querySelectorAll('script,style,iframe,noscript').forEach(function(el){ el.remove(); });
      cleanHtml = clone.innerHTML;
    }

    var out = {
      title: pick(txt('#activity-name'), txt('.rich_media_title'), meta('og:title'), document.title),
      contentHtml: cleanHtml,
      images: [],
      author: pick(txt('#js_author_name'), txt('.rich_media_meta_text'), meta('author')),
      account: pick(txt('#js_name'), meta('og:article:author')),
      publishTime: pick(txt('#publish_time')),
      template: useImage ? 'image_content' : 'content',
      _src: {
        titleEl: !!txt('#activity-name'),
        titleClass: !!txt('.rich_media_title'),
        ogTitle: !!meta('og:title'),
        nameEl: !!txt('#js_name'),
        ogAuthor: !!meta('og:article:author'),
        authorEl: !!txt('#js_author_name')
      }
    };
    // 图片从整个文章容器里找 —— 有的模板把图放在正文容器之外
    var scope = document.getElementById('js_article') || document.body;
    var seenSrc = {};
    scope.querySelectorAll('img').forEach(function(img){
      var src = img.getAttribute('data-src') || img.getAttribute('src') || '';
      if (!src || /^data:/.test(src) || seenSrc[src]) return;
      // 过滤掉头像/二维码/图标这类装饰图
      if (/qrcode|avatar|icon|logo/i.test(src)) return;
      seenSrc[src] = 1;
      out.images.push(src);
    });
    return JSON.stringify(out);
  })()`);

  let data;
  try { data = JSON.parse(payload); } catch { fail('无法解析渲染结果'); }

  if (!data.contentHtml || data.contentHtml.length < 80) {
    fail('渲染后正文仍为空', `长度 ${(data.contentHtml || '').length}`);
  }

  console.log(JSON.stringify({ ok: true, ...data }));
} catch (e) {
  fail('渲染过程出错', String(e).slice(0, 160));
} finally {
  try { await send('Target.closeTarget', { targetId }); } catch { /* 关不掉也不影响结果 */ }
  ws.close();
}
