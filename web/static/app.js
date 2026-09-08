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
    const ovCombo = ref({});        // Combo3 配置快照（名称/成员/区间）
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
      ovCombo.value = d.combo || {};
      ovPlain.value = d.equity_plain || [];
      ovRisk.value = d.equity_risk || [];
      await nextTick();
      const series = [{ name: 'Combo3 净值', type: 'line', showSymbol: false, data: ovEquity.value.map(r => num(r.equity)), lineStyle: { color: '#e24b4a', width: 2 } }];
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

    // ---------------- 因子清单（v1.2 只读：因子集锁定，不可增删） ----------------
    const fc = ref({});              // 因子配置（GET /api/factor-config）

    async function loadFactorConfig() {
      fc.value = await get('/api/factor-config') || {};
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

    // ---------------- 组合回测（v1.2 只读：固定 Combo3，仅可重跑） ----------------
    const cb = reactive({});          // 组合配置（GET /api/combo-config）
    const cbStart = ref(''); const cbEnd = ref('');
    const cbMsg = ref(''); const cbRunning = ref(false);
    const cbRes = ref({});

    const subW = sw => Object.entries(sw || {}).slice(0, 4)
      .map(([k, v]) => k + ' ' + (v * 100).toFixed(0) + '%').join(' · ') + ' …';
    const comboMembers = () => Object.keys(ovCombo.value.members || cb.members || {})
      .join(' + ') || 'MD-Mom50 + DV-LV50 + GM-LV50';

    async function loadCombo() {
      let d;
      try { d = await get('/api/combo-config'); }
      catch (e) { cbMsg.value = '组合配置加载失败'; return; }
      if (!d || d.error) { cbMsg.value = (d && d.error) || '加载失败'; return; }
      Object.keys(cb).forEach(k => delete cb[k]);
      Object.assign(cb, d);
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

    async function runCombo() {
      cbRunning.value = true; cbMsg.value = '回测启动…';
      // 固定配置重跑：不传参，区间/成员全部走锁定的 config/combo.yaml
      const rr = await fetch('/api/run/combo', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({}),
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
          loadOverview('latest');
          get('/api/backtests').then(d => { btList.value = d || []; });
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

    return { tab, ovTag, ovRuns, ovMetrics, ovEquity, ovCombo, comboMembers,
      btList, btDetail, icTable, icSeries, quantile, ftag,
      riskReport, riskEvents, rtag, mon, running, riskParm, num, pct, isPct, isLoss, fmtVal, fmtCell,
      fc, loadOverview, loadBacktest, runRisk,
      cb, cbStart, cbEnd, cbMsg, cbRunning, cbRes,
      subW, runCombo };
  },
});
app.mount('#app');
