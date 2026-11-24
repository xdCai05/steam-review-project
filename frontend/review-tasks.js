const reviewTasks = (() => {
  const dialog = document.querySelector('#review-task-dialog');
  const orb = document.querySelector('#task-orb');
  const orbCounter = orb.querySelector('.task-orb-count');
  const query = dialog.querySelector('#candidate-query');
  const genre = dialog.querySelector('#candidate-genre');
  const syncGenre = enhanceSelect(genre);
  const candidateResults = dialog.querySelector('#candidate-results');
  const jobResults = dialog.querySelector('#job-results');
  const message = dialog.querySelector('#task-message');
  const gamePanel = dialog.querySelector('#task-game');
  const execution = dialog.querySelector('#task-execution');
  const worker = reviewWorkerControl.mount(dialog.querySelector('#task-worker-control'),
    dialog.querySelector('#task-worker-feedback'), () => { if (dialog.open) renderMain(); });
  const recentMarkup = new WeakMap();
  const games = new Map();
  const active = new Set(['queued', 'running', 'pausing', 'paused']);
  const stateNames = {
    queued: '等待中', running: '采集中', pausing: '正在暂停', paused: '已暂停',
    completed: '采集完成', limited: '达到容量上限', failed: '采集失败', cancelled: '已结束'
  };
  const dateTime = new Intl.DateTimeFormat('zh-CN', {
    timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false
  });
  const formatTime = seconds => dateTime.format(new Date(seconds * 1000));
  const motion = window.matchMedia('(prefers-reduced-motion: reduce)');
  let candidates = [];
  let jobs = [];
  let mode = 'candidates';
  let selectedGame;
  let selectedJob;
  let detailJob;
  let page = 1;
  let candidateLoad;
  let detailLoad;
  let opening;
  let queryTimer;
  let polling;
  let hasPolled = false;
  let minimized = false;
  let posting = false;
  let listError = '';

  const statusLabel = job => stateNames[job.status] || job.status;
  const running = job => job.status === 'running' || job.status === 'pausing';

  function showMessage(content, isError = false) {
    message.textContent = content;
    message.hidden = !content;
    message.classList.toggle('error', isError);
  }

  function setMarkup(element, markup) {
    if (recentMarkup.get(element) === markup) return false;
    recentMarkup.set(element, markup);
    element.innerHTML = markup;
    return true;
  }

  function selectRow(list, row) {
    const previous = list.querySelector('[aria-pressed="true"]');
    if (previous === row) return;
    previous?.setAttribute('aria-pressed', 'false');
    row?.setAttribute('aria-pressed', 'true');
  }

  function updateOrb() {
    const queued = jobs.filter(job => job.status === 'queued').length;
    const current = jobs.find(running) || jobs.find(job => job.status === 'paused');
    const label = current ? statusLabel(current) : queued ? '等待中' : '评测采集';
    const description = `${label}${queued ? ` · ${queued} 项等待` : ''}`;
    orb.hidden = !minimized && !jobs.some(job => active.has(job.status));
    const nextState = current?.status || (queued ? 'queued' : 'idle');
    if (orb.dataset.state !== nextState) orb.dataset.state = nextState;
    if (orb.title !== description) {
      orb.title = description;
      orb.setAttribute('aria-label', `打开评测采集，${description}`);
    }
    orbCounter.hidden = queued === 0;
    if (orbCounter.textContent !== String(queued)) orbCounter.textContent = queued;
  }

  function renderCandidateRows() {
    setMarkup(candidateResults, candidates.length ? candidates.map(game => `<button type="button" class="task-row candidate-row"
      data-candidate="${esc(game.appid)}" aria-pressed="false">
      ${picture(game)}<span><strong>${esc(game.name)}</strong>
      <small>${game.in_overview ? '已加入总览 · ' : ''}AppID ${esc(game.appid)}</small></span>
      <span class="selection-check" aria-hidden="true"></span></button>`).join('') :
      '<p class="task-empty">未找到符合条件的游戏</p>');
    selectRow(candidateResults, candidateResults.querySelector(`[data-candidate="${selectedGame?.appid}"]`));
  }

  async function loadCandidates(targetPage = 1) {
    candidateLoad?.abort();
    const controller = new AbortController();
    candidateLoad = controller;
    const {signal} = controller;
    const params = new URLSearchParams({q: query.value.trim(), genre: genre.value, page: targetPage});
    try {
      const result = await get(`/api/candidates?${params}`, signal);
      await opening?.finished.catch(() => {});
      if (signal.aborted) return;
      candidates = result.items;
      candidates.forEach(game => games.set(game.appid, game));
      if (selectedGame) selectedGame = games.get(selectedGame.appid) || selectedGame;
      page = result.page;
      const filters = `<option value="">全部分类</option>${(result.genres || []).map(label => `<option>${esc(label)}</option>`).join('')}`;
      const chosen = genre.value;
      if (setMarkup(genre, filters)) {
        genre.value = [...genre.options].some(option => option.value === chosen) ? chosen : '';
        syncGenre();
      }
      dialog.querySelector('#candidate-prev').disabled = page <= 1;
      dialog.querySelector('#candidate-next').disabled = page >= result.pages;
      dialog.querySelector('#candidate-page').textContent = `${count(page)} / ${count(result.pages)} 页`;
      candidateResults.scrollTop = 0;
      renderCandidateRows();
      if (mode === 'candidates') renderMain();
    } catch (error) {
      if (!signal.aborted) setMarkup(candidateResults, `<p class="task-empty error">${esc(error.message)}</p>`);
    }
  }

  function renderJobs() {
    const rows = items => items.map(job => `<button type="button" class="task-row" data-job="${esc(job.id)}"
      aria-pressed="false"><span><strong>${esc(job.name)}</strong>
      <small>${esc(formatTime(job.created))}</small></span><span class="task-state" data-state="${esc(job.status)}">${esc(statusLabel(job))}</span>
      <span class="selection-check" aria-hidden="true"></span></button>`).join('');
    const queue = jobs.filter(job => active.has(job.status));
    const history = jobs.filter(job => !active.has(job.status));
    setMarkup(jobResults, `<h3>队列 <span>${count(queue.length)}</span></h3>
      ${queue.length ? rows(queue) : '<p class="task-empty">暂无等待或运行中的任务</p>'}
      <h3>运行历史</h3>${history.length ? rows(history) : '<p class="task-empty">暂无运行记录</p>'}`);
    selectRow(jobResults, jobResults.querySelector(`[data-job="${CSS.escape(selectedJob || '')}"]`));
  }

  function renderMain() {
    const baseJob = jobs.find(job => job.id === selectedJob);
    const job = detailJob?.id === selectedJob ? {...detailJob, ...baseJob} : baseJob;
    const candidate = mode === 'candidates' ? selectedGame : games.get(job?.appid);
    const target = mode === 'candidates' ? candidate : job;
    const selected = target && {...gamesById.get(target.appid), ...candidate, ...target};
    const actionFocus = execution.contains(document.activeElement) ? document.activeElement.dataset.jobAction : '';
    let actionMarkupChanged;

    if (!selected) {
      setMarkup(gamePanel, '');
      setMarkup(execution, `<p class="task-empty task-empty-main">${mode === 'jobs' ? '选择一条任务查看进度和日志' : '选择一款游戏开始采集评测'}</p>`);
      return;
    }

    const genres = selected.genres?.length ? `<span>${list(selected.genres)}</span>` : '';
    const release = selected.release_date || selected.release_date_raw;
    setMarkup(gamePanel, `<div class="task-game-card">${picture(selected)}<div>
      <h3>${esc(selected.name)}</h3><div class="task-game-meta"><span>AppID ${esc(selected.appid)}</span>${genres}
      ${release ? `<span>${esc(release)}</span>` : ''}</div></div></div>`);

    if (mode === 'candidates') {
      const existing = jobs.find(item => item.appid === candidate.appid && active.has(item.status));
      const actionLabel = existing ? '查看采集任务' : jobs.some(item => active.has(item.status)) || !worker.online ? '加入队列' : '开始采集';
      actionMarkupChanged = setMarkup(execution, `<div class="task-plan"><div><strong>最近 90 天评测</strong>
        <p>按任务开始时间计算，暂停后可从上次进度继续。</p></div>
        <button type="button" class="button button--primary" data-job-action="${existing ? 'view' : 'enqueue'}"${posting ? ' disabled' : ''}>${actionLabel}</button></div>`);
    } else {
      const progress = job.progress || {};
      const range = job.window_start && job.window_end ? `<span>采集范围 ${esc(formatTime(job.window_start))} 至 ${esc(formatTime(job.window_end))}</span>` : '';
      const of = progress.total || job.segments;
      const done = progress.done || 0;
      const amount = progress.bytes != null ? ` · 文件 ${formatReviewMB(progress.bytes)}${progress.max_bytes ? ` / ${formatReviewMB(progress.max_bytes)}` : ''}` : '';
      const month = progress.month ? `<span>当前月份 ${esc(progress.month)}</span>` : '';
      const metrics = (progress.pages != null && of) ? `<span>已处理 ${count(progress.pages)} 页 · 新增 ${count(progress.inserted)} 条${esc(amount)}</span>` : '';
      const output = job.logs?.length ? job.logs.join('\n') : (job.message || statusLabel(job));
      const jobButtons = {
        running: [['pause', '暂停', 'primary']],
        paused: [['resume', '继续', 'primary'], ['stop', '结束任务', 'danger']],
        queued: [['remove', '移出队列', 'danger']],
        limited: job.accepted > 0 && !job.in_overview ? [['publish', '加入总览', 'primary']] : []
      };
      let outcome = job.message;
      if (job.status === 'completed') outcome = job.accepted === 0 ? '本次采集范围内没有入库评测，游戏已加入总览。' : '游戏已加入总览。';
      if (job.status === 'queued' && !worker.online) outcome = '采集后端就绪后将继续处理队列。';
      setMarkup(execution, `<section class="task-run" aria-label="采集进度"></section>
        <section class="task-console"><h3>采集日志</h3><pre class="task-log" tabindex="0" aria-label="采集日志内容"></pre></section>`);
      actionMarkupChanged = setMarkup(execution.querySelector('.task-run'), `
        <div class="task-run-head"><span class="task-state" data-state="${esc(job.status)}">${esc(statusLabel(job))}</span>
          ${of ? `<strong>${count(done)} / ${count(of)} 段</strong>` : ''}</div>
        ${of ? `<progress max="${esc(of)}" value="${esc(done)}" aria-label="已完成采集分段"></progress>` : ''}
        <div class="task-run-details">${range}${month}${metrics}</div>
        ${outcome ? `<p class="task-outcome">${esc(outcome)}</p>` : ''}
        ${(jobButtons[job.status] || []).map(([name, label, intent]) => `<button type="button" data-job-action="${name}"
          class="button button--${intent}"${posting ? ' disabled' : ''}>${label}</button>`).join('')}`);
      const log = execution.querySelector('.task-log');
      const sameJob = log.dataset.jobId === job.id;
      if (!sameJob || log.textContent !== output) {
        const followLog = !sameJob || log.scrollHeight - log.clientHeight - log.scrollTop < 30;
        log.dataset.jobId = job.id;
        log.textContent = output;
        if (followLog) log.scrollTop = log.scrollHeight;
      }
    }
    if (actionMarkupChanged && actionFocus && !posting) {
      execution.querySelector(`[data-job-action="${actionFocus}"]`)?.focus({preventScroll: true});
    }
  }

  async function loadJob() {
    const summary = jobs.find(job => job.id === selectedJob);
    if (mode !== 'jobs' || !summary || !dialog.open) return false;
    if (detailJob?.id === summary.id && detailJob.status === summary.status && !running(summary)) return false;
    detailLoad?.abort();
    const controller = new AbortController();
    detailLoad = controller;
    try {
      const detail = await get(`/api/review-jobs/${encodeURIComponent(selectedJob)}`, controller.signal);
      if (controller.signal.aborted) return;
      detailJob = detail;
      renderMain();
      return true;
    } catch (error) {
      if (!controller.signal.aborted) showMessage(error.message, true);
    }
    return false;
  }

  async function readJobs() {
    try {
      const result = await get('/api/review-jobs');
      const before = new Map(jobs.map(job => [job.id, job]));
      jobs = result.items;
      if (hasPolled && jobs.some(job => job.in_overview && !active.has(job.status) &&
        (job.status !== before.get(job.id)?.status || !before.get(job.id)?.in_overview))) {
        window.dispatchEvent(new Event('review-collection-changed'));
      }
      hasPolled = true;
      if (listError && message.textContent === listError) showMessage('');
      listError = '';
      updateOrb();
      if (dialog.open) {
        if (mode === 'jobs') {
          selectedJob ||= jobs[0]?.id;
          renderJobs();
          if (await loadJob()) return;
        }
        if (dialog.open) renderMain();
      }
    } catch (error) {
      listError = '无法读取采集状态。' + error.message;
      if (dialog.open) showMessage(listError, true);
    }
  }

  function refreshJobs() {
    polling ||= readJobs().finally(() => { polling = null; });
    return polling;
  }

  function setMode(nextMode) {
    mode = nextMode;
    showMessage('');
    for (const button of dialog.querySelectorAll('[data-task-mode]')) {
      button.setAttribute('aria-pressed', String(button.dataset.taskMode === mode));
    }
    dialog.querySelector('#task-candidates').hidden = mode !== 'candidates';
    dialog.querySelector('#task-jobs').hidden = mode !== 'jobs';
    if (mode === 'jobs') {
      selectedJob ||= jobs[0]?.id;
      renderJobs();
      loadJob();
    } else {
      detailLoad?.abort();
      renderCandidateRows();
    }
    renderMain();
  }

  async function performAction(action) {
    if (posting) return;
    if (action === 'view') {
      selectedJob = jobs.find(job => job.appid === selectedGame.appid && active.has(job.status))?.id;
      setMode('jobs');
      return;
    }
    posting = true;
    showMessage('');
    renderMain();
    try {
      const isNew = action === 'enqueue';
      const targetJob = selectedJob;
      const response = await fetch(isNew ? '/api/review-jobs' : `/api/review-jobs/${encodeURIComponent(targetJob)}`, {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(isNew ? {appid: selectedGame.appid} : {action})
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || '任务操作失败');
      if (isNew) {
        selectedJob = data.id;
        setMode('jobs');
      } else if (action === 'publish') {
        showMessage('已加入游戏总览');
      }
      if (polling) await polling;
      await refreshJobs();
    } catch (error) {
      showMessage(error.message, true);
    } finally {
      posting = false;
      renderMain();
    }
  }

  function open(fromOrb = false) {
    if (dialog.open) return;
    const restore = fromOrb && minimized;
    if (!restore) setMode(fromOrb ? 'jobs' : 'candidates');
    dialog.showModal();
    worker.setVisible(true);
    if (mode === 'jobs') dialog.querySelector('[data-task-mode="jobs"]').focus({preventScroll: true});
    if (!motion.matches) {
      const fade = dialog.animate([{opacity: .55}, {opacity: 1}], {duration: 130, easing: 'ease-out'});
      opening = fade;
      const finish = () => { if (opening === fade) opening = null; };
      fade.finished.then(finish, finish);
    }
    if (restore) renderMain();
    minimized = false;
    updateOrb();
    if (mode === 'candidates') loadCandidates(page);
    refreshJobs();
  }

  function close(toOrb) {
    opening?.cancel();
    candidateLoad?.abort();
    detailLoad?.abort();
    clearTimeout(queryTimer);
    minimized = toOrb;
    updateOrb();
    dialog.close();
    worker.setVisible(false);
    syncGenre();
    if (toOrb) {
      orb.focus({preventScroll: true});
      if (!motion.matches) {
        orb.animate([{transform:'scale(.8)',opacity:.6}, {transform:'scale(1)',opacity:1}],
          {duration:160, easing:'ease-out'});
      }
    }
  }

  dialog.addEventListener('cancel', event => {
    event.preventDefault();
    close(false);
  });
  dialog.addEventListener('click', event => {
    const button = event.target.closest('button');
    if (!button) return;
    const {taskMode, taskWindow, candidate, job, jobAction} = button.dataset;
    if (taskMode) {
      setMode(taskMode);
      if (taskMode === 'candidates') loadCandidates(page);
    } else if (taskWindow) {
      close(taskWindow === 'minimize');
    } else if (candidate) {
      selectedGame = games.get(Number(candidate));
      showMessage('');
      selectRow(candidateResults, button);
      renderMain();
    } else if (job) {
      selectedJob = job;
      detailJob = null;
      showMessage('');
      selectRow(jobResults, button);
      renderMain();
      loadJob();
    } else if (jobAction) {
      performAction(jobAction);
    }
  });
  query.addEventListener('input', () => {
    clearTimeout(queryTimer);
    queryTimer = setTimeout(() => loadCandidates(), 230);
  });
  genre.addEventListener('change', () => loadCandidates());
  dialog.querySelector('#candidate-prev').addEventListener('click', () => loadCandidates(page - 1));
  dialog.querySelector('#candidate-next').addEventListener('click', () => loadCandidates(page + 1));
  orb.addEventListener('click', () => open(true));
  window.setInterval(() => { if (!document.hidden) refreshJobs(); }, 3000);
  document.addEventListener('visibilitychange', () => { if (!document.hidden) refreshJobs(); });
  refreshJobs();
  return {open};
})();
