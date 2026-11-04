'use strict';
(() => {
  const $ = window.$ || (id => document.getElementById(id));
  window.$ = $;
  const HASH_MAP = {
    '#h3': 'tool',
    '#tool': 'tool',
    '#qwen_image': 'qwen_image',
    '#qi': 'qwen_image',
    '#library': 'library',
    '#compute': 'compute',
    '#settings': 'connections',
    '#connections': 'connections'
  };

  let activeApp = 'tool';
  try {
    const savedApp = localStorage.getItem('lstudio-active-app');
    if (savedApp === 'tool' || savedApp === 'qwen_image') activeApp = savedApp;
  } catch (_) {}

  let savedKind = '';
  try { savedKind = localStorage.getItem('lstudio-library-kind') || ''; } catch (_) {}

  let page = 'tool', h3Page = 'tool', kind = savedKind, scopes = [], assets = [], loading = false, importing = false;
  let libraryRequest = 0, librarySignature = '', previewRequest = 0, previewURL = null;
  const thumbnails = new Map();
  const panels = {tool:'toolPage', characters:'characterPage', qwen_image:'qwenImagePage', library:'libraryPage', compute:'computePage', connections:'connectionPage'};
  const labels = {tool:['应用','H3 视频'], characters:['应用','H3 视频'], qwen_image:['应用','Qwen-Image-2.1'], library:['工作区','素材库'], compute:['工作区','算力'], connections:['工作区','设置']};
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
    const isQI = activeApp === 'qwen_image';
    const appName = isQI ? 'Qwen-Image-2.1' : 'H3 视频';
    const appGpuVal = isQI
      ? ($('qiGpu')?.value || localStorage.getItem('lstudio-qi-gpu') || 'T4')
      : ($('h3Gpu')?.value || localStorage.getItem('lstudio-h3-gpu') || 'G4');
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
    const appVisible = isH3 || isQwenImage;
    if (isH3) {
      h3Page = 'tool';
      activeApp = 'tool';
      try { localStorage.setItem('lstudio-active-app', 'tool'); } catch (_) {}
    } else if (isQwenImage) {
      activeApp = 'qwen_image';
      try { localStorage.setItem('lstudio-active-app', 'qwen_image'); } catch (_) {}
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
    for (const [id, selected] of [['h3AppTab',isH3],['qwenImageAppTab',isQwenImage],['libraryPageTab',next === 'library'],['computePageTab',next === 'compute'],['connectionPageTab',next === 'connections']]) {
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
    if (next === 'tool' || next === 'characters') {
      const h3Val = $('h3Gpu')?.value || localStorage.getItem('lstudio-h3-gpu') || 'G4';
      if ($('gpu') && [...$('gpu').options].some(o => o.value === h3Val)) {
        $('gpu').value = h3Val;
      }
      updateBalance();
    } else if (next === 'qwen_image') {
      const qiVal = $('qiGpu')?.value || localStorage.getItem('lstudio-qi-gpu') || 'T4';
      if ($('gpu') && [...$('gpu').options].some(o => o.value === qiVal)) {
        $('gpu').value = qiVal;
      }
      updateBalance();
    } else if (next === 'compute') {
      updateComputeAppInfo();
    }
    if (next === 'library') loadLibrary();
  }

  function updateCompute(status) {
    const busy = !!status.active, connected = !!status.managed_gpu;
    const active = status.jobs?.find(job => job.id === status.active);
    const gpu = active?.gpu_info?.name || status.compute?.gpu;
    $('computeGpuName').textContent = gpu || `${$('gpu').value} · 按需启动`;
    $('computeSummary').textContent = busy ? '正在运行' : connected ? 'GPU 已保留 · 仍在计费' : status.authorized ? '账号已连接 · GPU 未启动' : '未连接账号';
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

  function scopeName(asset) {
    const scope = scopes.find(item => item.app_id === asset.app_id && item.purpose === asset.purpose);
    return scope ? `${scope.app_name} / ${scope.purpose_name}` : `${asset.app_id} / ${asset.purpose}`;
  }

  function importDestination() {
    const app = $('libraryAppFilter').value, purpose = $('libraryPurposeFilter').value;
    return scopes.find(scope => (!app || scope.app_id === app) && (!purpose || scope.purpose === purpose) && (!kind || scope.kinds.includes(kind)));
  }

  function updateImportTarget() {
    const scope = importDestination();
    $('libraryImportTarget').textContent = scope ? `导入位置：${scope.app_name} / ${scope.purpose_name}` : '当前筛选下没有可导入的位置';
    $('libraryFiles').disabled = importing || !scope;
    const label = document.querySelector('label[for="libraryFiles"]');
    label.setAttribute('aria-disabled',String(importing || !scope));
    $('libraryFiles').accept = scope ? scope.kinds.map(value => ({images:'.png,.jpg,.jpeg,.webp',videos:'.mp4',audios:'.wav,.mp3,.m4a,.ogg,.flac,.aac'}[value])).join(',') : '';
  }

  function updateScopeFilters() {
    let savedApp = '';
    try { savedApp = localStorage.getItem('lstudio-library-app') || ''; } catch (_) {}
    const app = $('libraryAppFilter').value || savedApp, purpose = $('libraryPurposeFilter').value;
    const apps = new Map(scopes.map(scope => [scope.app_id,scope.app_name]));
    for (const asset of assets) if (!apps.has(asset.app_id)) apps.set(asset.app_id,asset.app_id);
    $('libraryAppFilter').replaceChildren(new Option('全部应用',''));
    for (const [id,name] of apps) $('libraryAppFilter').add(new Option(name,id));
    if (apps.has(app)) $('libraryAppFilter').value = app;
    const purposes = new Map(scopes.filter(scope => !app || scope.app_id === app).map(scope => [scope.purpose,scope.purpose_name]));
    $('libraryPurposeFilter').replaceChildren(new Option('全部用途',''));
    for (const [id,name] of purposes) $('libraryPurposeFilter').add(new Option(name,id));
    if (purposes.has(purpose)) $('libraryPurposeFilter').value = purpose;
    updateImportTarget();
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

  function renderLibrary() {
    $('libraryGrid').querySelectorAll('video, audio').forEach(media => media.pause());
    $('libraryGrid').replaceChildren();
    const search = $('librarySearch').value.trim().toLocaleLowerCase();
    const app = $('libraryAppFilter').value, purpose = $('libraryPurposeFilter').value;
    const filtered = assets.filter(asset => (!kind || asset.kind === kind) && (!app || asset.app_id === app) && (!purpose || asset.purpose === purpose) && asset.name.toLocaleLowerCase().includes(search));
    for (const button of document.querySelectorAll('[data-kind]')) button.setAttribute('aria-pressed', String(button.dataset.kind === kind));
    $('libraryFeedback').textContent = importing ? '正在导入素材…' : `${filtered.length} 项素材${kind ? ' · ' + kinds[kind] : ''}`;
    updateImportTarget();
    if (!filtered.length) {
      const empty = document.createElement('div'); empty.className = 'library-empty';
      const title = document.createElement('h3'); title.textContent = search ? '没有找到匹配的素材' : '素材会在这里汇集';
      const hint = document.createElement('p'); hint.textContent = search ? '试试其他名称，或切换文件夹。' : '导入图片、视频或音频；H3 生成的视频也会自动出现在这里。';
      empty.append(title,hint); $('libraryGrid').append(empty);
    }
    for (const asset of filtered) {
      const card = document.createElement('article'); card.className = 'library-asset';
      const preview = document.createElement('button'); preview.type = 'button'; preview.className = 'library-preview'; preview.setAttribute('aria-label','预览 ' + asset.name);
      if (asset.kind === 'images') {
        const img = document.createElement('img'); img.alt = asset.name; img.loading = 'lazy'; preview.append(img);
        // Start fetching only once the card has been inserted.
        queueMicrotask(() => thumbnail(asset,img));
      } else {
        const icon = document.createElement('span'); icon.className = 'library-media-icon'; icon.textContent = asset.kind === 'videos' ? '▷' : '♫'; preview.append(icon);
        if (asset.kind === 'videos' && asset.local) {
          const video = document.createElement('video'); video.src = '/video/' + asset.id + '#t=0.1'; video.preload = 'metadata'; video.muted = true; video.playsInline = true; preview.append(video);
        }
      }
      const badge = document.createElement('span'); badge.className = 'library-kind'; badge.textContent = kinds[asset.kind]; preview.append(badge);
      preview.onclick = () => openPreview(asset);
      const copy = document.createElement('div'); copy.className = 'library-asset-copy';
      const title = document.createElement('h3'); title.textContent = asset.name; title.title = asset.name;
      const location = document.createElement('p'); location.className = 'library-scope'; location.textContent = scopeName(asset);
      const meta = document.createElement('p'); meta.textContent = `${sizeLabel(asset.size)} · ${asset.local ? '本机素材' : asset.recoverable ? 'Drive 副本' : '本机文件缺失'}`;
      const actions = document.createElement('div'); actions.className = 'library-asset-actions';
      const view = document.createElement('button'); view.type = 'button'; view.textContent = '预览'; view.onclick = () => openPreview(asset); actions.append(view);
      if (asset.kind === 'images') {
        if (activeApp === 'qwen_image') {
          const useQi = document.createElement('button'); useQi.type = 'button'; useQi.textContent = '用于 Qwen-Image'; useQi.onclick = () => useImageForQi(asset); actions.append(useQi);
          const use = document.createElement('button'); use.type = 'button'; use.textContent = '用于 H3'; use.onclick = () => useImage(asset); actions.append(use);
        } else {
          const use = document.createElement('button'); use.type = 'button'; use.textContent = '用于 H3'; use.onclick = () => useImage(asset); actions.append(use);
          const useQi = document.createElement('button'); useQi.type = 'button'; useQi.textContent = '用于 Qwen-Image'; useQi.onclick = () => useImageForQi(asset); actions.append(useQi);
        }
      }
      copy.append(title,location,meta,actions); card.append(preview,copy); $('libraryGrid').append(card);
    }
  }

  async function loadLibrary() {
    const request = ++libraryRequest;
    loading = true; $('libraryRefresh').disabled = true; libraryError('');
    try {
      const data = await api('assets');
      if (request !== libraryRequest) return;
      clearThumbnails(); assets = data.assets; scopes = data.scopes; updateScopeFilters();
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
    const destination = importDestination();
    if (importing || !destination) return;
    importing = true; $('libraryFiles').disabled = true; libraryError('');
    let failure = '';
    for (const file of files) {
      try {
        const form = new FormData(); form.append('file',file); form.append('app_id',destination.app_id); form.append('purpose',destination.purpose);
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
    if (allRuntimes && allRuntimes.length) lastDiscoveredRuntimes = allRuntimes;
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

  function syncGpuSelects(allRuntimes) {
    if (allRuntimes && allRuntimes.length) lastDiscoveredRuntimes = allRuntimes;
    const targets = [
      { id: 'h3Gpu', defaultGpu: 'G4', storageKey: 'lstudio-h3-gpu' },
      { id: 'qiGpu', defaultGpu: 'T4', storageKey: 'lstudio-qi-gpu' },
      { id: 'gpu', defaultGpu: 'G4', storageKey: 'lstudio-compute-gpu' }
    ];

    function updateGpuCardDisplay(selectId, displayId, statusId, defaultDesc) {
      const select = $(selectId), display = $(displayId), status = $(statusId);
      if (!select || !display) return;
      const opt = select.selectedOptions?.[0];
      const text = (opt ? opt.textContent : select.value) || '';
      if (text.includes('(🟢')) {
        const parts = text.split('(🟢');
        display.textContent = parts[0].trim();
        if (status) {
          status.textContent = '🟢 ' + parts[1].replace(')', '').trim();
          status.classList.remove('status-idle');
          status.classList.add('status-online');
        }
      } else {
        display.textContent = text.trim();
        if (status) {
          status.textContent = defaultDesc || '按需启动';
          status.classList.remove('status-online');
          status.classList.add('status-idle');
        }
      }
    }

    for (const target of targets) {
      const select = $(target.id);
      if (!select) continue;
      let saved = null;
      try { saved = localStorage.getItem(target.storageKey); } catch (_) {}
      const chosen = select.value || saved || target.defaultGpu;
      const options = buildGpuOptions(allRuntimes);
      select.replaceChildren(...options);
      if ([...select.options].some(o => o.value === chosen)) {
        select.value = chosen;
      } else {
        select.value = target.defaultGpu;
      }
      if (target.id === 'h3Gpu') updateGpuCardDisplay('h3Gpu', 'h3GpuDisplay', 'h3GpuStatus', 'Wan 2.1 推荐配置');
      if (target.id === 'qiGpu') updateGpuCardDisplay('qiGpu', 'qiGpuDisplay', 'qiGpuStatus', '推荐 · 显存友好');
      if (!select.dataset.syncBound) {
        select.dataset.syncBound = 'true';
        select.addEventListener('change', () => {
          try { localStorage.setItem(target.storageKey, select.value); } catch (_) {}
          if (target.id === 'h3Gpu') updateGpuCardDisplay('h3Gpu', 'h3GpuDisplay', 'h3GpuStatus', 'Wan 2.1 推荐配置');
          if (target.id === 'qiGpu') updateGpuCardDisplay('qiGpu', 'qiGpuDisplay', 'qiGpuStatus', '推荐 · 显存友好');
          if (target.id === 'h3Gpu' || target.id === 'qiGpu') {
            const computeGpu = $('gpu');
            if (computeGpu && [...computeGpu.options].some(o => o.value === select.value)) {
              computeGpu.value = select.value;
              computeGpu.dispatchEvent(new Event('change'));
            }
          }
          if (target.id === 'gpu') {
            const isQI = activeApp === 'qwen_image';
            const targetSelect = $(isQI ? 'qiGpu' : 'h3Gpu');
            const targetStorage = isQI ? 'lstudio-qi-gpu' : 'lstudio-h3-gpu';
            try { localStorage.setItem(targetStorage, select.value); } catch (_) {}
            if (targetSelect && [...targetSelect.options].some(o => o.value === select.value)) {
              targetSelect.value = select.value;
              if (isQI) updateGpuCardDisplay('qiGpu', 'qiGpuDisplay', 'qiGpuStatus', '推荐 · 显存友好');
              else updateGpuCardDisplay('h3Gpu', 'h3GpuDisplay', 'h3GpuStatus', 'Wan 2.1 推荐配置');
            }
            updateBalance();
          }
        });
      }
    }
  }

  window.Workspace = {
    show,
    updateCompute,
    updateBalance,
    buildGpuOptions,
    syncGpuSelects,
    openAssetPicker,
    get page(){ return page; },
    get activeApp(){ return activeApp; }
  };
  $('h3AppTab').onclick = () => show('tool');
  $('qwenImageAppTab').onclick = () => show('qwen_image');
  if ($('qwenImageToolTab')) $('qwenImageToolTab').onclick = () => show('qwen_image');
  if ($('toolPageTab')) $('toolPageTab').onclick = () => show('tool');
  $('libraryPageTab').onclick = () => show('library');
  $('computePageTab').onclick = () => show('compute');
  $('connectionPageTab').onclick = () => show('connections');
  $('settingsOpenLibrary').onclick = () => show('library');

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
  $('librarySearch').oninput = renderLibrary;
  $('libraryFiles').onchange = () => importFiles([...$('libraryFiles').files]);
  $('libraryAppFilter').onchange = () => {
    try { localStorage.setItem('lstudio-library-app', $('libraryAppFilter').value); } catch (_) {}
    $('libraryPurposeFilter').value = '';
    updateScopeFilters();
    renderLibrary();
  };
  $('libraryPurposeFilter').onchange = renderLibrary;
  for (const button of document.querySelectorAll('[data-kind]')) button.onclick = () => {
    kind = button.dataset.kind;
    try { localStorage.setItem('lstudio-library-kind', kind); } catch (_) {}
    renderLibrary();
  };
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
      if (initialPage === 'tool' || initialPage === 'qwen_image') {
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
