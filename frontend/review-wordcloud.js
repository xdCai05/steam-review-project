const wordCloudPalettes = {
  light: {lead: '#26305f', colors: ['#e24d9b', '#219984', '#ed784e', '#288bd2', '#9364dc', '#be8b1e', '#56a06b', '#cf5681']},
  dark: {lead: '#d7efff', colors: ['#f28abe', '#72dbbd', '#ffa585', '#75c4f1', '#c1a3fa', '#e9c66f', '#a9d376', '#f396b0']}
};
const wordCloudFont = 'Microsoft YaHei, sans-serif';
const wordCloudColor = (index, {lead, colors}) => index === 0 ? lead : colors[(index - 1) % colors.length];

function wordCloudLayout(words, width, height, measure) {
  const step = 4;
  const cols = Math.floor(width / step);
  const rows = Math.floor(height / step);
  const stride = cols + 1;
  const occupied = new Uint8Array(rows * cols);
  const sums = new Int32Array((rows + 1) * stride);
  const limit = width < 480 ? 65 : width < 800 ? 95 : 140;
  const scale = Math.min(1.7, Math.sqrt(width * height / (960 * 360)), width / 950 + .15);
  const anchors = [[.5, .48], [.27, .25], [.72, .76]];
  const entries = [];

  for (const [index, entry] of words.slice(0, limit).entries()) {
    const group = index < 3 ? 0 : index < 14 ? 1 : 2;
    const weight = [850, 700, 600][group];
    const size = group === 0 ? 108 - index * 16 : group === 1 ? 57 - (index - 3) * 2 : 31 - (index - 14) * .085;
    const [anchorX, anchorY] = anchors[index] || [
      .10 + .80 * ((index * .61803399) % 1), .10 + .80 * ((index * .75487767) % 1)
    ];
    const padding = group === 0 ? 8 : 4;
    const goal = Math.max(13, Math.round(size * (group === 0 ? Math.min(1.34, scale) : scale)));

    // Summed occupancy lets each candidate rectangle check for overlaps in constant time.
    for (let y = 0; y < rows; y++) {
      let rowTotal = 0;
      for (let x = 0; x < cols; x++) {
        rowTotal += occupied[y * cols + x];
        sums[(y + 1) * stride + x + 1] = sums[y * stride + x + 1] + rowTotal;
      }
    }
    for (const fontSize of new Set([goal, Math.max(13, Math.round(goal * .82)), Math.max(13, Math.round(goal * .68))])) {
      const font = `${weight} ${fontSize}px ${wordCloudFont}`;
      const boxWidth = Math.ceil((measure(entry.word, font) + padding) / step);
      const boxHeight = Math.ceil((fontSize * 1.12 + padding) / step);
      const targetX = cols * anchorX - boxWidth / 2;
      const targetY = rows * anchorY - boxHeight / 2;
      let bestScore = Infinity;
      let bestX, bestY;
      for (let y = 1; y + boxHeight < rows; y++) {
        const top = y * stride;
        const bottom = (y + boxHeight) * stride;
        const dy = (y - targetY) ** 2;
        if (dy >= bestScore) continue;
        for (let x = 1; x + boxWidth < cols; x++) {
          const score = (x - targetX) ** 2 + dy;
          if (score >= bestScore) continue;
          const right = x + boxWidth;
          if (sums[bottom + right] - sums[bottom + x] - sums[top + right] + sums[top + x]) continue;
          bestScore = score;
          bestX = x;
          bestY = y;
        }
      }
      if (bestX === undefined) continue;
      for (let y = bestY; y < bestY + boxHeight; y++) {
        occupied.fill(1, y * cols + bestX, y * cols + bestX + boxWidth);
      }
      entries.push({...entry, font, fontSize, weight, index,
        x:(bestX + boxWidth / 2) * step, y:(bestY + boxHeight / 2) * step});
      break;
    }
  }
  return entries;
}

function reviewWordCloudMarkup() {
  return `<section class="panel wordcloud-panel" id="review-wordcloud" aria-labelledby="wordcloud-title">
    <header class="wordcloud-header">
      <h2 id="wordcloud-title">评测词云</h2>
      <div class="wordcloud-controls">
        <p class="wordcloud-status" role="status"></p>
        <button class="button button--primary" type="button" disabled>生成词云</button>
      </div>
    </header>
    <div class="wordcloud-display">
      <div class="wordcloud-surface">
        <div class="wordcloud-content">
          <div class="wordcloud-preview">
            <img src="/assets/review-wordcloud-preview.svg?v=2" alt="词云生成预览" width="1440" height="460" decoding="async">
          </div>
        </div>
      </div>
    </div>
  </section>`;
}

function wordCloudResultMarkup(hasWords) {
  return `<div class="wordcloud-content">${hasWords ? `<div class="wordcloud-live">
        <div class="wordcloud-chart" aria-label="按提及评测数绘制的词云"></div>
        <div class="wordcloud-ranked"><h3>提及最多</h3><ol></ol></div>
      </div>` : '<p class="wordcloud-empty"></p>'}</div><p class="wordcloud-meta"></p>`;
}

function mountWordCloudDisplay(display, signal, search) {
  const clock = new Intl.DateTimeFormat('zh-CN', {
    timeZone: 'Asia/Shanghai', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false
  });
  const reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)');
  let active = { element: display.firstElementChild };
  let staged;
  let motion;

  function renderSummary(element, result) {
    const empty = element.querySelector('.wordcloud-empty');
    if (empty) empty.textContent = result.reviews ? '有效词语不足，暂无可展示的词云。' : '当前筛选条件下暂无可统计的评测正文。';
    const coverage = result.reviews ? `${day(result.first)} 至 ${day(result.last)}` : '无';
    const meta = element.querySelector('.wordcloud-meta');
    const text = `评测覆盖区间 ${coverage} · 纳入 ${count(result.reviews)} 条 · ${clock.format(result.generated_at * 1000)} 更新`;
    if (meta.textContent !== text) meta.textContent = text;
  }

  function prepareChart(element, words) {
    if (!words.length) return;
    const ranked = element.querySelector('.wordcloud-ranked ol');
    const target = element.querySelector('.wordcloud-chart');
    ranked.innerHTML = words.slice(0, 5).map(({word, reviews}) =>
      `<li><button type="button" class="button button--quiet" data-word="${esc(word)}" title="搜索含「${esc(word)}」的评测">
       <i aria-hidden="true"></i>
       <span>${esc(word)}</span><strong>${count(reviews)} 条</strong></button></li>`).join('');
    const dots = ranked.querySelectorAll('i');
    const context = window.echarts && document.createElement('canvas').getContext('2d');
    const chart = context && window.echarts.init(target, null, {renderer: 'svg'});
    let items = [];
    const recolor = () => {
      const palette = wordCloudPalettes[isDarkTheme() ? 'dark' : 'light'];
      dots.forEach((dot, index) => {
        dot.style.setProperty('--word-color', wordCloudColor(index, palette));
      });
      chart?.setOption({
        animation: false,
        tooltip: {
          trigger:'item', renderMode:'richText', ...chartTooltipAppearance(),
          formatter:({value}) => `${value[0]} · ${count(value[1])} 条评测`
        },
        series: [{
          type:'custom', coordinateSystem:'none',
          data:items.map(({word, reviews}) => [word, reviews]),
          renderItem({dataIndex}) {
            const item = items[dataIndex];
            return {type:'text', x:item.x, y:item.y, cursor:'pointer',
              style:{text:item.word, font:item.font, fill:wordCloudColor(item.index, palette), align:'center', verticalAlign:'middle'}};
          }
        }]
      }, {notMerge:true});
    };
    if (!chart) {
      recolor();
      return {recolor};
    }
    const draw = () => {
      items = wordCloudLayout(words, chart.getWidth(), chart.getHeight(), (word, font) => {
        context.font = font;
        return context.measureText(word).width;
      });
      recolor();
    };
    try {
      draw();
    } catch (error) {
      chart.dispose();
      throw error;
    }
    chart.on('click', ({value}) => { if (value?.[0]) search(value[0]); });
    const observer = new ResizeObserver(([{contentRect: {width, height}}]) => {
      if (width && height && (Math.round(width) !== chart.getWidth() || Math.round(height) !== chart.getHeight())) {
        chart.resize({width: Math.round(width), height: Math.round(height)});
        draw();
      }
    });
    return { recolor, observe: () => observer.observe(target), dispose() { observer.disconnect(); chart.dispose(); } };
  }

  async function fade(element, from, to, duration) {
    motion = element.animate([{opacity: from}, {opacity: to}], {duration, fill: 'forwards', easing: 'ease-in-out'});
    await motion.finished.catch(() => {});
  }

  async function show(result) {
    const wordsKey = JSON.stringify(result.words);
    if (active.wordsKey === wordsKey) {
      renderSummary(active.element, result);
      return;
    }

    const element = document.createElement('div');
    element.className = 'wordcloud-surface is-staged';
    element.inert = true;
    element.innerHTML = wordCloudResultMarkup(result.words.length > 0);
    renderSummary(element, result);
    display.append(element);
    staged = { element, wordsKey };
    try {
      Object.assign(staged, prepareChart(element, result.words));
    } catch (error) {
      element.remove();
      staged = null;
      throw error;
    }

    const animate = !reducedMotion.matches;
    if (animate) await fade(active.element, 1, 0, 105);
    if (signal.aborted) return;
    motion?.cancel();
    active.dispose?.();
    active.element.remove();
    active = staged;
    staged = null;
    element.classList.remove('is-staged');
    element.inert = false;
    active.observe?.();
    if (animate) {
      await fade(element, 0, 1, 185);
      motion?.cancel();
    }
  }

  display.addEventListener('click', event => {
    const word = event.target.closest('[data-word]')?.dataset.word;
    if (word) search(word);
  });
  window.addEventListener('steam-theme-change', () => {
    active.recolor?.();
    staged?.recolor?.();
  }, {signal});
  signal.addEventListener('abort', () => {
    motion?.cancel();
    active.dispose?.();
    staged?.dispose?.();
  }, {once:true});
  return show;
}

function activateWordCloud(panel, params, signal) {
  const keys = ['game', 'start', 'end', 'rating', 'playtime'];
  const scope = new URLSearchParams(keys.filter(key => params.get(key)).map(key => [key, params.get(key)]));
  const url = '/api/review-wordcloud?' + scope;
  const status = panel.querySelector('.wordcloud-status');
  const button = panel.querySelector('.wordcloud-controls button');
  const show = mountWordCloudDisplay(panel.querySelector('.wordcloud-display'), signal, word => {
    const next = new URLSearchParams(params);
    next.set('q', word);
    next.delete('page');
    location.hash = route('reviews', Object.fromEntries(next));
  });
  let timer;
  let runningSince;
  let upgradingCache = false;

  async function refresh(start = false) {
    clearTimeout(timer);
    try {
      let state;
      if (start) {
        button.disabled = true;
        status.textContent = '正在统计评测…';
        status.classList.add('is-busy');
        runningSince = Date.now();
        const response = await fetch(url, {
          method: 'POST', headers: {'Content-Type': 'application/json'}, body: '{}', signal
        });
        state = await response.json();
        if (!response.ok) throw new Error(state.error || '词云生成失败');
      } else {
        state = await get(url, signal);
      }
      if (signal.aborted) return;
      if (state.needs_refresh && !state.running && !state.error && !upgradingCache) {
        upgradingCache = true;
        void refresh(true);
        return;
      }
      if (state.result && !state.needs_refresh && !state.running) {
        // Render only a finished result from the current cache format.
        await show(state.result);
        if (signal.aborted) return;
      }
      button.disabled = state.running;
      button.textContent = state.result && !state.needs_refresh ? '更新词云' : '生成词云';
      status.classList.toggle('is-busy', state.running);
      if (state.running) {
        runningSince ||= Date.now();
        status.textContent = Date.now() - runningSince > 5000 ? '仍在统计，可继续浏览评测' :
          state.needs_refresh ? '正在更新词云版本…' : '正在统计评测…';
        timer = setTimeout(refresh, 900);
      } else {
        runningSince = null;
        status.textContent = state.error || '';
      }
    } catch (error) {
      if (signal.aborted) return;
      status.classList.remove('is-busy');
      status.textContent = error.message || '无法读取词云';
      button.disabled = false;
    }
  }

  button.addEventListener('click', () => refresh(true));
  signal.addEventListener('abort', () => {
    clearTimeout(timer);
  }, {once:true});
  refresh();
}
