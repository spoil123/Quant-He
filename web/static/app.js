/* 量化系统前端逻辑（Vue3 + ECharts） */
const { createApp, ref, reactive, onMounted, nextTick, watch } = Vue;

const app = createApp({
  setup() {
    const tab = ref('overview');
    const ovTag = ref('');
    const ovRuns = ref([]);
    const ovMetrics = ref({});
    const ovEquity = ref([]);
    const ovPlain = ref([]);
    const ovRisk = ref([]);
    const btList = ref([]);
    const btDetail = ref({});
    const icTable = ref([]);
    const icSeries = ref([]);
    const quantile = ref([]);
    const ftag = ref('');
    const riskReport = ref([]);
    const riskEvents = ref([]);
    const rtag = ref('');
    const riskPlain = ref([]);
    const riskRisk = ref([]);
    const mon = ref(null);
    const running = ref(false);
    const riskParm = reactive({ single_stock_drawdown: -0.25, trailing_stop: -0.30, drawdown_trigger: -0.12 });

    const charts = {};

    const num = v => (typeof v === 'number' ? v : parseFloat(v) || 0);
    const pct = v => (v === null || v === undefined ? '-' : (num(v) * 100).toFixed(2) + '%');
    const isPct = k => ['收益率', '回撤', '波动率'].some(s => k.includes(s));
    const isLoss = v => v < 0;
    const fmtVal = (k, v) => (isPct(k) ? pct(v) : (typeof v === 'number' ? v.toFixed(3) : v));
    const fmtCell = v => (typeof v === 'number' ? (v * 100).toFixed(2) + '%' : (v ?? '-'));

    async function get(url) { const r = await fetch(url); return r.json(); }

    function chart(id, option) {
      const el = document.getElementById(id);
      if (!el) return;                       // 标签未挂载时跳过，切过去时由 watch 重绘
      if (!charts[id] || charts[id].isDisposed()) charts[id] = echarts.init(el);
      charts[id].setOption(option, true);
      charts[id].resize();
    }

    // 标签切换后重绘对应图表（启动时隐藏标签里的 div 不存在，图只能在此补画）
    watch(tab, t => {
      nextTick(() => {
        if (t === 'overview') loadOverview(ovTag.value || 'latest');
        else if (t === 'backtest' && btDetail.value.tag) loadBacktest(btDetail.value.tag);
        else if (t === 'factors') loadFactors();
        else if (t === 'risk') loadRisk();
        else if (t === 'combo') loadComboResults();
      });
    });
    function chartData(records) {
      const x = records.map(r => r.trade_date || r.date);
      const y = records.map(r => num(r.equity || r.mean_return || 0));
      return { x, y };
    }

    async function loadOverview(tag) {
      const d = await get('/api/overview');
      if (d.error) return;
      ovRuns.value = d.runs || [];
      ovTag.value = tag === 'latest' ? (d.tag || '') : tag;
      ovMetrics.value = d.metrics || {};
      ovEquity.value = d.equity || [];
      ovPlain.value = d.equity_plain || [];
      ovRisk.value = d.equity_risk || [];
      await nextTick();
      const series = [{ name: '无风控', type: 'line', showSymbol: false, data: ovEquity.value.map(r => num(r.equity)), lineStyle: { color: '#e24b4a', width: 2 } }];
      if (ovPlain.value.length && ovRisk.value.length) {
        series.push({ name: '风控前', type: 'line', showSymbol: false, data: ovPlain.value.map(r => num(r.equity)), lineStyle: { color: '#e24b4a', width: 1.5, type: 'dashed' } });
        series.push({ name: '风控后', type: 'line', showSymbol: false, data: ovRisk.value.map(r => num(r.equity)), lineStyle: { color: '#378add', width: 2 } });
      }
      chart('ovChart', {
        tooltip: { trigger: 'axis' }, legend: { textStyle: { color: '#9aa3b2' } },
        grid: { left: 50, right: 20, top: 30, bottom: 30 },
        xAxis: { type: 'category', data: ovEquity.value.map(r => r.trade_date), axisLabel: { color: '#9aa3b2' } },
        yAxis: { type: 'value', axisLabel: { color: '#9aa3b2' } }, series,
      });
    }

    async function loadBacktest(tag) {
      btDetail.value = await get('/api/backtest/' + tag);
      await nextTick();
      if (btDetail.value.equity) {
        const e = btDetail.value.equity;
        chart('btChart', {
          tooltip: { trigger: 'axis' }, grid: { left: 50, right: 20, top: 20, bottom: 30 },
          xAxis: { type: 'category', data: e.map(r => r.trade_date), axisLabel: { color: '#9aa3b2' } },
          yAxis: { type: 'value', axisLabel: { color: '#9aa3b2' } },
          series: [{ type: 'line', showSymbol: false, data: e.map(r => num(r.equity)), lineStyle: { color: '#378add', width: 2 } }],
        });
      }
    }

    async function loadFactors() {
      const d = await get('/api/factors');
      if (d.error) return;
      ftag.value = d.tag || '';
      icTable.value = d.ic_table || [];
      icSeries.value = d.ic_series || [];
      quantile.value = d.quantile || [];
      await nextTick();
      if (icSeries.value.length) {
        const keys = Object.keys(icSeries.value[0]).filter(k => k !== Object.keys(icSeries.value[0])[0]);
        chart('icChart', {
          tooltip: { trigger: 'axis' }, legend: { textStyle: { color: '#9aa3b2' } },
          grid: { left: 50, right: 20, top: 30, bottom: 30 },
          xAxis: { type: 'category', data: icSeries.value.map(r => r[Object.keys(r)[0]]), axisLabel: { color: '#9aa3b2' } },
          yAxis: { type: 'value', axisLabel: { color: '#9aa3b2' } },
          series: keys.map(k => ({ name: k, type: 'line', showSymbol: false, data: icSeries.value.map(r => num(r[k])), lineStyle: { width: 1.4 } })),
        });
      }
      if (quantile.value.length) {
        const factors = [...new Set(quantile.value.map(r => r.factor))];
        chart('qrChart', {
          tooltip: {}, legend: { textStyle: { color: '#9aa3b2' } },
          grid: { left: 50, right: 20, top: 30, bottom: 30 },
          xAxis: { type: 'category', data: ['1','2','3','4','5'], axisLabel: { color: '#9aa3b2' } },
          yAxis: { type: 'value', axisLabel: { color: '#9aa3b2' } },
          series: factors.map(f => ({
            name: f, type: 'bar', data: [1,2,3,4,5].map(g => {
              const r = quantile.value.find(q => q.factor === f && Number(q['组']) === g);
              return r ? num(r['mean_return']) * 100 : null;
            }),
          })),
        });
      }
    }

    async function loadRisk() {
      const d = await get('/api/risk');
      if (d.error) return;
      rtag.value = d.tag || '';
      riskReport.value = d.report || [];
      riskEvents.value = d.events || [];
      riskPlain.value = d.equity_plain || [];
      riskRisk.value = d.equity_risk || [];
      await nextTick();
      if (riskPlain.value.length && riskRisk.value.length) {
        chart('riskChart', {
          tooltip: { trigger: 'axis' }, legend: { textStyle: { color: '#9aa3b2' } },
          grid: { left: 50, right: 20, top: 30, bottom: 30 },
          xAxis: { type: 'category', data: riskPlain.value.map(r => r.trade_date), axisLabel: { color: '#9aa3b2' } },
          yAxis: { type: 'value', axisLabel: { color: '#9aa3b2' } },
          series: [
            { name: '无风控', type: 'line', showSymbol: false, data: riskPlain.value.map(r => num(r.equity)), lineStyle: { color: '#e24b4a', width: 1.6 } },
            { name: '有风控', type: 'line', showSymbol: false, data: riskRisk.value.map(r => num(r.equity)), lineStyle: { color: '#378add', width: 2 } },
          ],
        });
      }
    }

    async function loadMonitor() { mon.value = await get('/api/monitor'); }

    // ---------------- 因子配置（App 内调换因子） ----------------
    const fc = ref(null);            // 因子配置（GET /api/factor-config）
    const fcWeight = reactive({});   // 权重输入框
    const fcMsg = ref('');

    function fcWeightSum() {
      let s = 0;
      for (const k in fcWeight) s += num(fcWeight[k]);
      return s;
    }
    function fcWeightOk() {
      return !fc.value || fc.value.composite.method !== 'custom' || fcWeightSum() > 0;
    }

    async function loadFactorConfig() {
      fc.value = await get('/api/factor-config');
      if (!fc.value || fc.value.error) { fcMsg.value = (fc.value && fc.value.error) || '加载失败'; return; }
      for (const k in fc.value.factors) {
        fcWeight[k] = fc.value.composite.weights[k] ?? 0;
      }
    }

    async function saveFactorConfig(withRerun) {
      if (!fc.value) return;
      running.value = true; fcMsg.value = '保存中...';
      const payload = { factors: {}, composite: { method: fc.value.composite.method } };
      for (const k in fc.value.factors) {
        const f = fc.value.factors[k];
        payload.factors[k] = { enabled: !!f.enabled, direction: num(f.direction) };
      }
      if (fc.value.composite.method === 'custom') {
        const w = {};
        for (const k in fcWeight) w[k] = num(fcWeight[k]);
        payload.composite.weights = w;
      }
      const r = await fetch('/api/factor-config', {
        method: 'PUT', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      }).then(x => x.json());
      if (r.error) { fcMsg.value = '保存失败: ' + r.error; running.value = false; return; }
      fcMsg.value = '已保存（备份 ' + r.backup + '）' + (withRerun ? '，开始重跑...' : '');
      if (!withRerun) { running.value = false; await loadFactorConfig(); return; }

      const rr = await fetch('/api/run/backtest', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ start: '2019-01-01', end: '2023-12-31' }),
      }).then(x => x.json());
      const t0 = Date.now();
      const iv = setInterval(async () => {
        const s = await get('/api/task/' + rr.task_id);
        if (s.status === 'done' || s.status === 'failed' || Date.now() - t0 > 400000) {
          clearInterval(iv); running.value = false;
          fcMsg.value = s.status === 'done' ? '重跑完成，总览已刷新' : '重跑失败: ' + (s.error || (s.log_tail || []).join(' | ') || '');
          await loadFactorConfig();           // 重新读（后端归一化后的权重）
          loadOverview('latest');             // 刷新总览指标
        }
      }, 3000);
    }

    async function runRisk() {
      running.value = true;
      const r = await fetch('/api/run/risk', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ start: '2019-01-01', end: '2023-12-31' }),
      }).then(x => x.json());
      const t0 = Date.now();
      const iv = setInterval(async () => {
        const s = await get('/api/task/' + r.task_id);
        if (s.status === 'done' || s.status === 'failed' || Date.now() - t0 > 400000) {
          clearInterval(iv); running.value = false;
          loadRisk();
        }
      }, 3000);
    }

    // ---------------- 组合回测（勾选成员 + 调组合权重 + 一键回测） ----------------
    const cb = reactive({});          // 组合配置（GET /api/combo-config）
    const cbSel = reactive({});       // 成员勾选
    const cbWeight = reactive({});    // 成员组合权重输入
    const cbStart = ref(''); const cbEnd = ref('');
    const cbMsg = ref(''); const cbRunning = ref(false);
    const cbRes = ref({});

    const subW = sw => Object.entries(sw || {}).slice(0, 4)
      .map(([k, v]) => k + ' ' + (v * 100).toFixed(0) + '%').join(' · ') + ' …';
    function cbOk() {
      return Object.keys(cbSel).some(k => cbSel[k]);
    }

    async function loadCombo() {
      let d;
      try { d = await get('/api/combo-config'); }
      catch (e) { cbMsg.value = '组合配置加载失败'; return; }
      if (!d || d.error) { cbMsg.value = (d && d.error) || '加载失败'; return; }
      Object.keys(cb).forEach(k => delete cb[k]);
      Object.assign(cb, d);
      for (const f of d.catalog || []) {
        cbSel[f.name] = f.name in d.members;
        cbWeight[f.name] = d.members[f.name] ?? f.topn / 100.0;
      }
      cbStart.value = d.period.start; cbEnd.value = d.period.end;
    }

    async function loadComboResults() {
      let d;
      try { d = await get('/api/combo-results'); }
      catch (e) { return; }
      cbRes.value = d || {};
      if (d && d.error) return;
      await nextTick();
      if (cbRes.value.equity && cbRes.value.equity.length) {
        const e = cbRes.value.equity;
        chart('cbChart', {
          tooltip: { trigger: 'axis' }, grid: { left: 50, right: 20, top: 20, bottom: 30 },
          xAxis: { type: 'category', data: e.map(r => r.trade_date), axisLabel: { color: '#9aa3b2' } },
          yAxis: { type: 'value', axisLabel: { color: '#9aa3b2' } },
          series: [{ type: 'line', showSymbol: false, data: e.map(r => num(r.equity)), lineStyle: { color: '#d9a13d', width: 2 } }],
        });
      }
    }

    async function saveCombo(withRun) {
      if (!cb.members) return;
      cbRunning.value = true; cbMsg.value = '保存中...';
      const members = {};
      for (const k in cbSel) if (cbSel[k]) members[k] = num(cbWeight[k]) || 1.0;
      const payload = {
        name: cb.name, method: cb.method, members,
        top_n: cb.top_n, max_weight: cb.max_weight,
        period: { start: cbStart.value, end: cbEnd.value },
      };
      const r = await fetch('/api/combo-config', {
        method: 'PUT', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      }).then(x => x.json());
      if (r.error) { cbMsg.value = '保存失败: ' + r.error; cbRunning.value = false; return; }
      cbMsg.value = '已保存（备份 ' + r.backup + '）' + (withRun ? '，回测启动…' : '');
      if (!withRun) { cbRunning.value = false; await loadCombo(); return; }

      const rr = await fetch('/api/run/combo', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ start: cbStart.value, end: cbEnd.value }),
      }).then(x => x.json());
      const t0 = Date.now();
      const iv = setInterval(async () => {
        const s = await get('/api/task/' + rr.task_id);
        const sec = Math.round((Date.now() - t0) / 1000);
        cbMsg.value = '回测运行中… ' + sec + 's（首次需加载面板缓存，约 1~2 分钟）';
        if (s.status === 'done' || s.status === 'failed' || Date.now() - t0 > 700000) {
          clearInterval(iv); cbRunning.value = false;
          cbMsg.value = s.status === 'done'
            ? '回测完成（' + sec + 's），结果已刷新'
            : '回测失败: ' + (s.error || '超时或查看 startup.log');
          await loadComboResults();
        }
      }, 3000);
    }

    async function runRisk() {
      running.value = true;
      const r = await fetch('/api/run/risk', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ start: '2019-01-01', end: '2023-12-31' }),
      }).then(x => x.json());
      const t0 = Date.now();
      const iv = setInterval(async () => {
        const s = await get('/api/task/' + r.task_id);
        if (s.status === 'done' || s.status === 'failed' || Date.now() - t0 > 400000) {
          clearInterval(iv); running.value = false;
          loadRisk();
        }
      }, 3000);
    }

    onMounted(() => {
      // 并行加载、互不阻塞：/api/monitor 要查全库（约 1~2 分钟），
      // 若 await 串行会把后面的组合页配置一起拖死
      const jobs = [
        loadOverview('latest'),
        get('/api/backtests').then(d => { btList.value = d || []; }),
        loadFactors(), loadRisk(), loadMonitor(), loadFactorConfig(),
        loadCombo(), loadComboResults(),
      ];
      jobs.forEach(p => p && p.catch && p.catch(() => {}));
    });

    return { tab, ovTag, ovRuns, ovMetrics, ovEquity, btList, btDetail, icTable, icSeries, quantile, ftag,
      riskReport, riskEvents, rtag, mon, running, riskParm, num, pct, isPct, isLoss, fmtVal, fmtCell,
      fc, fcWeight, fcMsg, fcWeightSum, fcWeightOk,
      loadOverview, loadBacktest, runRisk, saveFactorConfig,
      cb, cbSel, cbWeight, cbStart, cbEnd, cbMsg, cbRunning, cbRes,
      cbOk, subW, saveCombo };
  },
});
app.mount('#app');
