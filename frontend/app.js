const app = document.querySelector('#app');
const number = new Intl.NumberFormat('zh-CN');
const percentage = new Intl.NumberFormat('zh-CN', { style: 'percent', maximumFractionDigits: 1 });
const formatDay = new Intl.DateTimeFormat('zh-CN', { timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit' });
const esc = value => String(value ?? '').replace(/[&<>"']/g, char => ({'&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;'}[char]));
const day = ts => ts == null ? '—' : formatDay.format(new Date(ts * 1000));
const count = n => number.format(n ?? 0);
const formatReviewMB = bytes => (bytes / 1000000).toLocaleString('zh-CN', {maximumFractionDigits: 1}) + ' MB';
const pct = (positive, rated) => rated ? (100 * positive / rated).toFixed(1) + '%' : '—';
const route = (path, params = {}) => '#/' + path + (Object.keys(params).length ? '?' + new URLSearchParams(params) : '');
const list = items => items?.length ? items.map(v => typeof v === 'string' ? v : v.description || v.name || '').filter(Boolean).map(esc).join('、') || '—' : '—';
const gamesById = new Map();
const gamesPerPage = 8;
const collectionStates = [
  ['采集完成', 'success', '#469cf2', '#aec567'],
  ['达到容量上限', 'limited', '#efa744', '#edb767'],
  ['采集未完成', 'limited', '#cf7467', '#f19788'],
  ['暂无采集记录', 'pending', '#7186a4', '#8a9eb1']
];
const isDarkTheme = () => document.documentElement.dataset.theme === 'dark';
const chartTooltipAppearance = () => isDarkTheme() ? {
  backgroundColor: 'rgba(29, 40, 52, .98)', borderColor: '#506779', borderWidth: 1,
  textStyle: { color: '#e8f2f9' }
} : {};
let activeLoad;
const navigation = [
  ['games', '', '游戏总览', '<rect x="3" y="6" width="18" height="12" rx="4"/><path d="M7 12h5m-2.5-2.5v5m6-3h.01m2.5 2h.01"/>'],
  ['reviews', 'reviews', '评测浏览', '<path d="M20 11.5a6.5 6.5 0 0 1-6.5 6.5H10l-4 3v-4a6.5 6.5 0 0 1 3.5-12h4a6.5 6.5 0 0 1 6.5 6.5Z"/><path d="M9 11h6"/>'],
  ['compare', 'compare', '游戏对比', '<path d="m12 3 9 5-9 5-9-5 9-5Zm-9 9 9 5 9-5m-18 4 9 5 9-5"/>'],
  ['agent', 'agent', '评测问答', '<circle cx="12" cy="12" r="9"/><path d="M9.5 9a2.5 2.5 0 1 1 4 2c-1 .7-1.5 1.2-1.5 2.5m0 3h.01"/>']
];
const icon = shape => `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${shape}</svg>`;

for (const selector of ['#side-nav', '#dock']) {
  document.querySelector(selector).innerHTML = '<span class="nav-indicator" aria-hidden="true"></span>' +
    navigation.map(([key,path,label,shape]) =>
      `<a href="${route(path)}" data-nav="${key}">${icon(shape)}<span>${label}</span></a>`).join('');
}

function updateNavigation(section) {
  const activeIndex = navigation.findIndex(([key]) => key === section);
  for (const nav of document.querySelectorAll('#side-nav, #dock')) {
    nav.classList.toggle('has-selection', activeIndex >= 0);
    if (activeIndex >= 0) nav.style.setProperty('--active-index', activeIndex);
    for (const link of nav.querySelectorAll('[data-nav]')) {
      if (link.dataset.nav === section) link.setAttribute('aria-current', 'page');
      else link.removeAttribute('aria-current');
    }
  }
}

async function get(path, signal) {
  const response = await fetch(path, { signal });
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || '读取失败');
  return data;
}

async function loadGames(signal) {
  const games = await get('/api/games', signal);
  games.forEach(game => gamesById.set(game.appid, game));
  return games;
}

function picture(game, extra = '') {
  return `<div class="cover ${extra}">${game.has_cover === false ? '' : `<img src="/cover/${game.appid}" alt="" width="460" height="215" loading="lazy" decoding="async">`}</div>`;
}

function status(game) {
  const state = collectionStates.find(([label]) => label === game.status);
  return `<span class="status ${state?.[1] || 'pending'}">${esc(game.status || '状态未知')}</span>`;
}

function gameCard(game) {
  const size = game.review_bytes == null ? '—' : game.review_bytes === 0 ? '0 MB' :
    game.review_bytes < 100000 ? '< 0.1 MB' : formatReviewMB(game.review_bytes);
  return `<a class="game-card" href="${route('game/' + game.appid)}">
    ${picture(game)}<div class="card-body"><h2 title="${esc(game.name)}">${esc(game.name)}</h2>
    <div class="card-footer"><div class="card-metrics"><span>${count(game.total)} 条评测</span>
    ${game.rated ? `<span class="recommendation" title="采集样本推荐比例">${pct(game.positive, game.rated)} 推荐</span>` : ''}
    ${game.store_ready === false ? `<span class="status pending">${game.store_checked ? '商店资料不可用' : '商店资料待同步'}</span>` : status(game)}</div>
    <span class="card-review-size" title="评测文件磁盘占用" aria-label="评测文件磁盘占用 ${esc(size)}">${esc(size)}</span></div>
    </div></a>`;
}

function collectionOverview(games) {
  const panel = app.querySelector('.collection-overview');
  const totals = new Map(collectionStates.map(([label]) => [label, 0]));
  const palettes = new Map(collectionStates.map(([label, , light, dark]) => [label, { light, dark }]));
  let reviewTotal = 0;
  for (const game of games) {
    const label = totals.has(game.status) ? game.status : '状态未知';
    totals.set(label, (totals.get(label) || 0) + 1);
    reviewTotal += game.total ?? 0;
  }
  const states = [...totals].map(([label, total]) => ({ label, total }));
  panel.innerHTML = `<div class="collection-summary">
    <h2 id="collection-title">采集总览</h2>
    <dl class="collection-totals">
      <div><dt>入库评测</dt><dd>${count(reviewTotal)}</dd></div>
    </dl></div>
    ${games.length ? `<div class="collection-distribution">
      <div class="collection-ring">
        <div class="collection-ring-fallback" aria-hidden="true"></div>
        <div class="collection-chart" aria-hidden="true"></div>
        <div class="collection-ring-total"><strong>${count(games.length)}</strong><span>款游戏</span></div>
      </div>
      <ul class="collection-legend" role="list" aria-label="全部游戏采集状态">
        ${states.map(({ label, total }) => `<li${total ? '' : ' class="is-zero"'}>
          <span class="collection-state"><span class="collection-dot" aria-hidden="true"></span>${label}</span>
          <span class="collection-quantity">${count(total)} 款</span>
          <span class="collection-share">${percentage.format(total / games.length)}</span>
        </li>`).join('')}
      </ul>
    </div>` : '<p class="notice">暂无游戏数据</p>'}`;

  if (!games.length) return;
  const container = panel.querySelector('.collection-chart');
  const fallback = panel.querySelector('.collection-ring-fallback');
  const dots = panel.querySelectorAll('.collection-dot');
  const instance = window.echarts?.init(container, null, { renderer: 'svg' });
  const updateColors = () => {
    const theme = isDarkTheme() ? 'dark' : 'light';
    const entries = states.map(({ label, total }, index) => {
      const color = (palettes.get(label) || palettes.get('暂无采集记录'))[theme];
      dots[index].style.setProperty('--status-color', color);
      return { name: label, value: total, itemStyle: { color } };
    }).filter(({ value }) => value > 0);

    if (!instance) {
      let angle = 0;
      const stops = entries.map(({ value, itemStyle: { color } }) => {
        const start = angle;
        angle += value / games.length * 360;
        return `${color} ${start}deg ${angle}deg`;
      });
      fallback.style.background = `conic-gradient(${stops.join(', ')})`;
      return;
    }
    instance.setOption({
      animation: false,
      tooltip: {
        trigger: 'item', renderMode: 'html', confine: false, transitionDuration: 0,
        ...chartTooltipAppearance(),
        position: (_point, _data, _element, _rect, size) => [(size.viewSize[0] - size.contentSize[0]) / 2, -8],
        formatter: ({ name, value }) => `${name}  ${count(value)} 款 · ${percentage.format(value / games.length)}`
      },
      series: [{
        type: 'pie', radius: ['57%', '86%'], padAngle: entries.length > 1 ? 3 : 0,
        label: { show: false }, labelLine: { show: false },
        itemStyle: { borderRadius: 4 }, emphasis: { scale: false }, data: entries
      }]
    }, { notMerge: true });
  };
  updateColors();
  window.addEventListener('steam-theme-change', updateColors);
  let observer;
  if (instance) {
    fallback.hidden = true;
    observer = new ResizeObserver(([{contentRect: {width, height}}]) => {
      if (width && height && (width !== instance.getWidth() || height !== instance.getHeight())) {
        instance.resize({width, height});
      }
    });
    observer.observe(container);
  }
  return () => {
    window.removeEventListener('steam-theme-change', updateColors);
    observer?.disconnect();
    instance?.dispose();
  };
}

let pickerSequence = 0;

function positionFilterPopup(trigger, popup, {maxHeight, matchWidth = false}) {
  const rect = trigger.getBoundingClientRect();
  const width = document.documentElement.clientWidth;
  const height = document.documentElement.clientHeight;
  if (rect.bottom < 0 || rect.top > height) {
    popup.hidePopover();
    return;
  }
  const below = height - rect.bottom - 14;
  const above = rect.top - 14;
  if (matchWidth) popup.style.minWidth = `${rect.width}px`;
  popup.style.maxWidth = `${width - 16}px`;
  const up = below < Math.min(popup.scrollHeight + 2, maxHeight) && above > below;
  popup.style.maxHeight = `${Math.max(0, Math.min(maxHeight, up ? above : below))}px`;
  popup.style.left = `${Math.max(8, Math.min(rect.left, width - 8 - popup.offsetWidth))}px`;
  popup.style.top = `${up ? rect.top - 6 - popup.offsetHeight : rect.bottom + 6}px`;
}

function enhanceSelect(select, signal) {
  if (!HTMLElement.prototype.showPopover) return () => {};

  const picker = document.createElement('div');
  picker.className = 'select-picker';
  const trigger = document.createElement('button');
  trigger.type = 'button';
  trigger.className = 'select-trigger';
  trigger.setAttribute('role', 'combobox');
  trigger.setAttribute('aria-expanded', 'false');
  const label = select.getAttribute('aria-label') || select.labels[0]?.textContent.trim();
  if (label) trigger.setAttribute('aria-label', label);
  trigger.innerHTML = `<span class="select-value"></span>${icon('<path d="m6 9 6 6 6-6"/>')}`;
  const value = trigger.firstElementChild;

  const menu = document.createElement('div');
  menu.id = `select-options-${++pickerSequence}`;
  menu.className = 'select-menu';
  menu.setAttribute('popover', 'auto');
  menu.setAttribute('role', 'listbox');
  if (label) menu.setAttribute('aria-label', label);
  trigger.setAttribute('aria-controls', menu.id);
  const fieldLabel = select.labels[0];
  select.before(picker);
  picker.append(select, trigger, menu);
  select.hidden = true;
  fieldLabel?.addEventListener('click', event => {
    if (!picker.contains(event.target)) {
      event.preventDefault();
      trigger.focus();
    }
  });

  let activeIndex = -1;
  let search = '';
  let lastTyped = 0;
  let positioning;
  const isOpen = () => menu.matches(':popover-open');
  const sync = () => {
    const option = select.selectedOptions[0];
    value.textContent = option?.label || '';
    trigger.disabled = select.disabled;
    trigger.classList.toggle('has-value', Boolean(option?.value));
    if (isOpen()) close();
  };
  const position = () => positionFilterPopup(trigger, menu, {maxHeight: 320, matchWidth: true});
  function close() {
    if (isOpen()) menu.hidePopover();
  }
  function highlight(index) {
    if (index < 0 || select.options[index]?.disabled || !menu.children[index]) return;
    menu.children[activeIndex]?.classList.remove('is-highlighted');
    activeIndex = index;
    const item = menu.children[index];
    item.classList.add('is-highlighted');
    trigger.setAttribute('aria-activedescendant', item.id);
    if (item.offsetTop < menu.scrollTop) menu.scrollTop = item.offsetTop;
    const bottom = item.offsetTop + item.offsetHeight;
    if (bottom > menu.scrollTop + menu.clientHeight) menu.scrollTop = bottom - menu.clientHeight;
  }
  function open() {
    menu.replaceChildren(...Array.from(select.options, (option, index) => {
      const item = document.createElement('div');
      item.id = `${menu.id}-${index}`;
      item.className = 'select-option';
      item.setAttribute('role', 'option');
      item.setAttribute('aria-selected', String(option.selected));
      if (option.disabled) item.setAttribute('aria-disabled', 'true');
      const text = document.createElement('span');
      text.textContent = option.label;
      item.append(text);
      item.insertAdjacentHTML('beforeend', '<span class="selection-check" aria-hidden="true"></span>');
      return item;
    }));
    menu.showPopover();
    trigger.setAttribute('aria-expanded', 'true');
    position();
    highlight(select.selectedIndex);
    positioning = new AbortController();
    window.addEventListener('resize', position, {signal: positioning.signal});
    window.addEventListener('scroll', event => {
      if (event.target !== menu) position();
    }, {capture: true, signal: positioning.signal});
  }
  function choose(index) {
    const option = select.options[index];
    if (!option || option.disabled) return;
    close();
    if (index === select.selectedIndex) return;
    select.selectedIndex = index;
    select.dispatchEvent(new Event('input', {bubbles: true}));
    select.dispatchEvent(new Event('change', {bubbles: true}));
  }
  menu.addEventListener('beforetoggle', event => {
    if (event.newState !== 'closed') return;
    positioning?.abort();
    activeIndex = -1;
    search = '';
    trigger.setAttribute('aria-expanded', 'false');
    trigger.removeAttribute('aria-activedescendant');
  });
  trigger.addEventListener('blur', close);
  trigger.addEventListener('click', () => isOpen() ? close() : open());
  menu.addEventListener('pointerdown', event => {
    if (event.target.closest('.select-option')) event.preventDefault();
  });
  menu.addEventListener('click', event => {
    const item = event.target.closest('.select-option');
    if (item) choose(Array.from(menu.children).indexOf(item));
  });
  trigger.addEventListener('keydown', event => {
    const {key} = event;
    if (key === 'Tab') return close();
    if (key === 'Escape' && isOpen()) {
      event.preventDefault();
      event.stopPropagation();
      return close();
    }
    if (['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(key)) {
      event.preventDefault();
      const wasOpen = isOpen();
      if (!wasOpen) open();
      const options = Array.from(select.options);
      const first = options.findIndex(option => !option.disabled);
      const last = options.findLastIndex(option => !option.disabled);
      if (key === 'Home') highlight(first);
      else if (key === 'End') highlight(last);
      else if (wasOpen) {
        const step = key === 'ArrowDown' ? 1 : -1;
        for (let index = activeIndex + step; index >= 0 && index < options.length; index += step) {
          if (!options[index].disabled) {
            highlight(index);
            break;
          }
        }
      }
      return;
    }
    if ((key === 'Enter' || key === ' ') && isOpen()) {
      event.preventDefault();
      return choose(activeIndex);
    }
    if (key.length !== 1 || key === ' ' || event.ctrlKey || event.metaKey || event.altKey || event.isComposing) return;
    event.preventDefault();
    if (!isOpen()) open();
    const now = Date.now();
    search = (now - lastTyped < 650 ? search : '') + key.toLocaleLowerCase();
    lastTyped = now;
    const index = [...select.options].findIndex(option => !option.disabled && option.label.toLocaleLowerCase().startsWith(search));
    highlight(index);
  });
  select.addEventListener('change', sync);
  signal?.addEventListener('abort', close, {once: true});
  sync();
  return sync;
}

function overview(games, signal) {
  const genre = item => typeof item === 'string' ? item : item.description || '';
  const genreOptions = () => [...new Set(games.flatMap(game => game.genres.map(genre)).filter(Boolean))]
    .map(value => `<option>${esc(value)}</option>`).join('');
  app.innerHTML = `<div class="overview"><div class="game-search">${icon('<circle cx="10.5" cy="10.5" r="6.5"/><path d="m16 16 4 4"/>')}
    <input id="game-search" type="search" aria-label="搜索游戏" placeholder="搜索游戏、类型或开发商"></div>
    <section class="page-head overview-head"><div class="title-row"><h1>游戏总览</h1><span class="game-count"></span></div>
    <div class="actions"><button class="button overview-add" type="button">添加游戏</button>
    <a class="button button--primary" href="#/reviews">浏览评测</a></div></section>
    <div class="catalog-tools"><select id="game-genre" aria-label="游戏类型"><option value="">全部类型</option>
    ${genreOptions()}</select>
    <select id="game-sort" aria-label="游戏排序"><option value="">默认排序</option><option value="count">评测量优先</option>
    <option value="rating">推荐比例优先</option></select></div>
    <p class="notice games-empty" hidden>没有符合条件的游戏</p>
    <div class="games-grid" id="game-cards"></div>
    <nav class="pagination catalog-pagination" aria-label="游戏分页">
      <button class="button" id="games-prev" aria-controls="game-cards" type="button">上一页</button>
      <span id="games-page" role="status"></span>
      <button class="button" id="games-next" aria-controls="game-cards" type="button">下一页</button>
    </nav>
    <section class="panel collection-overview" aria-labelledby="collection-title"></section></div>`;
  const search = app.querySelector('#game-search');
  const category = app.querySelector('#game-genre');
  const sort = app.querySelector('#game-sort');
  const syncCategory = enhanceSelect(category, signal);
  enhanceSelect(sort, signal);
  const grid = app.querySelector('#game-cards');
  const empty = app.querySelector('.games-empty');
  const pager = app.querySelector('.catalog-pagination');
  const prev = pager.querySelector('#games-prev');
  const next = pager.querySelector('#games-next');
  const pageLabel = pager.querySelector('#games-page');
  let page = 1;
  let visible = [];
  const pageCount = () => Math.ceil((visible.length + 1) / gamesPerPage);

  const showPage = () => {
    const pages = pageCount();
    const start = (page - 1) * gamesPerPage;
    const cards = visible.slice(start, start + gamesPerPage);
    const showAdd = page === pages;
    const slots = Math.min(visible.length + 1, gamesPerPage);
    empty.hidden = visible.length > 0;
    grid.innerHTML = cards.map(gameCard).join('');
    if (showAdd) grid.insertAdjacentHTML('beforeend', `<button class="add-game-tile" data-add-game type="button">
      ${icon('<path d="M12 5v14M5 12h14"/>')}<span>添加游戏</span></button>`);
    grid.insertAdjacentHTML('beforeend', '<div aria-hidden="true"></div>'.repeat(slots - cards.length - Number(showAdd)));
    pager.hidden = !visible.length;
    pageLabel.textContent = `第 ${count(page)} / ${count(pages)} 页`;
    prev.disabled = page === 1;
    next.disabled = page === pages;
  };

  const update = (keepPage = false) => {
    const query = search.value.trim().toLowerCase();
    visible = games.filter(game => (!category.value || game.genres.some(item => genre(item) === category.value))
      && [game.name, game.name_en, game.appid, ...game.genres.map(genre), ...game.developers].some(value => String(value || '').toLowerCase().includes(query)));
    const ratio = game => game.rated ? game.positive / game.rated : -1;
    if (sort.value === 'count') visible.sort((a,b) => b.total - a.total);
    if (sort.value === 'rating') visible.sort((a,b) => ratio(b) - ratio(a));
    app.querySelector('.game-count').textContent = `${count(visible.length)} 款游戏`;
    page = keepPage ? Math.min(page, pageCount()) : 1;
    showPage();
  };

  const stepPage = step => {
    const keyboard = document.activeElement?.matches(':focus-visible');
    page += step;
    showPage();
    if (keyboard) grid.querySelector('.game-card, .add-game-tile').focus({ preventScroll: true });
    if (grid.getBoundingClientRect().top < 0) grid.scrollIntoView({ block: 'start' });
  };
  prev.addEventListener('click', () => stepPage(-1));
  next.addEventListener('click', () => stepPage(1));
  search.addEventListener('input', () => update());
  category.addEventListener('change', () => update());
  sort.addEventListener('change', () => update());
  app.querySelector('.overview-add').addEventListener('click', () => reviewTasks.open());
  grid.addEventListener('click', event => {
    if (event.target.closest('[data-add-game]')) reviewTasks.open();
  });
  update();
  let disposeChart = collectionOverview(games);
  signal.addEventListener('abort', () => disposeChart?.(), { once: true });
  window.addEventListener('review-collection-changed', async () => {
    try {
      const updated = await loadGames(signal);
      if (signal.aborted) return;
      games = updated;
      const selected = category.value;
      category.innerHTML = `<option value="">全部类型</option>${genreOptions()}`;
      category.value = [...category.options].some(option => option.value === selected) ? selected : '';
      syncCategory();
      update(true);
      disposeChart?.();
      disposeChart = collectionOverview(games);
    } catch (error) {
      if (!signal.aborted) console.error('无法刷新游戏总览', error);
    }
  }, {signal});
}

function chart(game) {
  if (!game.daily.length) return '<p class="notice">暂无评测趋势</p>';
  const max = Math.max(...game.daily.map(point => point.total));
  return `<section class="panel"><div class="panel-head"><h2>每日评测</h2><span>北京时间</span></div>
    <div class="chart" aria-label="每日评测量柱状图">${game.daily.map(point => {
      const label = point.day + ' · ' + count(point.total) + ' 条 · 推荐 ' + pct(point.positive, point.rated);
      const href = route('reviews', {game: game.appid, start: point.day, end: point.day});
      return `<a class="bar-slot" href="${href}" aria-label="${label}" data-label="${label}"><span class="bar" style="--bar:${100 * point.total / max}%"></span></a>`;
    }).join('')}</div><div class="axis"><span>${game.daily[0].day}</span><span>${game.daily.at(-1).day}</span></div></section>`;
}

function detail(game) {
  const price = game.price;
  const priceText = price?.is_free ? '免费' : price?.price_final != null && price.currency ?
    new Intl.NumberFormat('zh-CN', {style:'currency', currency:price.currency}).format(price.price_final / 100) : '暂无价格';
  const score = game.store_rating;
  const values = [
    ['开发商', list(game.developers)], ['发行商', list(game.publishers)], ['类型', list(game.genres)],
    ['发行日期', esc(game.release_date || game.release_date_raw || '—')], ['平台', list(game.platforms)],
    ['价格快照', esc(priceText) + (price ? ' · ' + esc(price.region.toUpperCase()) + ' · ' + day(price.collected_at) : '')],
    ['Steam 简体中文评价', score ? (score.positive_percent_display == null ? '' : esc(score.positive_percent_display) + '% · ') + count(score.total_reviews) + ' 条 · ' + day(score.collected_at) : '暂无数据']
  ];
  return `<div class="back"><a href="#/">游戏总览</a></div>
    <section class="detail-hero panel">
      ${picture(game)}<div class="hero-content">${status(game)}<h1>${esc(game.name)}</h1>
      ${game.short_description ? `<p>${esc(game.short_description)}</p>` : ''}
      <div class="actions detail-actions"><a class="button button--primary" href="${route('reviews', {game: game.appid})}">浏览评测</a>
      <a class="button" href="https://store.steampowered.com/app/${game.appid}/" target="_blank" rel="noopener noreferrer">Steam 商店</a></div></div>
    </section>
    <div class="metrics"><div><span>入库评测</span><strong>${count(game.total)}</strong></div>
    <div><span>样本推荐比例</span><strong>${pct(game.positive, game.rated)}</strong></div>
    <div><span>评测覆盖日期</span><strong class="dates">${day(game.first)} 至 ${day(game.last)}</strong></div></div>
    ${chart(game)}
    <div class="detail-grid"><section class="panel"><h2>游戏资料</h2><dl>${values.map(([key,value]) => `<div><dt>${key}</dt><dd>${value}</dd></div>`).join('')}</dl></section>
    <section class="panel"><h2>发表评测时的游玩时长</h2>
    ${game.playtime.length ? `<table><thead><tr><th>游玩时长</th><th>评测数</th><th>推荐比例</th></tr></thead><tbody>
    ${game.playtime.map(row => `<tr><td>${['不足 2 小时','2 至不足 10 小时','10 小时及以上'][row.bucket]}</td><td>${count(row.total)}</td><td>${pct(row.positive, row.rated)}</td></tr>`).join('')}</tbody></table>` : '<p class="notice">暂无游玩时长数据</p>'}</section></div>`;
}

function enhanceDate(input, signal) {
  if (!HTMLElement.prototype.showPopover) return;

  const label = input.labels[0];
  const trigger = document.createElement('button');
  trigger.type = 'button';
  trigger.className = 'select-trigger date-trigger';
  trigger.setAttribute('aria-haspopup', 'dialog');
  trigger.setAttribute('aria-expanded', 'false');
  trigger.innerHTML = `<span class="select-value" id="${input.id}-value"></span>
    ${icon('<rect x="3" y="5" width="18" height="16" rx="3"/><path d="M7 3v4m10-4v4M3 10h18m-13 5h.01m4 0h.01m4 0h.01m-8 3h.01m4 0h.01"/>')}`;
  trigger.setAttribute('aria-labelledby', `${label.id} ${input.id}-value`);
  const value = trigger.firstElementChild;
  const calendar = document.createElement('div');
  calendar.id = `${input.id}-calendar`;
  calendar.className = 'select-menu date-calendar';
  calendar.setAttribute('popover', 'auto');
  calendar.setAttribute('role', 'dialog');
  calendar.setAttribute('aria-label', `选择${label.textContent}`);
  trigger.setAttribute('aria-controls', calendar.id);
  input.after(trigger, calendar);
  input.hidden = true;
  label.addEventListener('click', event => {
    event.preventDefault();
    trigger.focus();
  });

  calendar.innerHTML = `<div class="calendar-head">
    <button type="button" class="button button--quiet" data-month="-1" aria-label="上个月">${icon('<path d="m15 6-6 6 6 6"/>')}</button>
    <strong id="${calendar.id}-month" aria-live="polite"></strong>
    <button type="button" class="button button--quiet" data-month="1" aria-label="下个月">${icon('<path d="m9 6 6 6-6 6"/>')}</button>
    </div><div class="calendar-grid" role="grid" aria-labelledby="${calendar.id}-month"></div>
    <div class="calendar-actions"><button type="button" class="button button--quiet" data-date-action="clear">清除</button>
    <button type="button" class="button button--quiet" data-date-action="today">今天</button></div>`;
  const monthLabel = calendar.querySelector('strong');
  const grid = calendar.querySelector('.calendar-grid');
  const monthButtons = [...calendar.querySelectorAll('[data-month]')];
  const parse = iso => new Date(`${iso}T00:00:00Z`);
  const iso = date => date.toISOString().slice(0, 10);
  const localDay = date => `${date.getUTCFullYear()}年${date.getUTCMonth() + 1}月${date.getUTCDate()}日`;
  const supported = date => date.getUTCFullYear() >= 1 && date.getUTCFullYear() <= 9999;
  const addDays = (date, count) => {
    const next = new Date(date);
    next.setUTCDate(next.getUTCDate() + count);
    return next;
  };
  const addMonths = (date, count) => {
    const next = new Date(date);
    next.setUTCDate(1);
    next.setUTCMonth(next.getUTCMonth() + count + 1, 0);
    next.setUTCDate(Math.min(date.getUTCDate(), next.getUTCDate()));
    return next;
  };
  let activeDate;
  let today;
  let positioning;
  const isOpen = () => calendar.matches(':popover-open');
  const position = () => positionFilterPopup(trigger, calendar, {maxHeight: 410});
  const sync = () => {
    value.textContent = input.value ? localDay(parse(input.value)) : '选择日期';
    trigger.classList.toggle('has-value', Boolean(input.value));
  };
  const close = (restoreFocus = false) => {
    if (!isOpen()) return;
    calendar.hidePopover();
    if (restoreFocus) trigger.focus({preventScroll:true});
  };
  function renderMonth(focusDay = false) {
    const first = new Date(activeDate);
    first.setUTCDate(1);
    const month = first.getUTCMonth();
    const year = first.getUTCFullYear();
    const weekOffset = (first.getUTCDay() + 6) % 7;
    const activeKey = iso(activeDate);
    monthLabel.textContent = `${year}年${month + 1}月`;
    monthButtons[0].disabled = year === 1 && month === 0;
    monthButtons[1].disabled = year === 9999 && month === 11;
    const weekdays = [...'一二三四五六日'].map(name => `<span role="columnheader" aria-label="星期${name}">${name}</span>`).join('');
    grid.innerHTML = `<div class="calendar-weekdays" role="row">${weekdays}</div>` +
      Array.from({length: 6}, (_, week) => {
        const days = Array.from({length: 7}, (_, column) => {
          const date = addDays(first, week * 7 + column - weekOffset);
          if (!supported(date)) return '<span role="gridcell"></span>';
          const key = iso(date);
          const selected = key === input.value;
          const current = key === today;
          return `<div role="gridcell" aria-selected="${selected}"><button type="button" data-date="${key}"
            class="calendar-day button button--${selected ? 'primary' : 'quiet'}${date.getUTCMonth() !== month ? ' outside-month' : ''}" tabindex="${key === activeKey ? '0' : '-1'}"
            aria-label="${localDay(date)}"${current ? ' aria-current="date"' : ''}>${date.getUTCDate()}</button></div>`;
        }).join('');
        return `<div class="calendar-week" role="row">${days}</div>`;
      }).join('');
    if (focusDay) grid.querySelector('[tabindex="0"]').focus();
  }
  function open() {
    today = formatDay.format(new Date()).replaceAll('/', '-');
    activeDate = parse(/^\d{4}-\d{2}-\d{2}$/.test(input.value) ? input.value : today);
    renderMonth();
    calendar.showPopover();
    trigger.setAttribute('aria-expanded', 'true');
    position();
    grid.querySelector('[tabindex="0"]').focus({preventScroll:true});
    positioning = new AbortController();
    window.addEventListener('resize', position, {signal: positioning.signal});
    window.addEventListener('scroll', position, {signal: positioning.signal});
  }
  function choose(date) {
    input.value = date;
    sync();
    close(true);
  }
  trigger.addEventListener('click', () => isOpen() ? close() : open());
  trigger.addEventListener('keydown', event => {
    if (event.key === 'ArrowDown' && !isOpen()) {
      event.preventDefault();
      open();
    }
  });
  calendar.addEventListener('beforetoggle', event => {
    if (event.newState !== 'closed') return;
    trigger.setAttribute('aria-expanded', 'false');
    positioning?.abort();
  });
  calendar.addEventListener('focusout', event => {
    if (event.relatedTarget && event.relatedTarget !== trigger && !calendar.contains(event.relatedTarget)) close();
  });
  calendar.addEventListener('click', event => {
    const button = event.target.closest('button');
    if (!button) return;
    const {month, date, dateAction} = button.dataset;
    if (month) {
      activeDate = addMonths(activeDate, Number(month));
      renderMonth(button.disabled);
    } else if (date || dateAction) {
      choose(date || (dateAction === 'today' ? today : ''));
    }
  });
  calendar.addEventListener('keydown', event => {
    if (event.key === 'Escape') {
      event.preventDefault();
      close(true);
      return;
    }
    if (!event.target.matches('.calendar-day')) return;
    const current = parse(event.target.dataset.date);
    const offset = (current.getUTCDay() + 6) % 7;
    const steps = {ArrowLeft:-1, ArrowRight:1, ArrowUp:-7, ArrowDown:7, Home:-offset, End:6-offset};
    let next;
    if (Object.hasOwn(steps, event.key)) next = addDays(current, steps[event.key]);
    if (event.key === 'PageUp' || event.key === 'PageDown') {
      next = addMonths(current, (event.key === 'PageUp' ? -1 : 1) * (event.shiftKey ? 12 : 1));
    }
    if (!next) return;
    event.preventDefault();
    if (!supported(next)) return;
    activeDate = next;
    renderMonth(true);
  });
  signal.addEventListener('abort', () => close(), {once:true});
  sync();
}

function reviewControls(params, games, game) {
  const options = (name, values) => values.map(([value,label]) =>
    `<option value="${esc(value)}"${(params.get(name) || '') === String(value) ? ' selected' : ''}>${esc(label)}</option>`).join('');
  const field = (name, label, items) => `<div class="field"><label for="review-${name}">${label}</label>
    <select id="review-${name}" name="${name}">${options(name, items)}</select></div>`;
  const picker = field('game', '游戏', [['','请选择游戏'],
    ...games.filter(g => g.total > 0 || g.appid === game.appid).map(g => [g.appid,g.name])]);
  const dateField = (name, label) => `<div class="field">
    <label id="review-${name}-label" for="review-${name}">${label}</label>
    <input id="review-${name}" type="date" name="${name}" value="${esc(params.get(name))}">
    </div>`;
  return `<section class="panel review-controls">
    <a class="review-game" href="${route('game/' + game.appid)}">${picture(game)}<strong>${esc(game.name)}</strong></a>
    <form class="filters" id="filters">${picker}
    <label class="search">搜索正文<input type="search" name="q" maxlength="100" value="${esc(params.get('q'))}" placeholder="输入关键词"></label>
    ${field('rating', '推荐状态', [['','全部'],['1','推荐'],['0','不推荐']])}
    ${field('playtime', '游玩时长', [['','全部'],['short','不足 2 小时'],['medium','2 至不足 10 小时'],['long','10 小时及以上']])}
    ${dateField('start', '开始日期')}
    ${dateField('end', '结束日期')}
    ${field('sort', '排序', [['','最新发表'],['helpful','有用票最多']])}
    <div class="actions"><button class="button button--primary">筛选</button><a class="button" href="${route('reviews', {game: game.appid})}">重置</a></div></form></section>`;
}

function reviewGamePicker(games) {
  games = games.filter(game => game.total > 0);
  if (!games.length) return '<p class="notice">暂无可浏览的游戏</p>';
  return `<section class="review-selection" aria-labelledby="review-selection-title">
    <h2 id="review-selection-title">选择一款游戏</h2>
    <div class="review-choices">${games.map(game => `<a class="review-choice" href="${route('reviews', {game: game.appid})}">
      ${picture(game)}<div class="review-choice-name"><strong>${esc(game.name)}</strong><span>${count(game.total)} 条评测</span></div>
    </a>`).join('')}</div></section>`;
}

function reviewHeading(item) {
  const rating = item.is_recommended === 1 ? '推荐' : item.is_recommended === 0 ? '不推荐' : '状态未知';
  const ratingIcon = item.is_recommended === 1 ? 'thumbsup' : item.is_recommended === 0 ? 'thumbsdown' : null;
  const hours = minutes => (minutes / 60).toFixed(1) + ' 小时';
  const played = item.playtime_forever == null ? '' : `累计游玩 ${hours(item.playtime_forever)}`;
  const atReview = item.playtime_at_review == null ? '' : `评测时 ${hours(item.playtime_at_review)}`;
  const playtime = [played, atReview].filter(Boolean).join(' · ');
  return `<div class="review-top">
    ${ratingIcon ? `<img class="review-rating-icon" src="/assets/steam-${ratingIcon}.png" alt="" width="40" height="40">` : ''}
    <div class="review-verdict"><strong>${rating}</strong>${playtime ? `<span>${playtime}</span>` : ''}</div>
    <a href="${route('game/' + item.appid)}">${esc(gamesById.get(item.appid)?.name || item.appid)}</a>
  </div>`;
}

function reviewList(data, params) {
  const pageLink = page => route('reviews', {...Object.fromEntries(params), page});
  return `<div class="results-head"><strong>${count(data.total)} 条评测</strong><span>第 ${count(data.page)} / ${count(data.pages)} 页</span></div>
    ${data.items.length ? `<div class="reviews">${data.items.map(item => `<article class="panel review">
      ${reviewHeading(item)}
      <div class="review-meta">发布于 <time datetime="${new Date(item.created_at * 1000).toISOString()}">${day(item.created_at)}</time></div>
      <div class="review-body"><p class="review-text" id="review-text-${esc(item.review_id)}">${esc(item.content || '暂无正文')}</p>
      <button class="review-toggle" type="button" aria-expanded="false" aria-controls="review-text-${esc(item.review_id)}" hidden>展开全文</button></div>
      ${item.developer_response ? `<details class="reply"><summary>开发者回复${item.timestamp_dev_responded ? ' · ' + day(item.timestamp_dev_responded) : ''}</summary><p>${esc(item.developer_response)}</p></details>` : ''}
      ${item.helpful_votes > 0 ? `<p class="review-feedback">${count(item.helpful_votes)} 人认为有用</p>` : ''}
      </article>`).join('')}</div>` : '<p class="panel notice">没有符合筛选条件的评测</p>'}
      ${data.pages > 1 ? `<nav class="pagination" aria-label="评测分页">
      ${data.page > 1 ? `<a class="button" href="${pageLink(data.page-1)}">上一页</a>` : '<button class="button" disabled>上一页</button>'}
      ${data.page < data.pages ? `<a class="button" href="${pageLink(data.page+1)}">下一页</a>` : '<button class="button" disabled>下一页</button>'}</nav>` : ''}`;
}

function activateReviewText(result, signal) {
  const syncToggle = text => {
    const button = text.nextElementSibling;
    if (button.getAttribute('aria-expanded') === 'false') {
      button.hidden = text.scrollHeight <= text.clientHeight;
    }
  };
  const observer = new ResizeObserver(entries => entries.forEach(({target}) => syncToggle(target)));
  for (const text of result.querySelectorAll('.review-text')) {
    const button = text.nextElementSibling;
    syncToggle(text);
    observer.observe(text);
    button.addEventListener('click', () => {
      const expanded = text.classList.toggle('is-expanded');
      button.setAttribute('aria-expanded', String(expanded));
      button.textContent = expanded ? '收起全文' : '展开全文';
      syncToggle(text);
    });
  }
  signal.addEventListener('abort', () => observer.disconnect(), {once: true});
}

async function render() {
  activeLoad?.abort();
  const controller = new AbortController();
  activeLoad = controller;
  const { signal } = controller;
  const [path, query = ''] = (location.hash.slice(2) || '').split('?');
  const params = new URLSearchParams(query);
  const isGame = /^game\/\d+$/.test(path);
  const section = isGame || !path ? 'games' : path;
  updateNavigation(section);
  window.scrollTo(0,0);
  app.innerHTML = '<p class="notice" role="status">正在加载…</p>';
  try {
    if (!path) {
      const games = await loadGames(signal);
      if (!signal.aborted) overview(games, signal);
    } else if (isGame) {
      const game = await get('/api/games/' + path.split('/')[1], signal);
      if (!signal.aborted) app.innerHTML = detail(game);
    } else if (path === 'reviews') {
      const resolved = await loadGames(signal);
      if (signal.aborted) return;
      const game = resolved.find(item => String(item.appid) === params.get('game'));
      app.innerHTML = `<div class="review-page"><section class="page-head review-page-head"><h1>评测浏览</h1>
        ${game ? `<a class="button review-back" href="${route('game/' + game.appid)}" aria-label="返回${esc(game.name)}游戏详情"><span aria-hidden="true">←</span> 返回游戏详情</a>` : ''}</section>
        ${game ? reviewControls(params, resolved, game) : reviewGamePicker(resolved)}
        ${game ? `${reviewWordCloudMarkup()}<div id="results"><p class="notice" role="status">正在加载…</p></div>` : ''}</div>`;
      if (!game) return;
      activateWordCloud(app.querySelector('#review-wordcloud'), params, signal);
      app.querySelectorAll('#filters select').forEach(select => enhanceSelect(select, signal));
      app.querySelectorAll('#filters input[type="date"]').forEach(input => enhanceDate(input, signal));
      app.querySelector('select[name="game"]').addEventListener('change', event => {
        const selected = event.currentTarget.value;
        location.hash = route('reviews', selected ? {game: selected} : {});
      });
      app.querySelector('#filters').addEventListener('submit', event => {
        event.preventDefault();
        const values = Object.fromEntries([...new FormData(event.currentTarget)].filter(([,v]) => String(v).trim()));
        location.hash = route('reviews', values);
      });
      const result = document.querySelector('#results');
      try {
        const data = await get('/api/reviews?' + params, signal);
        if (signal.aborted) return;
        result.innerHTML = reviewList(data, params);
        activateReviewText(result, signal);
      } catch (error) {
        if (!signal.aborted) result.innerHTML = `<p class="notice error" role="alert">${esc(error.message)}</p>`;
      }
    } else if (path === 'compare') {
      const [games, { mountGameComparison }] = await Promise.all([loadGames(signal), import('/game-comparison.js')]);
      if (!signal.aborted) mountGameComparison(app, { games, params, signal, get, route, picture, status, esc, icon });
    } else {
      const title = path === 'agent' ? '评测问答' : null;
      app.innerHTML = `<section class="page-head"><h1>${title || '页面不存在'}</h1></section>
      <div class="panel notice">${title ? '功能待上线' : '<a href="#/">返回游戏总览</a>'}</div>`;
    }
  } catch (error) {
    if (!signal.aborted) app.innerHTML = `<p class="notice error" role="alert">${esc(error.message)} <a href="#/">游戏总览</a></p>`;
  }
}
window.addEventListener('hashchange', render);
render();
