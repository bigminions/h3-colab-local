'use strict';
(() => {
  const page = $('gemmaPage');
  const panel = $('gemmaDebugPanel');
  const trigger = $('gemmaDebug');
  const states = {
    idle: '远端空闲', busy: '远端运行中', connecting: '正在连接', switching: '正在切换',
    cancelling: '正在停止', blocked: '等待处理', unknown: '状态待确认',
  };
  const jobStates = {
    queued: '排队中', running: '执行中', cancelling: '正在停止', cancelled: '已停止',
    done: '已完成', error: '失败', interrupted: '已中断', warning: '完成，有警告', discarded: '已丢弃',
  };
  let timer, pending, eventSignature = '';
  const count = new Intl.NumberFormat('zh-CN');

  function showText(id, value) {
    const element = $(id);
    if (element.textContent !== value) element.textContent = value;
  }

  function clock(timestamp) {
    return new Date(timestamp * 1000).toLocaleTimeString('zh-CN', {hour12: false});
  }

  function runtimeLabel(data) {
    if (data.closing) return '正在关闭';
    if (data.probe_retry === 'retrying') return '正在重试探测';
    if (data.probe_retry === 'waiting') return '等待再次探测';
    if (data.position) return `队列第 ${data.position} 位`;
    const executing = data.active && ['running', 'cancelling'].includes(data.status);
    if (executing && !data.connected) return '正在连接 Colab';
    if (!data.connected) return '远端未连接';
    if (executing && data.consumer === 'idle') return '正在执行';
    return states[data.consumer] || '运行时已连接';
  }

  function duration(ms) {
    if (!Number.isFinite(ms)) return '—';
    return ms < 1000 ? `${Math.round(ms)} ms` : `${(ms / 1000).toFixed(2)} s`;
  }

  function renderPerformance(data) {
    $('gemmaDebugPerformance').hidden = data.operation !== 'reply' && !data.metrics;
    const metrics = data.metrics || {};
    showText('gemmaDebugSpeed', Number.isFinite(metrics.tokens_per_second)
      ? `${metrics.tokens_per_second.toFixed(1)} tok/s` : '—');
    showText('gemmaDebugFirstText', duration(metrics.first_text_ms));
    showText('gemmaDebugElapsed', duration(metrics.elapsed_ms));
    showText('gemmaDebugPromptTime', duration(metrics.prompt_ms));
    showText('gemmaDebugOutputTokens', Number.isFinite(metrics.output_tokens) ? count.format(metrics.output_tokens) : '—');
    showText('gemmaDebugInputTokens', Number.isFinite(metrics.input_tokens) ? count.format(metrics.input_tokens) : '—');
  }

  function setError(message) {
    $('gemmaDebugError').hidden = !message;
    showText('gemmaDebugError', message);
  }

  function renderEvents(events) {
    const signature = JSON.stringify(events);
    if (signature === eventSignature) return;
    const log = $('gemmaDebugEvents');
    const follow = !eventSignature || log.scrollHeight - log.scrollTop - log.clientHeight < 24;
    eventSignature = signature;
    const rows = events.map(event => {
      const item = document.createElement('li');
      const time = document.createElement('time');
      time.textContent = event.time;
      item.append(time, document.createTextNode(event.message));
      return item;
    });
    if (!rows.length) {
      const empty = document.createElement('li');
      empty.textContent = '暂无任务记录';
      rows.push(empty);
    }
    log.replaceChildren(...rows);
    if (follow) log.scrollTop = log.scrollHeight;
  }

  function render(data) {
    showText('gemmaDebugState', runtimeLabel(data));
    showText('gemmaDebugGpu', data.gpu?.name || data.gpu_name || '—');
    const gpu = data.gpu;
    showText('gemmaDebugUsage', gpu
      ? `${gpu.usage == null ? '—' : gpu.usage + '%'} · ${gpu.used_mib} / ${gpu.total_mib} MiB`
      : '暂无采样');
    const vramWrap = $('gemmaDebugVramWrap');
    if (vramWrap) {
      vramWrap.hidden = !gpu || !gpu.total_mib;
      if (gpu && gpu.total_mib) {
        const pct = Math.min(100, Math.round((gpu.used_mib / gpu.total_mib) * 100));
        const bar = $('gemmaDebugVramBar');
        const pctText = $('gemmaDebugVramPct');
        if (bar) bar.style.width = pct + '%';
        if (pctText) pctText.textContent = `${pct}% (${gpu.used_mib} / ${gpu.total_mib} MiB)`;
      }
    }
    const opLabel = data.operation === 'reply'
      ? '生成回复'
      : (data.chat_session === 'ready' ? '会话保持' : '加载模型');
    showText('gemmaDebugTask', data.job_id
      ? `${opLabel} · ${jobStates[data.status] || data.status} · ${data.job_id.slice(0, 8)}`
      : '暂无任务');
    const observation = [];
    if (data.connected && data.heartbeat) observation.push('心跳 ' + clock(data.heartbeat));
    else if (data.connected && data.observed_at) observation.push('检查 ' + clock(data.observed_at));
    if (data.gpu && data.sampled_at) observation.push('最近采样 ' + clock(data.sampled_at));
    showText('gemmaDebugHeartbeat', observation.join(' · ') || '—');
    $('gemmaDebugQueue').hidden = !data.waiting_count;
    if (data.waiting_count) {
      const positions = data.waiting_positions.join('、') + (data.waiting_count > data.waiting_positions.length ? '…' : '');
      showText('gemmaDebugQueue', `大模型排队 ${data.waiting_count} 项 · 队列第 ${positions} 位`);
    }
    setError(data.error || '');
    renderPerformance(data);
    renderEvents(data.events);
    $('gemmaDebugServer').hidden = !data.server_lines.length;
    const lines = $('gemmaDebugLines');
    const follow = lines.scrollHeight - lines.scrollTop - lines.clientHeight < 24;
    showText('gemmaDebugLines', data.server_lines.join('\n'));
    if (follow) lines.scrollTop = lines.scrollHeight;
  }

  async function refresh() {
    if (panel.hidden || page.hidden || document.hidden || pending) return;
    const request = new AbortController();
    pending = request;
    try {
      const data = await api('gemma/debug', {signal: request.signal});
      if (!request.signal.aborted) render(data);
    } catch (error) {
      if (!request.signal.aborted) setError('读取远端状态失败：' + error.message);
    } finally {
      if (pending === request) pending = null;
    }
  }

  function setOpen(open) {
    const returnFocus = !open && panel.contains(document.activeElement);
    const scroll = $('gemmaScroll');
    const follow = scroll.scrollHeight - scroll.scrollTop - scroll.clientHeight < 90;
    panel.hidden = !open;
    page.classList.toggle('gemma-debug-open', open);
    trigger.setAttribute('aria-expanded', String(open));
    if (follow) scroll.scrollTop = scroll.scrollHeight;
    if (open) {
      refresh();
      timer = setInterval(refresh, 1000);
      return;
    }
    clearInterval(timer);
    pending?.abort();
    pending = null;
    if (returnFocus) trigger.focus();
  }

  trigger.onclick = () => setOpen(panel.hidden);
  $('gemmaDebugClose').onclick = () => setOpen(false);
  panel.addEventListener('keydown', event => {
    if (event.key === 'Escape') {
      event.preventDefault();
      setOpen(false);
    }
  });
})();
