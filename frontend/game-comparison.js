import { bindComparisonReorder } from './compare-reorder.js';

const MIN_GAMES = 2;
const MAX_GAMES = 10;
const SERIES_COLORS = ['#246ccd', '#008475', '#8b4db8', '#b95521', '#a73167', '#54701e', '#147586', '#866113', '#5446a8', '#515e73'];
const DARK_SERIES_COLORS = ['#60b3e2', '#58d6b5', '#d4a1f5', '#ffad76', '#f28cac', '#aec567', '#73cbd0', '#e9c067', '#abaaf8', '#a7bbd1'];
const SERIES_SYMBOLS = ['circle', 'diamond', 'rect', 'triangle', 'roundRect'];
const SERIES_LINES = ['solid', 'dashed', 'dotted'];
const DAY_MS = 86_400_000;
const SPLIT_PEAK_RATIO = 10;
const SPLIT_PLOT_INSETS = { top: 32, bottom: 23 };
const TIME_BANDS = ['不足 2 小时', '2 至不足 10 小时', '10 小时及以上'];
const integer = new Intl.NumberFormat('zh-CN');
const percent = new Intl.NumberFormat('zh-CN', { style: 'percent', maximumFractionDigits: 1 });
const localDay = new Intl.DateTimeFormat('zh-CN', {
  timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit'
});
const axisDay = new Intl.DateTimeFormat('zh-CN', { timeZone: 'UTC', month: '2-digit', day: '2-digit' });

const count = value => integer.format(value ?? 0);
const ratio = (positive, rated) => rated > 0 ? positive / rated : null;
const percentage = value => value == null ? '—' : percent.format(value);
const day = seconds => seconds == null ? '—' : localDay.format(seconds * 1000);
const coverage = game => game.first == null || game.last == null ? '暂无评测' : `${day(game.first)} 至 ${day(game.last)}`;
const gameQuery = ids => new URLSearchParams(ids.map(id => ['game', id])).toString();
const gameCssColor = ({ slot }) => `light-dark(${SERIES_COLORS[slot]}, ${DARK_SERIES_COLORS[slot]})`;
const gameColor = (game, styleFor) => `style="--game-color:${gameCssColor(styleFor(game))}"`;
const metricValue = (row, metric) => metric === 'volume' ? row?.total : ratio(row?.positive, row?.rated);
const metricLabel = (value, metric) => value == null ? '—' : metric === 'volume' ? count(value) : percentage(value);

function comparisonStyles(ids) {
  const slots = new Map();
  const used = new Set();
  // Resolve palette collisions in AppID order so display order and shared links keep the same colors.
  for (const id of [...ids].sort((a, b) => Number(a) - Number(b))) {
    let slot = (Math.imul(Number(id), 2654435761) >>> 0) % SERIES_COLORS.length;
    while (used.has(slot)) slot = (slot + 1) % SERIES_COLORS.length;
    used.add(slot);
    slots.set(id, { slot, symbol: SERIES_SYMBOLS[slot % SERIES_SYMBOLS.length],
      line: SERIES_LINES[slot % SERIES_LINES.length] });
  }
  return game => slots.get(String(game.appid));
}

function priceLabel(price, esc) {
  if (!price) return '—';
  let text = '暂无价格';
  if (price.is_free) text = '免费';
  else if (price.price_final != null && /^[A-Z]{3}$/.test(price.currency)) {
    text = new Intl.NumberFormat('zh-CN', { style: 'currency', currency: price.currency }).format(price.price_final / 100);
  }
  const source = [price.region?.toUpperCase(), price.collected_at == null ? null : day(price.collected_at)].filter(Boolean);
  return `${esc(text)}${source.length ? `<small>${esc(source.join(' · '))}</small>` : ''}`;
}

function tableMarkup(games, label, rows, { esc, styleFor, gamesAsRows = false }) {
  const gameHeaders = games.map(game =>
    `<span class="compare-table-game" ${gameColor(game, styleFor)}>${esc(game.name)}</span>`);
  const columns = gamesAsRows ? rows.map(([heading]) => heading) : gameHeaders;
  const body = gamesAsRows
    ? gameHeaders.map((heading, index) => [heading, rows.map(([, cells]) => cells[index])])
    : rows;
  return `<div class="compare-table-wrap" role="region" aria-label="${label}" tabindex="0">
    <table class="compare-table" aria-label="${label}">
      <thead><tr><th scope="col">${gamesAsRows ? '游戏' : label}</th>${columns.map(heading =>
        `<th scope="col">${heading}</th>`).join('')}</tr></thead>
      <tbody>${body.map(([heading, cells]) => `<tr><th scope="row">${heading}</th>${cells.map(cell => `<td>${cell}</td>`).join('')}</tr>`).join('')}</tbody>
    </table></div>`;
}

function playtimeMarkup(games, { esc, route, styleFor }) {
  const bands = ['short', 'medium', 'long'];
  const rows = TIME_BANDS.map((label, bucket) => [label, games.map(game => {
    const band = game.playtime.find(row => row.bucket === bucket);
    if (!band?.total) return '—';
    return `<a class="compare-playtime-value" href="${route('reviews', { game: game.appid, playtime: bands[bucket] })}">
      <strong>${count(band.total)} 条</strong><small>推荐 ${percentage(ratio(band.positive, band.rated))}</small></a>`;
  })]);
  rows.push(['时长未知', games.map(game => `${count(game.total - game.playtime_known)} 条`)]);
  return `<section class="panel compare-playtime"><div class="compare-section-head"><div>
    <h2>发表评测时的游玩时长</h2><p>按发表时长分组，点击条数可查看对应评测。</p>
    </div></div>${tableMarkup(games, '游玩时长', rows, { esc, styleFor, gamesAsRows: true })}</section>`;
}

function profileMarkup(games, { esc, styleFor }) {
  const list = values => (values || []).map(item => typeof item === 'string' ? item : item?.description || item?.name)
    .filter(Boolean).map(esc).join('、') || '—';
  const rows = [
    ['价格快照', game => priceLabel(game.price, esc)],
    ['类型', game => list(game.genres)],
    ['发行日期', game => esc(game.release_date || game.release_date_raw || '—')],
    ['开发商', game => list(game.developers)],
    ['发行商', game => list(game.publishers)],
    ['平台', game => list(game.platforms)]
  ];
  return `<section class="panel compare-profile"><div class="compare-section-head"><div>
    <h2>游戏资料</h2><p>价格按各游戏最近一次入库的商店快照展示。</p></div></div>
    ${tableMarkup(games, '游戏资料', rows.map(([label, read]) => [label, games.map(read)]), { esc, styleFor, gamesAsRows: true })}</section>`;
}

function trendMarkup(games, { esc, styleFor }) {
  if (games.every(game => !game.daily.length)) {
    return '<section class="panel compare-message"><h2>每日评测趋势</h2><p>所选游戏暂无入库评测。</p></section>';
  }
  return `<section class="panel compare-trend">
    <div class="compare-section-head"><div><h2>每日评测趋势</h2><p>按北京时间对齐各游戏的评测发表日期。</p></div>
      <div class="compare-trend-controls">
        <div class="compare-toggle" role="group" aria-label="趋势指标">
          <button class="button" type="button" data-metric="volume" aria-pressed="true">评测量</button>
          <button class="button" type="button" data-metric="rate" aria-pressed="false">推荐比例</button>
        </div>
        <div class="compare-toggle" role="group" aria-label="评测量显示方式" data-volume-controls hidden>
          <button class="button" type="button" data-scale="shared" aria-pressed="false">同轴对比</button>
          <button class="button" type="button" data-scale="split" aria-pressed="false">分图看趋势</button>
        </div>
      </div></div>
    <ul class="compare-legend" role="list">${games.map(game =>
      `<li ${gameColor(game, styleFor)}>${esc(game.name)}</li>`).join('')}</ul>
    <div class="compare-chart-frame">
      <div class="compare-chart" aria-hidden="true"></div>
      <div class="compare-chart-order" data-reorder-list="charts" role="group" aria-label="分图排序" hidden></div>
    </div>
    <div class="compare-gaps" role="group" aria-label="各游戏无入库记录的日期区间" hidden></div>
    <p class="compare-gap-note" aria-live="polite" hidden></p>
    <p class="compare-caption"></p>
    <details class="compare-daily"><summary>查看逐日数据</summary><div></div></details>
  </section>`;
}

function seriesPoints(dates, rows, metric) {
  const points = [];
  let previous = null;
  for (const [key, date] of dates) {
    if (previous !== null && date - previous > DAY_MS) points.push([previous + DAY_MS, null]);
    const value = metricValue(rows.get(key), metric);
    points.push([date, value == null ? null : metric === 'rate' ? value * 100 : value]);
    previous = date;
  }
  return points;
}

// Gaps are relative to the dates shown in the comparison, including its two edges.
function missingIntervals(rows, first, last) {
  const intervals = [];
  let start = first;
  for (const { day } of rows) {
    const date = Date.parse(day);
    if (date > start) intervals.push([start, date - DAY_MS]);
    start = date + DAY_MS;
  }
  if (start <= last) intervals.push([start, last]);
  return intervals;
}

const dateKey = value => new Date(value).toISOString().slice(0, 10);

function gapDescription({ name, start, end }) {
  const range = start === end ? dateKey(start) : `${dateKey(start)} 至 ${dateKey(end)}`;
  return `${name} · ${range} · 无入库记录`;
}

function mountGapLabels(host, note, chartHost) {
  let activeKey = '';
  function updateNote() {
    const active = host.querySelector(`[data-gap="${activeKey}"]`);
    note.hidden = !active;
    note.textContent = active?.title || '';
    for (const button of host.children) button.setAttribute('aria-expanded', String(button === active));
  }
  host.addEventListener('click', event => {
    const button = event.target.closest('[data-gap]');
    if (!button) return;
    activeKey = activeKey === button.dataset.gap ? '' : button.dataset.gap;
    updateNote();
  });

  return function render(gaps, chart, min, max) {
    const focused = host.contains(document.activeElement) ? document.activeElement.dataset.gap : '';
    const axisPixel = day => chart.convertToPixel({ xAxisIndex: 0 }, day);
    const width = chartHost.clientWidth;
    const items = gaps.map(gap => {
      const start = axisPixel(Math.max(min, gap.start - DAY_MS / 2));
      const end = axisPixel(Math.min(max, gap.end + DAY_MS / 2));
      const expanded = end - start >= Math.min(215, 96 + gap.name.length * 12);
      const size = Math.max(44, end - start);
      const left = Math.max(0, Math.min(width - size, (start + end - size) / 2));
      return { ...gap, startPixel: start, endPixel: end, size, left, expanded };
    }).sort((a, b) => a.left - b.left);
    const lanes = [];
    const buttons = items.map(item => {
      let lane = lanes.findIndex(right => right + 6 <= item.left);
      if (lane < 0) lane = lanes.length;
      lanes[lane] = item.left + item.size;
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'compare-gap';
      button.dataset.gap = item.key;
      button.title = gapDescription(item);
      button.setAttribute('aria-label', button.title);
      button.style.left = `${item.left}px`;
      button.style.width = `${item.size}px`;
      button.style.top = `${lane * 32}px`;
      button.style.setProperty('--game-color', item.color);
      const bracket = document.createElement('span');
      bracket.className = 'compare-gap-bracket';
      bracket.style.left = `${item.startPixel - item.left}px`;
      bracket.style.width = `${Math.max(1, item.endPixel - item.startPixel)}px`;
      const label = document.createElement('span');
      label.className = 'compare-gap-label';
      label.textContent = item.expanded ? `${item.name} · 无入库记录` : '缺';
      button.append(bracket, label);
      return button;
    });
    host.hidden = !buttons.length;
    host.style.height = `${lanes.length * 32}px`;
    host.replaceChildren(...buttons);
    updateNote();
    if (focused) buttons.find(button => button.dataset.gap === focused)?.focus({ preventScroll: true });
  };
}

function reviewScale(peak) {
  const roughStep = Math.max(1, peak / 4);
  const magnitude = 10 ** Math.floor(Math.log10(roughStep));
  const interval = [1, 2, 2.5, 5, 10].map(factor => factor * magnitude)
    .find(step => Number.isInteger(step) && step >= roughStep);
  return { interval, max: Math.ceil(peak / interval) * interval };
}

function mountTrend(panel, games, { esc, route, styleFor, grip, view }) {
  if (!panel) return () => {};
  const chartHost = panel.querySelector('.compare-chart');
  const orderHost = panel.querySelector('.compare-chart-order');
  const { top: plotTop, bottom: plotBottom } = SPLIT_PLOT_INSETS;
  orderHost.style.setProperty('--compare-plot-top', `${plotTop}px`);
  orderHost.style.setProperty('--compare-plot-bottom', `${plotBottom}px`);
  const details = panel.querySelector('.compare-daily');
  const toggles = panel.querySelector('.compare-trend-controls');
  const scaleControls = panel.querySelector('[data-volume-controls]');
  const legend = panel.querySelector('.compare-legend');
  const caption = panel.querySelector('.compare-caption');
  const chart = window.echarts?.init(chartHost, null, { renderer: 'svg' });
  const renderGapLabels = mountGapLabels(panel.querySelector('.compare-gaps'), panel.querySelector('.compare-gap-note'), chartHost);
  const rowsByGame = games.map(game => new Map(game.daily.map(row => [row.day, row])));
  const days = [...new Set(rowsByGame.flatMap(rows => [...rows.keys()]))].sort();
  const dates = days.map(key => [key, Date.parse(key)]);
  const first = dates[0][1];
  const last = dates.at(-1)[1];
  const gapsByGame = games.map(game => missingIntervals(game.daily, first, last).map(([start, end]) => ({
    start, end, name: game.name, color: gameCssColor(styleFor(game)), key: `${game.appid}-${start}`
  })));
  const gaps = gapsByGame.flat();
  const plotted = games.map((game, index) => ({
    game, index, peak: game.daily.reduce((peak, row) => Math.max(peak, row.total), 0)
  })).filter(item => item.peak > 0);
  const peaks = plotted.map(item => item.peak);
  const canSplit = plotted.length > 1;
  let metric = view.metric || 'volume';
  let volumeScale = view.volumeScale || (canSplit && Math.max(...peaks) >= Math.min(...peaks) * SPLIT_PEAK_RATIO ? 'split' : 'shared');
  details.open = Boolean(view.dailyOpen);

  if (canSplit) orderHost.innerHTML = plotted.map(({game}, row) => `<div class="compare-chart-row" data-reorder-item="${game.appid}"
    style="--game-color:${gameCssColor(styleFor(game))}; top:${100 * row / plotted.length}%;height:${100 / plotted.length}%">
    ${grip(game, 'compare-chart-grip')}</div>`).join('');

  function updateDailyTable() {
    if (!details.open) return;
    const table = details.querySelector('div');
    table.innerHTML = tableMarkup(games, '日期', days.map(date => [esc(date), games.map((game, index) => {
      const value = metricValue(rowsByGame[index].get(date), metric);
      return value == null ? '—' : `<a href="${route('reviews', { game: game.appid, start: date, end: date })}">${metricLabel(value, metric)}</a>`;
    })]), { esc, styleFor });
  }

  function updateChart() {
    if (!chart) return;
    const split = metric === 'volume' && volumeScale === 'split';
    const dark = document.documentElement.dataset.theme === 'dark';
    const palette = dark ? DARK_SERIES_COLORS : SERIES_COLORS;
    const labelColor = dark ? '#b3c4d6' : '#4a607e';
    const axisStyle = dark ? { axisLine: { lineStyle: { color: '#64798f' } } } : {};
    const seriesColor = game => palette[styleFor(game).slot];
    chartHost.classList.toggle('is-split', split);
    chartHost.style.setProperty('--compare-plot-count', plotted.length);
    if (chart.getHeight() !== chartHost.clientHeight || chart.getWidth() !== chartHost.clientWidth) chart.resize();

    const xAxis = {
      type: 'time', min: first === last ? first - DAY_MS : first, max: first === last ? last + DAY_MS : last,
      minInterval: DAY_MS, ...axisStyle,
      axisLabel: { formatter: value => axisDay.format(value), hideOverlap: true, color: labelColor }
    };
    const yAxis = {
      type: 'value', min: 0, max: metric === 'rate' ? 100 : null, minInterval: 1, ...axisStyle,
      axisLabel: { color: labelColor, formatter: metric === 'rate' ? '{value}%' : count },
      splitLine: { lineStyle: { color: dark ? '#8ba9c238' : '#57769b26' } }
    };
    const rowHeight = chart.getHeight() / plotted.length;
    const left = Math.max(42, count(reviewScale(Math.max(...peaks)).max).length * 7 + 10);
    const right = split && canSplit ? 57 : 18;
    chart.setOption({
      useUTC: true, animation: false, color: palette,
      grid: split ? plotted.map((_, row) => ({ left, right, top: row * rowHeight + plotTop, height: rowHeight - plotTop - plotBottom })) :
        { left: 10, right, top: 14, bottom: 8, containLabel: true },
      title: split ? plotted.map(({ game }, row) => ({
        text: game.name, left, top: row * rowHeight + 5, padding: 0,
        textStyle: { color: seriesColor(game), fontSize: 13, width: Math.max(80, chart.getWidth() - left - right), overflow: 'truncate' }
      })) : [],
      axisPointer: split ? { link: [{ xAxisIndex: 'all' }] } : {},
      tooltip: {
        trigger: 'axis', confine: true, transitionDuration: 0,
        ...(dark ? {
          backgroundColor: '#253342', borderColor: '#627b91', textStyle: { color: '#eaf3fc' },
          axisPointer: { lineStyle: { color: '#8dabc1' } }
        } : {}),
        extraCssText: 'max-width: min(360px, 78vw); white-space: normal; overflow-wrap: anywhere;',
        formatter(items) {
          const date = new Date(Number(items[0]?.axisValue));
          if (Number.isNaN(date.getTime())) return '';
          const key = date.toISOString().slice(0, 10);
          return [key, ...games.map((game, index) => {
            const value = metricValue(rowsByGame[index].get(key), metric);
            const label = value == null ? (rowsByGame[index].has(key) ? '推荐状态未知' : '无入库记录') :
              metric === 'volume' ? `${count(value)} 条` : percentage(value);
            return `${esc(game.name)} · ${label}`;
          })].join('<br>');
        }
      },
      xAxis: split ? plotted.map((_, gridIndex) => ({ ...xAxis, gridIndex,
        axisLabel: { ...xAxis.axisLabel, show: gridIndex === plotted.length - 1 }
      })) : xAxis,
      yAxis: split ? plotted.map(({ peak }, gridIndex) => ({ ...yAxis, gridIndex, ...reviewScale(peak) })) : yAxis,
      series: plotted.map(({ game, index }, row) => ({
        id: String(game.appid), name: game.name, type: 'line', data: seriesPoints(dates, rowsByGame[index], metric),
        xAxisIndex: split ? row : 0, yAxisIndex: split ? row : 0, itemStyle: { color: seriesColor(game) },
        connectNulls: false, showSymbol: split || plotted.length <= 4, symbolSize: 5, sampling: 'none',
        markArea: split ? {
          silent: true, label: { position: 'inside', color: labelColor, fontSize: 12 },
          itemStyle: { color: dark ? '#8ba9c22b' : '#7790ad24' },
          data: gapsByGame[index].map(({ start, end }) => {
            const from = Math.max(xAxis.min, start - DAY_MS / 2);
            const to = Math.min(xAxis.max, end + DAY_MS / 2);
            const span = (to - from) / (xAxis.max - xAxis.min) * (chart.getWidth() - left - right);
            return [{ xAxis: from, name: '无入库记录', label: { show: span >= 112 } }, { xAxis: to }];
          })
        } : undefined,
        lineStyle: { width: 2, type: styleFor(game).line, color: seriesColor(game) },
        symbol: styleFor(game).symbol
      }))
    }, { notMerge: true });
    renderGapLabels(split ? [] : gaps, chart, xAxis.min, xAxis.max);
  }

  function syncControls() {
    const split = metric === 'volume' && volumeScale === 'split';
    scaleControls.hidden = !chart || !canSplit || metric !== 'volume';
    orderHost.hidden = !chart || !canSplit || !split;
    legend.hidden = !chart || split;
    for (const button of toggles.querySelectorAll('button')) {
      button.setAttribute('aria-pressed', String(button.dataset.metric === metric || button.dataset.scale === volumeScale));
    }
    const metricNote = metric === 'rate' ? '推荐比例仅统计推荐状态已知的评测。' : split ?
      '各图共用日期轴，纵轴刻度各自独立（条／日）。要比较数量差距，请切到同轴对比。' : '所有曲线共用同一纵轴（条／日）。';
    const gapNote = !gaps.length ? '' : split ? '阴影区间无入库记录。' : '日期轴下方的虚线括号标注无入库记录的区间。';
    caption.textContent = chart ? `${metricNote}${gapNote}` : '';
  }

  toggles.addEventListener('click', event => {
    const button = event.target.closest('button');
    if (!button || button.getAttribute('aria-pressed') === 'true') return;
    const nextMetric = button.dataset.metric;
    if (nextMetric) view.metric = metric = nextMetric;
    else view.volumeScale = volumeScale = button.dataset.scale;
    syncControls();
    updateChart();
    if (nextMetric) updateDailyTable();
  });
  details.addEventListener('toggle', () => {
    view.dailyOpen = details.open;
    updateDailyTable();
  });
  syncControls();
  if (!chart) {
    chartHost.hidden = true;
    details.open = true;
    updateDailyTable();
    return () => {};
  }
  updateChart();
  updateDailyTable();
  window.addEventListener('steam-theme-change', updateChart);
  const observer = new ResizeObserver(() => {
    if (chartHost.clientWidth && chartHost.clientHeight &&
        (chart.getWidth() !== chartHost.clientWidth || chart.getHeight() !== chartHost.clientHeight)) updateChart();
  });
  observer.observe(chartHost);
  return () => {
    window.removeEventListener('steam-theme-change', updateChart);
    observer.disconnect();
    chart.dispose();
  };
}

export function mountGameComparison(container, { games, params, signal, get, route, picture, status, esc, icon }) {
  const catalog = new Map(games.map(game => [String(game.appid), game]));
  const suggested = games.filter(game => game.total > 0).slice(0, MIN_GAMES).map(game => String(game.appid));
  const addIcon = icon('<path d="M12 5v14M5 12h14"/>');
  const closeIcon = icon('<path d="m6 6 12 12M18 6 6 18"/>');
  const gripIcon = icon('<path d="M9 6h.01M15 6h.01M9 12h.01M15 12h.01M9 18h.01M15 18h.01" stroke-width="3"/>');
  const layersIcon = icon('<path d="m12 3 9 5-9 5-9-5Zm-9 9 9 5 9-5m-18 4 9 5 9-5"/>');
  let selected = [...new Set(params.getAll('game'))].filter(id => catalog.has(id)).slice(0, MAX_GAMES);
  let styleFor = comparisonStyles(selected);
  let loadedGames;
  let view = {};
  let draft = [];
  let dialogOpener;
  let request;
  let disposeTrend = () => {};

  container.innerHTML = `<div class="compare-page">
    <header class="page-head compare-heading"><div><h1>游戏对比</h1><p>并排查看入库评测、游玩时长和游戏资料。</p></div>
      <div class="compare-toolbar"><span class="compare-count" role="status"></span>
      <button class="button button--primary" type="button" data-compare-open>${addIcon}<span>管理对比</span></button></div>
    </header>
    <span class="compare-sr-only" role="status" data-compare-order-status></span>
    <section class="compare-selection" aria-label="已选游戏"></section>
    <div class="compare-results"></div>
    <dialog class="compare-dialog" aria-labelledby="compare-dialog-title">
      <div class="compare-dialog-head"><h2 id="compare-dialog-title">选择对比游戏</h2>
        <button class="button button--quiet" type="button" data-compare-close aria-label="关闭选择窗口">${closeIcon}</button></div>
      <label class="compare-search">搜索游戏<input type="search" placeholder="输入游戏名称或 AppID" maxlength="100" autofocus></label>
      <div class="compare-choice-list" role="group" aria-label="可对比游戏"></div>
      <div class="compare-dialog-foot"><span role="status"></span><div class="actions">
        <button class="button" type="button" data-compare-close>取消</button>
        <button class="button button--primary compare-apply" type="button">应用对比</button></div></div>
    </dialog>
  </div>`;

  const page = container.firstElementChild;
  const selection = page.querySelector('.compare-selection');
  const results = page.querySelector('.compare-results');
  const toolbar = page.querySelector('.compare-toolbar');
  const chooseButton = toolbar.querySelector('[data-compare-open]');
  const dialog = page.querySelector('.compare-dialog');
  const search = dialog.querySelector('input');
  const choices = dialog.querySelector('.compare-choice-list');
  const apply = dialog.querySelector('.compare-apply');

  function grip(game, className) {
    const name = esc(game.name);
    return `<button class="button compare-grip ${className}" type="button" data-reorder-handle data-reorder-name="${name}"
      aria-label="调整${name}顺序，拖动或使用方向键" title="拖动交换顺序，或使用方向键">${gripIcon}</button>`;
  }

  function gameCard(game) {
    const name = esc(game.name);
    const detail = route('game/' + game.appid);
    const rate = game.recommendation_rate ?? ratio(game.positive, game.rated);
    return `<article class="compare-game" data-reorder-item="${game.appid}" ${gameColor(game, styleFor)}>
      <a class="compare-cover" href="${detail}" aria-label="查看${name}详情">${picture(game)}</a>
      ${selected.length > 1 ? grip(game, 'compare-card-grip') : ''}
      <button class="button compare-remove" type="button" data-remove="${game.appid}" aria-label="移除${name}">${closeIcon}</button>
      <div class="compare-game-body"><h2 class="compare-game-name"><a href="${detail}">${name}</a></h2>
        <div class="compare-game-status">${status(game)}</div>
        <dl class="compare-kpis"><div><dt>入库评测</dt><dd class="compare-total">${count(game.total)}</dd></div>
          <div><dt>样本推荐率</dt><dd class="compare-rate">${percentage(rate)}</dd></div></dl>
        <p class="compare-coverage"><span>评测覆盖 ${coverage(game)}</span><small${game.rated < game.total ? '' : ' hidden'}>${count(game.rated)} 条评测有推荐状态</small></p>
        <div class="compare-game-links"><a href="${detail}">游戏详情</a>${game.total > 0 ?
          `<a href="${route('reviews', { game: game.appid })}">浏览评测</a>` : ''}</div>
      </div></article>`;
  }

  function renderSelection() {
    const max = Math.min(MAX_GAMES, catalog.size);
    toolbar.hidden = !selected.length;
    toolbar.querySelector('.compare-count').textContent = `已选 ${selected.length} / ${MAX_GAMES} 款`;
    if (!selected.length) {
      selection.innerHTML = `<div class="panel compare-empty"><div class="compare-empty-copy">${layersIcon}
        <h2>${catalog.size >= MIN_GAMES ? '选几款游戏，开始对比' : '可对比的游戏不足两款'}</h2>
        <p>${catalog.size >= MIN_GAMES ? `从游戏总览中选 ${MIN_GAMES}–${MAX_GAMES} 款，即可比较各自的评测样本。` : '游戏总览里至少需要两款游戏。'}</p>
        <div class="actions">${catalog.size >= MIN_GAMES ? '<button class="button button--primary" type="button" data-compare-open>选择对比游戏</button>' :
          `<a class="button" href="${route('')}">前往游戏总览</a>`}${suggested.length === MIN_GAMES ?
          '<button class="button" type="button" data-compare-suggested>快速对比两款</button>' : ''}</div></div></div>`;
      return;
    }
    const hasAddTile = selected.length < max;
    selection.innerHTML = `<div class="compare-cards" data-reorder-list="cards" style="--compare-items:${selected.length + Number(hasAddTile)}">
      ${selected.map(id => gameCard(catalog.get(id))).join('')}
      ${hasAddTile ? `<button class="compare-add" type="button" data-compare-open>${addIcon}<strong>添加游戏</strong>
      <span>还可添加 ${max - selected.length} 款</span></button>` : ''}</div>`;
  }

  function focusSelectionControl() {
    (toolbar.hidden ? selection.querySelector('.compare-empty .actions :is(button, a)') : chooseButton)?.focus();
  }

  function updateSelectionStats(details) {
    const cards = selection.querySelectorAll('.compare-game');
    details.forEach((game, index) => {
      const card = cards[index];
      card.querySelector('.compare-total').textContent = count(game.total);
      card.querySelector('.compare-rate').textContent = percentage(game.recommendation_rate);
      card.querySelector('.compare-game-status').innerHTML = status(game);
      card.querySelector('.compare-coverage span').textContent = `评测覆盖 ${coverage(game)}`;
      const rated = card.querySelector('.compare-coverage small');
      rated.hidden = game.rated >= game.total;
      rated.textContent = `${count(game.rated)} 条评测有推荐状态`;
      catalog.set(String(game.appid), game);
    });
  }

  function renderComparison() {
    disposeTrend();
    const details = selected.map(id => loadedGames.get(id));
    const options = { esc, route, styleFor, view, grip };
    updateSelectionStats(details);
    results.innerHTML = `${trendMarkup(details, options)}${playtimeMarkup(details, options)}${profileMarkup(details, options)}`;
    disposeTrend = mountTrend(results.querySelector('.compare-trend'), details, options);
  }

  async function renderResults() {
    request?.abort();
    loadedGames = undefined;
    disposeTrend();
    disposeTrend = () => {};
    if (selected.length < MIN_GAMES) {
      results.innerHTML = selected.length ? '<p class="panel compare-message">再添加 1 款游戏即可对比。</p>' : '';
      return;
    }
    const active = new AbortController();
    request = active;
    results.innerHTML = '<p class="panel compare-message" role="status">正在读取对比数据…</p>';
    try {
      const data = await get('/api/games/compare?' + gameQuery(selected), active.signal);
      if (active.signal.aborted) return;
      loadedGames = new Map(data.games.map(game => [String(game.appid), game]));
      renderComparison();
    } catch (error) {
      if (!active.signal.aborted) results.innerHTML = `<div class="panel compare-message"><p role="alert">无法读取对比数据。${esc(error.message)}</p>
        <button class="button" type="button" data-compare-retry>重试</button></div>`;
    }
  }

  function writeSelectionUrl() {
    const query = gameQuery(selected);
    history.pushState(null, '', route('compare') + (query ? '?' + query : ''));
  }

  function setSelection(ids) {
    if (ids.join(',') === selected.join(',')) return;
    selected = [...ids];
    styleFor = comparisonStyles(selected);
    view = {};
    writeSelectionUrl();
    renderSelection();
    renderResults();
  }

  bindComparisonReorder(page, { signal, swap(source, target, list, keyboard) {
    const from = selected.indexOf(source);
    const to = selected.indexOf(target);
    if (from < 0 || to < 0 || from === to) return;
    [selected[from], selected[to]] = [selected[to], selected[from]];
    writeSelectionUrl();
    const scrollY = window.scrollY;
    renderSelection();
    if (loadedGames) renderComparison();
    window.scrollTo(0, scrollY);
    page.querySelector(`[data-reorder-list="${list}"] [data-reorder-item="${source}"] [data-reorder-handle]`)
      ?.focus({ preventScroll: !keyboard });
    page.querySelector('[data-compare-order-status]').textContent = `${catalog.get(source).name}已移至第 ${to + 1} 位。`;
  } });

  function updateDraftControls() {
    const max = Math.min(MAX_GAMES, catalog.size);
    for (const button of choices.querySelectorAll('[data-appid]')) {
      const chosen = draft.includes(button.dataset.appid);
      button.setAttribute('aria-pressed', String(chosen));
      button.setAttribute('aria-disabled', String(!chosen && draft.length >= max));
    }
    apply.disabled = draft.length < MIN_GAMES;
    const hint = apply.disabled ? '，至少选择 2 款' : draft.length === max ? '，可先取消一款再添加' : '';
    dialog.querySelector('.compare-dialog-foot [role="status"]').textContent = `已选 ${draft.length} / ${MAX_GAMES} 款${hint}`;
  }

  function renderChoices() {
    const keyword = search.value.trim().toLocaleLowerCase();
    const visible = [...catalog.values()].filter(game => [game.name, game.name_en, game.appid]
      .some(value => String(value ?? '').toLocaleLowerCase().includes(keyword)));
    choices.innerHTML = visible.length ? visible.map(game => `<button class="compare-choice" type="button" data-appid="${game.appid}">
      ${picture(game)}<span><strong>${esc(game.name)}</strong><small>${game.total > 0 ? `${count(game.total)} 条评测` : '暂无评测'} · AppID ${game.appid}</small></span>
      <span class="selection-check" aria-hidden="true"></span></button>`).join('') : '<p class="compare-message" role="status">没有匹配的游戏。</p>';
    choices.scrollTop = 0;
    updateDraftControls();
  }

  page.addEventListener('click', event => {
    const button = event.target.closest('button');
    if (!button) return;
    if (button.hasAttribute('data-compare-open')) {
      dialogOpener = button;
      draft = [...selected];
      search.value = '';
      renderChoices();
      dialog.showModal();
      return;
    }
    if (button.hasAttribute('data-compare-close')) dialog.close();
    if (button.hasAttribute('data-compare-suggested')) {
      setSelection(suggested);
      chooseButton.focus();
    }
    if (button.hasAttribute('data-compare-retry')) {
      renderResults();
      chooseButton.focus();
    }
    if (button.dataset.remove) {
      setSelection(selected.filter(id => id !== button.dataset.remove));
      focusSelectionControl();
    }
    const id = button.dataset.appid;
    if (id && button.getAttribute('aria-disabled') !== 'true') {
      draft = draft.includes(id) ? draft.filter(item => item !== id) : [...draft, id];
      updateDraftControls();
    }
    if (button === apply) {
      setSelection(draft);
      dialog.close();
    }
  });
  search.addEventListener('input', renderChoices);
  dialog.addEventListener('close', () => {
    if (!signal.aborted) (dialogOpener?.isConnected ? dialogOpener : chooseButton).focus();
  });
  signal.addEventListener('abort', () => {
    request?.abort();
    disposeTrend();
    if (dialog.open) dialog.close();
  }, { once: true });

  renderSelection();
  renderResults();
}
