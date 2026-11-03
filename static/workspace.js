'use strict';
(() => {
  let page = 'tool', h3Page = 'tool', kind = '', scopes = [], assets = [], loading = false, importing = false;
  let libraryRequest = 0, librarySignature = '', previewRequest = 0, previewURL = null;
  const thumbnails = new Map();
  const panels = {tool:'toolPage', characters:'characterPage', library:'libraryPage', compute:'computePage', connections:'connectionPage'};
  const labels = {tool:['应用','H3 视频'], characters:['应用','H3 视频'], library:['工作区','素材库'], compute:['工作区','算力'], connections:['工作区','设置']};
  const kinds = {images:'图片', videos:'视频', audios:'音频'};

  function show(next) {
    if (!panels[next]) return;
    page = next;
    const appVisible = next === 'tool' || next === 'characters';
    if (appVisible) h3Page = next;
    for (const [name, id] of Object.entries(panels)) {
      $(id).hidden = name !== next;
      $(id).classList.toggle('hidden', name !== next);
    }
    // Pause media hidden by page navigation.
    document.querySelectorAll('main[hidden] video, main[hidden] audio').forEach(media => media.pause());
    $('applicationHeading').hidden = !appVisible;
    if ($('workspaceSection')) $('workspaceSection').textContent = labels[next][0];
    if ($('workspaceTitle')) $('workspaceTitle').textContent = labels[next][1];
    for (const [id, selected] of [['h3AppTab',appVisible],['libraryPageTab',next === 'library'],['computePageTab',next === 'compute'],['connectionPageTab',next === 'connections']]) {
      $(id).classList.toggle('active', selected);
      if (selected) $(id).setAttribute('aria-current','page');
      else $(id).removeAttribute('aria-current');
    }
    for (const [id, name] of [['toolPageTab','tool'],['manageCharacters','characters']]) {
      $(id).setAttribute('aria-selected', String(next === name));
      $(id).tabIndex = next === name ? 0 : -1;
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

  function updateBalance(data, failure = null) {
    const gpu = $('gpu').value;
    const estimate = data?.gpu_estimates?.[gpu] || (gpu === 'A100' ? data?.a100_estimate : null);
    const minutes = estimate?.remaining_minutes;
    $('computeRemaining').textContent = typeof minutes === 'number' && Number.isFinite(minutes) && minutes >= 0 ? `${Math.floor(minutes / 60)}时${Math.floor(minutes % 60)}分` : '—';
    $('computePageTab').classList.toggle('stale', !!failure || !!data?.stale);
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
    const app = $('libraryAppFilter').value, purpose = $('libraryPurposeFilter').value;
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
        const use = document.createElement('button'); use.type = 'button'; use.textContent = '用于 H3'; use.onclick = () => useImage(asset); actions.append(use);
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
        const use = document.createElement('button'); use.className = 'secondary'; use.type = 'button'; use.textContent = '用于 H3 参考图'; use.onclick = () => useImage(asset); $('assetViewerActions').append(use);
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

  window.Workspace = {show, updateCompute, updateBalance, get page(){ return page; }};
  $('h3AppTab').onclick = () => { if (h3Page === 'characters') $('manageCharacters').click(); else show('tool'); };
  $('toolPageTab').onclick = () => show('tool');
  $('libraryPageTab').onclick = () => show('library');
  $('computePageTab').onclick = () => show('compute');
  $('connectionPageTab').onclick = () => show('connections');
  $('settingsOpenLibrary').onclick = () => show('library');
  $('chooseLibraryImage').onclick = () => { kind = 'images'; $('libraryAppFilter').value = ''; $('libraryPurposeFilter').value = ''; show('library'); };
  $('libraryRefresh').onclick = loadLibrary;
  $('librarySearch').oninput = renderLibrary;
  $('libraryFiles').onchange = () => importFiles([...$('libraryFiles').files]);
  $('libraryAppFilter').onchange = () => { $('libraryPurposeFilter').value = ''; updateScopeFilters(); renderLibrary(); };
  $('libraryPurposeFilter').onchange = renderLibrary;
  for (const button of document.querySelectorAll('[data-kind]')) button.onclick = () => { kind = button.dataset.kind; renderLibrary(); };
  $('closeAssetViewer').onclick = () => $('assetViewer').close();
  $('assetViewer').addEventListener('close', releasePreview);
  for (const [index,id] of ['toolPageTab','manageCharacters'].entries()) $(id).onkeydown = event => {
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
  initialize();
})();
