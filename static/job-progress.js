(function (root) {
  'use strict';
  const DAY = 86400000, OFFSET = 8 * 3600000;
  const activeStates = new Set(['queued','running','cancelling']);
  const valid = value => typeof value === 'number' && Number.isFinite(value);
  function lastEvent(job) {
    let cursor = valid(job.created) ? job.created * 1000 : null, latest = null;
    for (const event of job.events || []) {
      let at = valid(event.timestamp) ? event.timestamp * 1000 : null;
      // Older records contain only a China-local clock; walk forward across midnight.
      const clock = /^(\d{2}):(\d{2}):(\d{2})$/.exec(event.time || '');
      if (at === null && clock && cursor !== null) {
        at = Math.floor((cursor + OFFSET) / DAY) * DAY - OFFSET +
          (+clock[1] * 3600 + +clock[2] * 60 + +clock[3]) * 1000;
        if (at + 1000 <= cursor) at += DAY;
      }
      if (at !== null) cursor = at;
      latest = {at, message:event.message || ''};
    }
    return latest;
  }
  function snapshot(job, jobs = [], now = Date.now(), starts = {}, activeId = null) {
    if (!job) return {running:false,elapsed:null,message:'等待开始'};
    const group = job.batch_id ? jobs.filter(item => item.batch_id === job.batch_id) : [job];
    if (!group.length) group.push(job);
    const running = group.some(item => activeStates.has(item.status) || item.id === activeId);
    const start = Math.min(...group.map(item => valid(starts[item.id]) ? starts[item.id] : item.created * 1000).filter(valid));
    const events = group.map(lastEvent).filter(Boolean);
    const latest = events.reduce((a,b) => !a || (b.at ?? -Infinity) >= (a.at ?? -Infinity) ? b : a, null);
    const end = running ? now : latest?.at;
    const active = group.find(item => item.id === activeId) || group.find(item => activeStates.has(item.status));
    const message = active?.message || latest?.message || job.message || '等待开始';
    return {running,elapsed:valid(start) && valid(end) ? Math.max(0,end - start) : null,
      message:message.trim().split(/\r?\n/).filter(Boolean).pop() || '等待开始'};
  }
  function formatDuration(milliseconds) {
    if (!valid(milliseconds)) return '—';
    const seconds = Math.floor(Math.max(0,milliseconds) / 1000);
    const hours = Math.floor(seconds / 3600), minutes = Math.floor(seconds / 60) % 60;
    return (hours ? String(hours).padStart(2,'0') + ':' : '') +
      String(minutes).padStart(2,'0') + ':' + String(seconds % 60).padStart(2,'0');
  }
  const api = {snapshot,formatDuration};
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  else root.H3JobProgress = api;
})(typeof globalThis !== 'undefined' ? globalThis : this);
