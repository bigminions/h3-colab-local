function updateQiGpuDisplay() {
  const select = document.getElementById('qiGpu');
  const display = document.getElementById('qiGpuDisplay');
  const status = document.getElementById('qiGpuStatus');
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
      status.textContent = '推荐 · 显存友好';
      status.classList.remove('status-online');
      status.classList.add('status-idle');
    }
  }
}

'use strict';
(() => {
  const $ = id => document.getElementById(id);
  const getApiKey = () => $('apiKey')?.value || '__API_KEY__';

  const RATIO_NOTES = {
    '1:1': '1024 × 1024 px · 方图',
    '16:9': '1344 × 768 px · 宽屏横图',
    '9:16': '768 × 1344 px · 手机全屏',
    '4:3': '1152 × 864 px · 经典横版',
    '3:4': '864 × 1152 px · 社交竖图',
    '3:2': '1216 × 832 px · 单反横画幅',
    '2:3': '832 × 1216 px · 人像海报'
  };

  const DIMENSIONS = {
    '1:1': '1024 × 1024',
    '16:9': '1344 × 768',
    '9:16': '768 × 1344',
    '4:3': '1152 × 864',
    '3:4': '864 × 1152',
    '3:2': '1216 × 832',
    '2:3': '832 × 1216'
  };

  let mode = 'text'; // 'text' | 'image'
  let referenceImage = null; // { image_id, name, url }
  let submitting = false;
  let historyRemoval = null;
  let historyBusy = false;
  let latestJobs = [];
  let runStarts = {};
  let activeJobId = null;

  // Lightbox viewer state
  let viewerList = [];
  let viewerIndex = 0;

  async function api(path, options = {}) {
    const headers = Object.assign({ 'X-LStudio-Key': getApiKey() }, options.headers || {});
    const res = await fetch('/api/' + path, Object.assign({}, options, { headers }));
    if (!res.ok) {
      let errDetail = `请求失败 (${res.status})`;
      try {
        const data = await res.json();
        errDetail = data.detail || errDetail;
      } catch (_) {}
      throw new Error(errDetail);
    }
    return res.json();
  }

  async function refreshStatusNow() {
    try {
      const s = await api('status');
      latestJobs = s.jobs || [];
      renderTaskStream(s.jobs || []);
    } catch (_) {}
  }

  function setMode(newMode) {
    mode = newMode;
    const isImage = mode === 'image';
    $('qiTextTab')?.classList.toggle('active', !isImage);
    $('qiTextTab')?.setAttribute('aria-selected', String(!isImage));
    $('qiImageTab')?.classList.toggle('active', isImage);
    $('qiImageTab')?.setAttribute('aria-selected', String(isImage));
    const panel = $('qiImagePanel');
    if (panel) {
      panel.hidden = !isImage;
      panel.classList.toggle('hidden', !isImage);
    }
  }

  function selectedRatio() {
    return document.querySelector('input[name="qiAspectRatio"]:checked')?.value || '1:1';
  }

  function updateRatioNote() {
    const ratio = selectedRatio();
    if ($('qiFormatNote')) $('qiFormatNote').textContent = RATIO_NOTES[ratio] || '1024 × 1024 px';
  }

  function showError(msg) {
    const box = $('qiError');
    if (!box) return;
    box.textContent = msg;
    box.hidden = !msg;
    box.classList.toggle('hidden', !msg);
  }

  async function handleReferenceFile(file) {
    if (!file) return;
    showError('');
    $('qiReferenceStatus').textContent = '正在上传…';
    const form = new FormData();
    form.append('file', file);
    form.append('purpose', 'references');
    form.append('app_id', 'qwen_image_2_1');
    try {
      const res = await api('images', { method: 'POST', body: form });
      referenceImage = {
        image_id: res.image_id,
        name: file.name,
        url: URL.createObjectURL(file)
      };
      renderReferencePreview();
    } catch (e) {
      showError('参考图上传失败：' + e.message);
      $('qiReferenceStatus').textContent = '上传失败';
    }
  }

  function renderReferencePreview() {
    const placeholder = $('qiReferencePlaceholder');
    const preview = $('qiReferencePreview');
    const img = $('qiReferenceImg');
    const nameEl = $('qiReferenceName');
    const status = $('qiReferenceStatus');
    if (referenceImage) {
      if (placeholder) placeholder.classList.add('hidden');
      if (preview) {
        preview.hidden = false;
        preview.classList.remove('hidden');
      }
      if (img) {
        img.onerror = () => {
          if (referenceImage?.image_id && !img.dataset.retried) {
            img.dataset.retried = '1';
            fetch('/api/images/' + referenceImage.image_id, { headers: { 'X-LStudio-Key': getApiKey() } })
              .then(res => res.ok ? res.blob() : null)
              .then(blob => {
                if (blob && referenceImage) {
                  const blobUrl = URL.createObjectURL(blob);
                  referenceImage.url = blobUrl;
                  img.src = blobUrl;
                }
              })
              .catch(() => {});
          }
        };
        img.dataset.retried = '';
        if (referenceImage.url) {
          img.src = referenceImage.url;
        } else {
          img.removeAttribute('src');
        }
      }
      if (nameEl) nameEl.textContent = referenceImage.name || referenceImage.image_id || '';
      if (status) status.textContent = '已就绪 (1 张)';
    } else {
      if (placeholder) placeholder.classList.remove('hidden');
      if (preview) {
        preview.hidden = true;
        preview.classList.add('hidden');
      }
      if (img) {
        img.onerror = null;
        img.removeAttribute('src');
      }
      if (nameEl) nameEl.textContent = '';
      if (status) status.textContent = '未选择';
    }
  }

  async function pickLibraryImage() {
    if (!window.Workspace?.openAssetPicker) return;
    const picked = await window.Workspace.openAssetPicker({
      kind: 'images',
      maxCount: 1,
      alreadySelectedIds: referenceImage ? [referenceImage.image_id] : [],
      title: '从素材库选择参考图片'
    });
    if (!picked || !picked.length) return;
    const asset = picked[0];
    let url = '';
    try {
      const resp = await fetch(asset.url, { headers: { 'X-LStudio-Key': window.KEY || '' } });
      if (resp.ok) url = URL.createObjectURL(await resp.blob());
    } catch (_) {}
    if (referenceImage?.url && referenceImage.url.startsWith('blob:')) {
      URL.revokeObjectURL(referenceImage.url);
    }
    referenceImage = {
      image_id: asset.id,
      name: asset.name,
      url: url || asset.url
    };
    renderReferencePreview();
  }

  function clearReference() {
    if (referenceImage?.url && referenceImage.url.startsWith('blob:')) {
      URL.revokeObjectURL(referenceImage.url);
    }
    referenceImage = null;
    if ($('qiReferenceFile')) $('qiReferenceFile').value = '';
    renderReferencePreview();
  }

  function getReleaseMode() {
    return document.querySelector('input[name="releaseMode"]:checked')?.value || 'idle';
  }

  async function submitJob(e) {
    e.preventDefault();
    showError('');
    const prompt = $('qiPrompt')?.value?.trim();
    if (!prompt) {
      showError('请输入画面描述 (Prompt)');
      return;
    }
    if (mode === 'image' && !referenceImage) {
      showError('图生图模式需要至少上传一张参考图片');
      return;
    }

    const seedInput = $('qiSeed')?.value?.trim();
    const seed = seedInput === '' ? Math.floor(Math.random() * (2 ** 32)) : Number(seedInput);
    const count = Number($('qiCount')?.value || 1);
    const steps = Number($('qiSteps')?.value || 8);
    const cfg = Number($('qiCfg')?.value || 1.0);
    const denoise = Number($('qiDenoise')?.value || 0.75);
    const ratio = selectedRatio();
    const modelFile = $('qiModelFile')?.value || 'qwen-image-2.1-turbo-Q4_K_M.gguf';
    const encoder = $('qiEncoder')?.value || 'int8_convrot';
    const negativePrompt = $('qiNegativePrompt')?.value?.trim() || '';
    const gpu = $('qiGpu')?.value || localStorage.getItem('lstudio-qi-gpu') || 'T4';
    const idleMinutes = Number($('idleMinutes')?.value || 10);

    const payload = {
      app_id: 'qwen_image_2_1',
      provider_id: 'colab',
      prompt: prompt,
      negative_prompt: negativePrompt,
      aspect_ratio: ratio,
      model_file: modelFile,
      encoder: encoder,
      steps: steps,
      cfg: cfg,
      count: count,
      seed: seed,
      denoise: denoise,
      gpu: gpu,
      release_mode: getReleaseMode(),
      idle_minutes: idleMinutes,
      references: mode === 'image' && referenceImage ? [{ slot: 1, image_id: referenceImage.image_id }] : []
    };

    const submitBtn = $('qiGenerate');
    try {
      submitting = true;
      if (submitBtn) {
        submitBtn.disabled = true;
        submitBtn.textContent = '正在提交任务…';
      }
      await api('jobs', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload)
      });
      showError('');
      await refreshStatusNow();
    } catch (err) {
      showError('生图提交失败：' + err.message);
    } finally {
      submitting = false;
      if (submitBtn) {
        submitBtn.disabled = false;
        submitBtn.textContent = '开始生图';
      }
    }
  }

  // Elapsed time calculation matching H3
  function getJobElapsed(job) {
    if (!job) return null;
    if (window.H3JobProgress) {
      const progress = window.H3JobProgress.snapshot(job, [job], Date.now(), runStarts, activeJobId);
      const elapsed = progress?.elapsed;
      if (typeof elapsed === 'number' && Number.isFinite(elapsed) && elapsed >= 0) {
        const formatted = window.H3JobProgress.formatDuration(elapsed);
        if (formatted && formatted !== '—') {
          return {
            running: progress.running,
            elapsed,
            formatted,
            text: (job.status === 'queued' ? '排队 ' : (job.status === 'running' ? '已用时 ' : '耗时 ')) + formatted,
            title: `耗时 ${formatted}`
          };
        }
      }
    }
    if (job.timings?.generation_seconds) {
      const sec = Math.round(job.timings.generation_seconds);
      const m = Math.floor(sec / 60);
      const s = sec % 60;
      const formatted = `${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`;
      return {
        running: false,
        elapsed: sec * 1000,
        formatted,
        text: '耗时 ' + formatted,
        title: `耗时 ${formatted}`
      };
    }
    return null;
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
    const stream = $('qiTaskStreamList');
    if (!stream) return;
    stream.querySelectorAll('.task-card.is-running, .task-card.is-queued').forEach(card => {
      const groupId = card.dataset.jobId;
      const j = latestJobs.find(item => item.id === groupId || item.batch_id === groupId);
      if (j) {
        updateCardElapsed(card, j);
        const timer = card.querySelector('.task-elapsed-timer');
        if (timer) {
          const info = getJobElapsed(j);
          if (info) timer.textContent = `已用时 ${info.formatted}`;
        }
      }
    });
  }

  // History removal management matching H3
  function historyRemovalBox(jobId) {
    return jobId
      ? [...document.querySelectorAll('#qiTaskStreamList .task-card')].find(row => row.dataset.jobId === jobId)?.querySelector('.history-remove-confirm')
      : $('qiHistoryClearConfirm');
  }

  function closeHistoryRemoval() {
    if (historyBusy) return;
    document.querySelectorAll('#qiTaskStreamCard .history-remove-confirm').forEach(box => (box.hidden = true));
    historyRemoval = null;
  }

  function openHistoryRemoval(jobId = null) {
    if (historyBusy) return;
    const same = historyRemoval === (jobId || '*');
    closeHistoryRemoval();
    if (same) return;
    const box = historyRemovalBox(jobId);
    if (!box) return;
    historyRemoval = jobId || '*';
    box.hidden = false;
    const errEl = box.querySelector('.history-remove-error');
    if (errEl) errEl.textContent = '';
  }

  async function clearHistoryRecords(jobId = null) {
    if (historyBusy) return;
    const box = historyRemovalBox(jobId);
    if (!box || box.hidden) return;
    historyBusy = true;
    const commit = box.querySelector('.history-confirm-commit');
    if (commit) commit.textContent = '处理中…';
    document.querySelectorAll('#qiTaskStreamCard .history-remove-confirm button, #qiTaskStreamCard .history-clear').forEach(b => (b.disabled = true));
    if ($('qiClearHistory')) $('qiClearHistory').disabled = true;

    try {
      const result = await api('history/recycle', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ job_id: jobId, app_id: 'qwen_image_2_1' })
      });
      if ($('qiHistoryFeedback')) {
        $('qiHistoryFeedback').textContent = result.count ? `已将 ${result.count} 条作品移入系统回收箱` : '';
      }
      if (result.errors?.length) {
        const errEl = box.querySelector('.history-remove-error');
        if (errEl) errEl.textContent = result.errors[0].message;
      } else {
        box.hidden = true;
        historyRemoval = null;
      }
    } catch (e) {
      const errEl = box.querySelector('.history-remove-error');
      if (errEl) errEl.textContent = e.message;
    } finally {
      historyBusy = false;
      if (commit) commit.textContent = '移除';
      document.querySelectorAll('#qiTaskStreamCard .history-remove-confirm button, #qiTaskStreamCard .history-clear').forEach(b => (b.disabled = false));
      if ($('qiClearHistory')) $('qiClearHistory').disabled = false;
      await refreshStatusNow();
    }
  }

  function makeHistoryConfirmation(jobId, count = 1) {
    const box = document.createElement('div');
    box.className = 'history-remove-confirm';
    box.hidden = historyRemoval !== jobId;
    box.setAttribute('role', 'group');
    box.setAttribute('aria-label', '确认移入回收箱');

    const text = document.createElement('p');
    text.textContent = count > 1 ? `移到回收箱（共 ${count} 张图片）？` : '移到回收箱？';

    const actions = document.createElement('div');
    actions.className = 'history-confirm-actions';

    const cancel = document.createElement('button');
    cancel.type = 'button';
    cancel.textContent = '取消';
    cancel.onclick = closeHistoryRemoval;

    const confirm = document.createElement('button');
    confirm.type = 'button';
    confirm.className = 'history-confirm-commit';
    confirm.textContent = '移除';
    confirm.onclick = () => clearHistoryRecords(jobId);

    const message = document.createElement('p');
    message.className = 'history-remove-error';
    message.setAttribute('role', 'alert');

    actions.append(cancel, confirm);
    box.append(text, actions, message);
    return box;
  }

  // Job action controls
  async function cancelTask(id) {
    if (!id) return;
    try {
      await api('jobs/' + encodeURIComponent(id) + '/cancel', { method: 'POST', body: '{}' });
      await refreshStatusNow();
    } catch (e) {
      showError('取消任务失败：' + e.message);
    }
  }

  async function retryJob(id) {
    if (!id) return;
    try {
      await api('jobs/' + encodeURIComponent(id) + '/retry', { method: 'POST' });
      await refreshStatusNow();
    } catch (e) {
      showError('重试失败：' + e.message);
    }
  }

  async function resubmitJob(id) {
    if (!id) return;
    try {
      await api('jobs/' + encodeURIComponent(id) + '/resubmit', { method: 'POST' });
      await refreshStatusNow();
    } catch (e) {
      showError('重新提交失败：' + e.message);
    }
  }

  async function applyCreation(creation, button) {
    if (!creation) return;
    if (button) {
      button.disabled = true;
      button.textContent = '已应用';
    }
    try {
      if ($('qiPrompt')) $('qiPrompt').value = creation.prompt || '';
      if ($('qiNegativePrompt')) $('qiNegativePrompt').value = creation.negative_prompt || '';
      if (creation.aspect_ratio) {
        const radio = document.querySelector(`input[name="qiAspectRatio"][value="${creation.aspect_ratio}"]`);
        if (radio) {
          radio.checked = true;
          updateRatioNote();
        }
      }
      if ($('qiModelFile') && creation.model_file) {
        const mapped = creation.model_file.replace('qwen-image-2.1-UC-', 'qwen-image-2.1-turbo-');
        $('qiModelFile').value = mapped;
        if (!$('qiModelFile').value) {
          $('qiModelFile').value = creation.model_file;
        }
      }
      if ($('qiEncoder') && creation.encoder) $('qiEncoder').value = creation.encoder;
      if ($('qiSteps') && creation.steps) $('qiSteps').value = creation.steps;
      if ($('qiCfg') && creation.cfg) $('qiCfg').value = creation.cfg;
      if ($('qiCount') && creation.count) {
        $('qiCount').value = creation.count;
        if ($('qiDecreaseCount')) $('qiDecreaseCount').disabled = creation.count <= 1;
        if ($('qiIncreaseCount')) $('qiIncreaseCount').disabled = creation.count >= 8;
      }
      if ($('qiDenoise') && typeof creation.denoise === 'number') {
        $('qiDenoise').value = creation.denoise;
        if ($('qiDenoiseVal')) $('qiDenoiseVal').textContent = parseFloat(creation.denoise).toFixed(2);
      }
      // Note: Seed is explicitly NOT applied per requirement ("应用完整的配置项，除种子外")

      if (creation.references && creation.references.length > 0) {
        const ref = creation.references[0];
        if (referenceImage?.url && referenceImage.url.startsWith('blob:')) {
          URL.revokeObjectURL(referenceImage.url);
        }
        const displayName = ref.name || ref.image_id;
        referenceImage = {
          image_id: ref.image_id,
          name: displayName,
          url: '/image/' + ref.image_id
        };
        setMode('image');
        renderReferencePreview();

        fetch('/api/images/' + ref.image_id, { headers: { 'X-LStudio-Key': getApiKey() } })
          .then(res => res.ok ? res.blob() : null)
          .then(blob => {
            if (blob && referenceImage && referenceImage.image_id === ref.image_id) {
              const blobUrl = URL.createObjectURL(blob);
              referenceImage.url = blobUrl;
              if ($('qiReferenceImg')) $('qiReferenceImg').src = blobUrl;
            }
          })
          .catch(() => {});
      } else {
        setMode('text');
      }

      if (creation.gpu && $('qiGpu')) {
        if ([...$('qiGpu').options].some(o => o.value === creation.gpu)) {
          $('qiGpu').value = creation.gpu;
          $('qiGpu').dispatchEvent(new Event('change'));
          if (typeof updateQiGpuDisplay === 'function') updateQiGpuDisplay();
        }
      }

      if (creation.release_mode) {
        const rel = document.querySelector(`input[name="releaseMode"][value="${creation.release_mode}"]`);
        if (rel) {
          rel.checked = true;
          rel.dispatchEvent(new Event('change'));
        }
      }
      if (creation.idle_minutes && $('idleMinutes')) {
        $('idleMinutes').value = creation.idle_minutes;
        $('idleMinutes').dispatchEvent(new Event('change'));
      }

      window.Workspace?.show?.('qwen_image');
      if ($('qiPrompt')) $('qiPrompt').focus();
    } finally {
      setTimeout(() => {
        if (button) {
          button.disabled = false;
          button.textContent = '应用配置';
        }
      }, 1200);
    }
  }

  async function saveImageToLocation(url, suggestedName, button) {
    const safeName = (suggestedName || 'image.png').replace(/[<>:"/\\|?*\x00-\x1f]/g, '_').replace(/[. ]+$/, '').trim();
    const filename = safeName.endsWith('.png') ? safeName : safeName + '.png';
    const originalText = button ? button.textContent : '';
    if (button) {
      button.disabled = true;
      button.textContent = '正在下载…';
    }
    try {
      const a = document.createElement('a');
      a.href = url + (url.includes('?') ? '&' : '?') + 'download=true';
      a.download = filename;
      document.body.appendChild(a);
      a.click();
      a.remove();
      if (button) button.textContent = '已下载';
    } catch (e) {
      if (button) button.textContent = '下载失败';
    } finally {
      setTimeout(() => {
        if (button) {
          button.disabled = false;
          button.textContent = originalText;
        }
      }, 1500);
    }
  }

  async function downloadAllBatchImages(siblings, button) {
    if (!siblings || !siblings.length) return;
    const originalText = button ? button.textContent : '';
    if (button) {
      button.disabled = true;
      button.textContent = '正在下载全部…';
    }
    try {
      for (let i = 0; i < siblings.length; i++) {
        const sib = siblings[i];
        if (!sib.file) continue;
        const url = '/image/' + sib.file;
        const safeTitle = (sib.creation?.title || 'QI-' + sib.id).replace(/[<>:"/\\|?*\x00-\x1f]/g, '_').replace(/[. ]+$/, '').trim().slice(0, 80);
        const filename = `${safeTitle || 'QI-' + sib.id}_${i + 1}.png`;
        const a = document.createElement('a');
        a.href = url + '?download=true';
        a.download = filename;
        document.body.appendChild(a);
        a.click();
        a.remove();
        await new Promise(r => setTimeout(r, 220));
      }
      if (button) button.textContent = '全部已下载';
    } catch (e) {
      if (button) button.textContent = '下载失败';
    } finally {
      setTimeout(() => {
        if (button) {
          button.disabled = false;
          button.textContent = originalText;
        }
      }, 1800);
    }
  }

  function setAsReference(sibling, button) {
    if (!sibling?.file) return;
    if (referenceImage?.url && referenceImage.url.startsWith('blob:')) {
      URL.revokeObjectURL(referenceImage.url);
    }
    referenceImage = {
      image_id: sibling.file,
      name: sibling.file,
      url: '/image/' + sibling.file
    };
    setMode('image');
    renderReferencePreview();

    fetch('/api/images/' + sibling.file, { headers: { 'X-LStudio-Key': getApiKey() } })
      .then(res => res.ok ? res.blob() : null)
      .then(blob => {
        if (blob && referenceImage && referenceImage.image_id === sibling.file) {
          const blobUrl = URL.createObjectURL(blob);
          referenceImage.url = blobUrl;
          if ($('qiReferenceImg')) $('qiReferenceImg').src = blobUrl;
        }
      })
      .catch(() => {});

    if (button) {
      const orig = button.textContent;
      button.textContent = '已设为参考图';
      setTimeout(() => (button.textContent = orig), 1500);
    }
    $('qiPromptPanel')?.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  }

  // Lightbox viewer implementation
  function openLightboxViewer(items, startIndex = 0) {
    viewerList = items || [];
    viewerIndex = Math.max(0, Math.min(startIndex, viewerList.length - 1));
    const modal = $('qiImageViewer');
    if (!modal) return;
    updateLightboxStage();
    modal.showModal();
  }

  function updateLightboxStage() {
    const item = viewerList[viewerIndex];
    if (!item) return;
    const img = $('qiImageFull');
    const titleEl = $('qiImageTitle');
    const metaEl = $('qiImageMeta');
    const dlBtn = $('qiViewerDownload');
    const prevBtn = $('qiViewerPrev');
    const nextBtn = $('qiViewerNext');

    if (img) {
      img.hidden = false;
      img.src = item.url;
      img.alt = item.prompt || '生图预览';
    }
    if (titleEl) {
      titleEl.textContent = item.prompt ? (item.prompt.length > 50 ? item.prompt.slice(0, 50) + '…' : item.prompt) : '生图预览';
      titleEl.title = item.prompt || '';
    }
    if (metaEl) {
      const metaParts = [];
      if (viewerList.length > 1) metaParts.push(`${viewerIndex + 1} / ${viewerList.length}`);
      if (item.dimensions) metaParts.push(item.dimensions);
      if (item.model) metaParts.push(item.model);
      if (item.date) metaParts.push(item.date);
      metaEl.textContent = metaParts.join(' · ');
    }
    if (dlBtn) {
      dlBtn.href = item.url + '?download=true';
      dlBtn.download = item.filename || 'image.png';
    }
    if (prevBtn) prevBtn.disabled = viewerList.length <= 1;
    if (nextBtn) nextBtn.disabled = viewerList.length <= 1;
  }

  function lightboxPrev() {
    if (viewerList.length <= 1) return;
    viewerIndex = (viewerIndex - 1 + viewerList.length) % viewerList.length;
    updateLightboxStage();
  }

  function lightboxNext() {
    if (viewerList.length <= 1) return;
    viewerIndex = (viewerIndex + 1) % viewerList.length;
    updateLightboxStage();
  }

  // Main task stream rendering function - overall referenced to H3
  function renderTaskStream(allJobs) {
    const stream = $('qiTaskStreamList');
    if (!stream) return;
    const empty = $('qiTaskStreamEmpty');
    const scrollY = window.scrollY;

    const visibleJobs = (allJobs || []).filter(j => !j.history_hidden && j.app_id === 'qwen_image_2_1');

    if (!visibleJobs.length) {
      if ($('qiTaskStreamSummary')) $('qiTaskStreamSummary').textContent = '0 个任务';
      if ($('qiClearHistory')) $('qiClearHistory').disabled = true;
      if (empty) empty.classList.remove('hidden');
      stream.querySelectorAll('.task-card').forEach(c => c.remove());
      return;
    }
    if (empty) empty.classList.add('hidden');

    // Group jobs by batch_id
    const groups = [];
    const batchMap = new Map();
    for (const job of visibleJobs) {
      const batchId = job.batch_id || job.batch?.id;
      if (batchId) {
        if (!batchMap.has(batchId)) {
          const group = {
            isBatch: true,
            id: batchId,
            batchId: batchId,
            siblings: [job],
            created: job.created || 0,
          };
          batchMap.set(batchId, group);
          groups.push(group);
        } else {
          const group = batchMap.get(batchId);
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

      group.isRunning = !!group.runningSibling;
      group.isQueued = !group.runningSibling && group.queuedSiblings.length === group.siblings.length;
      group.isDone = group.completedSiblings.length === group.siblings.length;
      group.primaryJob = group.runningSibling || group.queuedSiblings[0] || group.completedSiblings[0] || group.siblings[0];
    }

    const runningCount = groups.filter(g => g.isRunning || g.isQueued).length;
    const doneCount = groups.filter(g => g.isDone).length;
    const summaryText = (runningCount ? `${runningCount} 个进行/排队 · ` : '') + `${doneCount} 个已完成`;
    if ($('qiTaskStreamSummary')) $('qiTaskStreamSummary').textContent = summaryText;

    const hasClearable = groups.some(g => g.siblings.some(s => s.can_clear_history) || g.isDone);
    if ($('qiClearHistory')) $('qiClearHistory').disabled = historyBusy || !hasClearable;

    // Sorting: Newest in front (最新在前面)
    // Running groups at top, queued next, completed groups sorted newest first (b.created - a.created)
    const runningGroups = groups.filter(g => g.isRunning).sort((a, b) => b.created - a.created);
    const queuedGroups = groups.filter(g => g.isQueued).sort((a, b) => a.created - b.created);
    const completedGroups = groups.filter(g => !g.isRunning && !g.isQueued).sort((a, b) => b.created - a.created);
    const sortedGroups = [...runningGroups, ...queuedGroups, ...completedGroups];

    const existing = new Map([...stream.querySelectorAll('.task-card')].map(c => [c.dataset.jobId, c]));

    sortedGroups.forEach(group => {
      const job = group.primaryJob;
      const creation = job.creation || {};
      const date = new Date(group.created * 1000).toLocaleString('zh-CN', {
        month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false
      });

      const fingerprint = JSON.stringify(group.siblings.map(j => [
        j.id, j.status, j.file, j.local_file, j.local_download_error,
        j.can_cancel, j.can_retry, j.can_resubmit,
        j.message, j.events?.length, j.creation?.prompt, j.creation?.seed
      ]));

      let card = existing.get(group.id);
      if (card && card.dataset.fingerprint === fingerprint) {
        if (group.isRunning && group.runningSibling) {
          const timer = card.querySelector('.task-elapsed-timer');
          if (timer && window.H3JobProgress) {
            const progress = window.H3JobProgress.snapshot(group.runningSibling, latestJobs, Date.now(), runStarts, activeJobId);
            timer.textContent = `已用时 ${window.H3JobProgress.formatDuration(progress.elapsed)}`;
          }
          const stagePill = card.querySelector('.task-stage-pill');
          const msg = (group.isBatch ? `第 ${group.runningSibling.batch_index || 1}/${group.total} 张：` : '') + (group.runningSibling.message || '正在处理中…');
          if (stagePill && stagePill.textContent !== msg) stagePill.textContent = msg;
        }
        updateCardElapsed(card, job);
        card.querySelectorAll('.history-clear').forEach(b => (b.disabled = historyBusy));
        existing.delete(group.id);
        return;
      }

      const row = document.createElement('article');
      row.className = 'task-card' + (group.isRunning ? ' is-running' : group.isQueued ? ' is-queued' : (group.isDone ? ' is-done' : (group.failedSiblings.length && !group.completedSiblings.length ? ' is-error' : (group.completedSiblings.length ? ' is-done' : ''))));
      row.dataset.jobId = group.id;
      row.dataset.fingerprint = fingerprint;

      // Header
      const header = document.createElement('div');
      header.className = 'task-card-header';

      const metaEl = document.createElement('div');
      metaEl.className = 'task-card-meta';

      const metaParts = [date];
      if (group.isBatch) metaParts.push(`批次任务（共 ${group.total} 张图片）`);
      if (creation.gpu) metaParts.push(creation.gpu);
      if (creation.aspect_ratio) {
        const dim = DIMENSIONS[creation.aspect_ratio] || '';
        metaParts.push(creation.aspect_ratio + (dim ? ` (${dim})` : ''));
      }
      if (creation.model_file) metaParts.push(shortModelName(creation.model_file));
      if (creation.steps) metaParts.push(`${creation.steps} 步`);
      if (creation.cfg) metaParts.push(`CFG ${creation.cfg}`);
      if (creation.denoise && creation.references?.length) metaParts.push(`重绘 ${creation.denoise}`);

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
        stateBadge.textContent = group.isBatch ? `排队中 #${qPos}（共 ${group.total} 张）` : `排队中 #${qPos}`;
      } else if (group.isRunning) {
        stateBadge.className = 'task-badge badge-running';
        const actIdx = group.runningSibling.batch_index || 1;
        const statusTxt = group.runningSibling.status === 'cancelling' ? '正在取消' : (group.isBatch ? `正在生成 第 ${actIdx}/${group.total} 张` : '正在生成');
        stateBadge.innerHTML = `<span class="pulse-dot"></span>${statusTxt}`;
      } else if (group.isDone) {
        stateBadge.className = 'task-badge badge-done';
        stateBadge.textContent = group.isBatch ? `已全部完成（共 ${group.total} 张）` : '已完成';
      } else if (group.completedSiblings.length > 0) {
        stateBadge.className = 'task-badge badge-done';
        stateBadge.textContent = `已完成 ${group.completedSiblings.length}/${group.total} 张`;
      } else if (group.failedSiblings.length > 0) {
        stateBadge.className = 'task-badge badge-error';
        stateBadge.textContent = '未完成 / 已暂停';
      } else {
        stateBadge.className = 'task-badge';
        stateBadge.textContent = statusLabel(job.status);
      }
      badges.append(stateBadge);
      header.append(metaEl, badges);
      row.append(header);

      // Prompt Box
      const promptText = (creation.prompt || '').trim();
      if (promptText) {
        const promptBox = document.createElement('div');
        promptBox.className = 'task-card-prompt';

        const textEl = document.createElement('div');
        textEl.className = 'task-card-prompt-text';
        textEl.textContent = promptText;
        promptBox.append(textEl);

        if (creation.negative_prompt) {
          const negEl = document.createElement('div');
          negEl.className = 'task-card-neg-prompt';
          negEl.innerHTML = `<span class="neg-label">反向:</span> ${escapeHtml(creation.negative_prompt)}`;
          promptBox.append(negEl);
        }

        if (promptText.length > 50 || promptText.includes('\n')) {
          const toggleBtn = document.createElement('button');
          toggleBtn.type = 'button';
          toggleBtn.className = 'task-card-prompt-toggle';
          toggleBtn.textContent = '展开全文 ▾';

          const doToggle = e => {
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

      // Live Monitor when running
      if (group.isRunning && group.runningSibling) {
        const liveMonitor = document.createElement('div');
        liveMonitor.className = 'task-card-live-monitor';

        const monitorHeader = document.createElement('div');
        monitorHeader.className = 'task-live-monitor-header';

        const statusLeft = document.createElement('div');
        statusLeft.className = 'task-live-monitor-status';
        const stagePill = document.createElement('span');
        stagePill.className = 'task-stage-pill';
        stagePill.textContent = (group.isBatch ? `第 ${group.runningSibling.batch_index || 1}/${group.total} 张：` : '') + (group.runningSibling.message || '正在处理中…');
        statusLeft.innerHTML = '<span class="pulse-dot"></span>';
        statusLeft.append(stagePill);

        const timer = document.createElement('span');
        timer.className = 'task-elapsed-timer';
        if (window.H3JobProgress) {
          const progress = window.H3JobProgress.snapshot(group.runningSibling, latestJobs, Date.now(), runStarts, activeJobId);
          timer.textContent = `已用时 ${window.H3JobProgress.formatDuration(progress.elapsed)}`;
        }

        monitorHeader.append(statusLeft, timer);
        liveMonitor.append(monitorHeader);

        const pre = document.createElement('pre');
        pre.className = 'task-live-logs';
        const logLines = (group.runningSibling.events || []).map(e => (e.time ? e.time + '  ' : '') + e.message);
        if (group.isBatch && group.completedSiblings.length > 0) {
          logLines.unshift(`[批次进度] 前序已完成 ${group.completedSiblings.length}/${group.total} 张图片，当前正在生成第 ${group.runningSibling.batch_index || 1} 张：`);
        }
        if (creation.seed !== undefined) logLines.push('随机种子：' + creation.seed);
        if (creation.model_file) logLines.push('扩散模型：' + creation.model_file);
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
        qText.innerHTML = `<span class="queued-icon">⏳</span><span>前序任务正在执行，已加入队列，轮到时将自动开始${group.isBatch ? `（共 ${group.total} 张图片）` : ''}。</span>`;
        const qCancelBtn = document.createElement('button');
        qCancelBtn.type = 'button';
        qCancelBtn.className = 'task-action-btn btn-cancel task-queued-cancel';
        qCancelBtn.textContent = '取消排队';
        qCancelBtn.onclick = () => cancelTask(group.primaryJob.id);
        qBox.append(qText, qCancelBtn);
        row.append(qBox);
      }

      // Variant Tabs (if batch > 1)
      let selectedVariantId = card?.dataset.selectedVariantId || group.primaryJob.id;
      if (!group.siblings.some(s => s.id === selectedVariantId)) {
        selectedVariantId = group.completedSiblings[0]?.id || group.primaryJob.id;
      }
      row.dataset.selectedVariantId = selectedVariantId;

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
          tab.textContent = `图片 ${sibling.batch_index || 1} ${sibIcon}`;
          tab.title = `第 ${sibling.batch_index || 1} 张图片${isSibDone ? '（已生成）' : sibling.status === 'running' ? '（生成中）' : sibling.status === 'queued' ? '（等待中）' : ''}`;
          tab.onclick = e => {
            e.stopPropagation();
            row.dataset.selectedVariantId = sibling.id;
            row.querySelectorAll('.task-card-variant-tab').forEach(t => t.classList.toggle('is-active', t.dataset.variantId === sibling.id));
            row.querySelectorAll('.qi-img-item').forEach(it => it.classList.toggle('is-selected', it.dataset.siblingId === sibling.id));
            updateCardActionButtons(row, group, sibling);
          };
          tabsWrap.append(tab);
        }
        row.append(tabsWrap);
      }

      // Image Stage / Gallery presentation
      const galleryData = group.completedSiblings.map(s => ({
        url: '/image/' + s.file,
        filename: s.file,
        prompt: creation.prompt || '',
        dimensions: creation.aspect_ratio ? DIMENSIONS[creation.aspect_ratio] : '',
        model: shortModelName(creation.model_file),
        date: date
      }));

      if (!group.isBatch || group.total === 1) {
        // Single image stage
        const sibling = group.siblings[0];
        const isDone = (sibling.local_file !== undefined ? sibling.local_file : !!sibling.file) && !sibling.local_download_error;
        const mediaWrap = document.createElement('div');
        mediaWrap.className = 'task-card-image-wrap';

        if (isDone) {
          const stage = document.createElement('div');
          stage.className = 'qi-single-image-stage';

          const img = document.createElement('img');
          img.src = '/image/' + sibling.file;
          img.alt = promptText;
          img.loading = 'lazy';
          img.style.cursor = 'pointer';
          img.onclick = (e) => {
            e.stopPropagation();
            openLightboxViewer(galleryData, 0);
          };

          const overlay = document.createElement('div');
          overlay.className = 'qi-img-overlay';
          overlay.style.cursor = 'pointer';
          overlay.onclick = (e) => {
            if (e.target.closest('.qi-overlay-btn')) return;
            openLightboxViewer(galleryData, 0);
          };
          stage.style.cursor = 'pointer';
          stage.onclick = (e) => {
            if (e.target.closest('.qi-overlay-btn')) return;
            openLightboxViewer(galleryData, 0);
          };

          const viewBtn = document.createElement('button');
          viewBtn.type = 'button';
          viewBtn.className = 'qi-overlay-btn';
          viewBtn.innerHTML = '🔍 放大查看';
          viewBtn.onclick = () => openLightboxViewer(galleryData, 0);

          const dlBtn = document.createElement('button');
          dlBtn.type = 'button';
          dlBtn.className = 'qi-overlay-btn';
          dlBtn.innerHTML = '💾 下载原图';
          dlBtn.onclick = () => saveImageToLocation('/image/' + sibling.file, sibling.file, dlBtn);

          const refBtn = document.createElement('button');
          refBtn.type = 'button';
          refBtn.className = 'qi-overlay-btn';
          refBtn.innerHTML = '🎨 设为参考图';
          refBtn.onclick = () => setAsReference(sibling, refBtn);

          overlay.append(viewBtn, dlBtn, refBtn);
          stage.append(img, overlay);
          mediaWrap.append(stage);
        } else if (sibling.status === 'running') {
          const placeholder = document.createElement('div');
          placeholder.className = 'task-card-image-placeholder';
          placeholder.innerHTML = '<span class="pulse-dot"></span><span>图片正在生成中…</span>';
          mediaWrap.append(placeholder);
        } else if (sibling.status === 'queued') {
          const placeholder = document.createElement('div');
          placeholder.className = 'task-card-image-placeholder';
          placeholder.innerHTML = '<span class="queued-icon">⏳</span><span>等待排队中…</span>';
          mediaWrap.append(placeholder);
        } else if (['error', 'interrupted'].includes(sibling.status)) {
          const placeholder = document.createElement('div');
          placeholder.className = 'task-card-image-placeholder is-error';
          placeholder.innerHTML = `<span>❌ 图片生成失败或已中断</span><span class="error-msg">${escapeHtml(sibling.message || '')}</span>`;
          mediaWrap.append(placeholder);
        }
        row.append(mediaWrap);
      } else {
        // Multi-image batch grid
        const grid = document.createElement('div');
        grid.className = 'task-card-image-grid';

        group.siblings.forEach((sib, idx) => {
          const isSibDone = (sib.local_file !== undefined ? sib.local_file : !!sib.file) && !sib.local_download_error;
          if (isSibDone) {
            const item = document.createElement('div');
            item.className = 'qi-img-item' + (sib.id === selectedVariantId ? ' is-selected' : '');
            item.dataset.siblingId = sib.id;

            const img = document.createElement('img');
            img.src = '/image/' + sib.file;
            img.alt = promptText;
            img.loading = 'lazy';
            img.style.cursor = 'pointer';
            img.onclick = (e) => {
              e.stopPropagation();
              const galleryIdx = galleryData.findIndex(g => g.filename === sib.file);
              openLightboxViewer(galleryData, galleryIdx >= 0 ? galleryIdx : 0);
            };

            const badge = document.createElement('div');
            badge.className = 'qi-img-badge';
            badge.textContent = `#${sib.batch_index || idx + 1}`;

            const overlay = document.createElement('div');
            overlay.className = 'qi-img-overlay';
            overlay.style.cursor = 'pointer';
            overlay.onclick = (e) => {
              if (e.target.closest('.qi-overlay-btn')) return;
              const galleryIdx = galleryData.findIndex(g => g.filename === sib.file);
              openLightboxViewer(galleryData, galleryIdx >= 0 ? galleryIdx : 0);
            };
            item.style.cursor = 'pointer';
            item.onclick = (e) => {
              if (e.target.closest('.qi-overlay-btn')) return;
              const galleryIdx = galleryData.findIndex(g => g.filename === sib.file);
              openLightboxViewer(galleryData, galleryIdx >= 0 ? galleryIdx : 0);
            };

            const viewBtn = document.createElement('button');
            viewBtn.type = 'button';
            viewBtn.className = 'qi-overlay-btn';
            viewBtn.innerHTML = '🔍';
            viewBtn.title = '放大预览';
            viewBtn.onclick = e => {
              e.stopPropagation();
              const galleryIdx = galleryData.findIndex(g => g.filename === sib.file);
              openLightboxViewer(galleryData, galleryIdx >= 0 ? galleryIdx : 0);
            };

            const dlBtn = document.createElement('button');
            dlBtn.type = 'button';
            dlBtn.className = 'qi-overlay-btn';
            dlBtn.innerHTML = '💾';
            dlBtn.title = '下载';
            dlBtn.onclick = e => {
              e.stopPropagation();
              saveImageToLocation('/image/' + sib.file, sib.file, dlBtn);
            };

            const refBtn = document.createElement('button');
            refBtn.type = 'button';
            refBtn.className = 'qi-overlay-btn';
            refBtn.innerHTML = '🎨';
            refBtn.title = '设为图生图参考图';
            refBtn.onclick = e => {
              e.stopPropagation();
              setAsReference(sib, refBtn);
            };

            overlay.append(viewBtn, dlBtn, refBtn);
            item.append(badge, img, overlay);
            grid.append(item);
          } else if (sib.status === 'running') {
            const pl = document.createElement('div');
            pl.className = 'qi-img-placeholder';
            pl.innerHTML = `<span class="pulse-dot"></span><span>第 ${sib.batch_index || idx + 1} 张生成中</span>`;
            grid.append(pl);
          } else if (sib.status === 'queued') {
            const pl = document.createElement('div');
            pl.className = 'qi-img-placeholder';
            pl.innerHTML = `<span class="queued-icon">⏳</span><span>第 ${sib.batch_index || idx + 1} 张等待中</span>`;
            grid.append(pl);
          } else {
            const pl = document.createElement('div');
            pl.className = 'qi-img-error';
            pl.innerHTML = `<span>❌ 第 ${sib.batch_index || idx + 1} 张失败</span>`;
            grid.append(pl);
          }
        });
        row.append(grid);
      }

      // Actions Bar
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
        resubmitBtn.onclick = () => resubmitJob(group.primaryJob.id);
        actionsLeft.append(resubmitBtn);
      }

      if (creation.prompt) {
        const applyBtn = document.createElement('button');
        applyBtn.type = 'button';
        applyBtn.className = 'task-action-btn';
        applyBtn.textContent = '应用配置';
        applyBtn.onclick = () => applyCreation(creation, applyBtn);
        actionsLeft.append(applyBtn);

        const applyPromptBtn = document.createElement('button');
        applyPromptBtn.type = 'button';
        applyPromptBtn.className = 'task-action-btn';
        applyPromptBtn.textContent = '应用提示词';
        applyPromptBtn.onclick = () => {
          if ($('qiPrompt')) $('qiPrompt').value = creation.prompt || '';
          if ($('qiNegativePrompt')) $('qiNegativePrompt').value = creation.negative_prompt || '';
          window.Workspace?.show?.('qwen_image');
          if ($('qiPrompt')) $('qiPrompt').focus();
          applyPromptBtn.textContent = '已应用提示词！';
          setTimeout(() => (applyPromptBtn.textContent = '应用提示词'), 1200);
        };
        actionsLeft.append(applyPromptBtn);
      }

      // History removal button on card
      const canClear = group.siblings.every(s => s.can_clear_history) || group.primaryJob.can_clear_history || group.isDone;
      if (canClear) {
        const clearGroup = document.createElement('div');
        clearGroup.className = 'history-clear-group task-clear-group';

        const removeBtn = document.createElement('button');
        removeBtn.type = 'button';
        removeBtn.className = 'task-action-btn btn-remove history-clear';
        removeBtn.textContent = '×';
        removeBtn.title = group.isBatch ? '将此批次记录移入回收箱' : '将此记录移入回收箱';
        removeBtn.disabled = historyBusy;
        removeBtn.onclick = e => {
          e.stopPropagation();
          openHistoryRemoval(group.id);
        };

        clearGroup.append(removeBtn, makeHistoryConfirmation(group.id, group.isBatch ? group.total : 1));
        actionsRight.append(clearGroup);
      }

      actions.append(actionsLeft, actionsRight);
      row.append(actions);

      const currentVariant = group.siblings.find(s => s.id === selectedVariantId) || group.primaryJob;
      updateCardActionButtons(row, group, currentVariant);

      // Collapsible execution logs
      if (!group.isRunning && job.events?.length) {
        const details = document.createElement('details');
        details.className = 'task-details-collapsible';
        const summary = document.createElement('summary');
        summary.textContent = '执行记录与日志 ▾';
        const pre = document.createElement('pre');
        const logLines = job.events.map(e => (e.time ? e.time + '  ' : '') + e.message);
        if (creation.seed !== undefined) logLines.push('随机种子：' + creation.seed);
        if (creation.model_file) logLines.push('扩散模型：' + creation.model_file);
        pre.textContent = logLines.join('\n');
        details.append(summary, pre);
        row.append(details);
      }

      if (card) {
        card.replaceWith(row);
      } else {
        stream.append(row);
      }
      existing.delete(group.id);
    });

    for (const card of existing.values()) {
      card.remove();
    }

    // Keep strict DOM order matching sortedGroups (newest in front)
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

  function updateCardActionButtons(row, group, currentSibling) {
    const actionsLeft = row.querySelector('.task-card-actions-left');
    if (!actionsLeft) return;
    actionsLeft.querySelectorAll('.btn-variant-action').forEach(b => b.remove());

    const isAvailable = !!currentSibling?.file;
    if (isAvailable) {
      const folderBtn = document.createElement('button');
      folderBtn.type = 'button';
      folderBtn.className = 'task-action-btn btn-variant-action';
      folderBtn.textContent = '打开文件夹';
      folderBtn.onclick = async () => {
        folderBtn.disabled = true;
        folderBtn.textContent = '打开中…';
        try {
          await api('history/' + encodeURIComponent(currentSibling.id) + '/open-folder', { method: 'POST' });
          folderBtn.textContent = '已打开';
          if ($('qiHistoryFeedback')) $('qiHistoryFeedback').textContent = '已打开图片所在文件夹';
        } catch (e) {
          folderBtn.textContent = '打开失败';
          alert(e.message);
        } finally {
          setTimeout(() => (folderBtn.disabled = false), 1000);
        }
      };

      actionsLeft.append(folderBtn);
    }

    if (group.isBatch && group.completedSiblings.length > 1) {
      const dlAllBtn = document.createElement('button');
      dlAllBtn.type = 'button';
      dlAllBtn.className = 'task-action-btn btn-variant-action';
      dlAllBtn.textContent = `全部下载 (${group.completedSiblings.length} 张图片)`;
      dlAllBtn.onclick = () => downloadAllBatchImages(group.completedSiblings, dlAllBtn);
      actionsLeft.append(dlAllBtn);
    }
  }

  function statusLabel(status) {
    switch (status) {
      case 'running': return '正在生成';
      case 'queued': return '排队中';
      case 'done': return '已完成';
      case 'error': return '错误';
      case 'interrupted': return '已中断';
      case 'cancelling': return '正在取消';
      default: return status || '就绪';
    }
  }

  function shortModelName(file) {
    if (!file) return 'Qwen-Image';
    if (file.includes('Q4_K_M')) return 'GGUF Q4';
    if (file.includes('fp8')) return 'FP8';
    if (file.includes('int8_convrot')) return 'INT8';
    if (file.includes('Q5_K_M')) return 'GGUF Q5';
    if (file.includes('Q8_0')) return 'GGUF Q8';
    if (file.includes('BF16')) return 'BF16';
    return 'Qwen-Image';
  }

  function escapeHtml(str) {
    return (str || '').replace(/[&<>"']/g, m => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#039;'
    }[m]));
  }

  // Stepper buttons for count
  function setupStepper() {
    const input = $('qiCount');
    const dec = $('qiDecreaseCount');
    const inc = $('qiIncreaseCount');
    if (!input || !dec || !inc) return;
    dec.onclick = () => {
      let v = Math.max(1, (parseInt(input.value, 10) || 1) - 1);
      input.value = v;
      dec.disabled = v <= 1;
      inc.disabled = v >= 8;
    };
    inc.onclick = () => {
      let v = Math.min(8, (parseInt(input.value, 10) || 1) + 1);
      input.value = v;
      dec.disabled = v <= 1;
      inc.disabled = v >= 8;
    };
  }

  function initialize() {
    $('qiTextTab')?.addEventListener('click', () => setMode('text'));
    $('qiImageTab')?.addEventListener('click', () => setMode('image'));

    document.querySelectorAll('input[name="qiAspectRatio"]').forEach(r => {
      r.addEventListener('change', updateRatioNote);
    });

    $('qiDenoise')?.addEventListener('input', e => {
      if ($('qiDenoiseVal')) $('qiDenoiseVal').textContent = parseFloat(e.target.value).toFixed(2);
    });

    // Reference file handling
    const dropZone = $('qiReferenceDropZone');
    const fileInput = $('qiReferenceFile');
    if (dropZone && fileInput) {
      dropZone.onclick = e => {
        if (e.target.closest('button')) return;
        fileInput.click();
      };
      fileInput.onchange = () => {
        if (fileInput.files?.[0]) handleReferenceFile(fileInput.files[0]);
      };
      dropZone.ondragover = e => { e.preventDefault(); dropZone.classList.add('dragging'); };
      dropZone.ondragleave = () => dropZone.classList.remove('dragging');
      dropZone.ondrop = e => {
        e.preventDefault();
        dropZone.classList.remove('dragging');
        if (e.dataTransfer.files?.[0]) handleReferenceFile(e.dataTransfer.files[0]);
      };
    }

    $('qiBtnUploadLocal')?.addEventListener('click', e => {
      e.stopPropagation();
      $('qiReferenceFile')?.click();
    });

    $('qiChooseLibraryImage')?.addEventListener('click', e => {
      e.stopPropagation();
      pickLibraryImage();
    });

    $('qiRemoveReference')?.addEventListener('click', e => {
      e.stopPropagation();
      clearReference();
    });

    $('qiGenerateForm')?.addEventListener('submit', submitJob);

    // History recycling controls
    $('qiClearHistory')?.addEventListener('click', () => openHistoryRemoval(null));
    $('qiConfirmClearHistory')?.addEventListener('click', () => clearHistoryRecords(null));
    $('qiCancelClearHistory')?.addEventListener('click', closeHistoryRemoval);

    // Lightbox modal controls
    $('qiViewerPrev')?.addEventListener('click', lightboxPrev);
    $('qiViewerNext')?.addEventListener('click', lightboxNext);
    $('qiCloseImageViewer')?.addEventListener('click', () => $('qiImageViewer')?.close());
    $('qiImageViewer')?.addEventListener('click', e => {
      const modal = $('qiImageViewer');
      if (e.target === modal || e.target.classList?.contains('qi-image-viewer-stage')) {
        modal.close();
      }
    });
    $('qiViewerUseRef')?.addEventListener('click', () => {
      const item = viewerList[viewerIndex];
      if (item) {
        setAsReference({ file: item.filename }, $('qiViewerUseRef'));
        $('qiImageViewer')?.close();
      }
    });

    document.addEventListener('keydown', e => {
      const viewer = $('qiImageViewer');
      if (viewer && viewer.open) {
        if (e.key === 'ArrowLeft') {
          e.preventDefault();
          lightboxPrev();
        } else if (e.key === 'ArrowRight') {
          e.preventDefault();
          lightboxNext();
        } else if (e.key === 'Escape') {
          viewer.close();
        }
      }
    });

    setupStepper();
    updateRatioNote();
    setInterval(updateRunMonitor, 1000);
  }

  async function useImage(asset) {
    let url = '';
    try {
      const resp = await fetch(asset.url, { headers: { 'X-LStudio-Key': getApiKey() } });
      if (resp.ok) url = URL.createObjectURL(await resp.blob());
    } catch (_) {}
    if (referenceImage?.url && referenceImage.url.startsWith('blob:')) {
      URL.revokeObjectURL(referenceImage.url);
    }
    referenceImage = {
      image_id: asset.id,
      name: asset.name,
      url: url || asset.url
    };
    setMode('image');
    renderReferencePreview();
    window.Workspace?.show?.('qwen_image');
  }

  window.QwenImage = {
    setMode,
    useImage,
    renderTaskStream,
    update: s => {
      latestJobs = s.jobs || [];
      renderTaskStream(s.jobs || []);
    }
  };

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', initialize);
  } else {
    initialize();
  }
})();
