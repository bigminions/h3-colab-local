'use strict';
(() => {
  const $ = window.$ || (id => document.getElementById(id));
  window.$ = $;
  const HASH_MAP = {
    '#h3': 'tool',
    '#tool': 'tool',
    '#qwen_image': 'qwen_image',
    '#qi': 'qwen_image',
    '#gemma': 'gemma',
    '#library': 'library',
    '#compute': 'compute',
    '#settings': 'connections',
    '#connections': 'connections'
  };

  let activeApp = 'tool';
  try {
    const savedApp = localStorage.getItem('lstudio-active-app');
    if (['tool', 'qwen_image', 'gemma'].includes(savedApp)) activeApp = savedApp;
  } catch (_) {}

  const libraryTags = {kind: new Set(), source: new Set(), app: new Set()};
  try {
    const saved = JSON.parse(localStorage.getItem('lstudio-library-kind') || '[]');
    if (Array.isArray(saved)) for (const item of saved) if (['images', 'videos', 'audios'].includes(item)) libraryTags.kind.add(item);
  } catch (_) {}

  let page = 'tool', h3Page = 'tool', scopes = [], assets = [], loading = false, importing = false;
  const librarySelected = new Set();
  let isManageMode = false;
  let libraryRequest = 0, librarySignature = '', previewRequest = 0, previewURL = null;
  const thumbnails = new Map();
  const panels = {tool:'toolPage', characters:'characterPage', qwen_image:'qwenImagePage', gemma:'gemmaPage', library:'libraryPage', compute:'computePage', connections:'connectionPage'};
  const labels = {tool:['应用','H3 视频'], characters:['应用','H3 视频'], qwen_image:['应用','Qwen-Image-2.1'], gemma:['应用','大模型对话'], library:['工作区','素材库'], compute:['工作区','算力'], connections:['工作区','设置']};
  const kinds = {images:'图片', videos:'视频', audios:'音频'};


  let assetPickerResolve = null;
  const assetPickerSelected = new Set();
  let assetPickerOptions = {};
  const pickerThumbnails = new Map();

  function clearPickerThumbnails() {
    for (const pending of pickerThumbnails.values()) pending.then(url => URL.revokeObjectURL(url), () => {});
    pickerThumbnails.clear();
  }

  function pickerThumbnail(asset, img) {
    if (!pickerThumbnails.has(asset.id)) pickerThumbnails.set(asset.id, blobURL(asset));
    pickerThumbnails.get(asset.id).then(url => { if (img.isConnected) img.src = url; }, () => { img.alt = '读取失败'; });
  }

  function updatePickerSelectionStatus() {
    const count = assetPickerSelected.size;
    const max = assetPickerOptions.maxCount || 9;
    const statusEl = $('assetPickerSelectedCount');
    if (statusEl) statusEl.textContent = `已选 ${count} 项`;
    const limitEl = $('assetPickerLimitHint');
    if (limitEl) {
      if (max > 1) {
        limitEl.textContent = `最多可选 ${max} 张图片`;
      } else {
        limitEl.textContent = '单选模式，点击选中后确认';
      }
    }
    const confirmBtn = $('confirmAssetPicker');
    if (confirmBtn) confirmBtn.disabled = count === 0;
  }

  function updatePickerScopeFilters() {
    const appSelect = $('assetPickerAppFilter');
    const purposeSelect = $('assetPickerPurposeFilter');
    if (!appSelect || !purposeSelect) return;
    const currentApp = appSelect.value;
    const currentPurpose = purposeSelect.value;

    const apps = new Map(scopes.map(scope => [scope.app_id, scope.app_name]));
    for (const asset of assets) if (!apps.has(asset.app_id)) apps.set(asset.app_id, asset.app_id);
    appSelect.replaceChildren(new Option('全部应用', ''));
    for (const [id, name] of apps) appSelect.add(new Option(name, id));
    if (apps.has(currentApp)) appSelect.value = currentApp;

    const purposes = new Map(scopes.filter(scope => !currentApp || scope.app_id === currentApp).map(scope => [scope.purpose, scope.purpose_name]));
    purposeSelect.replaceChildren(new Option('全部用途', ''));
    for (const [id, name] of purposes) purposeSelect.add(new Option(name, id));
    if (purposes.has(currentPurpose)) purposeSelect.value = currentPurpose;
  }

  function renderPickerGrid() {
    const grid = $('assetPickerGrid');
    if (!grid) return;
    grid.replaceChildren();
    clearPickerThumbnails();

    const search = ($('assetPickerSearch')?.value || '').trim().toLowerCase();
    const app = $('assetPickerAppFilter')?.value || '';
    const purpose = $('assetPickerPurposeFilter')?.value || '';
    const targetKind = assetPickerOptions.kind || 'images';
    const alreadyIds = new Set(assetPickerOptions.alreadySelectedIds || []);

    const filtered = assets.filter(asset => {
      if (asset.kind !== targetKind) return false;
      if (app && asset.app_id !== app) return false;
      if (purpose && asset.purpose !== purpose) return false;
      if (search && !asset.name.toLowerCase().includes(search)) return false;
      return true;
    });

    if (!filtered.length) {
      const empty = document.createElement('div');
      empty.className = 'asset-picker-empty';
      empty.textContent = search ? '没有找到匹配的素材' : '当前筛选下没有可用素材';
      grid.append(empty);
      return;
    }

    for (const asset of filtered) {
      const isAlready = alreadyIds.has(asset.id);
      const isSelected = assetPickerSelected.has(asset.id);

      const card = document.createElement('div');
      card.className = 'asset-picker-card' + (isSelected ? ' selected' : '') + (isAlready ? ' in-use' : '');
      card.setAttribute('role', 'option');
      card.setAttribute('aria-selected', String(isSelected));
      card.title = isAlready ? `${asset.name} (已在参考图中)` : asset.name;

      const preview = document.createElement('div');
      preview.className = 'asset-picker-thumb';
      const img = document.createElement('img');
      img.alt = asset.name;
      img.loading = 'lazy';
      preview.append(img);
      queueMicrotask(() => pickerThumbnail(asset, img));

      const check = document.createElement('span');
      check.className = 'asset-picker-check';
      check.textContent = isSelected ? '✓' : '';
      preview.append(check);

      if (isAlready) {
        const badge = document.createElement('span');
        badge.className = 'asset-picker-badge';
        badge.textContent = '已选';
        preview.append(badge);
      }

      const meta = document.createElement('div');
      meta.className = 'asset-picker-meta';
      const name = document.createElement('span');
      name.className = 'asset-picker-name';
      name.textContent = asset.name;
      const info = document.createElement('span');
      info.className = 'asset-picker-size';
      info.textContent = sizeLabel(asset.size);
      meta.append(name, info);

      card.append(preview, meta);

      if (!isAlready) {
        card.onclick = () => {
          const max = assetPickerOptions.maxCount || 9;
          if (max === 1) {
            assetPickerSelected.clear();
            assetPickerSelected.add(asset.id);
          } else {
            if (assetPickerSelected.has(asset.id)) {
              assetPickerSelected.delete(asset.id);
            } else {
              if (assetPickerSelected.size >= max) {
                const err = $('assetPickerError');
                if (err) {
                  err.textContent = `最多只能选择 ${max} 张图片`;
                  err.classList.remove('hidden');
                  setTimeout(() => err.classList.add('hidden'), 2500);
                }
                return;
              }
              assetPickerSelected.add(asset.id);
            }
          }
          grid.querySelectorAll('.asset-picker-card').forEach(c => {
            const id = c.dataset.assetId;
            const sel = assetPickerSelected.has(id);
            c.classList.toggle('selected', sel);
            c.setAttribute('aria-selected', String(sel));
            const chk = c.querySelector('.asset-picker-check');
            if (chk) chk.textContent = sel ? '✓' : '';
          });
          updatePickerSelectionStatus();
        };

        card.ondblclick = () => {
          assetPickerSelected.clear();
          assetPickerSelected.add(asset.id);
          confirmPicker();
        };
      }

      card.dataset.assetId = asset.id;
      grid.append(card);
    }
  }

  function confirmPicker() {
    const chosen = assets.filter(a => assetPickerSelected.has(a.id));
    const resolve = assetPickerResolve;
    assetPickerResolve = null;
    closePicker();
    if (resolve) resolve(chosen);
  }

  function closePicker() {
    clearPickerThumbnails();
    if ($('assetPickerDialog')?.open) {
      $('assetPickerDialog').close();
    }
    if (assetPickerResolve) {
      assetPickerResolve(null);
      assetPickerResolve = null;
    }
  }

  async function openAssetPicker(options = {}) {
    if (!assets.length && !loading) {
      await loadLibrary();
    }
    return new Promise((resolve) => {
      assetPickerResolve = resolve;
      assetPickerOptions = {
        kind: 'images',
        maxCount: 9,
        alreadySelectedIds: [],
        title: '从素材库选择图片',
        ...options
      };
      assetPickerSelected.clear();
      if ($('assetPickerTitle')) $('assetPickerTitle').textContent = assetPickerOptions.title || '从素材库选择素材';
      if ($('assetPickerSearch')) $('assetPickerSearch').value = '';
      if ($('assetPickerError')) {
        $('assetPickerError').classList.add('hidden');
        $('assetPickerError').textContent = '';
      }

      updatePickerScopeFilters();
      renderPickerGrid();
      updatePickerSelectionStatus();

      const dialog = $('assetPickerDialog');
      if (dialog && !dialog.open) {
        dialog.showModal();
      }
    });
  }

  function updateComputeAppInfo() {
    const appName = labels[activeApp][1];
    const appGpuVal = $(appGpuIds[activeApp])?.value;
    const nameEl = $('computeCurrentAppName');
    if (nameEl) nameEl.textContent = appName;
    const gpuSelect = $('gpu');
    if (gpuSelect && appGpuVal && [...gpuSelect.options].some(o => o.value === appGpuVal)) {
      gpuSelect.value = appGpuVal;
    }
    updateBalance();
  }

  function show(next) {
    if (next === 'characters') {
      const charModal = $('characterPage');
      if (charModal) {
        if (!charModal.open) charModal.showModal();
        return;
      }
    }
    if (!panels[next]) return;
    page = next;
    const isH3 = next === 'tool';
    const isQwenImage = next === 'qwen_image';
    const isGemma = next === 'gemma';
    if (isH3 || isQwenImage || isGemma) {
      if (isH3) h3Page = 'tool';
      activeApp = next;
      try { localStorage.setItem('lstudio-active-app', next); } catch (_) {}
    }
    try { localStorage.setItem('lstudio-active-page', next); } catch (_) {}
    try {
      const hashName = next === 'tool' ? 'h3' : next === 'connections' ? 'settings' : next;
      if (window.location.hash !== '#' + hashName) {
        history.replaceState(null, '', '#' + hashName);
      }
    } catch (_) {}

    delete document.documentElement.dataset.initialPage;

    for (const [name, id] of Object.entries(panels)) {
      if (name === 'characters') continue;
      const el = $(id);
      if (el) {
        el.hidden = name !== next;
        el.classList.toggle('hidden', name !== next);
        el.style.display = name === next ? '' : 'none';
      }
    }
    // Pause media hidden by page navigation.
    document.querySelectorAll('main[hidden] video, main[hidden] audio').forEach(media => media.pause());
    if ($('applicationHeading')) {
      $('applicationHeading').hidden = true;
      $('applicationHeading').classList.add('hidden');
      $('applicationHeading').style.display = 'none';
    }
    if ($('h3PageTabs')) {
      $('h3PageTabs').hidden = !isH3;
      $('h3PageTabs').classList.toggle('hidden', !isH3);
      $('h3PageTabs').style.display = isH3 ? '' : 'none';
    }
    if ($('qwenImagePageTabs')) {
      $('qwenImagePageTabs').hidden = !isQwenImage;
      $('qwenImagePageTabs').classList.toggle('hidden', !isQwenImage);
      $('qwenImagePageTabs').style.display = isQwenImage ? '' : 'none';
    }
    if ($('workspaceSection')) $('workspaceSection').textContent = labels[next][0];
    if ($('workspaceTitle')) $('workspaceTitle').textContent = labels[next][1];
    for (const [id, selected] of [['h3AppTab',isH3],['qwenImageAppTab',isQwenImage],['gemmaAppTab',isGemma],['libraryPageTab',next === 'library'],['computePageTab',next === 'compute'],['connectionPageTab',next === 'connections']]) {
      const el = $(id);
      if (el) {
        el.classList.toggle('active', selected);
        if (selected) el.setAttribute('aria-current','page');
        else el.removeAttribute('aria-current');
      }
    }
    for (const [id, name] of [['toolPageTab','tool'],['manageCharacters','characters'],['qwenImageToolTab','qwen_image']]) {
      const el = $(id);
      if (el) {
        el.setAttribute('aria-selected', String(next === name));
        el.tabIndex = next === name ? 0 : -1;
      }
    }
    if (isH3 || isQwenImage || isGemma || next === 'compute') updateComputeAppInfo();
    if (isGemma) window.GemmaChat?.refresh();
    if (next === 'library') loadLibrary();
  }

  function updateCompute(status) {
    const busy = !!status.active, connected = !!status.managed_gpu;
    const active = status.jobs?.find(job => job.id === status.active);
    const gpu = active?.gpu_info?.name || status.compute?.gpu;
    $('computeGpuName').textContent = gpu || `${$('gpu').value} · 按需启动`;
    const consumer = status.consumer || {};
    const labels = { connecting: '正在连接', busy: '正在处理', switching: '正在切换环境', cancelling: '正在取消', unknown: '远端状态待确认', blocked: '等待处理未完成任务' };
    $('computeSummary').textContent = labels[consumer.state] || (busy ? '正在运行' : connected ? '空闲 · 环境已保留' : status.authorized ? '账号已连接 · GPU 未启动' : '未连接账号');
    $('computeSummary').title = [consumer.job_id ? '任务 ' + consumer.job_id.slice(0, 8) : '', consumer.stage || '', consumer.error || ''].filter(Boolean).join(' · ');
    $('computePageTab').title = `${$('computeProviderName').textContent} · ${$('computeGpuName').textContent} · ${$('computeSummary').textContent} · ${$('balance').textContent} 单元`;
    $('computeIndicator').classList.toggle('connected', connected || status.authorized);
    $('computeIndicator').classList.toggle('busy', busy);
    const signature = JSON.stringify(status.jobs?.map(job => [job.id,job.status,job.local_file]));
    if (page === 'library' && signature !== librarySignature) {
      librarySignature = signature;
      loadLibrary();
    }
  }

  let cachedAccountData = null;
  function updateBalance(data = null, failure = null) {
    if (data) cachedAccountData = data;
    const currentData = data || cachedAccountData;
    const gpu = $('gpu')?.value;
    const estimate = currentData?.gpu_estimates?.[gpu] || (gpu === 'A100' ? currentData?.a100_estimate : null);
    const minutes = estimate?.remaining_minutes;
    $('computeRemaining').textContent = typeof minutes === 'number' && Number.isFinite(minutes) && minutes >= 0 ? `${Math.floor(minutes / 60)}时${Math.floor(minutes % 60)}分` : '—';
    $('computePageTab').classList.toggle('stale', !!failure || !!currentData?.stale);
  }

  function isGenerated(asset) { return asset.purpose === 'outputs'; }

  function appName(asset) {
    const scope = scopes.find(item => item.app_id === asset.app_id);
    return scope ? scope.app_name : asset.app_id;
  }

  function updateImportTarget() {
    const label = document.querySelector('label[for="libraryFiles"]');
    $('libraryFiles').disabled = importing;
    if (label) {
      label.setAttribute('aria-disabled', String(importing));
      label.classList.toggle('disabled', importing);
    }
  }

  function toggleLibraryTag(group, value) {
    const set = libraryTags[group];
    if (set.has(value)) set.delete(value); else set.add(value);
    if (group === 'kind') {
      try { localStorage.setItem('lstudio-library-kind', JSON.stringify([...set])); } catch (_) {}
    }
    renderLibrary();
  }

  function updateScopeFilters() {
    const apps = new Map(scopes.filter(scope => scope.purpose === 'outputs').map(scope => [scope.app_id, scope.app_name]));
    for (const asset of assets) if (isGenerated(asset) && !apps.has(asset.app_id)) apps.set(asset.app_id, asset.app_id);
    for (const id of [...libraryTags.app]) if (!apps.has(id)) libraryTags.app.delete(id);
    const groups = [
      ['kind', Object.entries(kinds)],
      ['source', [['imported', '导入'], ['generated', '生成']]],
      ['app', [...apps].map(([id, name]) => [id, name + '生成'])]
    ].filter(([, items]) => items.length);
    const box = $('libraryTags');
    box.replaceChildren();
    groups.forEach(([group, items], index) => {
      if (index) { const sep = document.createElement('span'); sep.className = 'library-tag-sep'; sep.setAttribute('aria-hidden', 'true'); box.append(sep); }
      for (const [value, label] of items) {
        const tag = document.createElement('button');
        tag.type = 'button'; tag.className = 'library-tag'; tag.textContent = label;
        tag.dataset.group = group; tag.dataset.value = value;
        tag.onclick = () => toggleLibraryTag(group, value);
        box.append(tag);
      }
    });
    syncLibraryTags();
  }

  function syncLibraryTags() {
    for (const tag of document.querySelectorAll('#libraryTags .library-tag')) {
      const on = libraryTags[tag.dataset.group].has(tag.dataset.value);
      tag.classList.toggle('active', on);
      tag.setAttribute('aria-pressed', String(on));
    }
    const any = Object.values(libraryTags).some(set => set.size);
    $('libraryTagClear')?.classList.toggle('hidden', !any);
  }

  function matchesTags(asset) {
    const {kind, source, app} = libraryTags;
    if (kind.size && !kind.has(asset.kind)) return false;
    if (source.size && !source.has(isGenerated(asset) ? 'generated' : 'imported')) return false;
    if (app.size && !(isGenerated(asset) && app.has(asset.app_id))) return false;
    return true;
  }

  function libraryError(message) {
    $('libraryError').textContent = message;
    $('libraryError').classList.toggle('hidden', !message);
  }

  function sizeLabel(size) {
    return size >= 1024 ** 3 ? `${(size / 1024 ** 3).toFixed(1)} GB` : size >= 1024 ** 2 ? `${(size / 1024 ** 2).toFixed(1)} MB` : `${Math.ceil(size / 1024)} KB`;
  }

  async function blobURL(asset) {
    const response = await fetch(asset.url, {headers:{'X-LStudio-Key':KEY}});
    if (!response.ok) throw Error('素材读取失败，请刷新或检查本机文件。');
    return URL.createObjectURL(await response.blob());
  }

  function clearThumbnails() {
    for (const pending of thumbnails.values()) pending.then(url => URL.revokeObjectURL(url), () => {});
    thumbnails.clear();
  }

  function thumbnail(asset, img) {
    if (!thumbnails.has(asset.id)) thumbnails.set(asset.id, blobURL(asset));
    thumbnails.get(asset.id).then(url => { if (img.isConnected) img.src = url; }, () => { img.alt = '图片读取失败'; });
  }


  function setLibraryManageMode(enabled) {
    isManageMode = Boolean(enabled);
    if (!isManageMode) {
      librarySelected.clear();
    }
    const btn = $('libraryManageToggle');
    if (btn) {
      btn.textContent = isManageMode ? '退出管理' : '批量管理';
      btn.classList.toggle('active', isManageMode);
    }
    const grid = $('libraryGrid');
    if (grid) {
      grid.classList.toggle('manage-mode', isManageMode);
    }
    updateLibraryBatchBar();
    renderLibrary();
  }

  function toggleCardSelection(assetId, card) {
    if (librarySelected.has(assetId)) {
      librarySelected.delete(assetId);
      card.classList.remove('selected');
      const chk = card.querySelector('.library-asset-check');
      if (chk) chk.textContent = '';
    } else {
      librarySelected.add(assetId);
      card.classList.add('selected');
      const chk = card.querySelector('.library-asset-check');
      if (chk) chk.textContent = '✓';
    }
    updateLibraryBatchBar();
  }

  function updateLibraryBatchBar() {
    const bar = $('libraryBatchBar');
    if (bar) bar.classList.toggle('hidden', !isManageMode);
    const count = librarySelected.size;
    const countEl = $('libraryBatchCount');
    if (countEl) countEl.textContent = `已选 ${count} 项`;
    const delBtn = $('libraryBatchDelete');
    if (delBtn) delBtn.disabled = count === 0;
    const selectAll = $('librarySelectAll');
    if (selectAll) {
      const visible = document.querySelectorAll('#libraryGrid .library-asset');
      if (!visible.length) {
        selectAll.checked = false;
        selectAll.indeterminate = false;
      } else {
        const allChecked = [...visible].every(card => librarySelected.has(card.dataset.assetId));
        const someChecked = [...visible].some(card => librarySelected.has(card.dataset.assetId));
        selectAll.checked = allChecked;
        selectAll.indeterminate = someChecked && !allChecked;
      }
    }
  }

  function expandWithDuplicates(ids) {
    const all = new Set();
    for (const id of ids) {
      all.add(id);
      const asset = assets.find(item => item.id === id);
      for (const twin of asset?.duplicates || []) all.add(twin);
    }
    return Array.from(all);
  }

  async function deleteSingleAsset(assetId) {
    libraryError('');
    try {
      const res = await api('assets/delete', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ ids: expandWithDuplicates([assetId]) })
      });
      for (const id of res.deleted || []) librarySelected.delete(id);
      await loadLibrary();
      if (res.errors?.length) libraryError(res.errors.map(item => item.error).join('；'));
    } catch (err) {
      libraryError('删除失败：' + err.message);
    }
  }

  async function deleteBatchAssets() {
    if (!librarySelected.size) return;
    if (!confirm(`确定删除选中的 ${librarySelected.size} 项素材及其重复副本吗？本机文件将被删除。`)) return;
    libraryError('');
    $('libraryFeedback').textContent = '正在删除素材…';
    try {
      const res = await api('assets/delete', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ ids: expandWithDuplicates(librarySelected) })
      });
      for (const id of res.deleted || []) librarySelected.delete(id);
      if (!res.errors?.length) setLibraryManageMode(false);
      await loadLibrary();
      if (res.errors?.length) {
        libraryError(`已删除 ${res.count || 0} 项，${res.errors.length} 项未删除：` + res.errors.map(item => item.error).join('；'));
        return;
      }
      $('libraryFeedback').textContent = `成功删除 ${res.count || 0} 项素材。`;
      setTimeout(() => {
        if ($('libraryFeedback')?.textContent.startsWith('成功删除')) {
          renderLibrary();
        }
      }, 3000);
    } catch (err) {
      libraryError('批量删除失败：' + err.message);
    }
  }

  function renderLibrary() {
    $('libraryGrid').querySelectorAll('video, audio').forEach(media => media.pause());
    $('libraryGrid').replaceChildren();
    $('libraryGrid').classList.toggle('manage-mode', isManageMode);
    const search = $('librarySearch').value.trim().toLocaleLowerCase();
    const filtered = assets.filter(asset => matchesTags(asset) && asset.name.toLocaleLowerCase().includes(search));
    syncLibraryTags();
    $('libraryFeedback').textContent = importing ? '正在导入素材…' : `${filtered.length} 项素材`;
    updateImportTarget();
    if (!filtered.length) {
      const empty = document.createElement('div'); empty.className = 'library-empty';
      const icon = document.createElement('div'); icon.className = 'library-empty-icon';
      icon.innerHTML = '<svg width="38" height="38" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><path stroke-linecap="round" stroke-linejoin="round" d="M3 7v10a2 2 0 002 2h14a2 2 0 002-2V9a2 2 0 00-2-2h-6l-2-2H5a2 2 0 00-2 2z"/></svg>';
      const title = document.createElement('h3'); title.textContent = search ? '没有找到匹配的素材' : '素材库暂无内容';
      const hint = document.createElement('p'); hint.textContent = search ? '请尝试更换关键词，或切换上方应用与用途筛选。' : '导入图片、音频或视频；H3 及 Qwen-Image 生成的素材也会自动汇集在此。';
      empty.append(icon, title, hint); $('libraryGrid').append(empty);
    }
    for (const asset of filtered) {
      const isSelected = librarySelected.has(asset.id);
      const card = document.createElement('article'); card.className = 'library-asset' + (isSelected ? ' selected' : '');
      card.dataset.assetId = asset.id;
      const preview = document.createElement('button'); preview.type = 'button'; preview.className = 'library-preview'; preview.setAttribute('aria-label', (isManageMode ? '选择 ' : '预览 ') + asset.name);

      if (isManageMode) {
        const check = document.createElement('span');
        check.className = 'library-asset-check';
        check.textContent = isSelected ? '✓' : '';
        preview.append(check);

        card.onclick = (e) => {
          e.stopPropagation();
          e.preventDefault();
          toggleCardSelection(asset.id, card);
        };
        preview.onclick = (e) => {
          e.stopPropagation();
          e.preventDefault();
          toggleCardSelection(asset.id, card);
        };
      } else {
        preview.onclick = () => openPreview(asset);
      }

      if (asset.kind === 'images') {
        const img = document.createElement('img'); img.alt = asset.name; img.loading = 'lazy'; preview.append(img);
        queueMicrotask(() => thumbnail(asset,img));
      } else {
        const icon = document.createElement('span'); icon.className = 'library-media-icon'; icon.textContent = asset.kind === 'videos' ? '▷' : '♫'; preview.append(icon);
        if (asset.kind === 'videos' && asset.local) {
          const video = document.createElement('video'); video.src = '/video/' + asset.id + '#t=0.1'; video.preload = 'metadata'; video.muted = true; video.playsInline = true; preview.append(video);
        }
      }
      const badge = document.createElement('span'); badge.className = 'library-kind'; badge.textContent = kinds[asset.kind]; preview.append(badge);

      const copy = document.createElement('div'); copy.className = 'library-asset-copy';
      const title = document.createElement('h3'); title.textContent = asset.name; title.title = asset.name;
      const location = document.createElement('p'); location.className = 'library-scope'; location.textContent = isGenerated(asset) ? '生成 · ' + appName(asset) : '导入';
      const meta = document.createElement('p'); meta.textContent = `${sizeLabel(asset.size)} · ${asset.local ? '本机素材' : asset.recoverable ? 'Drive 副本' : '本机文件缺失'}`;
      const actions = document.createElement('div'); actions.className = 'library-asset-actions';
      const view = document.createElement('button'); view.type = 'button'; view.textContent = '预览'; view.onclick = (e) => { e.stopPropagation(); openPreview(asset); }; actions.append(view);
      if (asset.kind === 'images') {
        if (activeApp === 'qwen_image') {
          const useQi = document.createElement('button'); useQi.type = 'button'; useQi.textContent = '用于 Qwen-Image'; useQi.onclick = (e) => { e.stopPropagation(); useImageForQi(asset); }; actions.append(useQi);
          const use = document.createElement('button'); use.type = 'button'; use.textContent = '用于 H3'; use.onclick = (e) => { e.stopPropagation(); useImage(asset); }; actions.append(use);
        } else {
          const use = document.createElement('button'); use.type = 'button'; use.textContent = '用于 H3'; use.onclick = (e) => { e.stopPropagation(); useImage(asset); }; actions.append(use);
          const useQi = document.createElement('button'); useQi.type = 'button'; useQi.textContent = '用于 Qwen-Image'; useQi.onclick = (e) => { e.stopPropagation(); useImageForQi(asset); }; actions.append(useQi);
        }
      }
      const del = document.createElement('button');
      del.type = 'button';
      del.className = 'library-action-delete';
      del.textContent = '删除';
      del.onclick = (e) => {
        e.stopPropagation();
        if (confirm(`确定从素材库删除“${asset.name}”及其重复副本吗？本机文件将被删除。`)) {
          deleteSingleAsset(asset.id);
        }
      };
      actions.append(del);
      copy.append(title,location,meta,actions); card.append(preview,copy); $('libraryGrid').append(card);
    }
    updateLibraryBatchBar();
  }

  async function loadLibrary() {
    const request = ++libraryRequest;
    loading = true; $('libraryRefresh').disabled = true; libraryError('');
    try {
      const data = await api('assets');
      if (request !== libraryRequest) return;
      clearThumbnails(); assets = data.assets; scopes = data.scopes; updateScopeFilters();
      for (const id of librarySelected) if (!assets.some(asset => asset.id === id)) librarySelected.delete(id);
      $('libraryPath').textContent = data.root;
      $('settingsLibraryPath').textContent = data.root;
      renderLibrary();
    } catch (error) { if (request === libraryRequest) libraryError(error.message); }
    finally { if (request === libraryRequest) { loading = false; $('libraryRefresh').disabled = false; } }
  }

  async function useImageForQi(asset) {
    libraryError('');
    try {
      if (window.QwenImage?.useImage) {
        await window.QwenImage.useImage(asset);
        if ($('assetViewer').open) $('assetViewer').close();
      } else {
        show('qwen_image');
      }
    } catch (error) {
      libraryError(error.message);
    }
  }

  async function useImage(asset) {
    libraryError('');
    if (references.some(ref => ref.image_id === asset.id)) { show('tool'); setMode('image'); return; }
    if (references.length >= 9) { libraryError('H3 最多使用 9 张图片，请先移除一张参考图。'); return; }
    try {
      const url = await blobURL(asset);
      // The user may have added another reference while this file was loading.
      if (references.length >= 9 || references.some(ref => ref.image_id === asset.id)) { URL.revokeObjectURL(url); return; }
      const slot = Array.from({length:9}, (_, i) => i + 1).find(index => !references.some(ref => ref.slot === index));
      references.push({slot, image_id:asset.id, role:'other', description:'', frame:'reference', url});
      show('tool'); setMode('image'); renderReferences(); saveDraft();
      if ($('assetViewer').open) $('assetViewer').close();
      $('prompt').focus();
    } catch (error) { libraryError(error.message); }
  }

  function releasePreview() {
    previewRequest++;
    $('assetViewerContent').querySelectorAll('video, audio').forEach(media => { media.pause(); media.removeAttribute('src'); media.load(); });
    $('assetViewerContent').replaceChildren();
    if (previewURL) URL.revokeObjectURL(previewURL);
    previewURL = null;
  }

  async function openPreview(asset) {
    releasePreview();
    const request = previewRequest;
    $('assetViewerTitle').textContent = asset.name;
    $('assetViewerActions').replaceChildren();
    $('assetViewerContent').textContent = '正在读取素材…';
    if (!$('assetViewer').open) $('assetViewer').showModal();
    try {
      if (!asset.local && !asset.recoverable) throw Error('本机素材已丢失，且没有可恢复的副本。');
      let url;
      if (asset.kind === 'videos') url = '/video/' + asset.id;
      else url = await blobURL(asset);
      if (request !== previewRequest) { if (asset.kind !== 'videos') URL.revokeObjectURL(url); return; }
      if (asset.kind !== 'videos') previewURL = url;
      const media = document.createElement(asset.kind === 'images' ? 'img' : asset.kind === 'videos' ? 'video' : 'audio');
      if (asset.kind === 'images') media.alt = asset.name;
      else { media.controls = true; media.preload = 'metadata'; }
      media.src = url; $('assetViewerContent').replaceChildren(media);
      const download = document.createElement('button'); download.className = 'secondary'; download.type = 'button'; download.textContent = '下载';
      download.onclick = () => downloadAsset(asset); $('assetViewerActions').append(download);
      const del = document.createElement('button'); del.className = 'secondary library-viewer-delete'; del.type = 'button'; del.textContent = '删除素材';
      del.onclick = async () => {
        if (confirm(`确定从素材库删除“${asset.name}”及其重复副本吗？本机文件将被删除。`)) {
          $('assetViewer').close();
          await deleteSingleAsset(asset.id);
        }
      };
      $('assetViewerActions').append(del);
      if (asset.kind === 'images') {
        if (activeApp === 'qwen_image') {
          const useQi = document.createElement('button'); useQi.className = 'secondary'; useQi.type = 'button'; useQi.textContent = '用于 Qwen-Image 参考图'; useQi.onclick = () => useImageForQi(asset); $('assetViewerActions').append(useQi);
          const use = document.createElement('button'); use.className = 'secondary'; use.type = 'button'; use.textContent = '用于 H3 参考图'; use.onclick = () => useImage(asset); $('assetViewerActions').append(use);
        } else {
          const use = document.createElement('button'); use.className = 'secondary'; use.type = 'button'; use.textContent = '用于 H3 参考图'; use.onclick = () => useImage(asset); $('assetViewerActions').append(use);
          const useQi = document.createElement('button'); useQi.className = 'secondary'; useQi.type = 'button'; useQi.textContent = '用于 Qwen-Image 参考图'; useQi.onclick = () => useImageForQi(asset); $('assetViewerActions').append(useQi);
        }
      }
    } catch (error) { if (request === previewRequest) $('assetViewerContent').textContent = error.message; }
  }

  async function downloadAsset(asset) {
    try {
      const url = asset.kind === 'videos' ? '/video/' + asset.id + '?download=true' : await blobURL(asset);
      const link = document.createElement('a'); link.href = url; link.download = asset.name; link.click();
      if (asset.kind !== 'videos') setTimeout(() => URL.revokeObjectURL(url), 1000);
    } catch (error) { $('assetViewerContent').textContent = error.message; }
  }

  async function importFiles(files) {
    if (importing) return;
    importing = true; $('libraryFiles').disabled = true; libraryError('');
    let failure = '';
    for (const file of files) {
      try {
        const form = new FormData(); form.append('file',file); form.append('app_id','shared'); form.append('purpose','imports');
        $('libraryFeedback').textContent = '正在导入 ' + file.name;
        await api('assets', {method:'POST', body:form});
      } catch (error) { failure += `${file.name}：${error.message}；`; }
    }
    importing = false; $('libraryFiles').disabled = false; $('libraryFiles').value = '';
    await loadLibrary(); if (failure) libraryError(failure);
  }

  async function initialize() {
    try {
      const data = await api('workspace');
      $('settingsLibraryPath').textContent = data.library.root;
      scopes = data.library.scopes; updateScopeFilters(); updateBalance(lastAccountData);
      $('computeProviderName').textContent = data.compute.provider_name;
      $('providerList').replaceChildren();
      for (const provider of data.providers) {
        const card = document.createElement('div'); card.className = 'provider-card' + (provider.id === data.compute.provider_id ? ' selected' : '') + (!provider.available ? ' unavailable' : '');
        const heading = document.createElement('div'); heading.className = 'provider-card-heading';
        const title = document.createElement('strong'); title.textContent = provider.name;
        const state = document.createElement('span'); state.textContent = provider.available ? '当前使用' : '尚未接入'; heading.append(title,state);
        const hint = document.createElement('p'); hint.textContent = provider.description; card.append(heading,hint); $('providerList').append(card);
      }
    } catch (error) { $('computeSummary').textContent = '连接失败 · 点击检查'; }
  }

  const KNOWN_GPU_LABELS = {
    G4: 'G4 · Blackwell · 96GB',
    A100: 'A100 · 80GB',
    H100: 'H100 · Hopper · 80GB',
    L4: 'L4 · 24GB',
    T4: 'T4 · 16GB'
  };

  let lastDiscoveredRuntimes = [];

  function buildGpuOptions(allRuntimes) {
    if (Array.isArray(allRuntimes)) lastDiscoveredRuntimes = allRuntimes;
    const onlineCounts = {};
    for (const r of (lastDiscoveredRuntimes || [])) {
      if (r.gpu) onlineCounts[r.gpu] = (onlineCounts[r.gpu] || 0) + 1;
    }
    const gpus = ['G4', 'A100', 'H100', 'L4', 'T4'];
    for (const g of Object.keys(onlineCounts)) {
      if (!gpus.includes(g)) gpus.push(g);
    }
    return gpus.map(g => {
      const base = KNOWN_GPU_LABELS[g] || `${g} · 远程检测`;
      const count = onlineCounts[g] || 0;
      const text = count > 0 ? `${base} (🟢 ${count}台在线)` : base;
      return new Option(text, g);
    });
  }

  const appGpuIds = {tool: 'h3Gpu', qwen_image: 'qiGpu', gemma: 'gemmaGpu'};

  function syncGpuSelects(allRuntimes) {
    const targets = [
      { id: 'h3Gpu', cardId: 'h3GpuCard', menuId: 'h3GpuMenu', defaultGpu: 'G4', storageKey: 'lstudio-h3-gpu' },
      { id: 'qiGpu', cardId: 'qiGpuCard', menuId: 'qiGpuMenu', defaultGpu: 'T4', storageKey: 'lstudio-qi-gpu' },
      { id: 'gemmaGpu', cardId: 'gemmaGpuCard', menuId: 'gemmaGpuMenu', defaultGpu: 'L4',
        storageKey: 'lstudio-gemma-gpu', gpuOrder: ['L4', 'A100', 'H100', 'G4', 'T4'] },
      { id: 'gpu', defaultGpu: 'G4', storageKey: 'lstudio-compute-gpu' }
    ];

    const GPU_STATUS_HINTS = {
      qiGpu: {
        L4: '推荐 · 编辑首选 (24GB)',
        A100: '极速推理 · 高显存 (80GB)',
        H100: '旗舰算力 · 极速 (80GB)',
        T4: '显存友好 · 适于文生图',
        G4: '超大显存 (96GB)'
      },
      h3Gpu: {
        G4: 'Wan 2.1 推荐配置',
        A100: '高速视频配置 (80GB)',
        H100: '旗舰算力 (80GB)',
        L4: '显存受限 · 适合短视频',
        T4: '显存不足 · 不推荐视频'
      },
      gemmaGpu: {
        L4: '推荐 · 日常聊天 (24GB)',
        A100: '高显存 · 余量充足 (80GB)',
        H100: '高显存 · 性能优先 (80GB)',
        G4: '高显存 · 性能优先 (96GB)',
        T4: '备选 · CPU 分担 (16GB)'
      }
    };

    function getGpuHint(targetId, val) {
      if (targetId === 'gemmaGpu') {
        const variant = document.querySelector('input[name="gemmaVariant"]:checked')?.value || 'native';
        if (variant === 'gpt_oss_120b') {
          return {
            A100: '推荐 · 80GB 显存 (120B 必备)',
            H100: '旗舰算力 · 80GB 显存',
            G4: '超大显存 (96GB) · 可运行',
            L4: '❌ 显存不足 (需 80GB，不可运行)',
            T4: '❌ 显存不足 (需 80GB，不可运行)'
          }[val] || '80G 显卡配置';
        }
        return GPU_STATUS_HINTS.gemmaGpu[val] || '按需启动';
      }
      return GPU_STATUS_HINTS[targetId]?.[val] || '按需启动';
    }

    function updateGpuCardDisplay(target) {
      const select = $(target.id), display = $(target.id + 'Display'), status = $(target.id + 'Status');
      if (!select || !display) return;
      const opt = select.selectedOptions?.[0];
      const text = (opt ? opt.textContent : select.value) || '';
      const val = select.value;
      const is120b = target.id === 'gemmaGpu' && (document.querySelector('input[name="gemmaVariant"]:checked')?.value === 'gpt_oss_120b');
      const defGpu = is120b ? 'A100' : target.defaultGpu;
      const isInsufficient = is120b && (val === 'L4' || val === 'T4');
      if (text.includes('(🟢')) {
        const parts = text.split('(🟢');
        display.textContent = parts[0].trim();
        if (status) {
          const recommended = target.id === 'gemmaGpu' && val === defGpu;
          status.textContent = (recommended ? '推荐 · ' : '🟢 ') + parts[1].replace(')', '').trim();
          status.classList.remove('status-idle', 'status-error');
          status.classList.add('status-online');
        }
      } else {
        display.textContent = text.trim();
        if (status) {
          status.textContent = getGpuHint(target.id, val) || '按需启动';
          status.classList.remove('status-online');
          status.classList.toggle('status-error', isInsufficient);
          status.classList.toggle('status-idle', !isInsufficient);
        }
      }
    }

    function closeGpuMenu(target) {
      if (!target || !target.menuId) return;
      const menu = $(target.menuId);
      const card = $(target.cardId);
      if (menu) menu.classList.add('hidden');
      if (card) card.setAttribute('aria-expanded', 'false');
    }

    function closeAllGpuMenus() {
      for (const t of targets) {
        if (t.menuId) closeGpuMenu(t);
      }
    }

    function renderGpuMenu(target) {
      if (!target.menuId || !target.cardId) return;
      const menu = $(target.menuId);
      const card = $(target.cardId);
      const select = $(target.id);
      if (!menu || !card || !select) return;

      const currentVal = select.value;
      const hints = GPU_STATUS_HINTS[target.id] || {};
      const focusedValue = menu.contains(document.activeElement) ? document.activeElement.dataset.val : null;
      menu.replaceChildren();

      for (const opt of select.options) {
        const val = opt.value;
        const rawText = opt.textContent || val;
        let baseName = rawText;
        let onlineCount = 0;
        if (rawText.includes('(🟢')) {
          const parts = rawText.split('(🟢');
          baseName = parts[0].trim();
          onlineCount = parseInt(parts[1].replace(/\D/g, ''), 10) || 1;
        }
        const hint = getGpuHint(target.id, val);
        const isSelected = val === currentVal;

        const item = document.createElement('button');
        item.type = 'button';
        item.tabIndex = -1;
        item.className = 'gpu-menu-item' + (isSelected ? ' selected' : '');
        item.setAttribute('role', 'option');
        item.setAttribute('aria-selected', isSelected ? 'true' : 'false');
        item.dataset.val = val;

        const info = document.createElement('span');
        info.className = 'gpu-menu-item-info';

        const header = document.createElement('span');
        header.className = 'gpu-menu-item-header';

        const nameSpan = document.createElement('strong');
        nameSpan.className = 'gpu-menu-item-name';
        nameSpan.textContent = baseName;
        header.appendChild(nameSpan);

        if (onlineCount > 0) {
          const onlinePill = document.createElement('span');
          onlinePill.className = 'gpu-menu-online-pill';
          onlinePill.textContent = `🟢 ${onlineCount}台在线`;
          header.appendChild(onlinePill);
        }
        info.appendChild(header);

        if (hint) {
          const descSpan = document.createElement('span');
          descSpan.className = 'gpu-menu-item-desc';
          descSpan.textContent = hint;
          info.appendChild(descSpan);
        }

        item.appendChild(info);

        if (isSelected) {
          const check = document.createElement('span');
          check.className = 'gpu-menu-check';
          check.setAttribute('aria-hidden', 'true');
          check.textContent = '✓';
          item.appendChild(check);
        }

        item.addEventListener('click', (e) => {
          e.stopPropagation();
          select.value = val;
          select.dispatchEvent(new Event('change'));
          closeGpuMenu(target);
          card.focus();
        });

        menu.appendChild(item);
        if (val === focusedValue) item.focus();
      }
    }

    function toggleGpuMenu(target, keyboard = false) {
      if (!target.menuId || !target.cardId) return;
      const menu = $(target.menuId);
      const card = $(target.cardId);
      if (!menu || !card) return;
      const isOpen = !menu.classList.contains('hidden');
      closeAllGpuMenus();
      if (!isOpen) {
        renderGpuMenu(target);
        menu.classList.remove('hidden');
        card.setAttribute('aria-expanded', 'true');
        if (keyboard) menu.querySelector('[aria-selected="true"]')?.focus();
      }
    }

    for (const target of targets) {
      const select = $(target.id);
      if (!select) continue;
      let saved = null;
      try { saved = localStorage.getItem(target.storageKey); } catch (_) {}
      const chosen = (target.id === 'gemmaGpu' && !select.dataset.syncBound ? saved : select.value) || saved || target.defaultGpu;
      let options = buildGpuOptions(allRuntimes);
      if (target.gpuOrder) {
        const byValue = new Map(options.map(option => [option.value, option]));
        options = target.gpuOrder.map(value => byValue.get(value)).filter(Boolean);
      }
      select.replaceChildren(...options);
      if ([...select.options].some(o => o.value === chosen)) {
        select.value = chosen;
      } else {
        select.value = target.defaultGpu;
      }
      if (target.cardId && target.menuId) {
        updateGpuCardDisplay(target);
        const card = $(target.cardId);
        const menu = $(target.menuId);
        if (card && !card.dataset.gpuCardBound) {
          card.dataset.gpuCardBound = 'true';
          card.setAttribute('tabindex', '0');
          card.setAttribute('role', 'button');
          card.setAttribute('aria-haspopup', 'listbox');
          card.setAttribute('aria-expanded', 'false');
          card.addEventListener('click', (e) => {
            if (e.target.closest('.gpu-dropdown-menu')) return;
            toggleGpuMenu(target);
          });
          card.addEventListener('keydown', (e) => {
            if (e.key === 'Enter' || e.key === ' ' || e.key === 'ArrowDown') {
              e.preventDefault();
              if (e.key === 'ArrowDown' && card.getAttribute('aria-expanded') === 'true') {
                menu?.querySelector('[aria-selected="true"]')?.focus();
              } else {
                toggleGpuMenu(target, true);
              }
            } else if (e.key === 'Escape') {
              closeGpuMenu(target);
            }
          });
          menu?.addEventListener('keydown', e => {
            if (e.key === 'Escape') {
              closeGpuMenu(target);
              card.focus();
              return;
            }
            if (e.key === 'Enter' || e.key === ' ') {
              e.preventDefault();
              e.target.closest('.gpu-menu-item')?.click();
              return;
            }
            if (!['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(e.key)) return;
            e.preventDefault();
            const items = Array.from(menu.children);
            const index = items.indexOf(document.activeElement);
            const step = e.key === 'ArrowDown' ? 1 : -1;
            const next = e.key === 'Home' ? 0 : e.key === 'End' ? items.length - 1 :
              (index + step + items.length) % items.length;
            items[next]?.focus();
          });
        }
        renderGpuMenu(target);
      }

      if (!select.dataset.syncBound) {
        select.dataset.syncBound = 'true';
        select.addEventListener('change', () => {
          try { localStorage.setItem(target.storageKey, select.value); } catch (_) {}
          if (target.cardId && target.menuId) {
            updateGpuCardDisplay(target);
            renderGpuMenu(target);
          }
          if (target.id === appGpuIds[activeApp]) {
            const computeGpu = $('gpu');
            if (computeGpu && [...computeGpu.options].some(o => o.value === select.value)) {
              computeGpu.value = select.value;
              computeGpu.dispatchEvent(new Event('change'));
            }
          }
          if (target.id === 'gpu') {
            const tConfig = targets.find(t => t.id === appGpuIds[activeApp]);
            const targetSelect = $(tConfig.id);
            if (targetSelect && [...targetSelect.options].some(o => o.value === select.value)) {
              try { localStorage.setItem(tConfig.storageKey, select.value); } catch (_) {}
              targetSelect.value = select.value;
              if (tConfig.menuId) {
                updateGpuCardDisplay(tConfig);
                renderGpuMenu(tConfig);
              }
            }
            updateBalance();
          }
        });
      }
    }

    if (typeof document !== 'undefined' && document.body && !document.body.dataset.gpuDropdownBound) {
      document.body.dataset.gpuDropdownBound = 'true';
      document.addEventListener('click', (e) => {
        if (!e.target.closest('.gpu-card-wrap') && !e.target.closest('.h3-gpu-card-wrap') && !e.target.closest('.qi-gpu-card-wrap')) {
          closeAllGpuMenus();
        }
      });
      document.addEventListener('keydown', (e) => {
        if (e.key === 'Escape') closeAllGpuMenus();
      });
    }
  }


  let queuePlacementResolve = null;

  function closeQueuePlacementModal(result = false) {
    const dialog = $('queuePlacementModal');
    if (dialog && dialog.open) {
      dialog.close();
    }
    if (queuePlacementResolve) {
      const resolve = queuePlacementResolve;
      queuePlacementResolve = null;
      resolve(result);
    }
  }

  let queueChoicePending = false;
  async function chooseQueuePlacement({ targetAppId, targetGpu }) {
    if (queueChoicePending) return null;
    queueChoicePending = true;
    try {
      const status = await api('status');
      let consumer = status.consumer || {};
      const waiting = status.queue || [];
      if (!status.active && !waiting.length && status.managed_gpu) {
        // A retained environment or a stale observer snapshot is not a waiting task.
        try { consumer = await api('consumer'); }
        catch (_) { consumer = {state: 'unknown'}; }
      }
      const needsQueue = !!status.active || waiting.length > 0 ||
        ['busy', 'connecting', 'switching', 'cancelling'].includes(consumer.state) && status.managed_gpu;
      if (!needsQueue) return 'back';
      const dialog = $('queuePlacementModal');
      if (!dialog) return 'back';
      $('queuePlacementTitle').textContent = '加入任务队列';
      const current = status.jobs?.find(j => j.id === status.active || j.id === consumer.job_id);
      const currentApp = current?.app_id || consumer.app_id || status.occupying_app_id || waiting[0]?.app_id || targetAppId;
      const name = ({h3: 'H3 视频', qwen_image_2_1: 'Qwen-Image-2.1', gemma_chat: '大模型对话'})[currentApp] || currentApp || '当前应用';
      const hasWaiting = waiting.length > 0;
      const processing = status.active || ['busy', 'connecting', 'switching', 'cancelling'].includes(consumer.state);
      $('queuePlacementSummary').textContent = `${name}${processing ? '正在处理' : status.managed_gpu ? '已保留环境' : '等待执行'} · 当前排队 ${waiting.length} 项`;
      const switching = status.compute?.gpu && (status.compute.gpu !== targetGpu || status.occupying_app_id !== targetAppId);
      $('queuePlacementHelpText').textContent = [
        '排队数仅包含等待中的任务，不包含正在执行的任务。',
        '排队后，优先执行与当前环境应用、显卡一致的任务，其余按入队顺序执行。',
        hasWaiting ? '插到队首会将本任务放到等待队列最前方，当前任务继续执行，其他等待任务的相对顺序保留。'
          : '当前没有其他等待任务，无需插队。当前任务完成后，将自动开始本任务。',
        switching ? '轮到本任务时，将自动切换应用或显卡环境。' : '相同应用与显卡会继续复用已准备好的环境。'
      ].join('\n\n');
      $('queuePlacementHelp').open = false;
      $('queuePlacementBack').classList.remove('hidden');
      $('queuePlacementBack').textContent = hasWaiting ? '排队（推荐）' : '排队';
      $('queuePlacementFront').classList.toggle('hidden', !hasWaiting);
      $('queuePlacementFront').disabled = !hasWaiting;
      $('queuePlacementFront').textContent = '插到队首';
      const choice = await new Promise(resolve => {
        queuePlacementResolve = resolve;
        dialog.showModal();
        $('queuePlacementBack').focus();
      });
      return choice;
    } finally {
      queueChoicePending = false;
    }
  }

  window.Workspace = {
    show,
    updateCompute,
    updateBalance,
    buildGpuOptions,
    syncGpuSelects,
    openAssetPicker,
    chooseQueuePlacement,
    get page(){ return page; },
    get activeApp(){ return activeApp; }
  };
  $('h3AppTab').onclick = () => show('tool');
  $('qwenImageAppTab').onclick = () => show('qwen_image');
  if ($('gemmaAppTab')) $('gemmaAppTab').onclick = () => show('gemma');
  if ($('qwenImageToolTab')) $('qwenImageToolTab').onclick = () => show('qwen_image');
  if ($('toolPageTab')) $('toolPageTab').onclick = () => show('tool');
  $('libraryPageTab').onclick = () => show('library');
  $('computePageTab').onclick = () => show('compute');
  $('connectionPageTab').onclick = () => show('connections');
  $('settingsOpenLibrary').onclick = () => show('library');


  $('cancelQueuePlacement')?.addEventListener('click', () => closeQueuePlacementModal(false));
  $('queuePlacementBack')?.addEventListener('click', () => closeQueuePlacementModal('back'));
  $('queuePlacementModal')?.addEventListener('cancel', () => closeQueuePlacementModal(false));
  $('queuePlacementModal')?.addEventListener('click', e => {
    if (e.target === $('queuePlacementModal')) closeQueuePlacementModal(false);
  });
  $('queuePlacementFront')?.addEventListener('click', () => closeQueuePlacementModal('front'));

  $('confirmAssetPicker')?.addEventListener('click', confirmPicker);
  $('cancelAssetPicker')?.addEventListener('click', closePicker);
  $('closeAssetPicker')?.addEventListener('click', closePicker);
  $('assetPickerDialog')?.addEventListener('cancel', closePicker);
  $('assetPickerDialog')?.addEventListener('click', e => {
    if (e.target === $('assetPickerDialog')) closePicker();
  });
  $('assetPickerSearch')?.addEventListener('input', renderPickerGrid);
  $('assetPickerAppFilter')?.addEventListener('change', () => {
    updatePickerScopeFilters();
    renderPickerGrid();
  });
  $('assetPickerPurposeFilter')?.addEventListener('change', renderPickerGrid);
  $('libraryRefresh').onclick = loadLibrary;
  $('libraryManageToggle')?.addEventListener('click', () => {
    setLibraryManageMode(!isManageMode);
  });
  $('librarySelectAll')?.addEventListener('change', () => {
    const checked = $('librarySelectAll').checked;
    document.querySelectorAll('#libraryGrid .library-asset').forEach(card => {
      const id = card.dataset.assetId;
      if (!id) return;
      if (checked) {
        librarySelected.add(id);
        card.classList.add('selected');
        const chk = card.querySelector('.library-asset-check');
        if (chk) chk.textContent = '✓';
      } else {
        librarySelected.delete(id);
        card.classList.remove('selected');
        const chk = card.querySelector('.library-asset-check');
        if (chk) chk.textContent = '';
      }
    });
    updateLibraryBatchBar();
  });
  $('libraryBatchDelete')?.addEventListener('click', deleteBatchAssets);
  $('libraryBatchCancel')?.addEventListener('click', () => {
    setLibraryManageMode(false);
  });
  $('librarySearch').oninput = renderLibrary;
  $('libraryFiles').onchange = () => importFiles([...$('libraryFiles').files]);
  $('libraryTagClear')?.addEventListener('click', () => {
    for (const set of Object.values(libraryTags)) set.clear();
    try { localStorage.setItem('lstudio-library-kind', '[]'); } catch (_) {}
    renderLibrary();
  });
  $('closeAssetViewer').onclick = () => $('assetViewer').close();
  $('assetViewer').addEventListener('close', releasePreview);
  for (const [index,id] of ['toolPageTab','manageCharacters'].entries()) if ($(id)) $(id).onkeydown = event => {
    if (!['ArrowLeft','ArrowRight','Home','End'].includes(event.key)) return;
    event.preventDefault();
    const target = event.key === 'Home' ? 'toolPageTab' : event.key === 'End' ? 'manageCharacters' : index ? 'toolPageTab' : 'manageCharacters';
    $(target).click(); $(target).focus();
  };
  function setCollapsed(collapsed) {
    document.body.classList.toggle('sidebar-collapsed', collapsed);
    $('sidebarToggle').setAttribute('aria-expanded',String(!collapsed));
    $('sidebarToggle').setAttribute('aria-label', collapsed ? '展开应用列表' : '收起应用列表');
    $('sidebarToggle').textContent = collapsed ? '›' : '‹';
  }
  try { const saved = localStorage.getItem('lstudio-sidebar-collapsed'); setCollapsed(saved === null ? window.matchMedia('(max-width:760px)').matches : saved === 'true'); } catch (_) { setCollapsed(window.matchMedia('(max-width:760px)').matches); }
  $('sidebarToggle').onclick = () => {
    const collapsed = !document.body.classList.contains('sidebar-collapsed'); setCollapsed(collapsed);
    try { localStorage.setItem('lstudio-sidebar-collapsed', String(collapsed)); } catch (_) {}
  };
  document.querySelector('.workspace-mark')?.addEventListener('click', () => {
    if (document.body.classList.contains('sidebar-collapsed')) {
      $('sidebarToggle')?.click();
    }
  });
  let initialPage = activeApp;
  try {
    const hashTarget = HASH_MAP[window.location.hash];
    const savedPage = localStorage.getItem('lstudio-active-page');
    if (hashTarget && panels[hashTarget] && hashTarget !== 'characters') {
      initialPage = hashTarget;
      if (['tool', 'qwen_image', 'gemma'].includes(initialPage)) {
        activeApp = initialPage;
        try { localStorage.setItem('lstudio-active-app', activeApp); } catch (_) {}
      }
    } else if (savedPage && panels[savedPage] && savedPage !== 'characters') {
      initialPage = savedPage;
    }
  } catch (_) {}

  window.addEventListener('hashchange', () => {
    const target = HASH_MAP[window.location.hash];
    if (target && panels[target] && target !== 'characters' && target !== page) {
      show(target);
    }
  });

  syncGpuSelects();
  show(initialPage);
  initialize();
})();
