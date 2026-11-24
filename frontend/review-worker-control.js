const reviewWorkerControl = (() => {
  const endpoint = '/api/review-worker';
  const labels = {
    checking: '正在连接采集后端', offline: '采集后端离线', starting: '采集后端正在启动',
    online: '采集后端在线', stopping: '采集后端正在关闭', unavailable: '采集后端状态不可用'
  };

  function mount(root, feedback, onOnlineChange) {
    const toggle = root.querySelector('[role="switch"]');
    const status = root.querySelector('[role="status"]');
    let state = 'checking';
    let visible = false;
    let timer;
    let reading = false;
    let changing = false;
    let revision = 0;
    let error = '';
    let serverMessage = '';

    function update(next = state) {
      const previousOnline = state === 'online';
      state = next;
      root.dataset.state = state;
      const label = labels[state] || labels.unavailable;
      if (status.textContent !== label) status.textContent = label;
      toggle.setAttribute('aria-checked', String(state === 'online' || state === 'starting'));
      const unavailable = changing || !['online', 'offline'].includes(state);
      toggle.setAttribute('aria-disabled', String(unavailable));
      toggle.title = state === 'online' ? '安全关闭采集后端并保留进度' : '启动采集后端并继续处理队列';
      const hint = state === 'stopping' ? '正在等待当前采集请求结束并保存进度。' : '';
      const text = error || serverMessage || hint;
      if (feedback.textContent !== text) feedback.textContent = text;
      feedback.hidden = !text;
      const online = state === 'online';
      if (previousOnline !== online) onOnlineChange(online);
    }

    function schedule() {
      clearTimeout(timer);
      if (visible) timer = setTimeout(refresh, ['starting', 'stopping'].includes(state) ? 750 : 3000);
    }

    async function request(options) {
      const response = await fetch(endpoint, options);
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.error || '采集后端连接失败');
      return payload;
    }

    async function refresh() {
      if (reading || changing) return;
      reading = true;
      const issued = revision;
      try {
        const payload = await request();
        if (issued !== revision) return;
        if (state === 'unavailable') error = '';
        serverMessage = payload.message;
        update(payload.state);
      } catch (cause) {
        if (issued !== revision) return;
        error = cause.message;
        update('unavailable');
      } finally {
        reading = false;
        schedule();
      }
    }

    toggle.addEventListener('click', async () => {
      if (toggle.getAttribute('aria-disabled') === 'true') return;
      const action = state === 'online' ? 'stop' : 'start';
      revision++;
      changing = true;
      error = '';
      serverMessage = '';
      update(action === 'start' ? 'starting' : 'stopping');
      try {
        const payload = await request({ method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ action }) });
        serverMessage = payload.message;
        update(payload.state);
      } catch (cause) {
        error = cause.message;
        update('unavailable');
      } finally {
        changing = false;
        update();
        schedule();
      }
    });

    return { get online() { return state === 'online'; }, setVisible(open) {
      visible = open;
      if (open) {
        if (!changing) update('checking');
        refresh();
      } else clearTimeout(timer);
    } };
  }

  return { mount };
})();
