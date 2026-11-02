'use strict';
(() => {
  let profiles = [], deletedProfiles = [], ready = false, editing = null, busy = 0, viewing = false, managerBusy = false, matches = [], activeOption = 0;
  const prompt = $('prompt'), menu = $('mentionMenu'), dialog = $('characterDialog');
  const manager = $('characterManager');
  const coverURLs = new Map();
  const imageViewer = $('characterImageViewer');
  let imageView = null;
  function closeImageViewer() {
    const state = imageView; imageView = null;
    $('characterImageFull').removeAttribute('src'); $('characterImageFull').hidden = true;
    if (state) for (const url of state.urls) URL.revokeObjectURL(url);
    document.body.classList.remove('image-viewing');
    if (imageViewer.open) imageViewer.close();
  }
  async function renderImageViewer() {
    const state = imageView;
    if (!state) return;
    const request = ++state.request, asset = state.assets[state.index];
    const image = $('characterImageFull'), status = $('characterImageStatus');
    $('characterImageTitle').textContent = `${state.name} · ${state.index + 1} / ${state.assets.length}`;
    $('previousCharacterImage').disabled = state.index === 0;
    $('nextCharacterImage').disabled = state.index === state.assets.length - 1;
    image.hidden = true; image.removeAttribute('src'); status.textContent = '正在加载图片…';
    try {
      if (!asset.url) {
        const url = await assetURL('images',asset.image_id);
        if (imageView !== state) { URL.revokeObjectURL(url); return; }
        asset.url = url; state.urls.add(url);
      }
      if (imageView !== state || state.request !== request) return;
      image.onload = () => {
        if (imageView !== state || state.request !== request) return;
        image.hidden = false; status.textContent = '';
      };
      image.onerror = () => {
        if (imageView !== state || state.request !== request) return;
        image.hidden = true; status.textContent = '图片暂时无法查看，请关闭后重试';
      };
      image.alt = `${state.name}，第 ${state.index + 1} 张参考图`; image.src = asset.url;
    } catch (error) {
      if (imageView === state && state.request === request) status.textContent = error.message;
    }
  }
  function openImageViewer(assets,index = 0,name = '角色参考图') {
    if (!assets.length) return;
    closeImageViewer();
    imageView = {assets:assets.map(asset=>({...asset})),index,name,urls:new Set(),request:0};
    imageViewer.showModal(); document.body.classList.add('image-viewing');
    renderImageViewer();
  }
  function changeImage(delta) {
    if (!imageView) return;
    const index = imageView.index + delta;
    if (index < 0 || index >= imageView.assets.length) return;
    imageView.index = index; renderImageViewer();
  }
  $('closeCharacterImage').onclick = closeImageViewer;
  $('previousCharacterImage').onclick = () => changeImage(-1);
  $('nextCharacterImage').onclick = () => changeImage(1);
  imageViewer.addEventListener('close',() => { if (!imageViewer.open) closeImageViewer(); });
  imageViewer.addEventListener('click',event => { if (event.target === imageViewer) closeImageViewer(); });
  imageViewer.addEventListener('keydown',event => {
    if (event.key === 'ArrowLeft' || event.key === 'ArrowRight') {
      event.preventDefault(); changeImage(event.key === 'ArrowLeft' ? -1 : 1);
    }
  });
  function coverURL(id) {
    if (!coverURLs.has(id)) {
      const pending = assetURL('images',id).catch(error => { coverURLs.delete(id); throw error; });
      coverURLs.set(id,pending);
    }
    return coverURLs.get(id);
  }
  function pruneCovers(all) {
    const ids = new Set(all.map(profile => profile.image_ids[0]).filter(Boolean));
    for (const [id,pending] of coverURLs) if (!ids.has(id)) {
      coverURLs.delete(id); pending.then(url => URL.revokeObjectURL(url),() => {});
    }
  }
  const pages = [['toolPageTab','toolPage','tool'],['manageCharacters','characterPage','characters'],['connectionPageTab','connectionPage','connections']];
  function showPage(page) {
    for (const [tabId,panelId,name] of pages) {
      const selected = page === name;
      $(panelId).hidden = !selected; $(panelId).classList.toggle('hidden',!selected);
      $(tabId).setAttribute('aria-selected',String(selected)); $(tabId).tabIndex = selected ? 0 : -1;
    }
    closeMenu();
  }
  function closeEditor() {
    revokeEditor(); editing = null; viewing = false;
    dialog.hidden = true; dialog.classList.add('hidden');
    manager.hidden = false; manager.classList.remove('hidden');
    $('newCharacter').focus();
  }
  const picturePattern = /(?<![A-Za-z0-9_@])@图片([0-9]+)/g;
  const pictureSlots = () => [...new Set([...prompt.value.matchAll(picturePattern)].map(m => Number(m[1])))];
  const names = () => [...new Set([...prompt.value.replace(picturePattern,'').matchAll(/(?<![A-Za-z0-9_@])@([\p{L}\p{N}_-]+)/gu)].map(m => m[1]))];
  const missingPictures = () => pictureSlots().filter(slot => studioMode !== 'image' || !references.some(r => r.slot === slot && r.image_id && !r.pending && !r.error));
  const used = () => names().map(name => profiles.find(p => p.name === name)).filter(Boolean);
  function editorError(message) {
    $('characterEditorError').textContent = message;
    $('characterEditorError').classList.toggle('hidden', !message);
  }
  function refreshUse() {
    const selected = used(), unknown = names().filter(name => !profiles.some(p => p.name === name));
    const images = new Set(studioMode === 'image' ? referenceData().map(r => r.image_id) : []);
    for (const profile of selected) for (const image of profile.image_ids) images.add(image);
    const audios = selected.flatMap(p => p.audios), seconds = audios.reduce((sum,a) => sum + a.duration,0);
    let message = selected.length ? `本次引用 ${selected.map(p=>'@'+p.name).join('、')} · 合计 ${images.size}/9 张图片 · ${audios.length}/3 段声音（${seconds.toFixed(1)}/15 秒）` : '';
    if (unknown.length) message += `${message ? '；' : ''}未匹配角色：${unknown.map(n=>'@'+n).join('、')}，请从 @ 菜单选择`;
    const missing = missingPictures();
    if (missing.length) message += `${message ? '；' : ''}图片未就绪：${missing.map(n=>'@图片'+n).join('、')}，请上传图片或修改引用`;
    const exceeded = images.size > 9 || audios.length > 3 || seconds > 15.0001;
    if (exceeded) message += '；素材超限，请减少图片或声音样本';
    $('characterUse').textContent = message;
    $('characterUse').classList.toggle('hidden', !message);
    $('characterUse').classList.toggle('character-warning', !!unknown.length || !!missing.length || exceeded);
    updateModelControls(); updateGenerate();
  }
  function validate() {
    const pictures = missingPictures();
    if (pictures.length) throw Error('引用的图片未就绪：' + pictures.map(n=>'@图片'+n).join('、') + '，请上传图片或修改引用。');
    if (!ready && names().length) throw Error('角色库尚未读取完成，请稍后重试。');
    const missing = names().filter(name => !profiles.some(p => p.name === name));
    if (missing.length) throw Error('角色不存在：' + missing.map(n=>'@'+n).join('、') + '，请从 @ 菜单选择角色。');
    const images = new Set(studioMode === 'image' ? referenceData().map(r=>r.image_id) : []);
    const audios = used().flatMap(p=>p.audios);
    for (const profile of used()) for (const id of profile.image_ids) images.add(id);
    if (images.size > 9) throw Error('本次角色图与首尾帧、其他参考图合计超过 9 张，请减少素材。');
    if (audios.length > 3 || audios.reduce((sum,a)=>sum+a.duration,0) > 15.0001) throw Error('本次声音样本合计最多 3 段、15 秒，请减少样本或引用角色。');
  }
  function context() {
    const end = prompt.selectionStart;
    const match = prompt.value.slice(0,end).match(/(?<![A-Za-z0-9_@])@([\p{L}\p{N}_-]*)$/u);
    return match ? {start:end-match[0].length,end,query:match[1]} : null;
  }
  function closeMenu() {
    menu.classList.add('hidden'); prompt.setAttribute('aria-expanded','false');
    prompt.removeAttribute('aria-activedescendant');
  }
  function insert(profile, fromMenu = false) {
    const mention = fromMenu ? context() : null;
    const start = mention?.start ?? prompt.selectionStart, end = mention?.end ?? prompt.selectionEnd;
    const prefix = !mention && start && /[A-Za-z0-9_@]$/.test(prompt.value.slice(0,start)) ? ' ' : '';
    prompt.setRangeText(prefix + '@' + profile.name + ' ',start,end,'end');
    closeMenu(); setMode('image'); prompt.focus(); refreshUse(); saveDraft();
  }
  function renderOptions() {
    menu.replaceChildren();
    matches.forEach((profile,index) => {
      const button = document.createElement('button'); button.type = 'button'; button.id = 'mention-option-' + profile.id;
      button.className = 'mention-option'; button.setAttribute('role','option'); button.setAttribute('aria-selected',String(index === activeOption));
      const name = document.createElement('strong'); name.textContent = '@' + profile.name;
      if (profile.url) { const image = document.createElement('img'); image.src = profile.url; image.alt = ''; image.className = 'mention-thumbnail'; button.append(image); }
      const detail = document.createElement('span'); detail.textContent = profile.kind === 'image' ? profile.detail : `角色 · ${profile.image_ids.length} 张参考图 · ${profile.audios.length} 段声音`;
      button.append(name,detail); button.onmousedown = event => event.preventDefault(); button.onclick = () => insert(profile,true); menu.append(button);
    });
    if (!matches.length) { const hint = document.createElement('div'); hint.className = 'hint'; hint.textContent = '没有匹配的图片或角色，可上传图片或在“角色管理”中添加角色'; menu.append(hint); }
    menu.classList.remove('hidden'); prompt.setAttribute('aria-expanded','true');
    if (matches[activeOption]) prompt.setAttribute('aria-activedescendant','mention-option-'+matches[activeOption].id);
    else prompt.removeAttribute('aria-activedescendant');
  }
  function suggest() {
    const mention = context();
    if (!mention) return closeMenu();
    const pictures = studioMode === 'image' ? references.filter(r => r.image_id && !r.pending && !r.error).sort((a,b)=>a.slot-b.slot).map(r => ({
      id:'image-'+r.slot,name:'图片'+r.slot,kind:'image',url:r.url,detail:{first:'首帧',last:'尾帧',reference:'参考图'}[r.frame || 'reference']
    })) : [];
    matches = [...pictures,...profiles].filter(p => p.name.toLowerCase().includes(mention.query.toLowerCase()));
    activeOption = 0; renderOptions();
  }
  function managerError(message) {
    $('characterManagerError').textContent = message;
    $('characterManagerError').classList.toggle('hidden', !message);
  }
  function renderManager() {
    const search = $('characterSearch').value.trim().toLowerCase();
    const list = [...profiles,...($('showDeletedCharacters').checked ? deletedProfiles : [])].filter(p => [p.name,p.description,p.voice].some(text=>text.toLowerCase().includes(search)));
    $('characterManagerCount').textContent = `${profiles.length} 个角色${$('showDeletedCharacters').checked ? ` · ${deletedProfiles.length} 个已删除` : ''}${search ? ` · 匹配 ${list.length} 个` : ''}`;
    $('characterManagerList').replaceChildren(); $('newCharacter').disabled = managerBusy;
    if (!list.length) { const hint = document.createElement('p'); hint.className = 'manager-empty'; hint.textContent = search ? '没有匹配的角色，试试其他名称或描述。' : '还没有角色，点击“新建角色”添加参考图与声音。'; $('characterManagerList').append(hint); }
    for (const profile of list) {
      const card = document.createElement('article'); card.className = 'manager-character';
      const viewProfile = async () => { try { await openEditor(await api('characters/'+profile.id),true); } catch (e) { managerError(e.message); } };
      const cover = document.createElement('button'); cover.type = 'button'; cover.className = 'manager-cover';
      cover.setAttribute('aria-label','查看角色 '+profile.name); cover.disabled = managerBusy; cover.onclick = viewProfile;
      const fallback = document.createElement('span'); fallback.className = 'manager-cover-fallback';
      const initial = document.createElement('strong'); initial.textContent = [...profile.name][0] || '角';
      const coverStatus = document.createElement('span'); coverStatus.textContent = profile.image_ids.length ? '正在读取封面…' : '暂无参考图';
      fallback.append(initial,coverStatus); cover.append(fallback);
      if (profile.image_ids[0]) {
        const image = document.createElement('img'); image.alt = profile.name+'的角色参考图'; image.hidden = true; image.decoding = 'async';
        image.onload = () => { image.hidden = false; fallback.hidden = true; };
        image.onerror = () => { image.hidden = true; fallback.hidden = false; coverStatus.textContent = '封面暂不可用'; };
        cover.append(image);
        coverURL(profile.image_ids[0]).then(url => { if (cover.isConnected) image.src = url; },() => { coverStatus.textContent = '封面暂不可用'; });
      }
      if (profile.deleted) { const badge = document.createElement('span'); badge.className = 'manager-deleted-badge'; badge.textContent = '已删除'; cover.append(badge); }
      const info = document.createElement('div'); info.className = 'manager-character-info';
      const title = document.createElement('h3'); title.textContent = profile.name;
      const assets = document.createElement('p'); assets.className = 'hint'; assets.textContent = `${profile.image_ids.length} 张参考图 · ${profile.audios.length ? profile.audios.length+' 段声音' : '未添加声音'}`;
      info.append(title,assets);
      const actions = document.createElement('div'); actions.className = 'manager-actions';
      const view = document.createElement('button'); view.type = 'button'; view.className = 'secondary'; view.textContent = '查看'; view.setAttribute('aria-label','查看角色 '+profile.name);
      view.onclick = viewProfile;
      actions.append(view);
      if (!profile.deleted) {
        const edit = document.createElement('button'); edit.type = 'button'; edit.className = 'secondary'; edit.textContent = '编辑'; edit.setAttribute('aria-label','编辑角色 '+profile.name);
        edit.onclick = () => openEditor(profile); actions.append(edit);
      }
      const action = document.createElement('button'); action.type = 'button'; action.className = profile.deleted ? 'secondary' : 'manager-delete'; action.textContent = profile.deleted ? '恢复' : '删除'; action.setAttribute('aria-label',(profile.deleted ? '恢复角色 ' : '删除角色 ')+profile.name);
      action.onclick = async () => {
        if (managerBusy) return;
        managerBusy = true; managerError(''); $('characterManagerStatus').textContent = profile.deleted ? '正在恢复…' : '正在删除…'; renderManager();
        try {
          await api('characters/'+profile.id+(profile.deleted ? '/restore' : ''),{method:profile.deleted ? 'POST' : 'DELETE'});
          await load();
          $('characterManagerStatus').textContent = profile.deleted ? `已恢复 ${profile.name}` : `已删除 ${profile.name}，可在“显示已删除角色”中恢复。`;
          closeMenu();
        } catch (e) { managerError(e.message); $('characterManagerStatus').textContent = ''; }
        finally { managerBusy = false; renderManager(); }
      };
      actions.append(action); for (const button of actions.children) button.disabled = managerBusy;
      card.append(cover,info,actions); card.classList.toggle('manager-character-deleted',profile.deleted); $('characterManagerList').append(card);
    }
  }
  async function load() {
    try { const all = (await api('characters?include_deleted=true')).characters; pruneCovers(all); profiles = all.filter(p=>!p.deleted); deletedProfiles = all.filter(p=>p.deleted); ready = true; renderManager(); refreshUse(); }
    catch (e) { ready = false; $('characterManagerCount').textContent = '角色库读取失败，请重新打开角色管理重试'; managerError(e.message); updateGenerate(); throw e; }
  }
  function revokeEditor() {
    if (editing) for (const asset of [...editing.images,...editing.audios]) if (asset.url) URL.revokeObjectURL(asset.url);
  }
  function renderEditor() {
    if (!editing) return;
    $('characterImageAssets').replaceChildren(); $('characterAudioAssets').replaceChildren();
    for (const [index,asset] of editing.images.entries()) {
      const box = document.createElement('div'); box.className = 'character-asset';
      if (asset.url) {
        const preview = document.createElement('button'); preview.type = 'button'; preview.className = 'character-image-preview';
        preview.setAttribute('aria-label',`查看第 ${index + 1} 张角色参考图`);
        preview.onclick = () => openImageViewer(editing.images,index,$('characterName').value || '角色参考图');
        const img = document.createElement('img'); img.src = asset.url; img.alt = '角色参考图'; preview.append(img); box.append(preview);
      }
      if (asset.error) { const warning = document.createElement('span'); warning.className = 'reference-error'; warning.textContent = asset.error; box.append(warning); }
      const remove = document.createElement('button'); remove.type = 'button'; remove.textContent = '移除'; remove.disabled = !!busy || viewing;
      remove.onclick = () => { if (asset.url) URL.revokeObjectURL(asset.url); editing.images = editing.images.filter(a=>a!==asset); renderEditor(); };
      if (!viewing) box.append(remove); $('characterImageAssets').append(box);
    }
    for (const asset of editing.audios) {
      const row = document.createElement('div'); row.className = 'character-audio';
      const name = document.createElement('span'); name.textContent = `${asset.name} · ${asset.duration.toFixed(1)} 秒`; row.append(name);
      if (asset.url) { const audio = document.createElement('audio'); audio.controls = true; audio.preload = 'metadata'; audio.src = asset.url; audio.setAttribute('aria-label','试听 '+asset.name); row.append(audio); }
      if (asset.error) { const warning = document.createElement('span'); warning.className = 'reference-error'; warning.textContent = asset.error; row.append(warning); }
      const remove = document.createElement('button'); remove.type = 'button'; remove.textContent = '移除'; remove.disabled = !!busy || viewing;
      remove.onclick = () => { if (asset.url) URL.revokeObjectURL(asset.url); editing.audios = editing.audios.filter(a=>a!==asset); renderEditor(); };
      if (!viewing) row.append(remove); $('characterAudioAssets').append(row);
    }
    $('characterEditorStatus').textContent = busy ? '正在准备素材，请稍候…' : `${editing.images.length}/9 张图片 · ${editing.audios.length}/3 段声音 · 合计 ${editing.audios.reduce((sum,a)=>sum+a.duration,0).toFixed(1)}/15 秒`;
    $('saveCharacter').disabled = $('closeCharacter').disabled = !!busy;
    $('characterImages').disabled = $('characterAudios').disabled = !!busy || viewing;
    for (const id of ['characterName','characterDescription','characterVoice']) $(id).disabled = viewing;
    dialog.querySelectorAll('.asset-upload').forEach(label=>label.classList.toggle('hidden',viewing));
    $('saveCharacter').classList.toggle('hidden',viewing);
  }
  async function assetURL(kind,id) {
    const response = await fetch('/api/'+kind+'/'+id,{headers:{'X-H3-Key':KEY}});
    if (!response.ok) throw Error('文件已丢失，请移除后重新上传');
    return URL.createObjectURL(await response.blob());
  }
  async function openEditor(profile = null, readOnly = false) {
    revokeEditor(); editorError('');
    viewing = readOnly;
    editing = {id:profile?.id,images:(profile?.image_ids || []).map(image_id=>({image_id})),audios:(profile?.audios || []).map(a=>({...a}))};
    $('characterDialogTitle').textContent = viewing ? '角色详情' : profile ? '编辑角色' : '新建角色';
    $('characterName').value = profile?.name || ''; $('characterDescription').value = profile?.description || ''; $('characterVoice').value = profile?.voice || '';
    $('characterImages').value = $('characterAudios').value = ''; busy = profile ? 1 : 0; renderEditor();
    showPage('characters'); manager.hidden = true; manager.classList.add('hidden');
    dialog.hidden = false; dialog.classList.remove('hidden');
    if (!viewing) $('characterName').focus();
    const state = editing;
    await Promise.all([...state.images.map(asset=>({asset,kind:'images',id:asset.image_id})),...state.audios.map(asset=>({asset,kind:'audios',id:asset.audio_id}))].map(async ({asset,kind,id}) => {
      try { asset.url = await assetURL(kind,id); } catch (e) { asset.error = e.message; }
    }));
    if (editing === state) { busy = 0; renderEditor(); }
  }
  async function upload(kind,files) {
    if (!editing || busy || viewing) return;
    files = [...files]; const assets = kind === 'images' ? editing.images : editing.audios;
    if (files.length + assets.length > (kind === 'images' ? 9 : 3)) return editorError(kind === 'images' ? '一个角色最多 9 张参考图。' : '一个角色最多 3 段声音样本。');
    busy = 1; editorError(''); renderEditor();
    try {
      for (const file of files) {
        const form = new FormData(); form.append('file',file);
        const data = await api(kind,{method:'POST',body:form});
        if (kind === 'audios' && editing.audios.reduce((sum,a)=>sum+a.duration,0) + data.duration > 15.0001) throw Error('声音样本总长超过 15 秒；已保留此前上传的样本，请裁剪后再添加。');
        assets.push({...data,url:URL.createObjectURL(file)}); renderEditor();
      }
    } catch (e) { editorError(e.message); }
    finally { busy = 0; $('characterImages').value = $('characterAudios').value = ''; renderEditor(); }
  }
  $('newCharacter').onclick = () => openEditor();
  $('toolPageTab').onclick = () => showPage('tool');
  $('connectionPageTab').onclick = () => showPage('connections');
  $('manageCharacters').onclick = async () => { showPage('characters'); managerError(''); $('characterManagerStatus').textContent = ''; renderManager(); if (!editing) $('characterSearch').focus(); try { await load(); } catch (_) {} };
  for (const [index,[id]] of pages.entries()) {
    $(id).onkeydown = event => {
      if (!['ArrowLeft','ArrowRight','Home','End'].includes(event.key)) return;
      event.preventDefault();
      const targetIndex = event.key === 'Home' ? 0 : event.key === 'End' ? pages.length - 1 : (index + (event.key === 'ArrowRight' ? 1 : -1) + pages.length) % pages.length;
      const target = pages[targetIndex][0];
      $(target).click(); $(target).focus();
    };
  }
  $('characterSearch').oninput = renderManager;
  $('showDeletedCharacters').onchange = renderManager;
  $('closeCharacter').onclick = () => { if (!busy) closeEditor(); };
  $('characterImages').onchange = () => upload('images',$('characterImages').files);
  $('characterAudios').onchange = () => upload('audios',$('characterAudios').files);
  $('characterForm').onsubmit = async event => {
    event.preventDefault(); if (busy || viewing) return;
    if (!editing.images.length) return editorError('请至少添加一张角色参考图。');
    if ([...editing.images,...editing.audios].some(a=>a.error)) return editorError('请替换或移除已丢失的素材。');
    busy = 1; editorError(''); renderEditor();
    try {
      const payload = {name:$('characterName').value.trim(),description:$('characterDescription').value.trim(),voice:$('characterVoice').value.trim(),image_ids:editing.images.map(a=>a.image_id),audios:editing.audios.map(({audio_id,name,duration})=>({audio_id,name,duration}))};
      if (editing.id) payload.id = editing.id;
      const saved = await api('characters',{method:'POST',body:JSON.stringify(payload)});
      busy = 0; closeEditor(); await load(); error(''); $('characterManagerStatus').textContent = `已保存 ${saved.name}`;
    } catch (e) { editorError(e.message); }
    finally { busy = 0; renderEditor(); }
  };
  prompt.addEventListener('input',() => { if (used().length) setMode('image'); refreshUse(); suggest(); });
  prompt.addEventListener('click',suggest);
  prompt.addEventListener('keydown',event => {
    if (menu.classList.contains('hidden')) return;
    if (event.key === 'Escape') { event.preventDefault(); closeMenu(); }
    else if (['ArrowDown','ArrowUp'].includes(event.key) && matches.length) { event.preventDefault(); activeOption = (activeOption + (event.key === 'ArrowDown' ? 1 : -1) + matches.length) % matches.length; renderOptions(); }
    else if (['Enter','Tab'].includes(event.key) && matches.length) { event.preventDefault(); insert(matches[activeOption],true); }
  });
  document.addEventListener('click',event => { if (event.target !== prompt && !menu.contains(event.target)) closeMenu(); });
  $('generateForm').addEventListener('input',refreshUse);
  window.H3Characters = {used,validate,refreshUse,insert,needsLibrary:()=>!ready && names().length > 0,get ready(){return ready;}};
  showPage('tool');
  load().catch(e=>error(e.message));
})();
