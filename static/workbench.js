function updateH3GpuDisplay() {
  const select = $('h3Gpu'), display = $('h3GpuDisplay'), status = $('h3GpuStatus');
  if (!select || !display) return;
  const opt = select.selectedOptions?.[0];
  const t = (opt ? opt.textContent : select.value) || '';
  if (t.includes('(🟢')) {
    const parts = t.split('(🟢');
    display.textContent = parts[0].trim();
    if (status) {
      status.textContent = '🟢 ' + parts[1].replace(')', '').trim();
      status.classList.remove('status-idle');
      status.classList.add('status-online');
    }
  } else {
    display.textContent = t.trim();
    if (status) {
      status.textContent = 'Wan 2.1 推荐配置';
      status.classList.remove('status-online');
      status.classList.add('status-idle');
    }
  }
}

'use strict';
window.$ = window.$ || (id => document.getElementById(id));
const KEY = $('apiKey').value;
let authorized = false;
let latestJobs = [];
let historyBusy = false, historyRemoval = null;
let lastActive = undefined, lastManaged = undefined, accountBusy = false;
let lastAccountData = null, studioMode = 'text';
let onlineGpuState = null, gpuOnlineReady = false, gpuDetectionBusy = false, gpuDetectionError = '', gpuReleaseMessage = '';
let generationSettings = null;
let references = [], serverActive = false, submitting = false, cacheTestRunning = false;
let recoveryJob = null, retrying = false, resubmitting = false;
let cancelling = false;
let nonReferencePeople = false;
let referenceModelActive = false;
let releasePolicyReady = false, releasePolicyBusy = false, releaseActionBusy = false;
let releasePolicyDirty = false, releasePolicyTimer = null;
let lastReleaseState = null, lastReleaseRevision = -1;
let submitCooldownTimer = null;

function getSubmitCooldownRemaining() {
  try {
    const until = Number(sessionStorage.getItem('lstudio-h3-submit-cooldown-until') || 0);
    const left = Math.ceil((until - Date.now()) / 1000);
    return left > 0 ? left : 0;
  } catch (_) {
    return 0;
  }
}

function startSubmitCooldown(seconds = 5) {
  if (submitCooldownTimer) clearInterval(submitCooldownTimer);
  try {
    sessionStorage.setItem('lstudio-h3-submit-cooldown-until', String(Date.now() + seconds * 1000));
  } catch (_) {}
  updateGenerate();
  submitCooldownTimer = setInterval(() => {
    const left = getSubmitCooldownRemaining();
    if (left <= 0) {
      clearInterval(submitCooldownTimer);
      submitCooldownTimer = null;
      try { sessionStorage.removeItem('lstudio-h3-submit-cooldown-until'); } catch (_) {}
    }
    updateGenerate();
  }, 1000);
}
let lastNativeDrivePrompt = null;
let activeJobId = null;
let runStarts = {};
try { runStarts = JSON.parse(localStorage.getItem('lstudio-h3-run-starts-v1') || '{}'); }
catch (_) { /* A missing browser record falls back to the task creation time. */ }
if (!runStarts || typeof runStarts !== 'object' || Array.isArray(runStarts)) runStarts = {};
const DRAFT = 'lstudio-h3-settings-v1';
const completionSound = new H3CompletionSound({
  getItem: key => localStorage.getItem(key), setItem: (key,value) => localStorage.setItem(key,value)
}, () => {
  const Audio = window.AudioContext || window.webkitAudioContext;
  return Audio ? new Audio() : null;
});
$('completionSound').checked = completionSound.enabled;
$('completionSound').onchange = () => completionSound.setEnabled($('completionSound').checked);
$('testNotification')?.addEventListener('click', () => {
  completionSound.unlock();
  completionSound.play();
  setTimeout(() => completionSound.playError(), 850);
  completionSound.notify('LStudio 提示通知', '提示音与桌面通知已就绪：任务完成或失败时将自动提醒。', 'h3-test');
});
document.addEventListener('pointerdown', () => completionSound.unlock(), {capture:true});
document.addEventListener('keydown', () => completionSound.unlock(), {capture:true});
window.addEventListener('storage', event => {
  if (event.key === 'lstudio-completion-sound-enabled-v1') {
    completionSound.enabled = event.newValue !== 'off';
    $('completionSound').checked = completionSound.enabled;
  }
});
async function api(path, options = {}) {
  const response = await fetch('/api/' + path, {
    ...options,
    headers: {'X-LStudio-Key': KEY, ...(options.body instanceof FormData ? {} : {'Content-Type':'application/json'}), ...options.headers}
  });
  let data = null;
  try {
    data = await response.json();
  } catch (_) {}
  if (!response.ok) {
    const detail = data && (typeof data.detail === 'string' ? data.detail : Array.isArray(data.detail) ? data.detail.map(item => String(item.msg || '参数不正确').replace(/^Value error, /,'')).join('；') : null);
    throw Error(detail || `请求失败 (${response.status})`);
  }
  return data;
}

function error(message) {
  const el = $('error');
  if (el) {
    el.textContent = message;
    el.classList.toggle('hidden', !message);
  }
}
window.showError = error;
window.error = error;

function updatePerformance() {
  const enabled = $('performance').checked;
  $('performanceOptions').disabled = !enabled;
  $('performanceDetails').classList.toggle('hidden', !enabled);
  $('performanceHint').textContent = enabled ? '使用加速配置，可展开单独调整。' : '原版权重与官方 BF16 编码器 · 常规 20 步 · 完整注意力，加载与生成更慢。';
  $('peopleEnhancement').disabled = referenceModelActive || !enabled;
  updateEnhancement();
}

function updateEnhancement() {
  $('strength').disabled = referenceModelActive || !$('performance').checked || !$('peopleEnhancement').checked;
}

function updateModelControls() {
  const useRefs = !!window.H3Characters?.used().length || studioMode === 'image' && references.some(r => (r.frame || 'reference') === 'reference');
  if (useRefs && !referenceModelActive) {
    nonReferencePeople = $('peopleEnhancement').checked;
    $('peopleEnhancement').checked = false;
  } else if (!useRefs && referenceModelActive) {
    $('peopleEnhancement').checked = nonReferencePeople;
  }
  referenceModelActive = useRefs;
  $('referenceAdvanced').classList.toggle('hidden', !useRefs);
  $('peopleEnhancement').disabled = useRefs || !$('performance').checked;
  updateEnhancement();
}

function setMode(mode) {
  studioMode = mode;
  $('textTab').setAttribute('aria-selected', String(studioMode === 'text'));
  $('imageTab').setAttribute('aria-selected', String(studioMode === 'image'));
  $('imagePanel').classList.toggle('hidden', studioMode !== 'image');
  updateModelControls();
  updateEnhancement(); updateFormat(); updateGenerate();
  window.H3Characters?.refreshUse();
}

function updateGenerate() {
  const waiting = studioMode === 'image' && references.some(r => r.pending);
  const storagePending = generationSettings?.storage_ready === false;
  const loadingCharacters = !!window.H3Characters?.needsLibrary();
  const cooldownRemaining = getSubmitCooldownRemaining();
  const isCooldown = cooldownRemaining > 0;

  if (recoveryJob && !serverActive) {
    $('generate').formNoValidate = true;
    $('generate').disabled = isCooldown || !authorized || retrying || cancelling || submitting || cacheTestRunning || releaseActionBusy || !!lastReleaseState?.checking;
    $('generate').textContent = cancelling ? '正在取消任务…' : recoveryJob.cancel_requested ? '重试取消并释放显卡' : retrying ? '正在恢复任务…' : '恢复未完成任务';
    $('generate').title = '恢复原任务并沿用原任务的设置；远端仍在执行时重新跟踪进度。';
    return;
  }
  $('generate').formNoValidate = false;
  $('generate').title = '';
  $('generate').disabled = isCooldown || !authorized || submitting || cacheTestRunning || !generationSettings || storagePending || !releasePolicyReady || releasePolicyBusy || releasePolicyDirty || releaseActionBusy || !!lastReleaseState?.checking || waiting || loadingCharacters;

  if (submitting) {
    $('generate').textContent = '正在提交…';
  } else if (isCooldown) {
    $('generate').textContent = `已提交 (${cooldownRemaining}s)`;
  } else if (cacheTestRunning) {
    $('generate').textContent = '正在准备模型缓存…';
  } else if (storagePending) {
    $('generate').textContent = '请检查成片存储配置';
  } else if (waiting) {
    $('generate').textContent = '正在准备图片…';
  } else if (serverActive) {
    $('generate').textContent = '加入生成队列';
    if (authorized && !$('generate').disabled) {
      $('generate').title = '已有任务正在执行，提交后将放入右侧队列按序生成，可随时取消。';
    }
  } else if (authorized) {
    $('generate').textContent = '生成视频';
  } else {
    $('generate').textContent = '请先连接 Google';
  }
}

function referenceData() {
  return references.filter(r => r.image_id).map(({slot, image_id, role, description, frame}) => ({slot, image_id, role, description, frame:frame || 'reference'}));
}


async function pickFromLibrary() {
  if (references.length >= 9) {
    return error('最多使用 9 张图片，请先移除已有参考图。');
  }
  const remaining = 9 - references.length;
  const inUse = references.map(r => r.image_id).filter(Boolean);

  if (!window.Workspace?.openAssetPicker) return;
  const picked = await window.Workspace.openAssetPicker({
    kind: 'images',
    maxCount: remaining,
    alreadySelectedIds: inUse,
    title: '从素材库选择参考图片'
  });

  if (!picked || !picked.length) return;

  for (const asset of picked) {
    if (references.length >= 9 || references.some(r => r.image_id === asset.id)) continue;
    const slot = Array.from({length: 9}, (_, i) => i + 1).find(idx => !references.some(r => r.slot === idx));
    if (!slot) break;
    let url = '';
    try {
      const resp = await fetch(asset.url, { headers: { 'X-LStudio-Key': KEY } });
      if (resp.ok) url = URL.createObjectURL(await resp.blob());
    } catch (_) {}
    references.push({
      slot,
      image_id: asset.id,
      role: 'other',
      description: '',
      frame: 'reference',
      url: url || asset.url
    });
  }
  setMode('image');
  renderReferences();
  saveDraft();
  window.H3Characters?.refreshUse();
}

function renderReferences() {
  $('referenceCount').textContent = `${references.length} / 9 张`;
  const dropZone = $('referenceDropZone');
  $('referenceCards').replaceChildren();
  $('referenceCards').classList.toggle('has-images', references.length > 0);
  for (const ref of [...references].sort((a,b) => a.slot - b.slot)) {
    const card = document.createElement('article'); card.className = 'reference-card';
    const preview = document.createElement('div'); preview.className = 'reference-preview'; preview.dataset.frame = ref.frame || 'reference';
    const frameLabel = {reference:'参考图',first:'首帧',last:'尾帧'}[ref.frame || 'reference'];
    if (ref.url) { const img = document.createElement('img'); img.src = ref.url; img.alt = `图片${ref.slot}${frameLabel}预览`; preview.append(img); }
    const badge = document.createElement('span'); badge.className = 'reference-badge'; badge.textContent = `图片${ref.slot} · ${frameLabel}`; preview.append(badge);
    const state = document.createElement('div'); state.className = 'hint'; state.textContent = ref.pending ? '正在上传到本机…' : ref.error || '';
    state.hidden = !state.textContent;
    state.classList.toggle('reference-error', !!ref.error);
    const frameButtons = document.createElement('div'); frameButtons.className = 'reference-frame-buttons';
    frameButtons.setAttribute('role', 'group'); frameButtons.setAttribute('aria-label', `图片${ref.slot}使用方式`);
    for (const [value,label] of Object.entries({reference:'参考图',first:'首帧',last:'尾帧'})) {
      const button = document.createElement('button'); button.type = 'button'; button.textContent = label;
      button.id = `reference-frame-${ref.slot}-${value}`;
      button.setAttribute('aria-pressed', String((ref.frame || 'reference') === value));
      button.onclick = () => {
        if (value !== 'reference' && references.some(r => r !== ref && r.frame === value)) {
          error(`${label}只能指定一张图片，请先修改已指定的图片。`); return;
        }
        ref.frame = value; error(''); renderReferences(); saveDraft();
        $(button.id)?.focus({preventScroll:true});
      };
      frameButtons.append(button);
    }
    const actions = document.createElement('div'); actions.className = 'reference-actions';
    const insert = document.createElement('button'); insert.type = 'button'; insert.textContent = `引用 @图片${ref.slot}`;
    insert.disabled = !!ref.pending || !!ref.error || !ref.image_id;
    insert.onclick = () => window.H3Characters.insert({name:`图片${ref.slot}`});
    const replace = document.createElement('button'); replace.type = 'button'; replace.textContent = '替换';
    const input = document.createElement('input'); input.type = 'file'; input.className = 'file-input'; input.accept = 'image/png,image/jpeg,image/webp'; input.setAttribute('aria-label', `替换图片${ref.slot}`);
    input.onchange = () => { if (input.files[0]) uploadReference(ref, input.files[0]); };
    replace.onclick = () => input.click(); replace.disabled = !!ref.pending;
    const remove = document.createElement('button'); remove.type = 'button'; remove.textContent = '移除'; remove.setAttribute('aria-label', `移除图片${ref.slot}`);
    remove.onclick = () => { if (ref.url) URL.revokeObjectURL(ref.url); references = references.filter(r => r !== ref); renderReferences(); saveDraft(); };
    actions.append(insert,replace,remove); card.append(preview,state,frameButtons,actions,input); $('referenceCards').append(card);
  }
  if (references.length < 9) {
    dropZone.hidden = false;
    dropZone.classList.remove('hidden');
    dropZone.style.display = '';
    $('referenceCards').append(dropZone);
  } else {
    dropZone.hidden = true;
    dropZone.classList.add('hidden');
    dropZone.style.display = 'none';
  }
  updateModelControls(); updateGenerate(); updateFormat();
  window.H3Characters?.refreshUse();
}

async function uploadReference(ref, file) {
  if (!['image/png','image/jpeg','image/webp'].includes(file.type) || file.size > 20 * 1024 ** 2) return error('请使用 20MB 以内的 PNG、JPEG 或 WebP 图片。');
  ref.pending = true; ref.error = ''; renderReferences(); error('');
  try {
    const form = new FormData(); form.append('file', file);
    const data = await api('images', {method:'POST',body:form});
    if (!references.includes(ref)) return;
    if (ref.url) URL.revokeObjectURL(ref.url);
    ref.image_id = data.image_id; ref.url = URL.createObjectURL(file);
  } catch (e) { if (references.includes(ref)) ref.error = e.message; }
  finally { ref.pending = false; renderReferences(); saveDraft(); }
}

async function addReferences(files) {
  files = [...files];
  if (files.length + references.length > 9) return error('最多支持 9 张图片，请先移除不需要的图片。');
  for (const file of files) {
    if (!['image/png','image/jpeg','image/webp'].includes(file.type) || file.size > 20 * 1024 ** 2) { error('请使用 20MB 以内的 PNG、JPEG 或 WebP 图片。'); continue; }
    const slot = Array.from({length:9}, (_,i) => i+1).find(n => !references.some(r => r.slot === n));
    const ref = {slot,role:'other',description:'',frame:'reference',pending:true}; references.push(ref);
    await uploadReference(ref,file);
  }
  $('referenceFiles').value = '';
}

async function restoreReferences(saved) {
  for (const ref of references) if (ref.url) URL.revokeObjectURL(ref.url);
  const used = new Set();
  references = (Array.isArray(saved) ? saved : []).slice(0,9).filter(r => {
    if (!Number.isInteger(r.slot) || r.slot < 1 || r.slot > 9 || used.has(r.slot) || !/^[0-9a-f]{32}\.(png|jpg|webp)$/.test(r.image_id)) return false;
    used.add(r.slot); return true;
  }).map(r => ({...r, frame:r.frame || 'reference', pending:true}));
  renderReferences();
  await Promise.all(references.map(async ref => {
    try {
      const response = await fetch('/api/images/' + ref.image_id, {headers:{'X-LStudio-Key':KEY}});
      if (!response.ok) throw Error(`图片${ref.slot}已丢失，请替换或移除`);
      const blob = await response.blob();
      if (references.includes(ref)) ref.url = URL.createObjectURL(blob);
    } catch (e) { ref.error = e.message; }
    finally { ref.pending = false; if (references.includes(ref)) renderReferences(); }
  }));
}

function selectedRatio() {
  return document.querySelector('input[name="aspectRatio"]:checked').value;
}

function updateFormat() {
  const count = Math.max(1,Math.min(8,Math.trunc(Number($('count').value) || 1)));
  $('count').value = String(count);
  $('decreaseCount').disabled = count === 1;
  $('increaseCount').disabled = count === 8;
  const ratio = selectedRatio();
  const size = generationSettings?.dimensions[$('resolution').value]?.[ratio];
  const duration = Number($('duration').value);
  if (!size) return;
  const [width, height] = size;
  $('formatNote').textContent = `${width} × ${height}`;
  document.querySelectorAll('.reference-preview').forEach(preview => {
    preview.style.aspectRatio = preview.dataset.frame === 'reference' ? '' : `${width} / ${height}`;
  });
  const imageSummary = studioMode === 'image' ? ['first','last','reference'].map(frame => {
    const count = references.filter(r => (r.frame || 'reference') === frame).length;
    return count ? `${{first:'首帧',last:'尾帧',reference:'参考图'}[frame]} ${count} 张` : '';
  }).filter(Boolean).join(' + ') : '';
  $('generationSummary').textContent = `本次生成：${$('count').value} 条 · ${imageSummary ? imageSummary + ' · ' : ''}${ratio} · ${$('resolution').value} · 每条约 ${duration} 秒 · 含声音`;
}

function selectedReleaseMode() {
  return document.querySelector('input[name="releaseMode"]:checked').value;
}

function updateReleaseUI(state = null) {
  if (state && state.revision >= lastReleaseRevision) {
    lastReleaseRevision = state.revision; lastReleaseState = state; releasePolicyReady = true;
    if (!releasePolicyBusy && !releasePolicyDirty) {
      document.querySelectorAll('input[name="releaseMode"]').forEach(input => { input.checked = input.value === state.policy.mode; });
      if (document.activeElement !== $('idleMinutes')) $('idleMinutes').value = String(state.policy.idle_minutes);
    }
  }
  const mode = selectedReleaseMode(), minutes = $('idleMinutes').value;
  const checking = releaseActionBusy || !!lastReleaseState?.checking;
  document.querySelectorAll('input[name="releaseMode"]').forEach(input => { input.disabled = releasePolicyBusy || checking; });
  $('idleReleaseOptions').classList.toggle('hidden', mode !== 'idle');
  $('idleMinutes').disabled = mode !== 'idle' || releasePolicyBusy || checking;
  $('idleMinutes').required = mode === 'idle';
  $('manualRelease').classList.toggle('hidden', mode !== 'manual');
  $('manualRelease').disabled = serverActive || checking || !releasePolicyReady || releasePolicyBusy;
  $('manualRelease').textContent = checking ? '正在检查…' : '检查并释放';
  $('release').disabled = serverActive || checking;
  renderOnlineGpuState();
  $('releaseHelp').textContent = mode === 'always' ? '整批保存后释放，下次重新准备模型。' : mode === 'idle' ? '整批完成后计时，保留期间仍计费。请保持本地后台运行。' : '保留 GPU 以连续生成，期间仍计费。使用后请手动释放。';
  let message = '当前未占用 GPU';
  if (!releasePolicyReady) message = '读取释放配置…';
  else if (releasePolicyBusy) message = '正在保存释放配置…';
  else if (checking) message = '正在检查任务、结果与显卡释放状态…';
  else if (lastReleaseState?.error) message = (lastManaged ? '显卡已保留：' : '') + lastReleaseState.error;
  else if (serverActive) message = '任务执行中，生成完成后按配置处理';
  else if (lastManaged && mode === 'idle' && lastReleaseState?.deadline) {
    const seconds = Math.max(0, Math.ceil(lastReleaseState.deadline - Date.now() / 1000));
    message = seconds ? `闲置倒计时 ${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2,'0')} · GPU 仍计费` : '闲置时间已到，等待后台检查并释放';
  } else if (lastManaged) message = mode === 'always' ? '正在等待后台检查并释放' : 'GPU 已保留，等待手动释放';
  else if (lastReleaseState?.message) message = lastReleaseState.message;
  $('releaseStatus').textContent = message;
  $('releaseStatus').classList.toggle('release-warning', !!lastReleaseState?.error);
  updateGenerate();
}

async function saveReleasePolicy() {
  clearTimeout(releasePolicyTimer);
  if (releasePolicyBusy) return;
  if (selectedReleaseMode() === 'idle' && !$('idleMinutes').checkValidity()) return;
  const policy = {mode:selectedReleaseMode(),idle_minutes:Number($('idleMinutes').value) || 10};
  releasePolicyBusy = true; updateReleaseUI();
  try {
    const state = await api('release-policy',{method:'PUT',body:JSON.stringify(policy)});
    releasePolicyBusy = releasePolicyDirty = false; updateReleaseUI(state); error('');
  } catch (e) {
    releasePolicyBusy = releasePolicyDirty = false; updateReleaseUI(lastReleaseState); error(e.message);
  }
}

function saveDraft() {
  updateReleaseUI();
  try {
    const value = {studioMode};
    for (const id of ['prompt','resolution','duration','count','mode','strength','encoder']) value[id] = $(id).value;
    value.aspectRatio = selectedRatio();
    value.gpu = $('h3Gpu')?.value || $('gpu')?.value || 'G4';
    value.peopleEnhancement = $('peopleEnhancement').checked;
    value.performance = $('performance').checked;
    value.quantizedModel = $('quantizedModel').checked;
    value.quantizedVae = $('quantizedVae').checked;
    value.veda = $('veda').checked;
    value.qwenFormatting = $('qwenFormatting').checked;
    value.referenceSize = $('referenceSize').value;
    if (referenceModelActive) value.peopleEnhancement = nonReferencePeople;
    localStorage.setItem(DRAFT, JSON.stringify(value));
  } catch (_) { /* Storage restrictions should not block generation. */ }
}

function restoreDraft() {
  try {
    $('seed').value = '';
    const draft = JSON.parse(localStorage.getItem(DRAFT) || 'null');
    const compute = JSON.parse(localStorage.getItem('lstudio-compute-v1') || 'null');
    const gpu = compute?.gpu;
    if (gpu && $('gpu')?.options && [...$('gpu').options].some(o => o.value === gpu)) $('gpu').value = gpu;
    const savedH3Gpu = localStorage.getItem('lstudio-h3-gpu') || draft?.gpu || 'G4';
    if (savedH3Gpu && $('h3Gpu')?.options && [...$('h3Gpu').options].some(o => o.value === savedH3Gpu)) $('h3Gpu').value = savedH3Gpu;
    if (typeof updateH3GpuDisplay === 'function') updateH3GpuDisplay();
    if (!draft) return;
    for (const id of ['prompt','resolution','duration','count','mode','strength','encoder']) {
      if (typeof draft[id] !== 'string') continue;
      const input = $(id);
      if (input.tagName === 'SELECT' && ![...input.options].some(o => o.value === draft[id])) continue;
      input.value = draft[id];
    }
    const ratio = [...document.querySelectorAll('input[name="aspectRatio"]')].find(input => input.value === draft.aspectRatio);
    if (ratio) ratio.checked = true;
    $('peopleEnhancement').checked = draft.peopleEnhancement === true;
    $('performance').checked = draft.performance !== false;
    $('quantizedModel').checked = draft.quantizedModel !== false;
    $('quantizedVae').checked = draft.quantizedVae !== false;
    $('veda').checked = draft.veda !== false;
    updatePerformance();
    $('qwenFormatting').checked = draft.qwenFormatting !== false;
    if (['match','max'].includes(draft.referenceSize)) $('referenceSize').value = draft.referenceSize;
    restoreReferences([]);
    setMode(['image','reference'].includes(draft.studioMode) ? draft.studioMode : 'text');
  } catch (_) {}
}

function getJobElapsed(job) {
  if (!job) return null;
  const progress = H3JobProgress.snapshot(job, [job], Date.now(), runStarts, activeJobId);
  const elapsed = progress?.elapsed;
  if (typeof elapsed !== 'number' || !Number.isFinite(elapsed) || elapsed < 0) return null;
  const formatted = H3JobProgress.formatDuration(elapsed);
  if (!formatted || formatted === '—') return null;

  let title = `任务总用时 ${formatted}`;
  if (job.timings) {
    const parts = [];
    if (job.timings.generation_seconds) parts.push(`生成 ${H3JobProgress.formatDuration(job.timings.generation_seconds * 1000)}`);
    if (job.timings.prepare_seconds) parts.push(`准备 ${H3JobProgress.formatDuration(job.timings.prepare_seconds * 1000)}`);
    if (parts.length) title += ` (${parts.join(' · ')})`;
  }
  return {
    running: progress.running,
    elapsed,
    formatted,
    text: (job.status === 'queued' ? '排队 ' : '耗时 ') + formatted,
    title
  };
}

function updateCardElapsed(card, job) {
  if (!card || !job) return;
  const elapsedInfo = getJobElapsed(job);
  let el = card.querySelector('.task-card-elapsed');
  if (!elapsedInfo) {
    if (el) el.remove();
    return;
  }
  if (!el) {
    el = document.createElement('span');
    el.className = 'task-card-elapsed';
    const metaEl = card.querySelector('.task-card-meta');
    if (metaEl) metaEl.append(el);
  }
  if (el) {
    if (el.textContent !== elapsedInfo.text) el.textContent = elapsedInfo.text;
    if (el.title !== elapsedInfo.title) el.title = elapsedInfo.title;
  }
}

function updateRunMonitor() {
  const stream = $('taskStreamList');
  if (stream) {
    stream.querySelectorAll('.task-card.is-running, .task-card.is-queued').forEach(card => {
      const jobId = card.dataset.jobId;
      const j = latestJobs.find(item => item.id === jobId);
      if (j) {
        updateCardElapsed(card, j);
        const timer = card.querySelector('.task-elapsed-timer');
        if (timer) {
          const info = getJobElapsed(j);
          if (info) {
            const timerText = `已用时 ${info.formatted}`;
            if (timer.textContent !== timerText) timer.textContent = timerText;
          }
        }
      }
    });
  }
}

async function saveVideoToLocation(url, suggestedName, button, feedbackEl) {
  const safeName = (suggestedName || 'video.mp4').replace(/[<>:"/\\|?*\x00-\x1f]/g, '_').replace(/[. ]+$/, '').trim();
  const filename = safeName.endsWith('.mp4') ? safeName : safeName + '.mp4';
  const originalText = button ? button.textContent : '';

  if (window.showSaveFilePicker) {
    let handle;
    try {
      handle = await window.showSaveFilePicker({
        id: 'h3-video-export',
        suggestedName: filename,
        types: [{
          description: 'MP4 视频文件',
          accept: { 'video/mp4': ['.mp4'] }
        }]
      });
    } catch (err) {
      if (err.name === 'AbortError') return;
      console.warn('showSaveFilePicker error, falling back:', err);
    }

    if (handle) {
      if (button) { button.disabled = true; button.textContent = '下载中…'; }
      if (feedbackEl) feedbackEl.textContent = '正在下载并写入指定位置…';
      try {
        const response = await fetch(url);
        if (!response.ok) throw new Error('读取视频失败（HTTP ' + response.status + '）');
        const writable = await handle.createWritable();
        if (response.body && response.body.pipeTo) {
          await response.body.pipeTo(writable);
        } else {
          const blob = await response.blob();
          await writable.write(blob);
          await writable.close();
        }
        if (button) button.textContent = '已保存';
        if (feedbackEl) feedbackEl.textContent = '视频已成功保存到指定位置';
        setTimeout(() => {
          if (button) { button.textContent = originalText; button.disabled = false; }
          if (feedbackEl && feedbackEl.textContent === '视频已成功保存到指定位置') feedbackEl.textContent = '';
        }, 2500);
        return;
      } catch (err) {
        if (button) { button.textContent = '下载失败'; button.disabled = false; }
        if (feedbackEl) feedbackEl.textContent = '保存失败：' + (err.message || err);
        return;
      }
    }
  }

  const a = document.createElement('a');
  a.href = url + (url.includes('?') ? '&' : '?') + 'download=true';
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  if (button) {
    button.textContent = '已触发下载';
    setTimeout(() => { if (button) button.textContent = originalText; }, 2000);
  }
}

function historyRemovalBox(jobId) {
  return jobId ? [...document.querySelectorAll('.task-card')].find(row=>row.dataset.jobId === jobId)?.querySelector('.history-remove-confirm') : $('historyClearConfirm');
}
function closeHistoryRemoval() {
  if (historyBusy) return;
  document.querySelectorAll('.history-remove-confirm').forEach(box=>box.hidden = true);
  historyRemoval = null;
}
function openHistoryRemoval(jobId = null) {
  if (historyBusy) return;
  const same = historyRemoval === (jobId || '*');
  closeHistoryRemoval();
  if (same) return;
  const box = historyRemovalBox(jobId);
  if (!box) return;
  historyRemoval = jobId || '*'; box.hidden = false;
  box.querySelector('.history-remove-error').textContent = '';
}
async function clearHistoryRecords(jobId = null) {
  if (historyBusy) return;
  const box = historyRemovalBox(jobId);
  if (!box || box.hidden) return;
  historyBusy = true;
  const commit = box.querySelector('.history-confirm-commit');
  if (commit) commit.textContent = '处理中…';
  document.querySelectorAll('.history-remove-confirm button, .history-clear').forEach(button=>button.disabled = true);
  if ($('clearHistory')) $('clearHistory').disabled = true;
  for (const card of $('taskStreamList')?.querySelectorAll('.task-card') || []) {
    if (jobId && card.dataset.jobId !== jobId) continue;
    const video = card.querySelector('video');
    if (video) { video.pause(); video.removeAttribute('src'); video.load(); }
  }
  try {
    const result = await api('history/recycle',{method:'POST',body:JSON.stringify({job_id:jobId})});
    $('historyFeedback').textContent = result.count ? `已将 ${result.count} 条作品移入系统回收箱` : '';
    if (result.errors?.length) box.querySelector('.history-remove-error').textContent = result.errors[0].message;
    else { box.hidden = true; historyRemoval = null; }
  } catch (e) { box.querySelector('.history-remove-error').textContent = e.message; }
  finally {
    historyBusy = false;
    if (commit) commit.textContent = '移除';
    document.querySelectorAll('.history-remove-confirm button, .history-clear').forEach(button=>button.disabled = false);
    if ($('clearHistory')) $('clearHistory').disabled = false;
    const stream = $('taskStreamList');
    if (stream) stream.dataset.fingerprint = '';
    await refreshStatus();

  }
}
function makeHistoryConfirmation(jobId, count = 1) {
  const box = document.createElement('div'); box.className = 'history-remove-confirm'; box.hidden = historyRemoval !== jobId;
  box.setAttribute('role','group'); box.setAttribute('aria-label','确认移入回收箱');
  const text = document.createElement('p'); text.textContent = count > 1 ? `移到回收箱（共 ${count} 个视频）？` : '移到回收箱？';
  const actions = document.createElement('div'); actions.className = 'history-confirm-actions';
  const cancel = document.createElement('button'); cancel.type = 'button'; cancel.textContent = '取消'; cancel.onclick = closeHistoryRemoval;
  const confirm = document.createElement('button'); confirm.type = 'button'; confirm.className = 'history-confirm-commit'; confirm.textContent = '移除'; confirm.onclick = ()=>clearHistoryRecords(jobId);
  const message = document.createElement('p'); message.className = 'history-remove-error'; message.setAttribute('role','alert');
  actions.append(cancel,confirm); box.append(text,actions,message); return box;
}

function renderBalance(data, failure = null) {
  if (data) lastAccountData = data;
  const available = data?.consumption?.paid_compute_units_balance;
  const rate = data?.consumption?.consumption_rate_hourly;
  const hasBalance = typeof available === 'number' && Number.isFinite(available);
  $('balance').textContent = hasBalance ? available.toFixed(2) : '—';
  $('balanceFull').textContent = hasBalance ? available.toFixed(2) + ' 单元' : '—';
  const gpu = $('gpu').value;
  $('estimateLabel').textContent = gpu + ' 预估可用';
  const estimate = data?.gpu_estimates?.[gpu] || (gpu === 'A100' ? data?.a100_estimate : null);
  const minutes = estimate?.remaining_minutes;
  const hasEstimate = hasBalance && typeof minutes === 'number' && Number.isFinite(minutes) && minutes >= 0;
  $('remainingTime').textContent = !hasEstimate ? '—' : minutes === 0 && available > 0 ? '不足 1 分钟' : `${Math.floor(minutes / 60)} 小时 ${minutes % 60} 分`;
  const rateSource = (failure || data?.stale) && estimate?.rate_source === 'live' ? 'last_observed' : estimate?.rate_source;
  const source = {reference:'参考',last_observed:'上次',live:'实时'}[rateSource];
  $('estimateBasis').textContent = estimate && source ? `按 ${estimate.rate_hourly.toFixed(2)} 单元/小时（${source}）` : `连接 ${gpu} 后读取实际费率`;
  $('estimateDetail').title = estimate && source ? `按 ${estimate.rate_hourly.toFixed(2)} 单元/小时换算。${rateSource === 'reference' ? '参考设置期间观测的费率。' : rateSource === 'last_observed' ? '使用账户上次单独运行 ' + gpu + ' 时的实际费率。' : '使用当前单张 ' + gpu + ' 的实际费率。'}这是累计 GPU 使用时长，包含模型准备，不是成片时长或单次连接时限。` : '';
  const updated = data?.updated_at ? new Date(data.updated_at * 1000).toLocaleTimeString('zh-CN', {hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false,timeZone:'Asia/Shanghai'}) : null;
  const stale = !!failure || data?.stale;
  $('remainingTime').classList.toggle('stale', stale);
  if (stale && hasEstimate) $('remainingTime').title = '根据上次读取的余额估算，当前更新失败或数据过期。';
  else $('remainingTime').title = '余额可支持的累计 ' + gpu + ' 使用时长，包含模型准备时间。';
  $('balanceInfo').classList.toggle('stale', !!stale);
  if (failure || data?.error) {
    $('balanceInfo').textContent = hasBalance ? `上次读取 ${updated} · 更新失败` : '暂时无法读取 · 点击刷新重试';
    $('balanceInfo').title = failure || data.error;
  } else if (hasBalance) {
    $('balanceInfo').textContent = `${typeof rate === 'number' ? rate.toFixed(2) + ' 单元/小时 · ' : ''}${stale ? '上次读取' : '更新'} ${updated}`;
    $('balanceInfo').title = `Colab 账户余额，每 30 秒自动刷新。当前 ${data.runtimes.length} 个运行时。`;
  } else {
    $('balanceInfo').textContent = authorized ? '正在读取 Colab 余额…' : '授权 Google 后自动显示';
    $('balanceInfo').title = '';
  }
  window.Workspace?.updateBalance(data, failure);
}

async function refreshAccount(force = false) {
  if (!authorized || accountBusy) return;
  accountBusy = true;
  $('balanceRefresh').disabled = true;
  try {
    const data = await api('account' + (force ? '?refresh=true' : ''));
    renderBalance(data);
    renderRuntimes(data);
    $('accountText').textContent = `已连接 Google · ${data.runtimes.length} 个运行时`;
  } catch (e) {
    renderBalance(lastAccountData, e.message);
  } finally {
    accountBusy = false;
    $('balanceRefresh').disabled = !authorized;
  }
}

const KNOWN_GPU_LABELS = {
  G4: 'G4 · Blackwell · 96GB',
  A100: 'A100 · 需要 80GB',
  H100: 'H100 · Hopper · 80GB',
  L4: 'L4 · 24GB',
  T4: 'T4 · 16GB'
};

function updateDiscoveredGpuOptions(allRuntimes) {
  if (window.Workspace?.syncGpuSelects) {
    window.Workspace.syncGpuSelects(allRuntimes);
    if (typeof updateH3GpuDisplay === 'function') updateH3GpuDisplay();
  }
  const gpuSelect = $('gpu');
  if (!gpuSelect) return;
  const currentGpu = gpuSelect.value || 'G4';
  const onlineCounts = {};
  for (const r of (allRuntimes || [])) {
    if (r.gpu) onlineCounts[r.gpu] = (onlineCounts[r.gpu] || 0) + 1;
  }
  const gpus = ['G4', 'A100', 'H100', 'L4', 'T4'];
  for (const g of Object.keys(onlineCounts)) {
    if (!gpus.includes(g)) gpus.push(g);
  }
  const options = [];
  for (const g of gpus) {
    const base = KNOWN_GPU_LABELS[g] || `${g} · 远程检测`;
    const count = onlineCounts[g] || 0;
    const text = count > 0 ? `${base} (🟢 ${count}台在线)` : base;
    options.push(new Option(text, g));
  }
  gpuSelect.replaceChildren(...options);
  if (gpus.includes(currentGpu)) {
    gpuSelect.value = currentGpu;
  } else if (gpus.length) {
    gpuSelect.value = gpus[0];
  }
}

function renderRuntimes(data) {
  const allRuntimes = (data?.runtimes && data.runtimes.length) ? data.runtimes : (onlineGpuState?.runtimes || []);
  updateDiscoveredGpuOptions(allRuntimes);
  const chosen = $('endpoint').value, gpu = $('gpu').value;
  $('endpoint').replaceChildren(new Option('自动申请 ' + gpu + ' · 按需连接', ''));
  const currentMatching = allRuntimes.filter(r => r.gpu === gpu);
  for (const runtime of currentMatching) {
    const note = runtime.managed ? ' (当前工作台占用)' : ' (在线可用)';
    $('endpoint').add(new Option('已有 ' + gpu + ' · ' + runtime.endpoint + note, runtime.endpoint));
  }
  const otherRuntimes = allRuntimes.filter(r => r.gpu !== gpu);
  if (otherRuntimes.length) {
    const optgroup = document.createElement('optgroup');
    optgroup.label = '其他型号在线显卡（选择后自动切换）';
    for (const r of otherRuntimes) {
      const note = r.managed ? ' (当前工作台占用)' : ' (在线可用)';
      const opt = new Option('已有 ' + r.gpu + ' · ' + r.endpoint + note, r.endpoint);
      opt.dataset.targetGpu = r.gpu;
      optgroup.appendChild(opt);
    }
    $('endpoint').appendChild(optgroup);
  }
  if ([...$('endpoint').options].some(o => o.value === chosen)) $('endpoint').value = chosen;
}

function renderOnlineGpuState() {
  const runtimes = onlineGpuState?.runtimes || [];
  $('refresh').disabled = !authorized || !gpuOnlineReady || gpuDetectionBusy || releaseActionBusy;
  $('refresh').textContent = gpuDetectionBusy ? '正在检测…' : '显卡在线检测';
  const checkedAt = onlineGpuState?.updated_at ? new Date(onlineGpuState.updated_at * 1000).toLocaleTimeString('zh-CN', {hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false,timeZone:'Asia/Shanghai'}) : '';
  $('gpuOnlineStatus').textContent = gpuDetectionBusy ? '正在读取 Colab 在线显卡…' : gpuDetectionError ? '检测失败：' + gpuDetectionError : onlineGpuState ?
    (runtimes.length ? `检测到 ${runtimes.length} 张在线显卡 · ${checkedAt}${serverActive ? ' · 生成任务执行中' : ''}` : `当前没有在线显卡 · ${checkedAt}`) : authorized ? (gpuOnlineReady ? '' : '正在准备显卡检测…') : '连接 Google 后可检测在线显卡。';
  const select = $('onlineGpuSelect'), chosen = select.value;
  const fingerprint = JSON.stringify(runtimes);
  if (select.dataset.fingerprint !== fingerprint) {
    select.replaceChildren();
    for (const runtime of runtimes) select.add(new Option(`${runtime.gpu} · ${runtime.managed ? '当前工作台 · ' : ''}${runtime.endpoint}`, runtime.endpoint));
    if (runtimes.some(runtime => runtime.endpoint === chosen)) select.value = chosen;
    else select.value = runtimes.find(runtime => runtime.managed)?.endpoint || runtimes[0]?.endpoint || '';
    select.dataset.fingerprint = fingerprint;
  }
  $('gpuOnlineTarget').classList.toggle('hidden', !runtimes.length);
  $('release').classList.toggle('hidden', !runtimes.length);
  $('release').disabled = !runtimes.length || serverActive || releaseActionBusy || gpuDetectionBusy || !!lastReleaseState?.checking;
  $('release').textContent = releaseActionBusy ? '正在检查并释放…' : '手动释放显卡';
  select.disabled = releaseActionBusy || gpuDetectionBusy;
  $('gpuOnlineMessage').textContent = gpuReleaseMessage;
  $('gpuOnlineMessage').classList.toggle('hidden', !gpuReleaseMessage);
}

async function detectOnlineGPUs(preserveMessage = false) {
  if (!authorized || !gpuOnlineReady || gpuDetectionBusy) return;
  gpuDetectionBusy = true; gpuDetectionError = '';
  if (!preserveMessage) gpuReleaseMessage = '';
  renderOnlineGpuState();
  try {
    const data = await api('gpu/online');
    if (!Array.isArray(data.runtimes)) throw new Error('在线显卡检测返回格式不正确');
    onlineGpuState = data;
    renderRuntimes(data);
    renderBalance(lastAccountData);
  } catch (e) { onlineGpuState = null; gpuDetectionError = e.message; }
  finally { gpuDetectionBusy = false; renderOnlineGpuState(); }
}

async function releaseOnlineGPU() {
  const endpoint = $('onlineGpuSelect').value;
  if (!endpoint || serverActive || releaseActionBusy) return;
  releaseActionBusy = true; gpuReleaseMessage = ''; updateReleaseUI();
  try {
    const result = await api('gpu/release', {method:'POST',body:JSON.stringify({endpoint})});
    gpuReleaseMessage = result.message;
    await refreshStatus(); await detectOnlineGPUs(true); await refreshAccount(true);
  } catch (e) { gpuReleaseMessage = '释放失败：' + e.message; await refreshStatus(); }
  finally { releaseActionBusy = false; updateReleaseUI(); }
}

function renderVariantVideoWrap(sibling) {
  const isLocal = sibling.local_file !== undefined ? !!sibling.local_file : !!sibling.file;
  const available = !!(sibling.file || sibling.drive);
  const isDownloading = !!(sibling.is_preparing || sibling.is_downloading);
  const videoWrap = document.createElement('div');
  videoWrap.className = 'task-card-video-wrap';
  videoWrap.dataset.variantId = sibling.id;

  if (available) {
    if (isLocal) {
      const video = document.createElement('video');
      video.controls = true;
      video.playsInline = true;
      video.preload = 'metadata';
      video.src = '/video/' + (sibling.file || sibling.id + '.mp4') + '#t=0.1';
      video.addEventListener('play', () => {
        for (const other of document.querySelectorAll('video')) if (other !== video) other.pause();
      });
      videoWrap.append(video);
    } else if (isDownloading) {
      const placeholder = document.createElement('div');
      placeholder.className = 'task-card-video-placeholder is-downloading';
      const statusBox = document.createElement('div');
      statusBox.className = 'task-card-downloading-status';
      statusBox.innerHTML = '<span class="pulse-dot"></span><span>正在从 Google Drive 下载视频到本机…</span>';
      const hint = document.createElement('span');
      hint.className = 'task-card-downloading-hint';
      hint.textContent = '已安全保存在云端，正在后台拉取到本地，下载并校验完成后自动就绪';
      placeholder.append(statusBox, hint);
      videoWrap.append(placeholder);
    } else {
      const placeholder = document.createElement('div');
      placeholder.className = 'task-card-video-placeholder';
      if (sibling.local_download_error) {
        const errBox = document.createElement('div');
        errBox.className = 'task-card-download-error';
        errBox.textContent = '本地下载未完成：' + sibling.local_download_error;
        placeholder.append(errBox);
      }
      const loadBtn = document.createElement('button');
      loadBtn.type = 'button';
      loadBtn.className = 'task-action-btn btn-drive-load';
      loadBtn.textContent = sibling.local_download_error ? '☁ 重新从 Drive 下载到本机' : '☁ 从 Drive 下载到本机并播放';
      loadBtn.onclick = async () => {
        loadBtn.disabled = true;
        loadBtn.textContent = '正在发起下载…';
        try {
          await api('jobs/' + encodeURIComponent(sibling.id) + '/retry', {method:'POST'});
          await refreshStatus();
        } catch (e) {
          loadBtn.disabled = false;
          loadBtn.textContent = '下载失败，点击重试：' + e.message;
        }
      };
      placeholder.append(loadBtn);
      videoWrap.append(placeholder);
    }
  } else if (sibling.status === 'running') {
    const placeholder = document.createElement('div');
    placeholder.className = 'task-card-video-placeholder';
    placeholder.innerHTML = `<span class="pulse-dot"></span><span>第 ${sibling.batch_index || 1} 个视频正在生成中…</span>`;
    videoWrap.append(placeholder);
  } else if (sibling.status === 'queued') {
    const placeholder = document.createElement('div');
    placeholder.className = 'task-card-video-placeholder';
    placeholder.innerHTML = `<span class="queued-icon">⏳</span><span>第 ${sibling.batch_index || 1} 个视频等待排队中…</span>`;
    videoWrap.append(placeholder);
  } else if (['error', 'interrupted'].includes(sibling.status)) {
    const placeholder = document.createElement('div');
    placeholder.className = 'task-card-video-placeholder';
    placeholder.innerHTML = `<span>第 ${sibling.batch_index || 1} 个视频生成未完成或已中断</span>`;
    videoWrap.append(placeholder);
  }
  return videoWrap;
}

function updateCardVariantActions(row, group, currentSibling) {
  const actionsLeft = row.querySelector('.task-card-actions-left');
  if (!actionsLeft) return;
  actionsLeft.querySelectorAll('.btn-variant-action').forEach(b => b.remove());

  const isAvailable = !!(currentSibling.file || currentSibling.drive);
  if (isAvailable) {
    const dlBtn = document.createElement('button');
    dlBtn.type = 'button';
    dlBtn.className = 'task-action-btn btn-variant-action';
    dlBtn.textContent = group.isBatch ? `下载视频 ${currentSibling.batch_index}` : '下载视频';
    dlBtn.onclick = async () => {
      const url = '/video/' + (currentSibling.file || currentSibling.id + '.mp4');
      const safeTitle = (currentSibling.creation?.title || 'H3-' + currentSibling.id).replace(/[<>:"/\\|?*\x00-\x1f]/g, '_').replace(/[. ]+$/, '').trim().slice(0, 80);
      const suggestedName = (safeTitle || 'H3-' + currentSibling.id) + '.mp4';
      await saveVideoToLocation(url, suggestedName, dlBtn, null);
    };

    const folderBtn = document.createElement('button');
    folderBtn.type = 'button';
    folderBtn.className = 'task-action-btn btn-variant-action';
    folderBtn.textContent = '打开文件夹';
    folderBtn.onclick = async () => {
      folderBtn.disabled = true;
      folderBtn.textContent = '打开中…';
      try {
        await api('history/' + encodeURIComponent(currentSibling.id) + '/open-folder', {method:'POST'});
        folderBtn.textContent = '已打开';
        if ($('historyFeedback')) $('historyFeedback').textContent = '已打开视频所在文件夹';
      } catch (e) {
        folderBtn.textContent = '打开失败';
        alert(e.message);
      } finally {
        folderBtn.disabled = false;
      }
    };
    actionsLeft.append(dlBtn, folderBtn);
  }

  if (group.isBatch && group.completedSiblings.length > 1) {
    const dlAllBtn = document.createElement('button');
    dlAllBtn.type = 'button';
    dlAllBtn.className = 'task-action-btn btn-variant-action';
    dlAllBtn.textContent = `全部下载 (${group.completedSiblings.length} 个视频)`;
    dlAllBtn.onclick = async () => {
      dlAllBtn.disabled = true;
      dlAllBtn.textContent = '正在下载全部…';
      for (const sib of group.completedSiblings) {
        const url = '/video/' + (sib.file || sib.id + '.mp4');
        const safeTitle = (sib.creation?.title || 'H3-' + sib.id).replace(/[<>:"/\\|?*\x00-\x1f]/g, '_').replace(/[. ]+$/, '').trim().slice(0, 80);
        const suggestedName = (safeTitle || 'H3-' + sib.id) + '.mp4';
        await saveVideoToLocation(url, suggestedName, null, null);
      }
      dlAllBtn.disabled = false;
      dlAllBtn.textContent = `全部下载 (${group.completedSiblings.length} 个视频)`;
    };
    actionsLeft.append(dlAllBtn);
  }
}

function renderTaskStream(jobs) {
  const stream = $('taskStreamList');
  if (!stream) return;
  const empty = $('taskStreamEmpty');
  const scrollY = window.scrollY;

  const visibleJobs = (jobs || []).filter(j => !j.history_hidden && (j.app_id === 'h3' || !j.app_id));

  if (!visibleJobs.length) {
    if ($('taskStreamSummary')) $('taskStreamSummary').textContent = '0 个任务';
    if (empty) empty.classList.remove('hidden');
    stream.querySelectorAll('.task-card').forEach(c => {
      c.querySelectorAll('video').forEach(v => { v.pause(); v.removeAttribute('src'); v.load(); });
      c.remove();
    });
    return;
  }
  if (empty) empty.classList.add('hidden');

  // Group by batch_id so that multi-count submissions are displayed as a unified task
  const groups = [];
  const batchMap = new Map();
  for (const job of visibleJobs) {
    if (job.batch_id) {
      if (!batchMap.has(job.batch_id)) {
        const group = {
          isBatch: true,
          id: job.batch_id,
          batchId: job.batch_id,
          siblings: [job],
          created: job.created || 0,
        };
        batchMap.set(job.batch_id, group);
        groups.push(group);
      } else {
        const group = batchMap.get(job.batch_id);
        group.siblings.push(job);
        group.created = Math.min(group.created, job.created || 0);
      }
    } else {
      groups.push({
        isBatch: false,
        id: job.id,
        siblings: [job],
        created: job.created || 0,
      });
    }
  }

  for (const group of groups) {
    group.siblings.sort((a, b) => (a.batch_index || 0) - (b.batch_index || 0));
    group.total = group.siblings[0]?.batch?.total || group.siblings[0]?.batch_total || group.siblings.length;
    group.runningSibling = group.siblings.find(j => ['running', 'cancelling'].includes(j.status));
    group.queuedSiblings = group.siblings.filter(j => j.status === 'queued');
    group.completedSiblings = group.siblings.filter(j => (j.local_file !== undefined ? j.local_file : !!j.file) && !j.local_download_error);
    group.failedSiblings = group.siblings.filter(j => ['error', 'interrupted'].includes(j.status));
    group.downloadingSiblings = group.siblings.filter(j => j.is_preparing || j.is_downloading);

    group.isRunning = !!group.runningSibling;
    group.isQueued = !group.runningSibling && group.queuedSiblings.length === group.siblings.length;
    group.isDone = group.completedSiblings.length === group.siblings.length;
    group.isDownloading = !group.isRunning && group.downloadingSiblings.length > 0;
    group.primaryJob = group.runningSibling || group.queuedSiblings[0] || group.completedSiblings[0] || group.siblings[0];
  }

  const runningCount = groups.filter(g => g.isRunning || g.isQueued).length;
  const doneCount = groups.filter(g => g.isDone).length;
  const summaryText = (runningCount ? `${runningCount} 个进行/排队 · ` : '') + `${doneCount} 个已完成`;
  if ($('taskStreamSummary')) $('taskStreamSummary').textContent = summaryText;

  const runningGroups = groups.filter(g => g.isRunning);
  const queuedGroups = groups.filter(g => g.isQueued).sort((a, b) => a.created - b.created);
  const completedGroups = groups.filter(g => !g.isRunning && !g.isQueued).sort((a, b) => b.created - a.created);
  const sortedGroups = [...runningGroups, ...queuedGroups, ...completedGroups];

  const existing = new Map([...stream.querySelectorAll('.task-card')].map(c => [c.dataset.jobId, c]));

  sortedGroups.forEach(group => {
    const job = group.primaryJob;
    const date = new Date(group.created * 1000).toLocaleString('zh-CN', {
      month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false, timeZone: 'Asia/Shanghai'
    });

    const fingerprint = JSON.stringify(group.siblings.map(j => [
      j.id, j.status, j.file, j.local_file, j.is_preparing, j.is_downloading, j.local_download_error,
      j.drive?.file_id, j.can_cancel, j.can_retry, j.can_resubmit,
      j.message, j.stage, j.events?.length, j.creation?.title, j.metadata
    ]));

    let card = existing.get(group.id);
    if (card && card.dataset.fingerprint === fingerprint) {
      if (group.isRunning && group.runningSibling) {
        const timer = card.querySelector('.task-elapsed-timer');
        const progress = H3JobProgress.snapshot(group.runningSibling, latestJobs, Date.now(), runStarts, activeJobId);
        if (timer) timer.textContent = `已用时 ${H3JobProgress.formatDuration(progress.elapsed)}`;
        const stagePill = card.querySelector('.task-stage-pill');
        const msg = (group.isBatch ? `第 ${group.runningSibling.batch_index}/${group.total} 个：` : '') + (group.runningSibling.message || '正在处理中…');
        if (stagePill && stagePill.textContent !== msg) {
          stagePill.textContent = msg;
        }
      }
      updateCardElapsed(card, job);
      card.querySelectorAll('.history-clear').forEach(b => b.disabled = historyBusy);
      existing.delete(group.id);
      return;
    }

    const row = document.createElement('article');
    row.className = 'task-card' + (group.isRunning ? ' is-running' : group.isQueued ? ' is-queued' : group.isDownloading ? ' is-downloading' : (group.isDone ? ' is-done' : (group.failedSiblings.length && !group.completedSiblings.length ? ' is-error' : (group.completedSiblings.length ? ' is-done' : ''))));
    row.dataset.jobId = group.id;
    row.dataset.fingerprint = fingerprint;

    const header = document.createElement('div');
    header.className = 'task-card-header';

    const metaEl = document.createElement('div');
    metaEl.className = 'task-card-meta';
    const metaParts = [date];
    if (group.isBatch) metaParts.push(`批次任务（共 ${group.total} 个视频）`);
    if (job.creation?.gpu) metaParts.push(job.creation.gpu);
    if (job.creation?.aspect_ratio) metaParts.push(job.creation.aspect_ratio);
    if (job.creation?.resolution) metaParts.push(job.creation.resolution);
    if (job.metadata) {
      metaParts.push(`${job.metadata.width} × ${job.metadata.height}`);
      if (job.metadata.duration) metaParts.push(H3JobProgress.formatDuration(job.metadata.duration * 1000));
      if (job.metadata.audio) metaParts.push('含声音');
    }

    const metaText = document.createElement('span');
    metaText.className = 'task-card-meta-text';
    metaText.textContent = metaParts.join(' · ');
    metaEl.append(metaText);

    const elapsedInfo = getJobElapsed(job);
    if (elapsedInfo) {
      const elapsedEl = document.createElement('span');
      elapsedEl.className = 'task-card-elapsed';
      elapsedEl.textContent = elapsedInfo.text;
      elapsedEl.title = elapsedInfo.title;
      metaEl.append(elapsedEl);
    }

    const badges = document.createElement('div');
    badges.className = 'task-badges';
    const stateBadge = document.createElement('span');

    if (group.isQueued) {
      const qPos = queuedGroups.findIndex(q => q.id === group.id) + 1;
      stateBadge.className = 'task-badge badge-queued';
      stateBadge.textContent = group.isBatch ? `排队中 #${qPos}（共 ${group.total} 个）` : `排队中 #${qPos}`;
    } else if (group.isRunning) {
      stateBadge.className = 'task-badge badge-running';
      const actIdx = group.runningSibling.batch_index || 1;
      const statusTxt = group.runningSibling.status === 'cancelling' ? '正在取消' : (group.isBatch ? `正在生成 第 ${actIdx}/${group.total} 个` : '正在生成');
      stateBadge.innerHTML = `<span class="pulse-dot"></span>${statusTxt}`;
    } else if (group.isDownloading) {
      stateBadge.className = 'task-badge badge-running badge-downloading';
      stateBadge.innerHTML = `<span class="pulse-dot"></span>从云端下载中`;
    } else if (group.isDone) {
      stateBadge.className = 'task-badge badge-done';
      stateBadge.textContent = group.isBatch ? `已全部完成（共 ${group.total} 个）` : '已完成';
    } else if (group.completedSiblings.length > 0) {
      stateBadge.className = 'task-badge badge-done';
      stateBadge.textContent = `已完成 ${group.completedSiblings.length}/${group.total} 个`;
    } else if (group.failedSiblings.length > 0) {
      stateBadge.className = 'task-badge badge-error';
      stateBadge.textContent = '未完成 / 已暂停';
    } else {
      stateBadge.className = 'task-badge';
      stateBadge.textContent = job.status;
    }
    badges.append(stateBadge);
    header.append(metaEl, badges);
    row.append(header);

    if (job.creation?.prompt) {
      const promptText = job.creation.prompt.trim();
      if (promptText) {
        const promptBox = document.createElement('div');
        promptBox.className = 'task-card-prompt';

        const textEl = document.createElement('div');
        textEl.className = 'task-card-prompt-text';
        textEl.textContent = promptText;
        promptBox.append(textEl);

        if (promptText.length > 50 || promptText.includes('\n')) {
          const toggleBtn = document.createElement('button');
          toggleBtn.type = 'button';
          toggleBtn.className = 'task-card-prompt-toggle';
          toggleBtn.textContent = '展开全文 ▾';

          const doToggle = (e) => {
            if (e) e.stopPropagation();
            const isExp = promptBox.classList.toggle('is-expanded');
            toggleBtn.textContent = isExp ? '收起 ▴' : '展开全文 ▾';
          };
          toggleBtn.onclick = doToggle;
          textEl.onclick = doToggle;
          promptBox.append(toggleBtn);
        }
        row.append(promptBox);
      }
    }

    if (group.isRunning && group.runningSibling) {
      const liveMonitor = document.createElement('div');
      liveMonitor.className = 'task-card-live-monitor';

      const monitorHeader = document.createElement('div');
      monitorHeader.className = 'task-live-monitor-header';

      const statusLeft = document.createElement('div');
      statusLeft.className = 'task-live-monitor-status';
      const stagePill = document.createElement('span');
      stagePill.className = 'task-stage-pill';
      stagePill.textContent = (group.isBatch ? `第 ${group.runningSibling.batch_index}/${group.total} 个：` : '') + (group.runningSibling.message || '正在处理中…');
      statusLeft.innerHTML = '<span class="pulse-dot"></span>';
      statusLeft.append(stagePill);

      const timer = document.createElement('span');
      timer.className = 'task-elapsed-timer';
      const progress = H3JobProgress.snapshot(group.runningSibling, latestJobs, Date.now(), runStarts, activeJobId);
      timer.textContent = `已用时 ${H3JobProgress.formatDuration(progress.elapsed)}`;

      monitorHeader.append(statusLeft, timer);
      liveMonitor.append(monitorHeader);

      const pre = document.createElement('pre');
      pre.className = 'task-live-logs';
      const logLines = (group.runningSibling.events || []).map(e => e.time + '  ' + e.message);
      if (group.isBatch && group.completedSiblings.length > 0) {
        logLines.unshift(`[批次进度] 前序已完成 ${group.completedSiblings.length}/${group.total} 个视频，当前正在执行第 ${group.runningSibling.batch_index} 个：`);
      }
      if (group.runningSibling.creation?.seed !== undefined) logLines.push('随机种子：' + group.runningSibling.creation.seed);
      if (group.runningSibling.model_load_summary) logLines.push(group.runningSibling.model_load_summary);
      if (!logLines.length) logLines.push(group.runningSibling.message || '任务已启动，正在连接运行时…');
      pre.textContent = logLines.join('\n');
      liveMonitor.append(pre);

      setTimeout(() => { pre.scrollTop = pre.scrollHeight; }, 0);
      row.append(liveMonitor);
    } else if (group.isQueued) {
      const qBox = document.createElement('div');
      qBox.className = 'task-card-queued-box';
      const qText = document.createElement('div');
      qText.className = 'task-card-queued-text';
      qText.innerHTML = `<span class="queued-icon">⏳</span><span>前序任务正在执行，已加入队列，轮到时将自动开始${group.isBatch ? `（共 ${group.total} 个视频）` : ''}。</span>`;
      const qCancelBtn = document.createElement('button');
      qCancelBtn.type = 'button';
      qCancelBtn.className = 'task-action-btn btn-cancel task-queued-cancel';
      qCancelBtn.textContent = '取消排队';
      qCancelBtn.onclick = () => cancelTask(group.primaryJob.id);
      qBox.append(qText, qCancelBtn);
      row.append(qBox);
    }

    // Determine default selected variant for batch
    let selectedVariantId = card?.dataset.selectedVariantId;
    if (!selectedVariantId || !group.siblings.some(s => s.id === selectedVariantId)) {
      if (group.runningSibling) selectedVariantId = group.runningSibling.id;
      else if (group.completedSiblings.length) selectedVariantId = group.completedSiblings[0].id;
      else selectedVariantId = group.siblings[0].id;
    }
    row.dataset.selectedVariantId = selectedVariantId;

    // If batch with multiple variants, render tab switcher
    if (group.isBatch && group.total > 1) {
      const tabsWrap = document.createElement('div');
      tabsWrap.className = 'task-card-variant-tabs';
      for (const sibling of group.siblings) {
        const tab = document.createElement('button');
        tab.type = 'button';
        tab.className = 'task-card-variant-tab' + (sibling.id === selectedVariantId ? ' is-active' : '');
        tab.dataset.variantId = sibling.id;
        const isSibDone = (sibling.local_file !== undefined ? sibling.local_file : !!sibling.file) && !sibling.local_download_error;
        const sibIcon = isSibDone ? '✓' : sibling.status === 'running' ? '◌' : sibling.status === 'queued' ? '⏳' : '✕';
        tab.textContent = `视频 ${sibling.batch_index} ${sibIcon}`;
        tab.title = `第 ${sibling.batch_index} 个视频${isSibDone ? '（已就绪）' : sibling.status === 'running' ? '（生成中）' : sibling.status === 'queued' ? '（等待中）' : ''}`;
        tab.onclick = (e) => {
          e.stopPropagation();
          row.dataset.selectedVariantId = sibling.id;
          row.querySelectorAll('.task-card-variant-tab').forEach(t => t.classList.toggle('is-active', t.dataset.variantId === sibling.id));
          const currentVideoWrap = row.querySelector('.task-card-video-wrap');
          const newVideoWrap = renderVariantVideoWrap(sibling);
          if (currentVideoWrap) {
            currentVideoWrap.querySelectorAll('video').forEach(v => { v.pause(); v.removeAttribute('src'); v.load(); });
            currentVideoWrap.replaceWith(newVideoWrap);
          }
          updateCardVariantActions(row, group, sibling);
        };
        tabsWrap.append(tab);
      }
      row.append(tabsWrap);
    }

    const currentVariant = group.siblings.find(s => s.id === selectedVariantId) || group.primaryJob;
    const initialVideoWrap = renderVariantVideoWrap(currentVariant);
    if (initialVideoWrap) row.append(initialVideoWrap);

    // Actions
    const actions = document.createElement('div');
    actions.className = 'task-card-actions';
    const actionsLeft = document.createElement('div');
    actionsLeft.className = 'task-card-actions-left';
    const actionsRight = document.createElement('div');
    actionsRight.className = 'task-card-actions-right';

    const canCancel = group.isRunning || group.isQueued || group.siblings.some(s => s.can_cancel);
    if (canCancel) {
      const cancelBtn = document.createElement('button');
      cancelBtn.type = 'button';
      cancelBtn.className = 'task-action-btn btn-cancel';
      cancelBtn.textContent = group.isQueued ? '取消排队' : (group.isBatch ? '取消批次任务并释放显卡' : '取消任务并释放显卡');
      cancelBtn.onclick = () => cancelTask(group.primaryJob.id);
      actionsLeft.append(cancelBtn);
    }

    if (group.siblings.some(s => s.can_retry)) {
      const retryBtn = document.createElement('button');
      retryBtn.type = 'button';
      retryBtn.className = 'task-action-btn';
      retryBtn.textContent = '重试取回结果';
      retryBtn.onclick = () => retryJob(group.primaryJob.id);
      actionsLeft.append(retryBtn);
    }

    if (group.siblings.some(s => s.can_resubmit)) {
      const resubmitBtn = document.createElement('button');
      resubmitBtn.type = 'button';
      resubmitBtn.className = 'task-action-btn btn-resubmit';
      resubmitBtn.textContent = group.isBatch ? '重新提交批次' : '重新提交';
      resubmitBtn.title = '重新提交该任务加入排队';
      resubmitBtn.onclick = () => resubmitJob(group.primaryJob.id);
      actionsLeft.append(resubmitBtn);
    }

    if (job.creation?.prompt) {
      const applyBtn = document.createElement('button');
      applyBtn.type = 'button';
      applyBtn.className = 'task-action-btn';
      applyBtn.textContent = '应用配置';
      applyBtn.onclick = async () => {
        applyBtn.disabled = true;
        applyBtn.textContent = '已应用';
        try {
          await applyCreation(job.creation, true);
        } finally {
          setTimeout(() => { applyBtn.disabled = false; applyBtn.textContent = '应用配置'; }, 1200);
        }
      };
      actionsLeft.append(applyBtn);
    }

    const canClear = group.siblings.every(s => s.can_clear_history) || group.primaryJob.can_clear_history;
    if (canClear) {
      const clearGroup = document.createElement('div');
      clearGroup.className = 'history-clear-group task-clear-group';

      const removeBtn = document.createElement('button');
      removeBtn.type = 'button';
      removeBtn.className = 'task-action-btn btn-remove history-clear';
      removeBtn.textContent = '×';
      removeBtn.title = group.isBatch ? '将此批次记录移入回收箱' : '将此记录移入回收箱';
      removeBtn.disabled = historyBusy;
      removeBtn.onclick = (e) => {
        e.stopPropagation();
        openHistoryRemoval(group.id);
      };

      clearGroup.append(removeBtn, makeHistoryConfirmation(group.id, group.isBatch ? group.total : 1));
      actionsRight.append(clearGroup);
    }

    actions.append(actionsLeft, actionsRight);
    row.append(actions);

    updateCardVariantActions(row, group, currentVariant);

    if (!group.isRunning && job.events?.length) {
      const details = document.createElement('details');
      details.className = 'task-details-collapsible';
      const summary = document.createElement('summary');
      summary.textContent = '执行记录与日志 ▾';
      const pre = document.createElement('pre');
      const logLines = job.events.map(e => e.time + '  ' + e.message);
      if (job.creation?.seed !== undefined) logLines.push('随机种子：' + job.creation.seed);
      if (job.model_load_summary) logLines.push(job.model_load_summary);
      pre.textContent = logLines.join('\n');
      details.append(summary, pre);
      row.append(details);
    }

    if (card) {
      card.querySelectorAll('video').forEach(v => { v.pause(); v.removeAttribute('src'); v.load(); });
      card.replaceWith(row);
    } else {
      stream.append(row);
    }
    existing.delete(group.id);
  });

  for (const card of existing.values()) {
    card.querySelectorAll('video').forEach(v => { v.pause(); v.removeAttribute('src'); v.load(); });
    card.remove();
  }

  sortedGroups.forEach((group, index) => {
    const el = stream.querySelector(`[data-job-id="${group.id}"]`);
    if (el && stream.children[index] !== el) {
      stream.insertBefore(el, stream.children[index] || null);
    }
  });

  if (Math.abs(window.scrollY - scrollY) > 2) {
    window.scrollTo({ top: scrollY, behavior: 'instant' });
  }
}

async function refreshStatus() {
  try {
    const s = await api('status');
    latestJobs = s.jobs;
    completionSound.update(s.jobs);
    const wasAuthorized = authorized;
    authorized = s.authorized;
    gpuOnlineReady = s.gpu_online_available === true;
    cacheTestRunning = !!s.drive_cache?.test_progress?.running;
    $('driveCacheStatus').textContent = s.drive_cache?.enabled ? '已启用 · H3-Model-Cache' : s.drive_cache?.authorized ? '已授权 · 缓存已停用' : '尚未连接';
    if (s.drive_cache?.backend === 'native' && s.drive_cache.enabled) $('driveCacheStatus').textContent = 'Colab 原生 Drive 缓存已启用 · H3-Model-Cache；运行时能访问整个 Drive。';
    if (s.drive_cache?.test_progress?.running) $('driveCacheStatus').textContent = (s.drive_cache.backend === 'api' ? 'Drive API 已连接 · ' : '') + s.drive_cache.test_progress.message;
    $('driveCacheStatus').textContent += s.drive_cache?.authorized ? ' · 成片保存到 Drive / H3-Videos' : ' · 成片保存到本机';
    const nativeAuth = s.drive_cache?.native_auth;
    $('nativeDriveAuthBox').classList.toggle('hidden', !nativeAuth?.pending);
    if (nativeAuth?.pending) {
      $('nativeDriveGoogleLink').href = nativeAuth.url;
      if (lastNativeDrivePrompt !== nativeAuth.url) $('computePageTab').click();
      lastNativeDrivePrompt = nativeAuth.url;
    } else lastNativeDrivePrompt = null;
    $('confirmNativeDrive').disabled = !!nativeAuth?.confirmed;
    $('confirmNativeDrive').textContent = nativeAuth?.confirmed ? '确认已提交，等待挂载…' : '已完成 Google 确认';
    $('driveDisable').classList.toggle('hidden', !s.drive_cache?.enabled);
    $('driveLogin').textContent = s.drive_cache?.enabled ? 'Drive 模型缓存已连接' : s.drive_cache?.authorized ? '启用 Drive 模型缓存' : '连接 Drive 模型缓存';
    $('driveLogin').disabled = !!s.drive_cache?.enabled || cacheTestRunning;
    $('driveLogin').dataset.authorized = String(!!s.drive_cache?.authorized);
    if ($('startDriveBackup')) {
      $('startDriveBackup').disabled = !s.drive_cache?.authorized || cacheTestRunning;
      $('startDriveBackup').textContent = cacheTestRunning ? '正在备份模型…' : '自动备份模型到 Drive';
    }
    $('connection').textContent = s.active ? 'Colab 正在生成' : s.managed_gpu ? 'GPU 已保留，仍在计费' : authorized ? 'Google 已连接 · 按需启动 GPU' : '首次使用请连接 Google';
    $('connection').classList.toggle('ok', authorized);
    window.Workspace?.updateCompute(s);
    serverActive = !!s.active;
    activeJobId = s.active || null;
    recoveryJob = s.jobs.find(j => (j.app_id === 'h3' || !j.app_id) && ((j.can_retry && j.recovery_kind !== 'download') || (j.can_cancel && j.cancel_requested))) || null;
    updateReleaseUI(s.release);
    updateGenerate();
    renderOnlineGpuState();
    $('login').textContent = authorized ? '重新授权 Google' : '连接 Google 账号';
    $('balanceRefresh').disabled = !authorized || accountBusy;
    if (!authorized) renderBalance(null);
    else if (!wasAuthorized) renderBalance(lastAccountData);
    $('clearHistory').disabled = historyBusy || !(s.history?.clearable ?? s.jobs.filter(j => (j.file || j.drive) && j.can_clear_history).length);
    renderTaskStream(s.jobs);
    window.QwenImage?.update?.(s);
    const accountChanged = s.active !== lastActive || s.managed_gpu !== lastManaged || authorized !== wasAuthorized;
    lastActive = s.active; lastManaged = s.managed_gpu;
    if (authorized && accountChanged) refreshAccount(true);
  } catch (e) { error(e.message); }
}

function generationConfig() {
  const seed = $('seed').value.trim();
  return {
    app_id: 'h3', provider_id: 'colab',
    aspect_ratio: selectedRatio(),
    generation_type: studioMode, references: studioMode === 'image' ? referenceData() : [], reference_size: $('referenceSize').value,
    performance: $('performance').checked,
    encoder: $('performance').checked ? $('encoder').value : 'official_bf16',
    quantized_model: $('performance').checked && $('quantizedModel').checked,
    quantized_vae: $('performance').checked && $('quantizedVae').checked,
    prompt: $('prompt').value.trim(), mode: $('performance').checked ? $('mode').value : 'base', resolution: $('resolution').value,
    prompt_processing: $('qwenFormatting').checked ? 'faithful' : 'direct',
    duration: Number($('duration').value), count: Number($('count').value), seed: seed === '' ? (typeof crypto !== 'undefined' ? crypto.getRandomValues(new Uint32Array(1))[0] : Math.floor(Math.random() * 2**32)) : Number(seed),
    people: $('performance').checked && !referenceModelActive && $('peopleEnhancement').checked, strength: Number($('strength').value), veda: $('performance').checked && $('veda').checked,
    release_mode: selectedReleaseMode(), idle_minutes: Number($('idleMinutes').value),
    gpu: $('h3Gpu')?.value || $('gpu').value, endpoint: $('endpoint').value || null
  };
}

$('textTab').onclick = () => { setMode('text'); saveDraft(); };
$('imageTab').onclick = () => { setMode('image'); saveDraft(); };
$('referenceFiles').onchange = () => addReferences($('referenceFiles').files);
const referenceDrop = $('referenceDropZone');
if (referenceDrop) {
  referenceDrop.onclick = e => {
    if (e.target.closest('button')) return;
    $('referenceFiles').click();
  };
  referenceDrop.ondragover = e => { e.preventDefault(); referenceDrop.classList.add('dragging'); };
  referenceDrop.ondragleave = () => referenceDrop.classList.remove('dragging');
  referenceDrop.ondrop = e => { e.preventDefault(); referenceDrop.classList.remove('dragging'); addReferences(e.dataTransfer.files); };
}
$('btnUploadLocal')?.addEventListener('click', e => {
  e.stopPropagation();
  $('referenceFiles').click();
});
$('chooseLibraryImage')?.addEventListener('click', e => {
  e.stopPropagation();
  pickFromLibrary();
});
$('peopleEnhancement').onchange = () => { updateEnhancement(); saveDraft(); };
$('qwenFormatting').onchange = saveDraft;
$('performance').onchange = () => { updatePerformance(); saveDraft(); };
if ($('h3Gpu')) {
  $('h3Gpu').onchange = () => {
    try { localStorage.setItem('lstudio-h3-gpu', $('h3Gpu').value); } catch (_) {}
    if ($('gpu')) {
      $('gpu').value = $('h3Gpu').value;
      $('endpoint').value = '';
      renderRuntimes(lastAccountData);
      renderBalance(lastAccountData);
    }
    if (typeof updateH3GpuDisplay === 'function') updateH3GpuDisplay();
    saveDraft();
  };
}
$('gpu').onchange = () => {
  $('endpoint').value = ''; renderRuntimes(lastAccountData); renderBalance(lastAccountData);
  try { localStorage.setItem('lstudio-compute-v1',JSON.stringify({provider_id:'colab',gpu:$('gpu').value})); } catch (_) {}
};
$('endpoint').onchange = () => {
  const selectedOpt = $('endpoint').selectedOptions?.[0];
  if (selectedOpt && selectedOpt.dataset.targetGpu && selectedOpt.dataset.targetGpu !== $('gpu').value) {
    $('gpu').value = selectedOpt.dataset.targetGpu;
    renderRuntimes(lastAccountData);
    renderBalance(lastAccountData);
    try { localStorage.setItem('lstudio-compute-v1',JSON.stringify({provider_id:'colab',gpu:$('gpu').value})); } catch (_) {}
  }
};
document.querySelectorAll('input[name="aspectRatio"]').forEach(input => { input.onchange = () => { updateFormat(); saveDraft(); }; });
for (const id of ['resolution','duration','count']) $(id).onchange = () => { updateFormat(); saveDraft(); };
function adjustCount(delta) {
  $('count').value = String(Number($('count').value) + delta);
  updateFormat(); saveDraft();
}
$('decreaseCount').onclick = () => adjustCount(-1);
$('increaseCount').onclick = () => adjustCount(1);
$('count').onkeydown = event => {
  if (event.key === 'ArrowUp' || event.key === 'ArrowDown') {
    event.preventDefault(); adjustCount(event.key === 'ArrowUp' ? 1 : -1);
  }
};
$('resetAdvanced').onclick = () => {
  nonReferencePeople = false;
  $('mode').value = 'pdd'; $('referenceSize').value = 'match'; $('seed').value = ''; $('strength').value = '.7';
  $('peopleEnhancement').checked = false;
  $('performance').checked = true; $('encoder').value = 'heretic_int8';
  $('quantizedModel').checked = $('quantizedVae').checked = true;
  $('veda').checked = true;
  updatePerformance();
  $('qwenFormatting').checked = true;
  updateEnhancement(); saveDraft();
};
$('generateForm').addEventListener('input', saveDraft);
function connectionError(message) {
  $('connectionError').textContent = message;
  $('connectionError').classList.toggle('hidden', !message);
}
$('login').onclick = async () => {
  try {
    const d = await api('authorize/start', {method:'POST'});
    $('googleLink').href = d.url;
    $('authBox').classList.remove('hidden'); $('computePageTab').click(); connectionError('');
  } catch (e) { connectionError(e.message); }
};
$('finishAuth').onclick = async () => {
  try {
    $('finishAuth').disabled = true;
    await api('authorize/finish', {method:'POST',body:JSON.stringify({code:$('authCode').value})});
    $('authCode').value = ''; $('authBox').classList.add('hidden');
    await refreshStatus(); await refreshAccount(true);
  } catch (e) { connectionError(e.message); }
  finally { $('finishAuth').disabled = false; }
};

$('driveLogin').onclick = async () => {
  try {
    if ($('driveLogin').dataset.authorized === 'true') {
      await api('drive-cache', {method:'PUT', body:JSON.stringify({enabled:true})});
      return await refreshStatus();
    }
    const data = await api('drive-cache/authorize/start', {method:'POST'});
    if (data.reused) return await refreshStatus();
    $('driveGoogleLink').href = data.url;
    $('driveAuthBox').classList.remove('hidden'); $('computePageTab').click(); connectionError('');
  } catch (e) { connectionError(e.message); }
};
$('finishDriveAuth').onclick = async () => {
  try {
    $('finishDriveAuth').disabled = true;
    await api('drive-cache/authorize/finish', {method:'POST', body:JSON.stringify({code:$('driveAuthCode').value})});
    $('driveAuthCode').value = ''; $('driveAuthBox').classList.add('hidden'); await refreshStatus();
  } catch (e) { connectionError(e.message); }
  finally { $('finishDriveAuth').disabled = false; }
};
$('confirmNativeDrive').onclick = async () => {
  try { await api('drive-cache/native/confirm', {method:'POST'}); connectionError(''); }
  catch (e) { connectionError(e.message); }
};
$('driveDisable').onclick = async () => {
  try { await api('drive-cache', {method:'PUT', body:JSON.stringify({enabled:false})}); await refreshStatus(); }
  catch (e) { connectionError(e.message); }
};

if ($('startDriveBackup')) {
  $('startDriveBackup').onclick = async () => {
    try {
      $('startDriveBackup').disabled = true;
      $('startDriveBackup').textContent = '正在启动备份…';
      const res = await api('drive-cache/backup', {method:'POST'});
      if (res.message) alert(res.message);
      await refreshStatus();
    } catch (e) {
      connectionError(e.message);
      alert('启动备份失败：' + e.message);
    } finally {
      await refreshStatus();
    }
  };
}



$('referenceHelpToggle').onclick = () => {
  const open = $('referenceHelpToggle').getAttribute('aria-expanded') !== 'true';
  $('referenceHelpToggle').setAttribute('aria-expanded', String(open));
  $('referenceHelp').classList.toggle('hidden', !open);
};
$('refresh').onclick = () => detectOnlineGPUs();
$('clearHistory').onclick = () => openHistoryRemoval();
$('confirmClearHistory').onclick = () => clearHistoryRecords();
$('cancelClearHistory').onclick = closeHistoryRemoval;
document.addEventListener('click',event=>{ if (!event.target.closest('.history-clear,.history-remove-confirm,.history-clear-group,.btn-remove')) closeHistoryRemoval(); });
document.addEventListener('keydown',event=>{ if (event.key === 'Escape') closeHistoryRemoval(); });
$('balanceRefresh').onclick = () => refreshAccount(true);
$('generateForm').onsubmit = async e => {
  e.preventDefault(); error('');
  if (getSubmitCooldownRemaining() > 0) return;
  if (recoveryJob && !serverActive) return recoveryJob.cancel_requested ? cancelTask(recoveryJob.id) : retryJob(recoveryJob.id);
  try {
    if (!$('prompt').value.trim()) return error('请先描述想生成的画面、动作和声音。');
    window.H3Characters?.validate();
    if (studioMode === 'image' && (!references.length && !window.H3Characters?.used().length || references.some(r => !r.image_id || r.pending || r.error))) return error('请先上传图片或引用角色，并替换或移除未就绪的图片。');
    submitting = true;
    const started = Date.now();
    updateGenerate();
    const jobId = (await api('jobs', {method:'POST',body:JSON.stringify(generationConfig())})).id;
    runStarts[jobId] = started;
    runStarts = Object.fromEntries(Object.entries(runStarts).sort((a,b)=>b[1]-a[1]).slice(0,100));
    try { localStorage.setItem('lstudio-h3-run-starts-v1',JSON.stringify(runStarts)); } catch (_) {}
    startSubmitCooldown(5);
    saveDraft(); await refreshStatus();
  } catch (e) { error(e.message); await refreshStatus(); }
  finally { submitting = false; updateGenerate(); }
};
async function retryJob(id) {
  const targetJob = latestJobs.find(j => j.id === id);
  const isDriveDownload = !!(targetJob?.drive && !targetJob?.local_file);
  if (!id || retrying || (!isDriveDownload && serverActive)) return;
  retrying = true; updateGenerate(); error('');
  try { await api('jobs/' + id + '/retry', {method:'POST'}); await refreshStatus(); }
  catch (e) { error(e.message); }
  finally { retrying = false; await refreshStatus(); updateGenerate(); }
}
async function resubmitJob(id) {
  if (!id || resubmitting || submitting || retrying || cancelling) return;
  resubmitting = true;
  error('');
  try {
    await api('jobs/' + encodeURIComponent(id) + '/resubmit', {method: 'POST'});
    await refreshStatus();
  } catch (e) {
    error('重新提交失败：' + e.message);
  } finally {
    resubmitting = false;
    await refreshStatus();
    updateGenerate();
  }
}
async function cancelTask(id) {
  if (!id || cancelling) return;
  cancelling = true; updateGenerate(); error('');
  try { await api('jobs/' + id + '/cancel',{method:'POST',body:'{}'}); await refreshStatus(); }
  catch (e) { error(e.message); }
  finally { cancelling = false; await refreshStatus(); updateGenerate(); }
}
async function applyCreation(config, focus = true) {
  if (!config?.prompt) return;
  setMode('text');
  for (const id of ['prompt','resolution','duration','count','mode','strength']) {
    if (config[id] !== undefined) $(id).value = String(config[id]);
  }
  $('seed').value = '';
  document.querySelectorAll('input[name="aspectRatio"]').forEach(input => { input.checked = input.value === (config.aspect_ratio || '16:9'); });
  $('peopleEnhancement').checked = !!config.people;
  $('performance').checked = config.performance !== false;
  $('encoder').value = config.encoder || 'heretic_int8';
  $('quantizedModel').checked = config.quantized_model !== false;
  $('quantizedVae').checked = config.quantized_vae !== false;
  $('veda').checked = config.veda === true;
  updatePerformance();
  $('qwenFormatting').checked = config.prompt_processing !== 'direct';
  $('referenceSize').value = config.reference_size || 'match';
  await restoreReferences((config.references || []).filter(r => !r.character_id));
  setMode(config.generation_type === 'image' || config.references?.length || window.H3Characters?.used().length ? 'image' : 'text');
  window.H3Characters?.refreshUse();
  updateEnhancement(); updateFormat(); saveDraft(); error('');
  if (focus) {
    $('prompt').scrollIntoView({behavior:'smooth', block:'center'});
    $('prompt').focus({preventScroll:true});
  }
}
async function checkAndRelease() {
  releaseActionBusy = true; updateReleaseUI(); error('');
  try {
    await api('release', {method:'POST',body:'{}'});
    await refreshStatus(); await refreshAccount(true);
  } catch (e) {
    if (e.message && e.message.includes('未取回')) {
      if (confirm('存在未取回或未完成的任务结果。继续释放显卡将放弃未保存的远端临时成片，是否确认释放？')) {
        try {
          await api('release', {method:'POST',body:JSON.stringify({discard_pending:true})});
          await refreshStatus(); await refreshAccount(true);
          return;
        } catch (e2) { error(e2.message); }
      }
    }
    error(e.message); await refreshStatus();
  } finally {
    releaseActionBusy = false; updateReleaseUI();
  }
}
$('release').onclick = releaseOnlineGPU;
$('manualRelease').onclick = checkAndRelease;
document.querySelectorAll('input[name="releaseMode"]').forEach(input => { input.onchange = saveReleasePolicy; });
$('idleMinutes').onchange = saveReleasePolicy;
$('idleMinutes').oninput = () => {
  releasePolicyDirty = true;
  clearTimeout(releasePolicyTimer);
  releasePolicyTimer = setTimeout(saveReleasePolicy, 400);
  updateGenerate();
};
$('showKey').onclick = () => { $('apiKey').type = $('apiKey').type === 'password' ? 'text' : 'password'; };
restoreDraft(); updateEnhancement(); saveDraft(); renderBalance(null);
if (getSubmitCooldownRemaining() > 0) startSubmitCooldown(getSubmitCooldownRemaining());
api('settings').then(settings => { generationSettings = settings; updateFormat(); return refreshStatus(); }).catch(e => error(e.message));
setInterval(refreshStatus, 2500);
setInterval(updateRunMonitor, 1000);
setInterval(() => refreshAccount(), 30000);