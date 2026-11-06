function updateQiGpuDisplay() {
  const select = document.getElementById('qiGpu');
  const display = document.getElementById('qiGpuDisplay');
  const status = document.getElementById('qiGpuStatus');
  if (!select || !display) return;
  const opt = select.selectedOptions?.[0];
  const t = (opt ? opt.textContent : select.value) || '';
  const val = select.value;
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
      const hints = {
        L4: '推荐 · 编辑首选 (24GB)',
        A100: '极速推理 · 高显存 (80GB)',
        H100: '旗舰算力 · 极速 (80GB)',
        T4: '显存友好 · 适于文生图',
        G4: '超大显存 (96GB)'
      };
      status.textContent = hints[val] || '按需启动';
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
  let references = []; // [{ slot: 1..3, image_id, name, url, pending, error }]
  let qiActiveMentionIndex = 0;
  let qiMentionMatches = [];
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
        if (typeof data.detail === 'string') {
          errDetail = data.detail;
        } else if (Array.isArray(data.detail)) {
          errDetail = data.detail.map(item => String(item.msg || item.message || JSON.stringify(item)).replace(/^Value error, /, '')).join('；');
        } else if (data.detail && typeof data.detail === 'object') {
          errDetail = JSON.stringify(data.detail);
        }
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

    const promptLabel = document.querySelector('label[for="qiPrompt"]');
    const promptArea = $('qiPrompt');
    const modelSelect = $('qiModelFile');
    const modelLabel = $('qiModelLabel') || document.querySelector('label[for="qiModelFile"]');
    const stepsInput = $('qiSteps');
    const cfgInput = $('qiCfg');
    const denoiseGroup = $('qiDenoiseGroup');
    const isRepaint = $('qiEditType')?.value === 'repaint';
    if (denoiseGroup) denoiseGroup.classList.toggle('hidden', !isImage || !isRepaint);

    // 区分模型列表：图生图仅展示编辑模型，文生图仅展示 Turbo 加速模型
    const editGroup = $('qiModelGroupEdit');
    const turboGroup = $('qiModelGroupTurbo');
    if (editGroup) {
      editGroup.hidden = !isImage;
      editGroup.style.display = isImage ? '' : 'none';
      [...editGroup.children].forEach(opt => {
        opt.hidden = !isImage;
        opt.disabled = !isImage;
        opt.style.display = isImage ? '' : 'none';
      });
    }
    if (turboGroup) {
      turboGroup.hidden = isImage;
      turboGroup.style.display = !isImage ? '' : 'none';
      [...turboGroup.children].forEach(opt => {
        opt.hidden = isImage;
        opt.disabled = isImage;
        opt.style.display = !isImage ? '' : 'none';
      });
    }

    if (isImage) {
      if (promptLabel) promptLabel.textContent = '编辑指令 (Instruction)';
      if (modelLabel) modelLabel.textContent = '编辑模型 (Qwen-Image-Edit 20B)';
      if (promptArea) {
        promptArea.placeholder = '输入编辑指令，说明需要保留和修改的内容…\n\n例如：\n• 把 @图片1 中的圆形改成方形\n• 将 @图片1 的背景替换为 @图片2 的背景\n• 保留主体的位置和大小';
      }
      if (modelSelect && (modelSelect.value.includes('turbo') || !modelSelect.value)) {
        modelSelect.value = 'qwen-image-edit-2511-Q4_K_M.gguf';
      }
      if (stepsInput && Number(stepsInput.value) <= 8) {
        stepsInput.value = '20';
      }
      if (cfgInput && Number(cfgInput.value) <= 1.0) {
        cfgInput.value = '4.0';
      }
    } else {
      if (promptLabel) promptLabel.textContent = '画面描述 (Prompt)';
      if (modelLabel) modelLabel.textContent = '生图模型 (Qwen-Image Turbo)';
      if (promptArea) {
        promptArea.placeholder = '描述画面主体、背景、光影细节与艺术风格…\n\n例如：白色背景上放着一个蓝色立方体，柔和光照，固定视角。';
      }
      if (modelSelect && (modelSelect.value.includes('edit') || !modelSelect.value)) {
        modelSelect.value = 'qwen-image-2.1-turbo-Q4_K_M.gguf';
      }
      if (stepsInput && Number(stepsInput.value) === 20) {
        stepsInput.value = '8';
      }
      if (cfgInput && Number(cfgInput.value) === 4.0) {
        cfgInput.value = '1.0';
      }
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

  function renderReferences() {
    references.forEach((ref, idx) => { ref.slot = idx + 1; });
    const countText = `${references.length} / 3 张`;
    if ($('qiReferenceCount')) $('qiReferenceCount').textContent = countText;
    if ($('qiReferenceStatus')) $('qiReferenceStatus').textContent = countText;

    const dropZone = $('qiReferenceDropZone');
    const cardsContainer = $('qiReferenceCards');
    if (!cardsContainer) return;

    cardsContainer.replaceChildren();
    cardsContainer.classList.toggle('has-images', references.length > 0);

    for (const ref of [...references].sort((a, b) => a.slot - b.slot)) {
      const card = document.createElement('article');
      card.className = 'reference-card';

      const preview = document.createElement('div');
      preview.className = 'reference-preview';

      if (ref.url) {
        const img = document.createElement('img');
        img.src = ref.url;
        img.alt = `图片${ref.slot}预览`;
        img.onerror = () => {
          if (ref.image_id && !img.dataset.retried) {
            img.dataset.retried = '1';
            fetch('/api/images/' + ref.image_id, { headers: { 'X-LStudio-Key': getApiKey() } })
              .then(res => res.ok ? res.blob() : null)
              .then(blob => {
                if (blob && references.includes(ref)) {
                  const blobUrl = URL.createObjectURL(blob);
                  ref.url = blobUrl;
                  img.src = blobUrl;
                }
              })
              .catch(() => {});
          }
        };
        preview.append(img);
      }

      const badge = document.createElement('span');
      badge.className = 'reference-badge';
      badge.textContent = `图片${ref.slot}`;
      preview.append(badge);

      const state = document.createElement('div');
      state.className = 'hint';
      state.textContent = ref.pending ? '正在上传到本机…' : (ref.error || '');
      state.hidden = !state.textContent;
      state.classList.toggle('reference-error', !!ref.error);

      // Actions bar (exact H3 .reference-actions)
      const actions = document.createElement('div');
      actions.className = 'reference-actions';

      const insert = document.createElement('button');
      insert.type = 'button';
      insert.textContent = `引用 @图片${ref.slot}`;
      insert.disabled = !!ref.pending || !!ref.error || !ref.image_id;
      insert.onclick = () => insertMention(`图片${ref.slot}`);

      const replace = document.createElement('button');
      replace.type = 'button';
      replace.textContent = '替换';
      const input = document.createElement('input');
      input.type = 'file';
      input.className = 'file-input';
      input.accept = 'image/png,image/jpeg,image/webp';
      input.setAttribute('aria-label', `替换图片${ref.slot}`);
      input.onchange = () => {
        if (input.files && input.files[0]) uploadSingleReference(input.files[0], ref);
      };
      replace.onclick = () => input.click();
      replace.disabled = !!ref.pending;

      const remove = document.createElement('button');
      remove.type = 'button';
      remove.textContent = '移除';
      remove.setAttribute('aria-label', `移除图片${ref.slot}`);
      remove.onclick = () => {
        if (ref.url && ref.url.startsWith('blob:')) URL.revokeObjectURL(ref.url);
        references = references.filter(r => r !== ref);
        references.forEach((r, idx) => { r.slot = idx + 1; });
        renderReferences();
      };

      actions.append(insert, replace, remove);
      card.append(preview, state, actions, input);
      cardsContainer.append(card);
    }

    if (references.length < 3) {
      dropZone.hidden = false;
      dropZone.classList.remove('hidden');
      dropZone.style.display = '';
      cardsContainer.append(dropZone);
    } else {
      dropZone.hidden = true;
      dropZone.classList.add('hidden');
      dropZone.style.display = 'none';
    }
  }

  async function uploadSingleReference(file, existingRef = null) {
    if (!file) return;
    showError('');
    const form = new FormData();
    form.append('file', file);
    form.append('purpose', 'references');
    form.append('app_id', 'qwen_image_2_1');

    let targetRef = existingRef;
    if (!targetRef) {
      if (references.length >= 3) return;
      targetRef = {
        slot: references.length + 1,
        image_id: '',
        name: file.name,
        url: URL.createObjectURL(file),
        pending: true
      };
      references.push(targetRef);
    } else {
      targetRef.pending = true;
      targetRef.name = file.name;
      if (targetRef.url && targetRef.url.startsWith('blob:')) URL.revokeObjectURL(targetRef.url);
      targetRef.url = URL.createObjectURL(file);
    }
    renderReferences();

    try {
      const res = await api('images', { method: 'POST', body: form });
      targetRef.image_id = res.image_id;
      targetRef.pending = false;
      renderReferences();
    } catch (e) {
      showError('参考图上传失败：' + e.message);
      references = references.filter(r => r !== targetRef);
      renderReferences();
    }
  }

  async function handleReferenceFiles(files) {
    if (!files || !files.length) return;
    const availableSlots = 3 - references.length;
    const toAdd = Array.from(files).slice(0, availableSlots);
    for (const f of toAdd) {
      await uploadSingleReference(f);
    }
  }

  async function pickLibraryImages() {
    if (!window.Workspace?.openAssetPicker) return;
    const maxAvailable = 3 - references.length;
    if (maxAvailable <= 0) return;
    const picked = await window.Workspace.openAssetPicker({
      kind: 'images',
      maxCount: maxAvailable,
      alreadySelectedIds: references.map(r => r.image_id).filter(Boolean),
      title: '从素材库选择参考图片 (最多可选 ' + maxAvailable + ' 张)'
    });
    if (!picked || !picked.length) return;
    for (const asset of picked) {
      if (references.length >= 3) break;
      let url = '';
      try {
        const resp = await fetch(asset.url, { headers: { 'X-LStudio-Key': window.KEY || '' } });
        if (resp.ok) url = URL.createObjectURL(await resp.blob());
      } catch (_) {}
      references.push({
        slot: references.length + 1,
        image_id: asset.id,
        name: asset.name,
        url: url || asset.url,
        pending: false
      });
    }
    renderReferences();
  }

  function clearAllReferences() {
    for (const ref of references) {
      if (ref.url && ref.url.startsWith('blob:')) URL.revokeObjectURL(ref.url);
    }
    references = [];
    if ($('qiReferenceFile')) $('qiReferenceFile').value = '';
    renderReferences();
  }

  function insertMention(name) {
    const textarea = $('qiPrompt');
    if (!textarea) return;
    const text = textarea.value;
    const start = textarea.selectionStart ?? text.length;
    const end = textarea.selectionEnd ?? text.length;
    const before = text.slice(0, start);

    const mentionMatch = before.match(/(?<![A-Za-z0-9_@])@([\w\u4e00-\u9fa5]*)$/);
    let insertText = '@' + name + ' ';
    let repStart = start;
    if (mentionMatch) {
      repStart = start - mentionMatch[0].length;
    } else if (start > 0 && !/[\s\n，。！？；、]$/.test(before)) {
      insertText = ' ' + insertText;
    }

    textarea.setRangeText(insertText, repStart, end, 'end');
    closeQiMentionMenu();
    textarea.focus();
  }

  function getCaretCoordinates(element, position) {
    const div = document.createElement('div');
    const style = div.style;
    const computed = window.getComputedStyle(element);

    style.whiteSpace = 'pre-wrap';
    style.wordWrap = 'break-word';
    style.position = 'absolute';
    style.visibility = 'hidden';
    style.overflow = 'hidden';
    style.top = '0';
    style.left = '-9999px';

    const properties = [
      'direction', 'boxSizing', 'width', 'height', 'overflowX', 'overflowY',
      'borderTopWidth', 'borderRightWidth', 'borderBottomWidth', 'borderLeftWidth',
      'borderStyle', 'paddingTop', 'paddingRight', 'paddingBottom', 'paddingLeft',
      'fontStyle', 'fontVariant', 'fontWeight', 'fontStretch', 'fontSize',
      'fontSizeAdjust', 'lineHeight', 'fontFamily', 'textAlign', 'textTransform',
      'textIndent', 'textDecoration', 'letterSpacing', 'wordSpacing', 'tabSize', 'MozTabSize'
    ];
    properties.forEach(prop => { style[prop] = computed[prop]; });

    div.textContent = element.value.substring(0, position);
    const span = document.createElement('span');
    span.textContent = element.value.substring(position) || '.';
    div.appendChild(span);

    document.body.appendChild(div);
    const coordinates = {
      top: span.offsetTop + parseInt(computed.borderTopWidth || 0),
      left: span.offsetLeft + parseInt(computed.borderLeftWidth || 0),
      height: parseInt(computed.lineHeight) || parseInt(computed.fontSize || 14) * 1.5 || 24
    };
    document.body.removeChild(div);
    return coordinates;
  }

  function positionQiMentionMenu(startPos) {
    const textarea = $('qiPrompt');
    const menu = $('qiMentionMenu');
    if (!textarea || !menu) return;
    const coords = getCaretCoordinates(textarea, startPos);
    const textareaOffsetTop = textarea.offsetTop;
    const textareaOffsetLeft = textarea.offsetLeft;

    let top = textareaOffsetTop + coords.top - textarea.scrollTop + coords.height + 4;
    let left = textareaOffsetLeft + coords.left - textarea.scrollLeft;

    const menuWidth = 240;
    const maxLeft = textareaOffsetLeft + textarea.offsetWidth - menuWidth - 8;
    if (left > maxLeft) left = Math.max(textareaOffsetLeft + 8, maxLeft);
    if (left < textareaOffsetLeft + 8) left = textareaOffsetLeft + 8;

    const menuHeight = 220;
    if (top + menuHeight > textareaOffsetTop + textarea.offsetHeight && coords.top > menuHeight) {
      top = textareaOffsetTop + coords.top - textarea.scrollTop - menuHeight - 4;
    }

    menu.style.position = 'absolute';
    menu.style.top = `${top}px`;
    menu.style.left = `${left}px`;
    menu.style.bottom = 'auto';
    menu.style.right = 'auto';
  }

  function getQiMentionContext() {
    const textarea = $('qiPrompt');
    if (!textarea) return null;
    const text = textarea.value;
    const end = textarea.selectionStart;
    const match = text.slice(0, end).match(/(?<![A-Za-z0-9_@])@([\w\u4e00-\u9fa5]*)$/);
    if (!match) return null;
    return {
      start: end - match[0].length,
      end: end,
      query: match[1].toLowerCase()
    };
  }

  function updateQiMentionMenu() {
    const menu = $('qiMentionMenu');
    const textarea = $('qiPrompt');
    if (!menu || !textarea) return;
    const ctx = getQiMentionContext();
    if (!ctx) {
      closeQiMentionMenu();
      return;
    }

    const available = [];
    for (const r of references) {
      available.push({
        slot: r.slot,
        name: `图片${r.slot}`,
        altName: `图${r.slot}`,
        label: `图片 ${r.slot}`,
        url: r.url
      });
    }

    qiMentionMatches = available.filter(item =>
      item.name.toLowerCase().includes(ctx.query) ||
      item.altName.toLowerCase().includes(ctx.query) ||
      item.label.toLowerCase().includes(ctx.query)
    );

    if (!qiMentionMatches.length) {
      closeQiMentionMenu();
      return;
    }

    if (qiActiveMentionIndex >= qiMentionMatches.length) qiActiveMentionIndex = 0;

    menu.replaceChildren();
    qiMentionMatches.forEach((item, idx) => {
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.id = 'qi-mention-option-' + item.slot;
      btn.className = 'mention-option';
      btn.setAttribute('role', 'option');
      btn.setAttribute('aria-selected', String(idx === qiActiveMentionIndex));

      const thumb = document.createElement('div');
      thumb.className = 'mention-thumb-box';
      if (item.url) {
        const img = document.createElement('img');
        img.src = item.url;
        img.alt = item.name;
        thumb.append(img);
      } else {
        thumb.textContent = '🖼️';
      }

      const nameText = document.createElement('span');
      nameText.className = 'mention-name';
      nameText.textContent = `@${item.name}`;

      const tagText = document.createElement('span');
      tagText.className = 'mention-tag';
      tagText.textContent = `图${item.slot}`;

      btn.append(thumb, nameText, tagText);
      btn.onmousedown = e => e.preventDefault();
      btn.onclick = () => insertMention(item.name);
      menu.append(btn);
    });

    menu.classList.remove('hidden');
    menu.style.display = 'block';
    positionQiMentionMenu(ctx.start);
  }

  function closeQiMentionMenu() {
    const menu = $('qiMentionMenu');
    if (menu) {
      menu.classList.add('hidden');
      menu.style.display = 'none';
    }
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
    if (mode === 'image' && references.length === 0) {
      showError('智能图像编辑模式需要至少添加一张图片（图1 作为主体基底）');
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

    const editType = $('qiEditType')?.value || 'instruction';

    const payload = {
      app_id: 'qwen_image_2_1',
      edit_type: editType,
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
      references: mode === 'image' ? references.map(r => ({ slot: r.slot, image_id: r.image_id })) : []
    };

    const gpuOk = await (window.Workspace?.chooseQueuePlacement ? window.Workspace.chooseQueuePlacement({
      targetAppId: 'qwen_image_2_1',
      targetGpu: gpu || 'T4'
    }) : Promise.resolve('back'));
    if (!gpuOk) return;

    const submitBtn = $('qiGenerate');
    try {
      submitting = true;
      if (submitBtn) {
        submitBtn.disabled = true;
        submitBtn.textContent = '正在提交任务…';
      }
      await api('jobs?placement=' + gpuOk, {
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
    const targetGpu = $('qiGpu')?.value || localStorage.getItem('lstudio-qi-gpu') || 'T4';
    const gpuOk = await (window.Workspace?.chooseQueuePlacement ? window.Workspace.chooseQueuePlacement({
      targetAppId: 'qwen_image_2_1',
      targetGpu: targetGpu
    }) : Promise.resolve('back'));
    if (!gpuOk) return;
    try {
      await api('jobs/' + encodeURIComponent(id) + '/resubmit?placement=' + gpuOk, { method: 'POST' });
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
          const opt = document.createElement('option');
          opt.value = mapped;
          opt.textContent = mapped + ' (历史配置)';
          $('qiModelFile').appendChild(opt);
          $('qiModelFile').value = mapped;
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
      if ($('qiEditType') && creation.edit_type) {
        $('qiEditType').value = creation.edit_type;
        const denoiseGroup = $('qiDenoiseGroup');
        if (denoiseGroup) denoiseGroup.classList.toggle('hidden', creation.edit_type !== 'repaint');
      }
      // Note: Seed is explicitly NOT applied per requirement ("应用完整的配置项，除种子外")

      if (creation.references && creation.references.length > 0) {
        for (const ref of references) {
          if (ref.url && ref.url.startsWith('blob:')) URL.revokeObjectURL(ref.url);
        }
        references = creation.references.slice(0, 3).map((ref, idx) => ({
          slot: idx + 1,
          image_id: ref.image_id,
          name: ref.name || ref.image_id,
          url: '/image/' + ref.image_id,
          pending: false
        }));
        setMode('image');
        renderReferences();

        references.forEach(ref => {
          fetch('/api/images/' + ref.image_id, { headers: { 'X-LStudio-Key': getApiKey() } })
            .then(res => res.ok ? res.blob() : null)
            .then(blob => {
              if (blob && references.includes(ref)) {
                const blobUrl = URL.createObjectURL(blob);
                ref.url = blobUrl;
                renderReferences();
              }
            })
            .catch(() => {});
        });
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
    const isIcon = button && (button.classList?.contains('qi-overlay-icon-btn') || button.querySelector('svg'));
    const originalText = button ? button.textContent : '';
    const originalHtml = button ? button.innerHTML : '';
    const originalTitle = button ? button.title : '';
    if (button) {
      button.disabled = true;
      if (isIcon) {
        button.title = '正在下载…';
        button.innerHTML = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" class="qi-spin"><circle cx="12" cy="12" r="10" stroke-dasharray="32" stroke-dashoffset="10"/></svg>';
      } else {
        button.textContent = '正在下载…';
      }
    }
    try {
      const a = document.createElement('a');
      a.href = url + (url.includes('?') ? '&' : '?') + 'download=true';
      a.download = filename;
      document.body.appendChild(a);
      a.click();
      a.remove();
      if (button) {
        if (isIcon) {
          button.title = '已下载';
          button.classList.add('is-success');
          button.innerHTML = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="#4ade80" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg>';
        } else {
          button.textContent = '已下载';
        }
      }
    } catch (e) {
      if (button) {
        if (isIcon) {
          button.title = '下载失败';
          button.innerHTML = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="#f87171" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>';
        } else {
          button.textContent = '下载失败';
        }
      }
    } finally {
      setTimeout(() => {
        if (button) {
          button.disabled = false;
          if (isIcon) {
            button.innerHTML = originalHtml;
            button.title = originalTitle;
            button.classList.remove('is-success');
          } else {
            button.textContent = originalText;
          }
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
    const newRef = {
      slot: Math.min(references.length + 1, 3),
      image_id: sibling.file,
      name: sibling.file,
      url: '/image/' + sibling.file,
      pending: false
    };
    if (references.length >= 3) {
      references[0] = newRef;
    } else {
      references.push(newRef);
    }
    setMode('image');
    renderReferences();

    fetch('/api/images/' + sibling.file, { headers: { 'X-LStudio-Key': getApiKey() } })
      .then(res => res.ok ? res.blob() : null)
      .then(blob => {
        if (blob && references.includes(newRef)) {
          const blobUrl = URL.createObjectURL(blob);
          newRef.url = blobUrl;
          renderReferences();
        }
      })
      .catch(() => {});

    if (button) {
      const isIcon = button.classList?.contains('qi-overlay-icon-btn') || button.querySelector('svg');
      const origHtml = button.innerHTML;
      const origTitle = button.title;
      if (isIcon) {
        button.title = `已添加为图${newRef.slot}`;
        button.classList.add('is-success');
        button.innerHTML = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="#4ade80" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg>';
        setTimeout(() => {
          button.innerHTML = origHtml;
          button.title = origTitle;
          button.classList.remove('is-success');
        }, 1500);
      } else {
        const orig = button.textContent;
        button.textContent = `已添加为图${newRef.slot}`;
        setTimeout(() => (button.textContent = orig), 1500);
      }
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
    const queuedGroups = groups.filter(g => g.isQueued).sort((a, b) => Math.min(...a.queuedSiblings.map(j => j.queue_position || Infinity)) - Math.min(...b.queuedSiblings.map(j => j.queue_position || Infinity)));
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
        j.id, j.status, j.queue_position, j.queue_reason, j.file, j.local_file, j.local_download_error,
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
        const qPos = Math.min(...group.queuedSiblings.map(j => j.queue_position || Infinity));
        stateBadge.className = 'task-badge badge-queued';
        stateBadge.textContent = group.isBatch ? `预计 #${Number.isFinite(qPos) ? qPos : '—'}（共 ${group.total} 张）` : `预计 #${Number.isFinite(qPos) ? qPos : '—'}`;
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
      header.append(badges);
      row.append(header, metaEl);

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

          const actions = document.createElement('div');
          actions.className = 'qi-overlay-actions';
          actions.onclick = (e) => e.stopPropagation();

          const dlBtn = document.createElement('button');
          dlBtn.type = 'button';
          dlBtn.className = 'qi-overlay-btn qi-overlay-icon-btn';
          dlBtn.title = '下载原图';
          dlBtn.setAttribute('aria-label', '下载原图');
          dlBtn.innerHTML = '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>';
          dlBtn.onclick = (e) => {
            e.stopPropagation();
            saveImageToLocation('/image/' + sibling.file, sibling.file, dlBtn);
          };

          const refBtn = document.createElement('button');
          refBtn.type = 'button';
          refBtn.className = 'qi-overlay-btn qi-overlay-icon-btn';
          refBtn.title = '设为参考图';
          refBtn.setAttribute('aria-label', '设为参考图');
          refBtn.innerHTML = '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="18" height="18" rx="2" ry="2"/><circle cx="8.5" cy="8.5" r="1.5"/><polyline points="21 15 16 10 5 21"/></svg>';
          refBtn.onclick = (e) => {
            e.stopPropagation();
            setAsReference(sibling, refBtn);
          };

          actions.append(dlBtn, refBtn);
          overlay.append(actions);
          stage.append(img, overlay);
          stage.style.cursor = 'zoom-in';
          stage.onclick = (e) => {
            if (e.target.closest('.qi-overlay-btn') || e.target.closest('.qi-overlay-actions')) return;
            openLightboxViewer(galleryData, 0);
          };
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

            const actions = document.createElement('div');
            actions.className = 'qi-overlay-actions';
            actions.onclick = (e) => e.stopPropagation();

            const dlBtn = document.createElement('button');
            dlBtn.type = 'button';
            dlBtn.className = 'qi-overlay-btn qi-overlay-icon-btn';
            dlBtn.title = '下载图片';
            dlBtn.setAttribute('aria-label', '下载图片');
            dlBtn.innerHTML = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>';
            dlBtn.onclick = e => {
              e.stopPropagation();
              saveImageToLocation('/image/' + sib.file, sib.file, dlBtn);
            };

            const refBtn = document.createElement('button');
            refBtn.type = 'button';
            refBtn.className = 'qi-overlay-btn qi-overlay-icon-btn';
            refBtn.title = '设为参考图';
            refBtn.setAttribute('aria-label', '设为参考图');
            refBtn.innerHTML = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="18" height="18" rx="2" ry="2"/><circle cx="8.5" cy="8.5" r="1.5"/><polyline points="21 15 16 10 5 21"/></svg>';
            refBtn.onclick = e => {
              e.stopPropagation();
              setAsReference(sib, refBtn);
            };

            actions.append(dlBtn, refBtn);
            overlay.append(actions);
            item.append(badge, img, overlay);
            item.style.cursor = 'zoom-in';
            item.onclick = (e) => {
              if (e.target.closest('.qi-overlay-btn') || e.target.closest('.qi-overlay-actions')) return;
              const galleryIdx = galleryData.findIndex(g => g.filename === sib.file);
              openLightboxViewer(galleryData, galleryIdx >= 0 ? galleryIdx : 0);
            };
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
        cancelBtn.disabled = group.siblings.some(s => s.status === 'cancelling');
        cancelBtn.textContent = cancelBtn.disabled ? '正在取消…' : '取消任务';
        cancelBtn.title = group.isBatch ? '取消本批次尚未完成的任务，已完成的作品保留' : '取消此任务，保留显卡环境';
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
      header.append(actions);

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
      case 'cancelled': return '已取消';
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
    $('qiEditType')?.addEventListener('change', e => {
      const isRepaint = e.target.value === 'repaint';
      $('qiDenoiseGroup')?.classList.toggle('hidden', !isRepaint);
    });

    document.querySelectorAll('input[name="qiAspectRatio"]').forEach(r => {
      r.addEventListener('change', updateRatioNote);
    });

    $('qiDenoise')?.addEventListener('input', e => {
      if ($('qiDenoiseVal')) $('qiDenoiseVal').textContent = parseFloat(e.target.value).toFixed(2);
    });

    // Help toggle
    $('qiReferenceHelpToggle')?.addEventListener('click', () => {
      const btn = $('qiReferenceHelpToggle');
      const open = btn.getAttribute('aria-expanded') !== 'true';
      btn.setAttribute('aria-expanded', String(open));
      $('qiReferenceHelp')?.classList.toggle('hidden', !open);
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
        if (fileInput.files) handleReferenceFiles(fileInput.files);
      };
      dropZone.ondragover = e => { e.preventDefault(); dropZone.classList.add('dragging'); };
      dropZone.ondragleave = () => dropZone.classList.remove('dragging');
      dropZone.ondrop = e => {
        e.preventDefault();
        dropZone.classList.remove('dragging');
        if (e.dataTransfer.files) handleReferenceFiles(e.dataTransfer.files);
      };
    }

    $('qiBtnUploadLocal')?.addEventListener('click', e => {
      e.stopPropagation();
      $('qiReferenceFile')?.click();
    });

    $('qiChooseLibraryImage')?.addEventListener('click', e => {
      e.stopPropagation();
      pickLibraryImages();
    });

    // Mention Menu & Prompt bindings
    const promptEl = $('qiPrompt');
    if (promptEl) {
      promptEl.addEventListener('input', updateQiMentionMenu);
      promptEl.addEventListener('click', updateQiMentionMenu);
      promptEl.addEventListener('scroll', () => {
        const menu = $('qiMentionMenu');
        if (menu && !menu.classList.contains('hidden') && menu.style.display !== 'none') {
          const ctx = getQiMentionContext();
          if (ctx) positionQiMentionMenu(ctx.start);
        }
      });
      promptEl.addEventListener('blur', () => setTimeout(closeQiMentionMenu, 250));
      promptEl.addEventListener('keydown', e => {
        const menu = $('qiMentionMenu');
        if (!menu || menu.classList.contains('hidden') || menu.style.display === 'none') return;
        if (e.key === 'ArrowDown') {
          e.preventDefault();
          qiActiveMentionIndex = (qiActiveMentionIndex + 1) % qiMentionMatches.length;
          updateQiMentionMenu();
        } else if (e.key === 'ArrowUp') {
          e.preventDefault();
          qiActiveMentionIndex = (qiActiveMentionIndex - 1 + qiMentionMatches.length) % qiMentionMatches.length;
          updateQiMentionMenu();
        } else if (e.key === 'Enter' || e.key === 'Tab') {
          if (qiMentionMatches[qiActiveMentionIndex]) {
            e.preventDefault();
            insertMention(qiMentionMatches[qiActiveMentionIndex].name);
          }
        } else if (e.key === 'Escape') {
          closeQiMentionMenu();
        }
      });
    }

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

    window.addEventListener('resize', () => {
      const menu = $('qiMentionMenu');
      if (menu && !menu.classList.contains('hidden') && menu.style.display !== 'none') {
        const ctx = getQiMentionContext();
        if (ctx) positionQiMentionMenu(ctx.start);
      }
    });

    setupStepper();
    updateRatioNote();
    setMode('text');
    setInterval(updateRunMonitor, 1000);
  }

  async function useImage(asset) {
    let url = '';
    try {
      const resp = await fetch(asset.url, { headers: { 'X-LStudio-Key': getApiKey() } });
      if (resp.ok) url = URL.createObjectURL(await resp.blob());
    } catch (_) {}
    const newRef = {
      slot: Math.min(references.length + 1, 3),
      image_id: asset.id,
      name: asset.name,
      url: url || asset.url,
      pending: false
    };
    if (references.length >= 3) {
      references[0] = newRef;
    } else {
      references.push(newRef);
    }
    setMode('image');
    renderReferences();
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
