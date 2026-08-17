"""平博下单 UI 模拟页面（对齐真实站点 DOM 特征）。

特征对齐：
- 侧栏导航（体育/滚球盘/篮球/冰球）+ 顶部搜索框（placeholder 搜索）
- 联赛区块（多个 section 含大量赔率格）—— 用于测行选择惩罚
- 单场行：队名 + 比分 + 时间 + 小球行（含盘口线与小球赔率按钮）
- 2 字队名用例（坎昆）
- 误中陷阱：其它行含 2 字片段（如"普雷斯顿"含"坎"字变体不适用，用别名重叠）
- 点击赔率叶子 → 打开投注单（投下1注 / 风险 / 可赢 / 最低投注额 / 总注金）
- 点「投下1注」→ 弹「您是否想要投注」+ OK/取消
- 点击非叶子容器块 → 投注单不开（对齐 slip_not_open 故障）
- 余额显示（410.20 CNY），确认后扣减
"""
import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

PAGE = """<!DOCTYPE html>
<html lang="zh-cn"><head><meta charset="utf-8"><title>滚球</title>
<style>
body{font-family:sans-serif;margin:0;display:flex}
#side{width:150px;border-right:1px solid #ccc;padding:8px}
#side div{padding:8px;cursor:pointer}
#main{flex:1;padding:10px}
#search{width:280px;height:26px;margin-bottom:10px}
section{border:1px solid #eee;margin-bottom:8px;padding:6px}
.lg{background:#f7f7f7;font-weight:bold;padding:3px}
.row{display:flex;align-items:center;gap:14px;padding:7px 4px;border-bottom:1px solid #f2f2f2}
.teams{width:250px}
.score{color:#c33;width:60px}
.clock{color:#888;width:44px}
.ou{display:flex;gap:10px;align-items:center}
.ou .line{color:#555;width:40px}
.odd{cursor:pointer;border:1px solid #d5d5d5;padding:4px 10px;background:#fff}
.odd:hover{background:#f0f7ff}
.blk{color:#333}
#slip{position:fixed;right:0;top:0;width:300px;border-left:1px solid #999;padding:12px;display:none;background:#fff}
#slip.open{display:block}
#modal{position:fixed;inset:0;background:rgba(0,0,0,.4);display:none;align-items:center;justify-content:center}
#modal.open{display:flex}
#modalBox{background:#fff;padding:20px 30px;border-radius:8px}
#bal{position:fixed;left:160px;bottom:0;padding:8px;background:#eef}
</style></head>
<body>
<div id="side">
  <div>体育</div><div>滚球盘</div><div>足球</div><div>篮球</div><div>冰球</div>
</div>
<div id="main">
  <input id="search" placeholder="搜索 队伍 / 联赛">
  <div id="list"></div>
</div>
<div id="slip">
  <div style="font-weight:bold">投注单</div>
  <div id="slipSel"></div>
  <label><input type="checkbox">接受更佳的赔率</label>
  <div>风险 <input id="stake" type="number" inputmode="decimal" style="width:90px"> CNY</div>
  <div>可赢 <span id="toWin">0.00</span></div>
  <div>最低投注额 5</div>
  <div>总注金 <span id="total">0.00</span> CNY</div>
  <button id="placeBtn">投下1注</button>
  <button id="removeAll">移除全部</button>
</div>
<div id="modal"><div id="modalBox">
  <div>您是否想要投注</div>
  <button id="okBtn">OK</button> <button id="cancelBtn">取消</button>
</div></div>
<div id="bal">410.20 CNY</div>
<script>
window.__state = { picked: null, bets: [] };
const DATA = [
  { lg: '墨西哥乙级联赛', rows: [
    { h: '坎昆', a: '德杜兰戈阿拉克兰内斯', sc: '0-0', ck: "23'", line: '2.5', ov: 2.00, un: 1.79 },
    { h: '普雷斯顿坎普队', a: '维拉FC', sc: '1-1', ck: "41'", line: '2.5', ov: 1.85, un: 1.95 },
    { h: '坎普埃奎诺', a: '拉莫斯阿罗约', sc: '2-1', ck: '2H', line: '2.75', ov: 1.93, un: 1.88 },
  ]},
  { lg: '巴西圣保罗州杯', rows: [
    { h: '圣安德雷', a: '欧斯特', sc: '0-1', ck: "67'", line: '2.0', ov: 2.05, un: 1.72 },
    { h: '陶朗加市', a: '奥克兰城', sc: '0-0', ck: "12'", line: '3.0', ov: 1.90, un: 1.90 },
  ]},
  { lg: 'NBA', rows: [
    { h: '洛杉矶湖人', a: '芝加哥公牛', sc: '10-10', ck: 'Q2 08:00', line: '180.5', ov: 1.95, un: 1.88 },
  ]},
];
function render(filter) {
  const el = document.getElementById('list');
  el.innerHTML = '';
  for (const sec of DATA) {
    const s = document.createElement('section');
    s.className = 'blk';
    const lg = document.createElement('div'); lg.className='lg'; lg.textContent = sec.lg; s.appendChild(lg);
    let any = false;
    for (const m of sec.rows) {
      if (filter && !(m.h.includes(filter) || m.a.includes(filter))) continue;
      any = true;
      const r = document.createElement('div'); r.className='row';
      r.innerHTML = `<span class="clock">${m.ck}</span><span class="teams">${m.h}<br>${m.a}</span><span class="score">${m.sc}</span>`;
      const ou = document.createElement('div'); ou.className='ou';
      ou.innerHTML = `<span class="line">${m.line}</span>`;
      const mkBtn = (label, val, dir) => {
        const b = document.createElement('span'); b.className='odd'; b.textContent = val.toFixed(2).replace(/0$/,'');
        b.addEventListener('click', () => openSlip(m, dir, val));
        return b;
      };
      // 模拟页面仅提供小球赔率。
      const unLbl = document.createElement('span'); unLbl.textContent='小';
      ou.appendChild(unLbl); ou.appendChild(mkBtn('小', m.un, 'under'));
      r.appendChild(ou);
      s.appendChild(r);
    }
    if (any) el.appendChild(s);
  }
}
function openSlip(m, dir, val) {
  window.__state.picked = { m, dir, val };
  document.getElementById('slip').classList.add('open');
  document.getElementById('slipSel').textContent = `${m.h} vs ${m.a} — 小球 ${m.line} @ ${val}`;
  document.getElementById('toWin').textContent = '0.00';
  document.getElementById('total').textContent = '0.00';
  document.getElementById('stake').value = '';
}
document.getElementById('stake').addEventListener('input', () => {
  const v = parseFloat(document.getElementById('stake').value || '0');
  const p = window.__state.picked;
  const win = p ? (v * p.val - v).toFixed(2) : '0.00';
  document.getElementById('toWin').textContent = win;
  document.getElementById('total').textContent = v.toFixed(2);
});
document.getElementById('placeBtn').addEventListener('click', () => {
  const v = parseFloat(document.getElementById('stake').value || '0');
  if (!window.__state.picked || !(v > 0)) return;
  if (v < 5) { alert('您的投注不能低于最低投注金额'); return; }
  document.getElementById('modal').classList.add('open');
});
document.getElementById('okBtn').addEventListener('click', () => {
  const v = parseFloat(document.getElementById('stake').value || '0');
  const p = window.__state.picked;
  window.__state.bets.push({ ...p, stake: v });
  const cur = parseFloat(document.getElementById('bal').textContent);
  document.getElementById('bal').textContent = (cur - v).toFixed(2) + ' CNY';
  document.getElementById('modal').classList.remove('open');
  document.getElementById('slip').classList.remove('open');
});
document.getElementById('cancelBtn').addEventListener('click', () => {
  document.getElementById('modal').classList.remove('open');
});
document.getElementById('removeAll').addEventListener('click', () => {
  document.getElementById('slip').classList.remove('open');
});
document.getElementById('search').addEventListener('input', (e) => render(e.target.value));
render('');
</script></body></html>"""


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        # 平博 UI 会按球种跳转到 /zh-cn/compact/sports/<sport>/live；
        # mock 对所有同源 GET 返回同一份可交互页面，以覆盖该导航路径。
        body = PAGE.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def serve(port: int):
    srv = HTTPServer(("127.0.0.1", port), H)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv


async def main():
    port = 9877
    srv = serve(port)
    print(f"mock pinnacle live on http://127.0.0.1:{port}/live")
    try:
        while True:
            await asyncio.sleep(3600)
    except KeyboardInterrupt:
        srv.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
