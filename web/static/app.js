/* Quant·He 因子工作台（Vue3 + ECharts） */
const { createApp, ref, reactive, computed, onMounted, nextTick, watch } = Vue;

const app = createApp({
  setup() {
    const tab = ref('overview');
    const titles = { overview: '总览', workbench: '因子工作台', combo: '配方组合',
                     backtest: '回测记录', factors: '因子检验', risk: '风控与监控' };
    const pageTitle = computed(() => titles[tab.value] || '');

    const ovTag = ref('');
    const ovRuns = ref([]);
    const ovMetrics = ref({});
    const ovEquity = ref([]);
    const ovCombo = ref({});
    const btList = ref([]);
    const btDetail = ref({});
    const icTable = ref([]);
    const icSeries = ref([]);
    const quantile = ref([]);
    const ftag = ref('');
    const riskReport = ref([]);
    const riskEvents = ref([]);
    const rtag = ref('');
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
    // 风控对比表：夏普/卡玛等比率是倍数不是百分比，var/cvar/收益率/回撤才是百分比
    const fmtMetric = (name, v) => {
      if (typeof v !== 'number') return (v ?? '-');
      if (String(name).includes('比率')) return v.toFixed(3);
      return (v * 100).toFixed(2) + '%';
    };

    async function get(url) { const r = await fetch(url); return r.json(); }

    function chart(id, option) {
      const el = document.getElementById(id);
      if (!el) return;
      if (!charts[id] || charts[id].isDisposed()) charts[id] = echarts.init(el);
      charts[id].setOption(option, true);
      charts[id].resize();
    }
    const axisStyle = { axisLabel: { color: '#94a0b4', formatter: v => Math.abs(v) >= 10000 ? (v / 10000) + '万' : v },
                        axisLine: { lineStyle: { color: 'rgba(148,163,184,.16)' } },
                        splitLine: { lineStyle: { color: 'rgba(148,163,184,.09)' } } };
    const tooltipStyle = { backgroundColor: 'rgba(14,19,29,.95)', borderColor: 'rgba(148,163,184,.28)',
                           textStyle: { color: '#e9edf4', fontSize: 12 } };
    function lineOption(x, series) {
      return { tooltip: { trigger: 'axis', ...tooltipStyle },
               legend: series.length > 1 ? { textStyle: { color: '#94a0b4' } } : undefined,
               grid: { left: 56, right: 20, top: 30, bottom: 32 },
               xAxis: { type: 'category', data: x, ...axisStyle },
               yAxis: { type: 'value', ...axisStyle }, series };
    }
    const equitySeries = (name, data, color, width = 2) => ({
      name, type: 'line', showSymbol: false, data: data.map(r => num(r.equity)),
      lineStyle: { color, width } });

    watch(tab, t => {
      nextTick(() => {
        if (t === 'overview') loadOverview(ovTag.value || 'latest');
        else if (t === 'workbench') { if (!pool.value.length) loadWorkbench(); drawCbChart(); }
        else if (t === 'backtest' && btDetail.value.tag) loadBacktest(btDetail.value.tag);
        else if (t === 'factors') loadFactors();
        else if (t === 'risk') loadRisk();
        else if (t === 'combo') { if (cb.name === undefined) loadCombo(); drawRcChart(); }
      });
    });

    // ---------------- 总览 ----------------
    const comboMembers = () => {
      const m = Object.keys(ovCombo.value.members || {});
      return m.length ? m.map(k => factorName(k)).join(' + ') : '—';
    };

    async function loadOverview(tag) {
      const d = await get('/api/overview');
      if (d.error) return;
      ovRuns.value = d.runs || [];
      ovTag.value = tag === 'latest' ? (d.tag || '') : tag;
      ovMetrics.value = d.metrics || {};
      ovEquity.value = d.equity || [];
      ovCombo.value = d.combo || {};
      await nextTick();
      chart('ovChart', lineOption(
        ovEquity.value.map(r => r.trade_date),
        [equitySeries((ovCombo.value.name || '组合') + ' 净值', ovEquity.value, '#f05d56')]));
    }

    async function loadBacktest(tag) {
      btDetail.value = await get('/api/backtest/' + tag);
      await nextTick();
      if (btDetail.value.equity) {
        const e = btDetail.value.equity;
        chart('btChart', lineOption(e.map(r => r.trade_date),
          [equitySeries('净值', e, '#38bdf8')]));
      }
    }

    // ---------------- 因子检验 ----------------
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
        chart('icChart', lineOption(
          icSeries.value.map(r => r[Object.keys(r)[0]]),
          keys.map(k => ({ name: k, type: 'line', showSymbol: false,
            data: icSeries.value.map(r => num(r[k])), lineStyle: { width: 1.4 } }))));
      }
      if (quantile.value.length) {
        const factors = [...new Set(quantile.value.map(r => r.factor))];
        chart('qrChart', {
          tooltip: { ...tooltipStyle }, legend: { textStyle: { color: '#94a0b4' } },
          grid: { left: 56, right: 20, top: 30, bottom: 32 },
          xAxis: { type: 'category', data: ['1', '2', '3', '4', '5'], ...axisStyle },
          yAxis: { type: 'value', ...axisStyle },
          series: factors.map(f => ({
            name: f, type: 'bar',
            data: [1, 2, 3, 4, 5].map(g => {
              const r = quantile.value.find(q => q.factor === f && Number(q['组']) === g);
              return r ? num(r['mean_return']) * 100 : null;
            }),
          })),
        });
      }
    }

    // ---------------- 风控与监控 ----------------
    async function loadRisk() {
      const d = await get('/api/risk');
      if (d.error) return;
      rtag.value = d.tag || '';
      riskReport.value = d.report || [];
      riskEvents.value = d.events || [];
      const p = d.equity_plain || [], r = d.equity_risk || [];
      await nextTick();
      if (p.length && r.length) {
        chart('riskChart', lineOption(p.map(x => x.trade_date), [
          equitySeries('无风控', p, '#f05d56', 1.6),
          equitySeries('有风控', r, '#38bdf8')]));
      }
    }

    async function loadMonitor() { mon.value = await get('/api/monitor'); }

    async function runRisk() {
      running.value = true;
      const body = {
        tag: (cbRes.value && cbRes.value.tag) || '',
        single_stock_drawdown: num(riskParm.single_stock_drawdown),
        trailing_stop: num(riskParm.trailing_stop),
        drawdown_trigger: num(riskParm.drawdown_trigger),
      };
      const r = await fetch('/api/run/risk', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
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

    // ---------------- 因子清单（只读视图，工作台负责编辑） ----------------
    const fc = ref({});

    async function loadFactorConfig() {
      fc.value = await get('/api/factor-config') || {};
    }

    // ---------------- 因子工作台 ----------------
    const pool = ref([]);              // 12 个基础因子
    const wbSel = reactive({});        // 是否选中
    const wbWeight = reactive({});     // 原始权重
    const wbTouched = reactive({});    // 是否改过权重（未改默认 1.0）
    const cb = reactive({});           // combo 配置（GET /api/combo-config）
    const cbStart = ref(''); const cbEnd = ref('');
    const cbMsg = ref(''); const cbRunning = ref(false);
    const cbRes = ref({});
    const cbSel = reactive({});        // 配方模式成员勾选
    const cbWeight = reactive({});     // 配方模式成员权重

    const wbSelected = computed(() => Object.keys(wbSel).filter(k => wbSel[k]));
    const subW = sw => Object.entries(sw || {}).slice(0, 4)
      .map(([k, v]) => k + ' ' + (v * 100).toFixed(0) + '%').join(' · ') + ' …';
    function cbOk() { return Object.keys(cbSel).some(k => cbSel[k]); }

    // ---------------- 自定义因子（表达式） ----------------
    const facEdit = reactive({
      open: false, isNew: true, saving: false, msg: '', ok: false,
      form: { key: '', name: '', expr: '', desc: '' }, vars: {}, funcs: {},
    });
    const customCount = computed(() => pool.value.filter(f => f.custom).length);

    function openFacEditor(f) {
      facEdit.msg = ''; facEdit.ok = false;
      facEdit.vars = {};
      facEdit.funcs = {};
      if (f) {                       // 编辑
        facEdit.isNew = false;
        facEdit.form = { key: f.key, name: f.name, expr: f.expr || '', desc: f.desc || '' };
      } else {                       // 新建：自动分配不冲突的 key
        facEdit.isNew = true;
        let i = 1;
        const exist = new Set(pool.value.map(x => x.key));
        while (exist.has('fac' + i)) i += 1;
        facEdit.form = { key: 'fac' + i, name: '', expr: '', desc: '' };
      }
      get('/api/factor-pool').then(d => {
        if (d && !d.error) { facEdit.vars = d.variables || {}; facEdit.funcs = d.functions || {}; }
      });
      facEdit.open = true;
    }

    function _customList() {
      return pool.value.filter(f => f.custom)
        .map(f => ({ key: f.key, name: f.name, expr: f.expr, desc: f.desc || '' }));
    }

    async function saveFactor() {
      const fm = facEdit.form;
      if (!fm.name.trim()) { facEdit.msg = '请填写因子名称'; facEdit.ok = false; return; }
      if (!fm.expr.trim()) { facEdit.msg = '请填写表达式'; facEdit.ok = false; return; }
      let list = _customList();
      if (facEdit.isNew) {
        list = list.filter(f => f.key !== fm.key.trim());
        list.push({ key: fm.key.trim(), name: fm.name.trim(),
                    expr: fm.expr.trim(), desc: fm.desc.trim() });
      } else {
        list = list.map(f => f.key === fm.key
          ? { ...f, name: fm.name.trim(), expr: fm.expr.trim(), desc: fm.desc.trim() } : f);
      }
      facEdit.saving = true; facEdit.msg = '试算校验中…'; facEdit.ok = false;
      const r = await fetch('/api/custom-factors', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ factors: list }),
      }).then(x => x.json()).catch(() => ({ error: '网络错误' }));
      facEdit.saving = false;
      if (r.error) { facEdit.msg = r.error; return; }
      facEdit.msg = '已保存（备份 ' + r.backup + '）'; facEdit.ok = true;
      await loadWorkbench();
      setTimeout(() => { facEdit.open = false; }, 500);
    }

    async function _deleteFactorReq(key) {
      const list = _customList().filter(f => f.key !== key);
      const r = await fetch('/api/custom-factors', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ factors: list }),
      }).then(x => x.json()).catch(() => ({ error: '网络错误' }));
      if (r.error) return r.error;
      wbSel[key] = false; delete wbWeight[key];
      await loadWorkbench();
      return '';
    }

    async function removeFactor(f) {
      if (!confirm('确定删除自定义因子「' + f.name + '」（' + f.key + '）？该操作不可恢复。')) return;
      const err = await _deleteFactorReq(f.key);
      if (err) alert('删除失败：' + err);
    }

    async function deleteFactor() {
      const key = facEdit.form.key;
      const f = _customList().find(x => x.key === key);
      if (!confirm('确定删除自定义因子「' + (f ? f.name : key) + '」（' + key + '）？该操作不可恢复。')) return;
      facEdit.saving = true; facEdit.msg = '删除中…'; facEdit.ok = false;
      const err = await _deleteFactorReq(key);
      facEdit.saving = false;
      if (err) { facEdit.msg = err; return; }
      facEdit.open = false;
    }

    function factorName(key) {
      const f = pool.value.find(x => x.key === key);
      if (f) return f.name;
      const meta = (cb.factor_meta || {})[key];
      return meta ? meta.name : key;
    }
    function catColor(c) {
      return { 价格: 'blue', 红利: 'amber', 价值: 'teal', 质量: 'green',
               风险: 'gray', 交易: 'red', 规模: 'blue', 自定义: 'teal' }[c] || 'gray';
    }
    function wbToggle(key) {
      wbSel[key] = !wbSel[key];
      if (wbSel[key] && !(key in wbWeight)) wbWeight[key] = 1.0;
    }
    function wbTouch(key) { wbTouched[key] = true; }
    function wbWeightOf(key) { return wbTouched[key] ? num(wbWeight[key]) : 1.0; }
    function wbNormPct(key) {
      const tot = wbSelected.value.reduce((s, k) => s + wbWeightOf(k), 0);
      return tot > 0 ? (wbWeightOf(key) / tot * 100).toFixed(1) + '%' : '-';
    }
    function wbEqual() {
      for (const k of wbSelected.value) { wbWeight[k] = 1.0; wbTouched[k] = true; }
    }

    async function loadWorkbench() {
      const d = await get('/api/factor-pool');
      pool.value = (d && d.factors) || [];
      for (const f of pool.value) if (!(f.key in wbSel)) wbSel[f.key] = false;
      await loadCombo();          // 顺带取 combo.yaml（参数区共用）
      // 预选：当前已是自定义混合则回显；否则不预选
      const blend = cb.custom_blend || {};
      for (const k of Object.keys(blend)) { wbSel[k] = true; wbWeight[k] = blend[k]; wbTouched[k] = true; }
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
        cbWeight[f.name] = d.members[f.name] ?? 1.0;
      }
      cbStart.value = d.period.start; cbEnd.value = d.period.end;
    }

    async function saveComboConfig(payload) {
      const r = await fetch('/api/combo-config', {
        method: 'PUT', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      }).then(x => x.json());
      if (r.error) { cbMsg.value = '保存失败: ' + r.error; return false; }
      await loadCombo();
      return true;
    }

    function pollComboRun() {
      return new Promise(resolve => {
        fetch('/api/run/combo', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({}),
        }).then(x => x.json()).then(rr => {
          const t0 = Date.now();
          const iv = setInterval(async () => {
            const s = await get('/api/task/' + rr.task_id);
            const sec = Math.round((Date.now() - t0) / 1000);
            cbMsg.value = '回测运行中… ' + sec + 's（走面板缓存，秒级~2分钟）';
            if (s.status === 'done' || s.status === 'failed' || Date.now() - t0 > 700000) {
              clearInterval(iv); cbRunning.value = false;
              if (s.status === 'done') {
                await loadComboResults();
                loadOverview('latest');
                get('/api/backtests').then(d => { btList.value = d || []; });
                const mode = cbRes.value.config && cbRes.value.config.mode;
                const modeTxt = mode === 'custom_blend' ? '自定义混合' : '配方组合';
                cbMsg.value = '回测完成（' + sec + 's）✓ 结果 tag=' + (cbRes.value.tag || '?') + ' · ' + modeTxt;
                resolve(true);
              } else {
                cbMsg.value = '回测失败: ' + (s.error || '查看 startup.log');
                resolve(false);
              }
            }
          }, 2500);
        });
      });
    }

    async function runWorkbench() {
      if (!wbSelected.value.length) return;
      cbRunning.value = true; cbMsg.value = '保存配置…';
      const blend = {};
      for (const k of wbSelected.value) blend[k] = wbWeightOf(k);
      const ok = await saveComboConfig({
        custom_blend: blend,
        top_n: cb.top_n, max_weight: cb.max_weight,
        period: { start: cbStart.value, end: cbEnd.value },
      });
      if (!ok) return;
      cbMsg.value = '配置已保存，回测启动…';
      await pollComboRun();
    }

    async function runRecipe() {
      if (!cb.members) return;
      cbRunning.value = true; cbMsg.value = '保存配置…';
      const members = {};
      for (const k in cbSel) if (cbSel[k]) members[k] = num(cbWeight[k]) || 1.0;
      const ok = await saveComboConfig({
        name: cb.name, method: cb.method, members,
        top_n: cb.top_n, max_weight: cb.max_weight,
        period: { start: cbStart.value, end: cbEnd.value },
        custom_blend: {},            // 切回配方模式
      });
      if (!ok) return;
      cbMsg.value = '配置已保存，回测启动…';
      await pollComboRun();
    }

    async function loadComboResults() {
      let d;
      try { d = await get('/api/combo-results'); }
      catch (e) { return; }
      cbRes.value = d || {};
      if (d && d.error) return;
      await nextTick();
      drawCbChart(); drawRcChart();
    }
    function drawCbChart() {
      const e = cbRes.value.equity;
      if (e && e.length) chart('cbChart', lineOption(e.map(r => r.trade_date),
        [equitySeries('净值', e, '#22d3ee')]));
    }
    function drawRcChart() {
      const e = cbRes.value.equity;
      if (e && e.length) chart('rcChart', lineOption(e.map(r => r.trade_date),
        [equitySeries('净值', e, '#d9a13d')]));
    }

    onMounted(() => {
      const jobs = [
        loadOverview('latest'),
        get('/api/backtests').then(d => { btList.value = d || []; }),
        loadFactors(), loadRisk(), loadMonitor(), loadFactorConfig(),
        loadWorkbench(), loadComboResults(),
      ];
      jobs.forEach(p => p && p.catch && p.catch(() => {}));
    });

    return { tab, pageTitle, ovTag, ovRuns, ovMetrics, ovEquity, ovCombo, comboMembers,
      btList, btDetail, icTable, icSeries, quantile, ftag,
      riskReport, riskEvents, rtag, mon, running, riskParm, num, pct, isPct, isLoss, fmtVal, fmtCell, fmtMetric,
      fc, pool, wbSel, wbWeight, wbSelected, wbToggle, wbTouch, wbNormPct, wbEqual,
      factorName, catColor, customCount,
      facEdit, openFacEditor, saveFactor, deleteFactor, removeFactor,
      cb, cbSel, cbWeight, cbStart, cbEnd, cbMsg, cbRunning, cbRes,
      cbOk, subW, runWorkbench, runRecipe, loadOverview, loadBacktest, runRisk };
  },
});
app.mount('#app');
