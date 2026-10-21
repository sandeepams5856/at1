/* at1 dashboard front end.
 *
 * Five stacked panes (price, volume, RSI, MACD, composite score) rather than one
 * chart with several price scales - a second y-axis on the same plot makes two
 * unrelated scales look comparable, which is the fastest way to misread a chart.
 * The panes share one time scale and one crosshair, so reading across them is
 * still a single gesture.
 *
 * Every pane carries a legend whose values track the crosshair, so a line is
 * never identified by colour alone.
 */
'use strict';

const LWC = window.LightweightCharts;

/* ---------- palette (read from CSS so the theme toggle drives the charts) --- */

const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();

function palette() {
  return {
    surface: css('--surface-1'),
    text: css('--text-secondary'),
    textStrong: css('--text-primary'),
    muted: css('--text-muted'),
    grid: css('--grid'),
    axis: css('--axis'),
    good: css('--good'),
    critical: css('--critical'),
    warning: css('--warning'),
    s1: css('--series-1'),
    s2: css('--series-2'),
    s3: css('--series-3'),
    s4: css('--series-4'),
    s5: css('--series-5'),
  };
}

/* ---------- small helpers -------------------------------------------------- */

const $ = (id) => document.getElementById(id);
const IN = (n, d = 2) =>
  n === null || n === undefined || Number.isNaN(n)
    ? '—'
    : Number(n).toLocaleString('en-IN', { minimumFractionDigits: d, maximumFractionDigits: d });

function compact(n) {
  if (n === null || n === undefined || Number.isNaN(n)) return '—';
  const a = Math.abs(n);
  if (a >= 1e7) return (n / 1e7).toFixed(2) + ' Cr';
  if (a >= 1e5) return (n / 1e5).toFixed(2) + ' L';
  if (a >= 1e3) return (n / 1e3).toFixed(1) + 'K';
  return String(Math.round(n));
}

const GLYPH = { 'STRONG BUY': '▲', BUY: '▲', HOLD: '●', SELL: '▼', 'STRONG SELL': '▼', 'NO DATA': '–', Buy: '▲', Sell: '▼', Neutral: '●', 'n/a': '–' };
const toneOf = (v) => (v === 'BUY' || v === 'STRONG BUY' ? 'buy' : v === 'SELL' || v === 'STRONG SELL' ? 'sell' : 'hold');
const chip = (label) => `<span class="chip" data-sig="${label}">${GLYPH[label] || ''} ${label}</span>`;

function istLabel(t, intraday) {
  // Times arriving from the API are already shifted to IST wall clock, so read
  // them back as UTC to avoid the browser's own zone shifting them twice.
  const d = new Date(t * 1000);
  const date = d.toLocaleDateString('en-IN', { day: '2-digit', month: 'short', timeZone: 'UTC' });
  if (!intraday) return date;
  const time = d.toLocaleTimeString('en-IN', { hour: '2-digit', minute: '2-digit', hour12: false, timeZone: 'UTC' });
  return `${date} ${time}`;
}

function precisionFor(price) {
  const a = Math.abs(price || 0);
  if (a < 5) return 4;
  if (a < 100) return 2;
  return 2;
}

/* ---------- state --------------------------------------------------------- */

const state = {
  universe: null,
  mode: 'stocks',          // 'stocks' | 'options' | 'bench'
  group: 'indices',
  timeframe: '1d',
  symbol: '^NSEI',
  optionSymbol: 'NIFTY',
  optionExpiry: null,
  search: '',
  sort: 'default',
  analysis: null,
  chainRows: [],
  targetStrike: null,      // planner: which strike is being planned
  targetLeg: null,         // planner: CE or PE, defaulting to the chain's own lean
  plan: null,
  // The bench: `null` legs mean "never been seeded", so the server opens on the
  // ITM/ATM/OTM ladder; `[]` means the user cleared it and it stays cleared.
  bench: null,
  benchLegs: null,
  benchFocus: 2,
  autoTimer: null,
  lastAuto: 0,
};

/* ---------- charts -------------------------------------------------------- */

const charts = {};        // pane name -> { chart, series:{}, maps:{} }
let syncing = false;

function baseOptions(p, { height, timeVisible, axis = true, logo = false }) {
  return {
    height,
    layout: {
      background: { type: 'solid', color: p.surface },
      textColor: p.text,
      fontFamily: 'system-ui, -apple-system, "Segoe UI", sans-serif',
      fontSize: 11,
      attributionLogo: logo,
    },
    grid: { vertLines: { color: p.grid }, horzLines: { color: p.grid } },
    // A fixed scale width is what keeps the five panes' plot areas in one
    // column; otherwise "27,000.00" and "80.0" produce different left edges and
    // reading straight down the crosshair stops working.
    rightPriceScale: {
      borderColor: p.axis, minimumWidth: 74, scaleMargins: { top: 0.12, bottom: 0.12 },
    },
    // Only the top and bottom panes carry a date axis - four repeats of the same
    // axis is 120px of chrome saying nothing new.
    timeScale: {
      borderColor: p.axis, timeVisible, secondsVisible: false, rightOffset: 4,
      visible: axis,
    },
    crosshair: {
      mode: LWC.CrosshairMode.Normal,
      vertLine: { color: p.muted, width: 1, style: 3, labelBackgroundColor: p.axis },
      horzLine: { color: p.muted, width: 1, style: 3, labelBackgroundColor: p.axis },
    },
    handleScale: { axisPressedMouseMove: { time: true, price: false } },
    localization: { locale: 'en-IN' },
  };
}

function destroyCharts() {
  Object.values(charts).forEach((c) => c.chart.remove());
  for (const k of Object.keys(charts)) delete charts[k];
}

function buildCharts(data, intraday) {
  destroyCharts();
  const p = palette();
  const lastClose = data.candles.length ? data.candles[data.candles.length - 1].close : 100;
  const precision = precisionFor(lastClose);
  const priceFormat = { type: 'price', precision, minMove: Math.pow(10, -precision) };

  /* -- price pane -- */
  const priceChart = LWC.createChart($('chart-price'), baseOptions(p, { height: 392, timeVisible: intraday, logo: true }));
  const candles = priceChart.addCandlestickSeries({
    upColor: p.good, downColor: p.critical,
    borderUpColor: p.good, borderDownColor: p.critical,
    wickUpColor: p.good, wickDownColor: p.critical,
    priceFormat,
  });
  candles.setData(data.candles);

  const mkLine = (chart, points, color, opts = {}) => {
    const s = chart.addLineSeries({
      color, lineWidth: opts.width || 2, lineStyle: opts.style || 0,
      priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false,
      priceFormat,
    });
    s.setData(points || []);
    return s;
  };

  const priceSeries = {
    ema9: mkLine(priceChart, data.ema9, p.s1),
    ema21: mkLine(priceChart, data.ema21, p.s2),
    ema50: mkLine(priceChart, data.ema50, p.s3),
    ema200: mkLine(priceChart, data.ema200, p.s4),
    vwap: mkLine(priceChart, data.vwap, p.s5, { style: 2 }),
    bbUpper: mkLine(priceChart, data.bb_upper, p.muted, { width: 1 }),
    bbLower: mkLine(priceChart, data.bb_lower, p.muted, { width: 1 }),
    supertrend: mkLine(priceChart, data.supertrend, p.muted, { width: 1, style: 1 }),
  };

  charts.price = {
    chart: priceChart,
    series: { candles, ...priceSeries },
    maps: {
      candles: indexBy(data.candles),
      ema9: indexBy(data.ema9), ema21: indexBy(data.ema21),
      ema50: indexBy(data.ema50), ema200: indexBy(data.ema200),
      vwap: indexBy(data.vwap),
    },
  };

  /* -- volume pane -- */
  const volChart = LWC.createChart($('chart-volume'), baseOptions(p, { height: 128, timeVisible: intraday, axis: false }));
  const volSeries = volChart.addHistogramSeries({
    priceFormat: { type: 'volume' },
    priceScaleId: 'right',
  });
  volSeries.setData((data.volume || []).map((v) => ({
    time: v.time, value: v.value,
    color: v.up ? p.good + 'aa' : p.critical + 'aa',
  })));
  const volAvg = volChart.addLineSeries({
    color: p.text, lineWidth: 2, priceLineVisible: false,
    lastValueVisible: false, crosshairMarkerVisible: false,
    priceFormat: { type: 'volume' },
  });
  volAvg.setData(data.vol_sma || []);
  charts.volume = {
    chart: volChart, series: { volSeries, volAvg },
    maps: { volume: indexBy(data.volume), vol_sma: indexBy(data.vol_sma) },
  };

  /* -- RSI pane -- */
  const rsiChart = LWC.createChart($('chart-rsi'), baseOptions(p, { height: 128, timeVisible: intraday, axis: false }));
  const rsi = rsiChart.addLineSeries({
    color: p.s1, lineWidth: 2, priceLineVisible: false, lastValueVisible: true,
    priceFormat: { type: 'price', precision: 1, minMove: 0.1 },
  });
  rsi.setData(data.rsi || []);
  [[70, p.critical, 'overbought 70'], [50, p.axis, 'midline 50'], [30, p.good, 'oversold 30']]
    .forEach(([v, color, title]) => rsi.createPriceLine({
      price: v, color, lineWidth: 1, lineStyle: 2, axisLabelVisible: false, title,
    }));
  rsiChart.priceScale('right').applyOptions({ autoScale: false });
  rsi.applyOptions({ autoscaleInfoProvider: () => ({ priceRange: { minValue: 0, maxValue: 100 } }) });
  charts.rsi = { chart: rsiChart, series: { rsi }, maps: { rsi: indexBy(data.rsi) } };

  /* -- MACD pane -- */
  const macdChart = LWC.createChart($('chart-macd'), baseOptions(p, { height: 128, timeVisible: intraday, axis: false }));
  const hist = macdChart.addHistogramSeries({ priceFormat: { type: 'price', precision, minMove: Math.pow(10, -precision) } });
  hist.setData((data.macd_hist || []).map((d) => ({
    time: d.time, value: d.value, color: d.value >= 0 ? p.good + 'aa' : p.critical + 'aa',
  })));
  const macdLine = mkLine(macdChart, data.macd, p.s1);
  const macdSignal = mkLine(macdChart, data.macd_signal, p.s2);
  charts.macd = {
    chart: macdChart, series: { hist, macdLine, macdSignal },
    maps: { macd: indexBy(data.macd), macd_signal: indexBy(data.macd_signal), macd_hist: indexBy(data.macd_hist) },
  };

  /* -- composite score pane -- */
  const scoreChart = LWC.createChart($('chart-score'), baseOptions(p, { height: 118, timeVisible: intraday }));
  const score = scoreChart.addHistogramSeries({
    priceFormat: { type: 'price', precision: 0, minMove: 1 },
  });
  score.setData((data.score || []).map((d) => ({
    time: d.time, value: d.value,
    color: d.value >= 45 ? p.good : d.value >= 15 ? p.good + '99'
      : d.value <= -45 ? p.critical : d.value <= -15 ? p.critical + '99' : p.muted + '77',
  })));
  [[45, p.good, 'strong buy'], [15, p.good, 'buy'], [-15, p.critical, 'sell'], [-45, p.critical, 'strong sell']]
    .forEach(([v, color, title]) => score.createPriceLine({
      price: v, color, lineWidth: 1, lineStyle: 2, axisLabelVisible: false, title,
    }));
  charts.score = { chart: scoreChart, series: { score }, maps: { score: indexBy(data.score) } };

  applyMarkers();
  applyToggles();
  syncPanes();
  wireCrosshair(intraday);
  priceChart.timeScale().fitContent();
}

function indexBy(arr) {
  const m = new Map();
  (arr || []).forEach((d) => m.set(d.time, d));
  return m;
}

function syncPanes() {
  const list = Object.values(charts);
  list.forEach(({ chart }) => {
    chart.timeScale().subscribeVisibleLogicalRangeChange((range) => {
      if (syncing || !range) return;
      syncing = true;
      list.forEach((o) => { if (o.chart !== chart) o.chart.timeScale().setVisibleLogicalRange(range); });
      syncing = false;
    });
  });
}

function wireCrosshair(intraday) {
  const panes = Object.entries(charts);
  panes.forEach(([name, { chart }]) => {
    chart.subscribeCrosshairMove((param) => {
      updateLegends(param.time, intraday);
      // Mirror the crosshair into the other panes so one hover reads across all.
      panes.forEach(([other, o]) => {
        if (other === name) return;
        if (param.time === undefined) { o.chart.clearCrosshairPosition(); return; }
        const key = Object.keys(o.series)[0];
        const pt = o.maps[Object.keys(o.maps)[0]]?.get(param.time);
        const v = pt ? (pt.value ?? pt.close ?? 0) : 0;
        o.chart.setCrosshairPosition(v, param.time, o.series[key]);
      });
    });
  });
}

/* ---------- legends ------------------------------------------------------- */

function legendRow(items) {
  return items.map((i) => `<span class="key"><span class="swatch${i.dot ? ' dot' : ''}" style="background:${i.color}${i.dash ? ';opacity:.8' : ''}"></span>${i.label} <span class="val">${i.value}</span></span>`).join('');
}

function updateLegends(time, intraday) {
  const a = state.analysis;
  if (!a) return;
  const p = palette();
  const d = a.chart;
  // With the crosshair on a bar, read that bar; with it off the chart, read the
  // latest bar - so the legend always shows numbers rather than dashes.
  const at = (map) => (map && time !== undefined ? map.get(time) : undefined);
  const last = (arr) => (arr && arr.length ? arr[arr.length - 1] : undefined);

  const c = at(charts.price?.maps.candles) || last(d.candles);
  const dec = precisionFor(c ? c.close : 100);
  const pick = (name) => {
    const hit = at(charts.price?.maps[name]) || last(d[name]);
    return hit ? IN(hit.value, dec) : '—';
  };

  const stamp = c ? istLabel(c.time, intraday) : '';
  $('legend-price').innerHTML =
    `<span class="key" style="color:var(--text-muted)">${stamp}</span>` +
    `<span class="key">O <span class="val">${IN(c?.open, dec)}</span></span>` +
    `<span class="key">H <span class="val">${IN(c?.high, dec)}</span></span>` +
    `<span class="key">L <span class="val">${IN(c?.low, dec)}</span></span>` +
    `<span class="key">C <span class="val">${IN(c?.close, dec)}</span></span>` +
    legendRow([
      { color: p.s1, label: 'EMA 9', value: pick('ema9') },
      { color: p.s2, label: 'EMA 21', value: pick('ema21') },
      { color: p.s3, label: 'EMA 50', value: pick('ema50') },
      { color: p.s4, label: 'EMA 200', value: pick('ema200') },
      { color: p.s5, label: 'VWAP', value: pick('vwap'), dash: true },
    ]);

  if (a.volume_available) {
    const v = at(charts.volume?.maps.volume) || last(d.volume);
    const av = at(charts.volume?.maps.vol_sma) || last(d.vol_sma);
    $('legend-volume').innerHTML = legendRow([
      { color: v && v.up ? p.good : p.critical, label: 'Volume', value: compact(v?.value), dot: true },
      { color: p.text, label: 'Avg 20', value: compact(av?.value) },
    ]);
  } else {
    $('legend-volume').innerHTML = '<span class="key" style="color:var(--text-muted)">Feed reports no volume for this symbol — volume measures abstain</span>';
  }

  const r = at(charts.rsi?.maps.rsi) || last(d.rsi);
  const rv = r ? r.value : null;
  const rsiRead = rv === null ? '' : rv >= 70 ? ' (overbought)' : rv <= 30 ? ' (oversold)' : rv >= 50 ? ' (bullish half)' : ' (bearish half)';
  $('legend-rsi').innerHTML = legendRow([{ color: p.s1, label: 'RSI 14', value: (rv === null ? '—' : IN(rv, 1)) + rsiRead }]);

  const ml = at(charts.macd?.maps.macd) || last(d.macd);
  const ms = at(charts.macd?.maps.macd_signal) || last(d.macd_signal);
  const mh = at(charts.macd?.maps.macd_hist) || last(d.macd_hist);
  $('legend-macd').innerHTML = legendRow([
    { color: p.s1, label: 'MACD', value: IN(ml?.value, dec) },
    { color: p.s2, label: 'Signal', value: IN(ms?.value, dec) },
    { color: (mh?.value ?? 0) >= 0 ? p.good : p.critical, label: 'Histogram', value: IN(mh?.value, dec), dot: true },
  ]);

  const sc = at(charts.score?.maps.score) || last(d.score);
  const sv = sc ? sc.value : null;
  const band = sv === null ? 'no data' : sv >= 45 ? 'STRONG BUY' : sv >= 15 ? 'BUY' : sv > -15 ? 'HOLD' : sv > -45 ? 'SELL' : 'STRONG SELL';
  $('legend-score').innerHTML = legendRow([
    { color: sv >= 15 ? p.good : sv <= -15 ? p.critical : p.warning, label: 'Score', value: `${sv === null ? '—' : IN(sv, 0)} · ${band}`, dot: true },
  ]);
}

/* ---------- markers & toggles -------------------------------------------- */

function applyMarkers() {
  if (!charts.price) return;
  const on = document.querySelector('[data-series="markers"]').checked;
  const p = palette();
  // Only the 20 most recent flips, and only the strong ones get a text label.
  // Labelling every flip with its score turned the chart into overlapping text;
  // the arrow's direction and position already say which side it is, and the
  // exact score for any bar is in the composite pane and the bars table.
  const marks = on
    ? (state.analysis.chart.markers || []).slice(-20).map((m) => ({
        time: m.time,
        position: m.side === 'BUY' ? 'belowBar' : 'aboveBar',
        color: m.side === 'BUY' ? p.good : p.critical,
        shape: m.side === 'BUY' ? 'arrowUp' : 'arrowDown',
        text: m.strong ? m.side : '',
      }))
    : [];
  charts.price.series.candles.setMarkers(marks);
}

function applyToggles() {
  if (!charts.price) return;
  const s = charts.price.series;
  const get = (k) => document.querySelector(`[data-series="${k}"]`).checked;
  s.ema9.applyOptions({ visible: get('ema9') });
  s.ema21.applyOptions({ visible: get('ema21') });
  s.ema50.applyOptions({ visible: get('ema50') });
  s.ema200.applyOptions({ visible: get('ema200') });
  s.vwap.applyOptions({ visible: get('vwap') });
  s.bbUpper.applyOptions({ visible: get('bb') });
  s.bbLower.applyOptions({ visible: get('bb') });
  s.supertrend.applyOptions({ visible: get('supertrend') });
}

/* ---------- render: verdict, plan, measures ------------------------------ */

function renderAnalysis(a) {
  state.analysis = a;
  const v = a.verdict;
  const snap = a.snapshot;

  $('inst-name').textContent = a.meta.name;
  $('inst-sym').textContent = `${a.symbol} · ${a.meta.exchange} · ${a.timeframe.label} · ${snap.bars} bars`;
  const dec = precisionFor(snap.ltp);
  $('inst-ltp').textContent = IN(snap.ltp, dec);
  const up = (snap.change ?? 0) >= 0;
  $('inst-chg').innerHTML = `<span class="${up ? 'up' : 'down'}">${up ? '▲' : '▼'} ${IN(Math.abs(snap.change), dec)} (${IN(snap.change_pct, 2)}%)</span>`;

  $('verdict-card').dataset.tone = toneOf(v.label);
  $('verdict-glyph').textContent = GLYPH[v.label] || '●';
  $('verdict-glyph').style.color = `var(--${toneOf(v.label) === 'buy' ? 'good' : toneOf(v.label) === 'sell' ? 'critical' : 'warning'})`;
  $('verdict-label').textContent = v.label;
  $('verdict-score').textContent = v.score === null ? '—' : (v.score > 0 ? '+' : '') + IN(v.score, 1);
  $('verdict-cov').textContent = v.coverage < 100 ? `· ${v.coverage}% of measures have data` : '';
  $('tally-buy').textContent = v.counts.buy;
  $('tally-sell').textContent = v.counts.sell;
  $('tally-neutral').textContent = v.counts.neutral;
  $('score-needle').style.left = `${((v.score ?? 0) + 100) / 2}%`;

  // trade plan
  const p = a.plan;
  const rows = [
    ['Side', p.side === 'WAIT' ? chip('WAIT') : `<span class="${p.side === 'LONG' ? 'up' : 'down'}">${p.side}</span>`],
    ['Entry (last)', IN(p.entry, dec)],
    ['Stop loss', p.stop === null ? '—' : IN(p.stop, dec)],
    ['Target 1 (1.5R)', p.target1 === null ? '—' : IN(p.target1, dec)],
    ['Target 2 (3R)', p.target2 === null ? '—' : IN(p.target2, dec)],
    ['Risk / unit', p.risk === null ? '—' : IN(p.risk, dec)],
    ['ATR (14)', IN(p.atr, dec)],
    ['Support (20-bar)', IN(p.support, dec)],
    ['Resistance (20-bar)', IN(p.resistance, dec)],
  ];
  $('plan-kv').innerHTML = rows.map(([k, val]) => `<dt>${k}</dt><dd>${val}</dd>`).join('');

  const bullet = (items) => items.length
    ? items.map((e) => `<li data-tone="${e.tone}"><span class="glyph">${e.tone === 'up' ? '▲' : e.tone === 'down' ? '▼' : '●'}</span><span>${e.text}</span></li>`).join('')
    : '<li data-tone="flat"><span class="glyph">●</span><span>Nothing notable on the latest bars.</span></li>';
  $('event-list').innerHTML = bullet(a.events);
  $('driver-list').innerHTML = bullet(a.drivers.map((d) => ({
    tone: d.signal === 'Buy' ? 'up' : d.signal === 'Sell' ? 'down' : 'flat',
    text: `${d.label} — ${d.signal.toLowerCase()} (${d.value})`,
  })));

  // measures table, grouped
  const groups = {};
  a.measures.forEach((m) => { (groups[m.group] ||= []).push(m); });
  const pal = palette();
  let html = '';
  for (const [group, items] of Object.entries(groups)) {
    html += `<tr class="group-head"><td colspan="6">${group}</td></tr>`;
    for (const m of items) {
      const sc = m.score;
      const width = sc === null ? 0 : Math.abs(sc) * 50;
      const color = sc === null ? pal.muted : sc > 0 ? pal.good : pal.critical;
      const bar = `<div class="bar-cell"><i style="${sc >= 0 ? 'left:50%' : `right:50%`};width:${width}%;background:${color}"></i></div>`;
      html += `<tr><td>${m.label}</td><td class="num">${m.value}</td><td>${chip(m.signal)}</td>`
        + `<td class="num">${sc === null ? '—' : (sc > 0 ? '+' : '') + sc.toFixed(2)}</td>`
        + `<td>${bar}</td><td class="num">${m.weight.toFixed(1)}</td></tr>`;
    }
  }
  $('measure-body').innerHTML = html;
  $('measure-count').textContent = `(${a.measures.length} indicators, weighted)`;

  // table view of the bars themselves
  const scoreMap = indexBy(a.chart.score);
  const tail = a.chart.candles.slice(-20).reverse();
  const volMap = indexBy(a.chart.volume);
  $('bars-body').innerHTML = tail.map((c) => {
    const s = scoreMap.get(c.time);
    const vol = volMap.get(c.time);
    return `<tr><td>${istLabel(c.time, a.timeframe.intraday)}</td>`
      + `<td class="num">${IN(c.open, dec)}</td><td class="num">${IN(c.high, dec)}</td>`
      + `<td class="num">${IN(c.low, dec)}</td><td class="num ${c.close >= c.open ? 'up' : 'down'}">${IN(c.close, dec)}</td>`
      + `<td class="num">${vol ? compact(vol.value) : '—'}</td>`
      + `<td class="num">${s ? IN(s.value, 0) : '—'}</td></tr>`;
  }).join('');

  buildCharts(a.chart, a.timeframe.intraday);
  updateLegends(undefined, a.timeframe.intraday);
}

/* ---------- API ----------------------------------------------------------- */

async function api(path) {
  const r = await fetch(path);
  if (!r.ok) {
    let detail = `HTTP ${r.status}`;
    try { detail = (await r.json()).detail || detail; } catch { /* keep status */ }
    const err = new Error(detail);
    err.status = r.status;   // the caller needs the code, not just FastAPI's prose
    throw err;
  }
  return r.json();
}

function showError(msg) {
  $('alert-slot').innerHTML = `<div class="error"><b>Could not load.</b> ${msg}</div>`;
}
const clearError = () => { $('alert-slot').innerHTML = ''; };

async function loadSymbol(symbol, { force = false } = {}) {
  state.symbol = symbol;
  markSelected();
  syncHash();
  clearError();
  $('inst-name').textContent = 'Loading…';
  try {
    const a = await api(`/api/analyze?symbol=${encodeURIComponent(symbol)}&tf=${state.timeframe}${force ? '&force=true' : ''}`);
    renderAnalysis(a);
  } catch (e) {
    showError(e.message);
    $('inst-name').textContent = symbol;
  }
}

/* ---------- right-hand panel --------------------------------------------- */

function currentItems() {
  if (state.mode === 'options' || state.mode === 'bench') {
    return state.universe.option_underlyings.map((u) => ({
      symbol: u.code, name: u.name, exchange: u.type === 'Indices' ? 'NSE index' : 'NSE stock',
    }));
  }
  const g = state.universe.groups.find((x) => x.id === state.group);
  return g ? g.items : [];
}

function filterItems(items) {
  const q = state.search.trim().toLowerCase();
  if (!q) return items;
  return items.filter((i) => i.name.toLowerCase().includes(q) || i.symbol.toLowerCase().includes(q));
}

const wlScores = new Map();   // symbol -> watchlist row

function renderPanel() {
  const items = filterItems(currentItems());
  const list = $('watchlist');

  if (state.mode === 'options' || state.mode === 'bench') {
    $('panel-note').textContent = `${items.length} underlyings with listed options · live NSE chain`;
    list.innerHTML = items.map((i) => `
      <div class="wl-row" data-code="${i.symbol}" aria-selected="${i.symbol === state.optionSymbol}">
        <div class="nm">${i.symbol}</div>
        <div class="sub">${i.name} · ${i.exchange}</div>
      </div>`).join('');
    list.querySelectorAll('.wl-row').forEach((el) => {
      el.onclick = () => {
        state.optionSymbol = el.dataset.code;
        state.optionExpiry = null;
        if (state.mode === 'bench') {
          // Another underlying has different strikes, so the bench starts again
          // from its ladder rather than carrying meaningless codes across.
          state.benchLegs = null;
          loadBench();
        } else loadChain();
      };
    });
    return;
  }

  let rows = items.map((i) => ({ ...i, ...(wlScores.get(i.symbol) || {}) }));
  if (state.sort === 'buy') rows.sort((a, b) => (b.score ?? -999) - (a.score ?? -999));
  else if (state.sort === 'sell') rows.sort((a, b) => (a.score ?? 999) - (b.score ?? 999));
  else if (state.sort === 'gainers') rows.sort((a, b) => (b.change_pct ?? -999) - (a.change_pct ?? -999));
  else if (state.sort === 'losers') rows.sort((a, b) => (a.change_pct ?? 999) - (b.change_pct ?? 999));

  const g = state.universe.groups.find((x) => x.id === state.group);
  $('panel-note').textContent = `${g ? g.note : ''} · scored on the ${state.timeframe} timeframe`;

  list.innerHTML = rows.map((i) => {
    const dec = precisionFor(i.ltp ?? 100);
    const pct = i.change_pct;
    const cls = pct === null || pct === undefined ? 'flat' : pct > 0 ? 'up' : pct < 0 ? 'down' : 'flat';
    return `
      <div class="wl-row" data-symbol="${i.symbol}" aria-selected="${i.symbol === state.symbol}">
        <div class="nm">${i.name}</div>
        <div class="px">${i.ltp === undefined || i.ltp === null ? '' : IN(i.ltp, dec)}</div>
        <div class="sub">${i.symbol}</div>
        <div class="pct ${cls}">${pct === null || pct === undefined ? '' : (pct > 0 ? '+' : '') + IN(pct, 2) + '%'}</div>
        ${i.verdict ? chip(i.verdict) : '<span class="chip" data-sig="n/a">– scoring…</span>'}
      </div>`;
  }).join('');

  list.querySelectorAll('.wl-row').forEach((el) => {
    el.onclick = () => loadSymbol(el.dataset.symbol);
  });
}

function markSelected() {
  document.querySelectorAll('#watchlist .wl-row').forEach((el) => {
    el.setAttribute('aria-selected', el.dataset.symbol === state.symbol || el.dataset.code === state.optionSymbol);
  });
}

async function refreshWatchlist() {
  if (state.mode === 'options') return;
  const items = currentItems();
  if (!items.length) return;
  const symbols = items.map((i) => i.symbol).join(',');
  try {
    const res = await api(`/api/watchlist?symbols=${encodeURIComponent(symbols)}&tf=${state.timeframe}`);
    res.rows.forEach((r) => wlScores.set(r.symbol, r));
    renderPanel();
  } catch (e) {
    $('panel-note').textContent = `Scoring failed: ${e.message}`;
  }
}

/* ---------- options view -------------------------------------------------- */

function setMode(mode) {
  state.mode = mode;
  ['stocks', 'options', 'bench'].forEach((m) =>
    $(`mode-${m}`).setAttribute('aria-pressed', m === mode));
  $('view-chart').hidden = mode !== 'stocks';
  $('view-options').hidden = mode !== 'options';
  $('view-bench').hidden = mode !== 'bench';
  $('group-select').style.display = mode === 'stocks' ? '' : 'none';
  $('sort-select').style.display = mode === 'stocks' ? '' : 'none';
  renderPanel();
  if (mode === 'options') loadChain();
  else if (mode === 'bench') loadBench();
  else refreshWatchlist();
}

async function loadChain() {
  markSelected();
  syncHash();
  $('oc-alert').innerHTML = '';
  $('oc-name').textContent = `${state.optionSymbol} — loading chain…`;
  const q = new URLSearchParams({
    symbol: state.optionSymbol, tf: state.timeframe, window: $('oc-window').value,
  });
  if (state.optionExpiry) q.set('expiry', state.optionExpiry);
  let c;
  try {
    c = await api(`/api/options/chain?${q}`);
  } catch (e) {
    $('oc-alert').innerHTML = `<div class="error"><b>Chain unavailable.</b> ${e.message}</div>`;
    return;
  }
  if (c.available === false) {
    $('oc-name').textContent = state.optionSymbol;
    $('oc-alert').innerHTML = `<div class="banner"><b>NSE did not serve the chain.</b> ${c.reason}. ${c.hint || ''}</div>`;
    return;
  }
  renderChain(c);
}

function renderChain(c) {
  state.chain = c;
  state.optionExpiry = c.expiry;
  const p = palette();

  $('oc-name').textContent = `${c.symbol} — ${c.name}`;
  // The shared header tracks whichever view is open.
  $('inst-name').textContent = `${c.symbol} options`;
  $('inst-sym').textContent = `NSE · ${c.name} · expiry ${c.expiry}`;
  $('inst-ltp').textContent = IN(c.spot, 2);
  $('inst-chg').innerHTML = `<span class="flat">spot · ATM ${c.atm}</span>`;
  $('oc-expiry').innerHTML = c.expiries.map((e) => `<option ${e === c.expiry ? 'selected' : ''}>${e}</option>`).join('');
  $('oc-stamp').textContent = c.timestamp ? `NSE stamp: ${c.timestamp}` : '';

  const v = c.signal;
  $('oc-verdict').dataset.tone = toneOf(v.verdict);
  $('oc-glyph').textContent = GLYPH[v.verdict] || '●';
  $('oc-label').textContent = v.verdict;
  $('oc-source').innerHTML = `direction from <b>${v.source}</b> · score <strong>${v.score > 0 ? '+' : ''}${IN(v.score, 1)}</strong>`
    + (c.chart_signal ? ` (${c.chart_signal.yahoo}, ${c.chart_signal.timeframe})` : '');

  const t = c.totals;
  const stats = [
    ['Spot', IN(c.spot, 2), `ATM strike ${c.atm}`],
    ['PCR (OI)', t.pcr === null ? '—' : IN(t.pcr, 2), t.pcr >= 1.3 ? 'bullish positioning' : t.pcr <= 0.7 ? 'bearish positioning' : 'balanced'],
    ['Max pain', c.max_pain ?? '—', 'expiry magnet'],
    ['OI support', c.oi_support ?? '—', 'biggest put wall'],
    ['OI resistance', c.oi_resistance ?? '—', 'biggest call wall'],
    ['Call OI', compact(t.ce_oi), `chg ${compact(t.ce_oi_change)}`],
    ['Put OI', compact(t.pe_oi), `chg ${compact(t.pe_oi_change)}`],
    ['ATM IV', c.atm_iv.ce ? `${IN(c.atm_iv.ce, 1)} / ${IN(c.atm_iv.pe, 1)}` : '—', 'CE / PE'],
  ];
  $('oc-stats').innerHTML = stats.map(([label, value, sub]) =>
    `<div class="stat"><div class="label">${label}</div><div class="value">${value}</div><div class="sub">${sub}</div></div>`).join('');

  const s = c.suggestion;
  $('oc-suggestion').innerHTML = `
    <div style="display:flex;align-items:center;gap:10px;margin-bottom:8px">
      ${chip(s.action === 'WAIT' ? 'WAIT' : (s.leg === 'CE' ? 'BUY' : 'SELL'))}
      <strong style="font-size:16px">${s.action}</strong>
    </div>
    <p style="margin:0 0 9px;color:var(--text-secondary)">${s.text}</p>
    ${s.leg ? `<dl class="kv">
      <dt>Strike</dt><dd>${s.strike}</dd>
      <dt>Premium (LTP)</dt><dd>${IN(s.premium, 2)}</dd>
      <dt>IV</dt><dd>${IN(s.iv, 1)}</dd>
      <dt>Breakeven at expiry</dt><dd>${s.breakeven ? IN(s.breakeven, 2) : '—'}</dd>
      <dt>Invalidation (spot)</dt><dd>${s.invalidate ?? '—'}</dd>
    </dl>` : ''}`;

  $('oc-notes').innerHTML = c.notes.map((n) =>
    `<li data-tone="${n.tone}"><span class="glyph">${n.tone === 'up' ? '▲' : n.tone === 'down' ? '▼' : '●'}</span><span>${n.text}</span></li>`).join('');

  renderChainTable();
  $('oc-sub').textContent = `· ${c.expiry} · ${c.rows.length} strikes around ATM ${c.atm}`;

  // The planner follows the chain: a new symbol, expiry or window re-plans with
  // whatever strike and leg are still valid.
  syncTargetForm(c);
  loadTargets();
}

function renderChainTable() {
  const chain = state.chain;
  if (!chain) return;
  const view = chain.rows;
  // OI bars are scaled against the largest OI on screen, so the column reads as
  // a depth profile: where the writers actually are.
  const maxOI = Math.max(1, ...view.flatMap((r) => [r.ce.oi || 0, r.pe.oi || 0]));

  const cell = (leg, side) => {
    const w = ((leg.oi || 0) / maxOI) * 100;
    return `<td class="num oi ${side}"><i style="width:${w}%;${side === 'ce' ? 'left:0' : 'right:0;left:auto'}"></i><span>${compact(leg.oi)}</span></td>`;
  };
  const chgCls = (n) => (n > 0 ? 'up' : n < 0 ? 'down' : 'flat');

  $('oc-body').innerHTML = view.map((r) => {
    return `<tr class="${r.strike === chain.atm ? 'atm' : ''}">
      ${cell(r.ce, 'ce')}
      <td class="num ${chgCls(r.ce.oi_change)}">${compact(r.ce.oi_change)}</td>
      <td class="num">${IN(r.ce.iv, 1)}</td>
      <td class="num">${IN(r.ce.ltp, 2)}</td>
      <td style="font-size:11px;color:var(--text-secondary)">${r.ce.buildup}</td>
      <td class="num"><strong>${r.strike}</strong></td>
      <td style="font-size:11px;color:var(--text-secondary)">${r.pe.buildup}</td>
      <td class="num">${IN(r.pe.ltp, 2)}</td>
      <td class="num">${IN(r.pe.iv, 1)}</td>
      <td class="num ${chgCls(r.pe.oi_change)}">${compact(r.pe.oi_change)}</td>
      ${cell(r.pe, 'pe')}
    </tr>`;
  }).join('');
}

/* ---------- target planner ------------------------------------------------ */
/* One question: where does the target go? Type the contract in, read back the
 * move each candidate target needs, how much has traded at that price, and how
 * often the move has happened before. Nothing here can send an order. */

function syncTargetForm(c) {
  const sel = $('tp-strike');
  const wanted = state.targetStrike ?? c.atm;
  const keep = c.rows.some((r) => r.strike === wanted) ? wanted : c.atm;
  state.targetStrike = keep;

  sel.innerHTML = c.rows.map((r) => {
    // Which side is in the money is a property of the strike, not of the leg
    // picked next to it, so the label names the side rather than saying ITM/OTM.
    const tag = r.strike === c.atm ? 'ATM' : r.strike < c.spot ? 'calls ITM' : 'puts ITM';
    return `<option value="${r.strike}" ${r.strike === keep ? 'selected' : ''}>${r.strike} · ${tag}</option>`;
  }).join('');

  // First visit takes the leg the chain signal already points at.
  if (!state.targetLeg) state.targetLeg = (c.suggestion && c.suggestion.leg) || 'CE';
  $('tp-leg').value = state.targetLeg;

  const row = c.rows.find((r) => r.strike === keep);
  const ltp = row ? row[state.targetLeg.toLowerCase()].ltp : null;
  $('tp-premium').placeholder = ltp ? `chain LTP ${IN(ltp, 2)}` : 'chain LTP';

  if (!$('tp-horizon').options.length) buildHorizonOptions('tp-horizon', 3);
}

function buildHorizonOptions(id, sessions, expirySessions) {
  const opts = [1, 2, 3, 5, 10].filter((n) => !expirySessions || n < expirySessions);
  if (expirySessions) opts.push(expirySessions);
  $(id).innerHTML = opts.map((n) => {
    const label = expirySessions && n === expirySessions
      ? `By expiry (${n} session${n === 1 ? '' : 's'})`
      : `${n} session${n === 1 ? '' : 's'}`;
    return `<option value="${n}" ${n === sessions ? 'selected' : ''}>${label}</option>`;
  }).join('');
}

async function loadTargets() {
  if (state.mode !== 'options' || !state.chain) return;
  state.targetStrike = Number($('tp-strike').value) || state.targetStrike;
  state.targetLeg = $('tp-leg').value;
  syncHash();

  const q = new URLSearchParams({
    symbol: state.optionSymbol,
    strike: state.targetStrike,
    leg: state.targetLeg,
    stop_pct: $('tp-stop').value || 25,
    qty: $('tp-qty').value || 1,
    tf: state.timeframe,
    window: $('oc-window').value,
  });
  if (state.optionExpiry) q.set('expiry', state.optionExpiry);
  if ($('tp-premium').value) q.set('premium', $('tp-premium').value);
  if ($('tp-horizon').value) q.set('horizon', $('tp-horizon').value);

  $('tp-sub').textContent = '· working…';
  let p;
  try {
    p = await api(`/api/options/targets?${q}`);
  } catch (e) {
    $('tp-sub').textContent = '';
    $('tp-body').innerHTML = `<tr><td colspan="11" class="flat">Could not plan targets: ${e.message}</td></tr>`;
    return;
  }
  if (p.available === false) {
    $('tp-sub').textContent = '';
    $('tp-body').innerHTML = `<tr><td colspan="11" class="flat">${p.reason}. ${p.hint || ''}</td></tr>`;
    return;
  }
  renderTargets(p);
}

function renderTargets(p) {
  state.plan = p;
  buildHorizonOptions('tp-horizon', p.horizon_sessions, p.sessions_to_expiry || undefined);
  $('tp-sub').textContent = `· ${p.contract} · ${p.horizon_sessions} session`
    + `${p.horizon_sessions === 1 ? '' : 's'} on ${p.timeframe_label} candles`;

  const dec = precisionFor(p.entry || 100);
  const stats = [
    ['Entry premium', IN(p.entry, dec), p.entry_source],
    ['Stop premium', IN(p.stop, dec), `−${IN(p.stop_pct, 0)}% of premium`],
    ['Spot now', IN(p.spot, 2), `ATM ${state.chain.atm}`],
    ['Spot at your stop', IN(p.spot_at_stop, 2),
      p.spot_move_at_stop_pct === null ? 'needs IV' : `${IN(p.spot_move_at_stop_pct, 2)}% away`],
    ['IV used', IN(p.iv, 1), p.iv_source],
    ['Time left', p.days_to_expiry === null ? '—' : `${p.days_to_expiry}d`,
      `${p.sessions_to_expiry} session${p.sessions_to_expiry === 1 ? '' : 's'} to ${p.expiry}`],
    ['Attempts counted', p.samples ? IN(p.samples, 0) : '—',
      p.samples >= p.min_samples ? 'past bars with a full horizon' : `under the ${p.min_samples} needed`],
  ];
  $('tp-stats').innerHTML = stats.map(([label, value, sub]) =>
    `<div class="stat"><div class="label">${label}</div><div class="value">${value}</div><div class="sub">${sub}</div></div>`).join('');

  const pct = (n, d = 1) => (n === null || n === undefined ? '—' : `${n > 0 ? '+' : ''}${IN(n, d)}%`);
  const rate = (n) => (n === null || n === undefined ? '—' : `${IN(n, 0)}%`);

  $('tp-body').innerHTML = p.rows.map((r) => {
    if (!r.reachable) {
      return `<tr class="tp-out"><td>${r.label}</td><td class="num">${IN(r.target, dec)}</td>
        <td colspan="9" class="flat">Unreachable at any spot — a put's premium is capped by its strike.</td></tr>`;
    }
    const share = r.spot_volume_share === null || r.spot_volume_share === undefined
      ? '' : `<div style="font-size:10.5px;color:var(--text-muted)">${IN(r.spot_volume_share, 1)}% of the profile</div>`;
    const atStrike = r.strike_near === null
      ? '<span class="flat">off chain</span>'
      : `${compact(r.volume_at_strike)}<div style="font-size:10.5px;color:var(--text-muted)">OI ${compact(r.oi_at_strike)} @ ${r.strike_near}</div>`;
    return `<tr class="${r.multiple === 2 ? 'tp-pick' : ''}">
      <td>${r.label}</td>
      <td class="num">${IN(r.target, dec)}</td>
      <td class="num up">${pct(r.move_pct)}</td>
      <td class="num">${r.gain === null || r.gain === undefined ? '—' : `₹${IN(r.gain, 0)}`}</td>
      <td class="num">${IN(r.spot_needed, 2)}</td>
      <td class="num">${pct(r.spot_move_pct, 2)}</td>
      <td class="num">${IN(r.spot_needed_late, 2)}</td>
      <td class="num">${compact(r.spot_volume_at)}${share}</td>
      <td class="num">${atStrike}</td>
      <td class="num">${rate(r.chance)}</td>
      <td class="num">${rate(r.chance_model)}</td>
    </tr>`;
  }).join('');

  $('tp-notes').innerHTML = p.notes.map((n) =>
    `<li data-tone="${n.tone}"><span class="glyph">${n.tone === 'down' ? '!' : '●'}</span><span>${n.text}</span></li>`).join('');

  renderProfile(p);
}

function renderProfile(p) {
  const card = $('tp-profile-card');
  if (!p.profile) { card.hidden = true; return; }
  card.hidden = false;
  const pr = p.profile;
  $('tp-profile-sub').textContent =
    `· last ${pr.bars} ${p.timeframe_label} bars of ${p.underlying || p.symbol} · bands of ${IN(pr.step, 2)}`;

  const colour = css('--series-1');
  $('tp-profile-body').innerHTML = pr.bands.map((b) => {
    const w = (b.volume / pr.max) * 100;
    const marks = b.marks.map((m) => {
      const kind = m === 'spot now' ? 'spot' : m === 'stop' ? 'stop' : 'target';
      return `<span class="tp-mark" data-kind="${kind}">← ${m}</span>`;
    }).join('');
    return `<tr>
      <td class="num">${IN(b.low, 2)} – ${IN(b.high, 2)}</td>
      <td><div class="bar-cell"><i style="left:0;width:${w.toFixed(1)}%;background:${colour}"></i></div></td>
      <td class="num">${compact(b.volume)}</td>
      <td class="num">${IN(b.share, 1)}%</td>
      <td><div class="tp-marks">${marks}</div></td>
    </tr>`;
  }).join('');
}

/* ---------- option bench -------------------------------------------------- */
/* Add contracts one at a time and read them against each other: what each costs,
 * how much of that is time value, how much has traded in it, what target it needs
 * and how far spot has to go to get there. One stop and one horizon for all of
 * them, because that is the only way the columns mean the same thing on each row.
 * Nothing here can send an order either. */

async function loadBench() {
  markSelected();
  syncHash();
  $('bn-alert').innerHTML = '';
  $('bn-name').textContent = `${state.optionSymbol} — loading…`;

  const q = new URLSearchParams({
    symbol: state.optionSymbol,
    stop_pct: $('bn-stop').value || 25,
    qty: $('bn-qty').value || 1,
    focus: state.benchFocus,
    tf: state.timeframe,
  });
  // Absent means "seed the ladder"; present-and-empty means "the user cleared it".
  if (state.benchLegs !== null) q.set('legs', state.benchLegs.join(','));
  if (state.optionExpiry) q.set('expiry', state.optionExpiry);
  if ($('bn-horizon').value) q.set('horizon', $('bn-horizon').value);

  let b;
  try {
    b = await api(`/api/options/bench?${q}`);
  } catch (e) {
    $('bn-name').textContent = state.optionSymbol;
    // A 404 here means the page is newer than the server process: static files are
    // re-read from disk on every request, but routes are only loaded at startup.
    const stale = e.status === 404
      ? ' This route is missing, which means the server was started before it existed'
        + ' — stop it (Ctrl+C) and run run.bat again.'
      : '';
    $('bn-alert').innerHTML = `<div class="error"><b>Bench unavailable.</b> ${e.message}.${stale}</div>`;
    return;
  }
  if (b.available === false) {
    $('bn-name').textContent = state.optionSymbol;
    $('bn-alert').innerHTML = `<div class="banner"><b>NSE did not serve the chain.</b> ${b.reason}. ${b.hint || ''}</div>`;
    return;
  }
  renderBench(b);
}

function renderBench(b) {
  state.bench = b;
  state.optionExpiry = b.expiry;
  // Whatever the server actually priced is the bench, including a seeded ladder.
  state.benchLegs = b.legs.map((l) => l.code);
  syncHash();

  $('bn-name').textContent = `${b.symbol} — ${b.name}`;
  $('inst-name').textContent = `${b.symbol} bench`;
  $('inst-sym').textContent = `NSE · ${b.name} · expiry ${b.expiry}`;
  $('inst-ltp').textContent = IN(b.spot, 2);
  $('inst-chg').innerHTML = `<span class="flat">spot · ATM ${b.atm}</span>`;
  $('bn-expiry').innerHTML = b.expiries.map((e) => `<option ${e === b.expiry ? 'selected' : ''}>${e}</option>`).join('');
  $('bn-sub').textContent = `· ${b.legs.length} of ${b.max_legs} contracts · ${b.expiry}`
    + ` · ${b.timeframe_label} candles`;
  $('bn-th-target').textContent = `Target ${IN(b.focus, b.focus % 1 ? 1 : 0)}:1`;

  buildFocusOptions(b);
  buildHorizonOptions('bn-horizon', b.horizon_sessions, b.sessions_to_expiry || undefined);
  buildBenchStrikes(b);

  const sess = b.horizon_sessions;
  $('bn-stats').innerHTML = [
    ['Spot', IN(b.spot, 2), `ATM strike ${b.atm}`],
    ['On the bench', `${b.legs.length}`, `up to ${b.max_legs} at a time`],
    ['Same stop', `−${IN(b.stop_pct, 0)}%`, 'of each premium'],
    ['Same horizon', sess ? `${sess} session${sess === 1 ? '' : 's'}` : '—',
      `${b.sessions_to_expiry ?? '—'} to expiry`],
    ['In focus', `${IN(b.focus, b.focus % 1 ? 1 : 0)}:1`, 'reward against that stop'],
    ['"Can keep" line', `${IN(b.keep_rate, 0)}%`, 'counted hit rate, at least even money'],
  ].map(([label, value, sub]) =>
    `<div class="stat"><div class="label">${label}</div><div class="value">${value}</div><div class="sub">${sub}</div></div>`).join('');

  renderBenchRows(b);

  $('bn-read').innerHTML = b.read.map((n) =>
    `<li data-tone="${n.tone}"><span class="glyph">${n.tone === 'up' ? '▲' : n.tone === 'down' ? '!' : '●'}</span><span>${n.text}</span></li>`).join('');
  $('bn-notes').innerHTML = b.notes.map((n) =>
    `<li data-tone="${n.tone}"><span class="glyph">${n.tone === 'down' ? '!' : '●'}</span><span>${n.text}</span></li>`).join('');
}

function buildFocusOptions(b) {
  $('bn-focus').innerHTML = b.multiples.map((m) =>
    `<option value="${m}" ${m === b.focus ? 'selected' : ''}>${m}:1</option>`).join('');
  state.benchFocus = b.focus;
}

function buildBenchStrikes(b) {
  const leg = $('bn-leg').value;
  const keep = Number($('bn-strike').value);
  const has = b.strikes.some((r) => r.strike === keep);
  $('bn-strike').innerHTML = b.strikes.map((r) => {
    // The same strike is ITM for one leg and OTM for the other, so the tag is
    // rebuilt whenever the leg changes rather than being a property of the row.
    const tag = r.strike === b.atm ? 'ATM'
      : leg === 'CE' ? (r.strike < b.spot ? 'ITM' : 'OTM') : (r.strike > b.spot ? 'ITM' : 'OTM');
    const ltp = r[leg.toLowerCase()].ltp;
    const sel = (has ? r.strike === keep : r.strike === b.atm) ? 'selected' : '';
    return `<option value="${r.strike}" ${sel}>${r.strike} · ${tag} · ${ltp ? IN(ltp, 2) : 'no quote'}</option>`;
  }).join('');
}

function renderBenchRows(b) {
  if (!b.legs.length) {
    $('bn-body').innerHTML = `<tr><td colspan="12" class="flat">
      The bench is empty. Pick a strike and leg above and <b>Add this contract</b>, or
      <b>Add the ITM / ATM / OTM ladder</b> to compare five strikes at once.</td></tr>`;
    return;
  }

  const pct = (n, d = 1) => (n === null || n === undefined ? '—' : `${n > 0 ? '+' : ''}${IN(n, d)}%`);
  const rate = (n) => (n === null || n === undefined ? '—' : `${IN(n, 0)}%`);
  const sub = (text) => `<div class="bn-sub">${text}</div>`;

  $('bn-body').innerHTML = b.legs.map((l) => {
    const dec = precisionFor(l.entry || 100);
    const g = l.greeks;
    const q = l.quote;
    const r = (l.rows || []).find((x) => x.multiple === b.focus);
    const k = l.keeps;

    const money = l.moneyness
      ? `<span class="chip" data-mny="${l.moneyness}">${l.moneyness}</span>`
        + sub(`${pct(l.moneyness_pct, 2)} in the money`)
      : '<span class="flat">—</span>';

    const premium = l.entry === null || l.entry === undefined
      ? '<span class="flat">no quote</span>'
      : IN(l.entry, dec) + sub(`stop ${IN(l.stop, dec)} · BE ${g.breakeven === null ? '—' : IN(g.breakeven, 2)}`);

    const timeValue = g.extrinsic === null || g.extrinsic === undefined
      ? '—'
      : `${IN(g.extrinsic, dec)}${sub(`${IN(g.extrinsic_pct, 0)}% of premium · θ ${g.theta_session === null ? '—' : IN(g.theta_session, 2)}/session`)}`;

    const volume = q.volume
      ? compact(q.volume) + sub(`OI ${compact(q.oi)}${q.volume_share ? ` · ${IN(q.volume_share, 0)}% of side` : ''}`)
      : '<span class="flat">—</span>';

    const target = !r ? '—'
      : !r.reachable ? '<span class="flat">unreachable</span>'
        : IN(r.target, dec) + sub(`${pct(r.move_pct)} · ₹${IN(r.gain, 0)}`);

    const spotNeeded = !r || r.spot_needed === null ? '—'
      : IN(r.spot_needed, 2) + sub(`${pct(r.spot_move_pct, 2)} · late ${IN(r.spot_needed_late, 2)}`);

    const traded = !r || r.spot_volume_at === null || r.spot_volume_at === undefined
      ? '<span class="flat">—</span>'
      : compact(r.spot_volume_at) + sub(`${IN(r.spot_volume_share, 1)}% of the profile`);

    const reached = !r ? '—'
      : rate(r.chance) + sub(r.samples ? `${IN(r.samples, 0)} attempts` : 'no history');

    const keeps = k
      ? `${k.label}${sub(`${IN(k.chance, 0)}% · spot ${pct(k.spot_move_pct, 2)}`)}`
      : `<span class="flat">none</span>${sub(`nothing held ${IN(b.keep_rate, 0)}%`)}`;

    return `<tr>
      <td><strong>${l.strike}</strong> ${l.leg}
        <div class="bn-actions">
          <button data-act="plan" data-code="${l.code}">Plan ▸</button>
          <button data-act="drop" data-code="${l.code}">✕ remove</button>
        </div>
      </td>
      <td>${money}</td>
      <td class="num">${premium}</td>
      <td class="num">${timeValue}</td>
      <td class="num">${g.delta === null || g.delta === undefined ? '—' : IN(g.delta, 2)}</td>
      <td class="num">${volume}</td>
      <td class="num bn-focus">${target}</td>
      <td class="num bn-focus">${spotNeeded}</td>
      <td class="num bn-focus">${traded}</td>
      <td class="num bn-focus">${reached}</td>
      <td class="num bn-focus">${rate(r ? r.chance_model : null)}</td>
      <td class="num bn-keep">${keeps}</td>
    </tr>`;
  }).join('');

  $('bn-body').querySelectorAll('button[data-act]').forEach((el) => {
    el.onclick = () => {
      if (el.dataset.act === 'drop') benchDrop(el.dataset.code);
      else benchPlan(el.dataset.code);
    };
  });
}

function benchAdd() {
  const code = `${Number($('bn-strike').value)}${$('bn-leg').value}`;
  const legs = state.benchLegs || [];
  if (legs.includes(code)) {
    $('bn-alert').innerHTML = `<div class="banner"><b>${code} is already on the bench.</b> Pricing it twice would say nothing new.</div>`;
    return;
  }
  if (legs.length >= (state.bench ? state.bench.max_legs : 8)) {
    $('bn-alert').innerHTML = `<div class="banner"><b>The bench is full.</b> Remove a row before adding another — past about eight rows the table stops being readable anyway.</div>`;
    return;
  }
  state.benchLegs = [...legs, code];
  loadBench();
}

function benchDrop(code) {
  state.benchLegs = (state.benchLegs || []).filter((c) => c !== code);
  loadBench();
}

function benchLadder() {
  const b = state.bench;
  if (!b) return;
  const leg = $('bn-leg').value;
  const i = b.strikes.findIndex((r) => r.strike === b.atm);
  const near = b.strikes.slice(Math.max(0, i - 2), i + 3).map((r) => r.strike);
  if (leg === 'PE') near.reverse();          // ITM first for a put means descending
  const legs = [...(state.benchLegs || [])];
  near.forEach((s) => {
    const code = `${s}${leg}`;
    if (!legs.includes(code) && legs.length < b.max_legs) legs.push(code);
  });
  state.benchLegs = legs;
  loadBench();
}

/* A row on the bench leads to the planner for that one contract: the bench is
 * where you choose the strike, the planner is where you work its target out. */
function benchPlan(code) {
  const m = /^(\d+(?:\.\d+)?)(CE|PE)$/.exec(code);
  if (!m) return;
  state.targetStrike = Number(m[1]);
  state.targetLeg = m[2];
  setMode('options');
}

/* ---------- boot ---------------------------------------------------------- */

/* Whatever view is open, reload it - the timeframe, Refresh and the auto-refresh
 * timer all mean the same thing in all three. */
function reloadCurrentView({ force = false } = {}) {
  if (state.mode === 'bench') loadBench();
  else if (state.mode === 'options') loadChain();
  else { loadSymbol(state.symbol, { force }); refreshWatchlist(); }
}

const AUTO_REFRESH_MS = 5 * 60 * 1000;

function setAutoRefresh(on) {
  clearInterval(state.autoTimer);
  state.autoTimer = null;
  localStorage.setItem('at1.autoRefresh', on ? '1' : '0');
  if (!on) return;
  state.lastAuto = Date.now();
  state.autoTimer = setInterval(() => { state.lastAuto = Date.now(); reloadCurrentView(); }, AUTO_REFRESH_MS);
}

/* Mobile browsers freeze timers while the screen is off or the tab is in the
 * background, so catch up as soon as the page is visible again. */
document.addEventListener('visibilitychange', () => {
  if (document.hidden || !state.autoTimer) return;
  if (Date.now() - state.lastAuto < AUTO_REFRESH_MS) return;
  setAutoRefresh(true);   // restart the 5-minute clock from now
  reloadCurrentView();
});

function buildControls() {
  const tf = $('timeframes');
  tf.innerHTML = state.universe.timeframes.map((t) =>
    `<button data-tf="${t.id}" aria-pressed="${t.id === state.timeframe}">${t.label}</button>`).join('');
  tf.querySelectorAll('button').forEach((b) => {
    b.onclick = () => {
      state.timeframe = b.dataset.tf;
      tf.querySelectorAll('button').forEach((x) => x.setAttribute('aria-pressed', x === b));
      reloadCurrentView();
    };
  });

  $('group-select').innerHTML = state.universe.groups.map((g) =>
    `<option value="${g.id}" ${g.id === state.group ? 'selected' : ''}>${g.label} (${g.items.length})</option>`).join('');
  $('group-select').onchange = (e) => { state.group = e.target.value; renderPanel(); refreshWatchlist(); };

  $('sort-select').onchange = (e) => { state.sort = e.target.value; renderPanel(); };
  $('search').oninput = (e) => { state.search = e.target.value; renderPanel(); };
  $('mode-stocks').onclick = () => setMode('stocks');
  $('mode-options').onclick = () => setMode('options');
  $('mode-bench').onclick = () => setMode('bench');
  $('btn-refresh').onclick = () => reloadCurrentView({ force: true });
  $('auto-refresh').onchange = (e) => setAutoRefresh(e.target.checked);
  // Remembered so a phone left on the dashboard keeps refreshing after a reload.
  if (localStorage.getItem('at1.autoRefresh') === '1') {
    $('auto-refresh').checked = true;
    setAutoRefresh(true);
  }
  $('btn-theme').onclick = () => {
    const light = document.documentElement.dataset.theme === 'light';
    document.documentElement.dataset.theme = light ? 'dark' : 'light';
    document.body.dataset.mode = light ? 'dark' : 'light';
    $('btn-theme').textContent = light ? 'Light' : 'Dark';
    if (state.analysis) renderAnalysis(state.analysis);   // charts re-read the palette
  };
  $('pane-toggles').onchange = (e) => {
    if (e.target.dataset.series === 'markers') applyMarkers(); else applyToggles();
  };
  $('oc-expiry').onchange = (e) => { state.optionExpiry = e.target.value; loadChain(); };
  $('oc-window').onchange = () => loadChain();   // the window is applied server-side

  // Every planner input re-plans; `change` fires on blur or Enter, so typing a
  // premium digit by digit does not fire six requests at the server.
  $('tp-go').onclick = () => loadTargets();
  ['tp-strike', 'tp-leg', 'tp-premium', 'tp-stop', 'tp-qty', 'tp-horizon'].forEach((id) => {
    $(id).onchange = () => {
      if (id === 'tp-strike' || id === 'tp-leg') {
        // A different contract has a different LTP, so a premium typed for the old
        // one is cleared rather than silently carried across.
        $('tp-premium').value = '';
      }
      loadTargets();
    };
  });

  // Bench: adding and removing are explicit clicks, and the four shared settings
  // re-price every row at once because that is the whole point of the page.
  $('bn-add').onclick = () => benchAdd();
  $('bn-ladder').onclick = () => benchLadder();
  $('bn-clear').onclick = () => { state.benchLegs = []; loadBench(); };
  $('bn-leg').onchange = () => { if (state.bench) buildBenchStrikes(state.bench); };
  $('bn-expiry').onchange = (e) => { state.optionExpiry = e.target.value; loadBench(); };
  $('bn-focus').onchange = (e) => { state.benchFocus = Number(e.target.value); loadBench(); };
  ['bn-stop', 'bn-qty', 'bn-horizon'].forEach((id) => { $(id).onchange = () => loadBench(); });

  window.addEventListener('resize', () => {
    Object.values(charts).forEach(({ chart }) => chart.applyOptions({}));
  });
}

/* The URL hash holds the whole view, so a setup can be bookmarked or pasted to
 * someone else: #symbol=RELIANCE.NS&tf=15m or #mode=options&code=BANKNIFTY */

function syncHash() {
  const q = new URLSearchParams();
  q.set('mode', state.mode);
  if (state.mode === 'options') {
    q.set('code', state.optionSymbol);
    if (state.targetStrike) { q.set('strike', state.targetStrike); q.set('leg', state.targetLeg || 'CE'); }
  } else if (state.mode === 'bench') {
    q.set('code', state.optionSymbol);
    // A whole bench in one link: the contracts, and the reward:risk it was read at.
    if (state.benchLegs !== null) q.set('legs', state.benchLegs.join(','));
    q.set('r', state.benchFocus);
  } else { q.set('symbol', state.symbol); q.set('group', state.group); }
  q.set('tf', state.timeframe);
  if (document.documentElement.dataset.theme === 'light') q.set('theme', 'light');
  history.replaceState(null, '', `#${q}`);
}

function applyHash() {
  const h = new URLSearchParams(location.hash.slice(1));
  if (h.get('theme') === 'light') {
    document.documentElement.dataset.theme = 'light';
    document.body.dataset.mode = 'light';
    $('btn-theme').textContent = 'Dark';
  }
  const tf = h.get('tf');
  if (tf && state.universe.timeframes.some((t) => t.id === tf)) state.timeframe = tf;
  const group = h.get('group');
  if (group && state.universe.groups.some((g) => g.id === group)) state.group = group;
  if (h.get('symbol')) state.symbol = h.get('symbol');
  if (h.get('code')) state.optionSymbol = h.get('code').toUpperCase();
  if (h.get('strike')) state.targetStrike = Number(h.get('strike'));
  if (h.get('leg')) state.targetLeg = h.get('leg').toUpperCase() === 'PE' ? 'PE' : 'CE';
  if (h.has('legs')) state.benchLegs = h.get('legs').split(',').filter(Boolean);
  if (h.get('r')) state.benchFocus = Number(h.get('r'));
  const mode = h.get('mode');
  return mode === 'options' || mode === 'bench' ? mode : null;
}

async function boot() {
  if (!LWC) {
    showError('The charting library did not load. This page needs internet access to unpkg.com on first load.');
    return;
  }
  try {
    state.universe = await api('/api/universe');
  } catch (e) {
    showError(`API not reachable: ${e.message}`);
    return;
  }
  state.timeframe = state.universe.default_timeframe;
  const optionMode = applyHash();
  buildControls();
  if (optionMode) {
    setMode(optionMode);
    return;
  }
  renderPanel();
  await loadSymbol(state.symbol);
  refreshWatchlist();
}

boot();
