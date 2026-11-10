'use strict';
(() => {
  const root = $('gemmaPage');
  const prompt = $('gemmaPrompt');
  const chatMenu = $('gemmaChatMenu');
  const removeChat = $('gemmaRemoveChat');
  const pendingStates = new Set(['queued', 'running', 'cancelling']);
  const modelNames = {
    native: '原生版 (31B)',
    heretic: 'ARA Heretic 破甲版 (31B)',
    gpt_oss_120b: 'GPT-OSS 120B (OpenAI)',
  };
  const drafts = new Map();
  const chatCharacters = new Map();
  const newId = () => crypto.randomUUID().replaceAll('-', '');
  let chatId = newId();
  let state = null, action = null, request = 0, fetching = false;
  let listSignature = '', turnSignature = '', logSignature = '';
  let loadError = '', actionError = '';
  let dismissedNotice = '';
  try {
    dismissedNotice = sessionStorage.getItem('lstudio-gemma-dismissed-notice') || '';
  } catch (_) {}
  let dismissedError = '';
  let noticeDismissTimer = null;
  let bannerFadeTimer = null;

  function dismissBanner() {
    if (noticeDismissTimer) {
      clearTimeout(noticeDismissTimer);
      noticeDismissTimer = null;
    }
    const banner = $('gemmaError');
    if (!banner || banner.hidden) return;
    banner.classList.add('gemma-error-fading');
    if (bannerFadeTimer) clearTimeout(bannerFadeTimer);
    bannerFadeTimer = setTimeout(() => {
      banner.hidden = true;
      banner.classList.remove('gemma-error-fading');
    }, 220);
  }
  let menuTarget = null;
  let activeStreamAbort = null;

  const DEFAULT_CHARACTERS = {
    assistant: {
      id: 'assistant',
      name: '小A',
      avatar: '',
      tag: 'AI 助手 · 默认',
      desc: '知识渊博、理性条理且富有温度的拟人化数智伙伴，擅长各类答疑、深度推演与日常协作。',
      placeholder: '向小A提问，Shift + Enter 换行，Enter 发送...',
      system_prompt: '你是小A，一位拟人化的数智 AI 助手。你专业、敏锐、真诚且知识渊博，善于用清晰条理、逻辑严密且富有温度的语言回答用户的各类问题。在阐述专业知识时深入浅出，遇到复杂问题善于分步骤剖析。保持礼貌友善、谦逊温和的交谈姿态。',
      builtin: true,
    },
    scarlett: {
      id: 'scarlett',
      name: '林薇',
      avatar: '',
      tag: '热情奔放 · 元气满满',
      desc: '性格开朗明媚、热情直爽的大美女，笑容治愈、活力四射，喜欢分享美好事物，永远给你满满的正能量！',
      placeholder: '哈喽！今天有什么开心的事想和我聊聊嘛？...',
      system_prompt: '你是林薇（Scarlett），一位性格热情奔放、阳光明媚且极具感染力的年轻美女。你说话直接爽朗、生动有趣，常常带着灿烂的笑容和满满的正能量。你善于倾听、真诚共情，当朋友遇到烦恼时你会以大方热烈的态度鼓励对方，带给用户无尽的温暖与快乐。',
      builtin: true,
    },
    leo: {
      id: 'leo',
      name: '陆扬',
      avatar: '',
      tag: '阳光爽朗 · 靠谱温暖',
      desc: '热爱运动与生活的阳光大男孩，眼神清澈、真诚坦率，随和风趣又靠谱，随时为你排忧解难！',
      placeholder: '嘿，有什么烦恼或者计划？随时跟我说说吧！...',
      system_prompt: '你是陆扬（Leo），一位阳光帅气、开朗幽默且真诚靠谱的青年。你热爱运动与生活，性格随和开朗，说话幽默得体，像个温暖热心的大男孩。面对朋友的倾诉或求助，你既能陪着轻松调侃，又能在关键时刻给出务实稳重的建议与毫无保留的支持。',
      builtin: true,
    },
  };

  let CHARACTERS = {...DEFAULT_CHARACTERS};
  try {
    const cached = JSON.parse(localStorage.getItem('lstudio-gemma-all-characters') || '[]');
    if (Array.isArray(cached)) {
      for (const c of cached) {
        if (c && c.id) {
          if (c.builtin && !DEFAULT_CHARACTERS[c.id]) continue;
          const base = DEFAULT_CHARACTERS[c.id] || {};
          const item = {...base, ...c};
          delete item.persona;
          delete item.cover;
          CHARACTERS[c.id] = item;
        }
      }
    }
  } catch (_) {}

  function syncCharacters(backendChars) {
    if (!Array.isArray(backendChars) || !backendChars.length) return;
    const next = {...DEFAULT_CHARACTERS};
    for (const c of backendChars) {
      if (c && c.id) {
        if (c.builtin && !DEFAULT_CHARACTERS[c.id]) continue;
        const item = {...(next[c.id] || {}), ...c};
        delete item.persona;
        delete item.cover;
        next[c.id] = item;
      }
    }
    CHARACTERS = next;
    try {
      localStorage.setItem('lstudio-gemma-all-characters', JSON.stringify(Object.values(CHARACTERS)));
    } catch (_) {}
  }

  let defaultCharacterId = 'assistant';
  try {
    const savedChar = localStorage.getItem('lstudio-gemma-character');
    if (savedChar && CHARACTERS[savedChar]) defaultCharacterId = savedChar;
  } catch (_) {}

  function getCharacterByIdOrName(id, name) {
    if (id && CHARACTERS[id]) return CHARACTERS[id];
    if (name) {
      const match = Object.values(CHARACTERS).find(c => c.name === name);
      if (match) return match;
    }
    return null;
  }

  function getActiveCharacter(cId = chatId) {
    if (chatCharacters.has(cId)) {
      return CHARACTERS[chatCharacters.get(cId)] || CHARACTERS.assistant;
    }
    if (state?.chats) {
      const chat = state.chats.find(c => c.id === cId);
      if (chat) {
        const found = getCharacterByIdOrName(chat.character_id, chat.character_name);
        if (found) {
          chatCharacters.set(cId, found.id);
          return found;
        }
      }
    }
    if (cId === chatId && state?.turns?.length) {
      for (const t of state.turns) {
        const found = getCharacterByIdOrName(t.character_id, t.character_name);
        if (found) {
          chatCharacters.set(cId, found.id);
          return found;
        }
      }
    }
    return CHARACTERS[defaultCharacterId] || CHARACTERS.assistant;
  }

  function getCharAvatar(char) {
    if (!char) return '/apps/gemma_chat/web/characters/assistant_avatar.jpg';
    if (char.avatar && (char.avatar.startsWith('/') || char.avatar.startsWith('http') || char.avatar.startsWith('data:image'))) {
      return char.avatar;
    }
    if (char.cover) return char.cover;
    return '/apps/gemma_chat/web/characters/assistant_avatar.jpg';
  }

  function createAvatarNode(char, className = 'gemma-reply-avatar') {
    const wrap = node('div', className);
    wrap.title = char?.name || '';
    const avatarUrl = getCharAvatar(char);
    wrap.dataset.avatarUrl = avatarUrl;
    if (avatarUrl) {
      const img = document.createElement('img');
      img.src = avatarUrl;
      img.alt = char?.name || '';
      img.decoding = 'async';
      img.className = 'gemma-avatar-img';
      img.onerror = () => {
        img.remove();
        wrap.textContent = (char?.name || 'A').slice(0, 1);
      };
      wrap.append(img);
    } else {
      wrap.textContent = (char?.name || 'A').slice(0, 1);
    }
    return wrap;
  }

  function updateAvatarNode(wrap, char) {
    if (!wrap) return;
    const avatarUrl = getCharAvatar(char);
    if (wrap.dataset.avatarUrl === avatarUrl) return;
    wrap.dataset.avatarUrl = avatarUrl;
    wrap.title = char?.name || '';
    const img = wrap.querySelector('img');
    if (img) {
      img.src = avatarUrl;
      img.alt = char?.name || '';
    } else if (avatarUrl) {
      wrap.replaceChildren();
      const newImg = document.createElement('img');
      newImg.src = avatarUrl;
      newImg.alt = char?.name || '';
      newImg.decoding = 'async';
      newImg.className = 'gemma-avatar-img';
      newImg.onerror = () => {
        newImg.remove();
        wrap.textContent = (char?.name || 'A').slice(0, 1);
      };
      wrap.append(newImg);
    } else {
      wrap.textContent = (char?.name || 'A').slice(0, 1);
    }
  }

  function updateAvatarPreview(url) {
    const squareImg = $('gemmaCharSquareImg');
    const squareEmpty = $('gemmaCharSquareEmpty');
    const circleImg = $('gemmaCharCircleImg');
    const circleEmpty = $('gemmaCharCircleEmpty');
    if (url) {
      if (squareImg) { squareImg.src = url; squareImg.classList.remove('hidden'); }
      if (squareEmpty) squareEmpty.classList.add('hidden');
      if (circleImg) { circleImg.src = url; circleImg.classList.remove('hidden'); }
      if (circleEmpty) circleEmpty.classList.add('hidden');
    } else {
      if (squareImg) { squareImg.src = ''; squareImg.classList.add('hidden'); }
      if (squareEmpty) squareEmpty.classList.remove('hidden');
      if (circleImg) { circleImg.src = ''; circleImg.classList.add('hidden'); }
      if (circleEmpty) circleEmpty.classList.remove('hidden');
    }
  }

  function setActiveCharacter(charId, cId = chatId) {
    if (!CHARACTERS[charId]) return;
    chatCharacters.set(cId, charId);
    defaultCharacterId = charId;
    try { localStorage.setItem('lstudio-gemma-character', charId); } catch (_) {}
    renderCharacterGrid();
    updateComposer();
    if (state?.state === 'ready') {
      const activeChar = CHARACTERS[charId];
      const avatarUrl = getCharAvatar(activeChar);
      $('gemmaSubtitle').innerHTML = `<span class="gemma-subtitle-badge"><img src="${escapeHtml(avatarUrl)}" class="gemma-subtitle-avatar" alt=""><span>${escapeHtml(activeChar.name)}</span></span>`;
    }
  }

  function openCharModal(char = null) {
    const modal = $('gemmaCharModal');
    if (!modal) return;
    const isEdit = !!char;
    const isBuiltin = !!char?.builtin;
    const modalTitle = $('gemmaModalTitle');
    if (modalTitle) {
      if (isBuiltin) {
        modalTitle.innerHTML = `<span>✏️</span><span>编辑默认角色：${escapeHtml(char.name)}</span>`;
      } else if (isEdit) {
        modalTitle.innerHTML = `<span>✏️</span><span>编辑角色：${escapeHtml(char.name)}</span>`;
      } else {
        modalTitle.innerHTML = `<span>✨</span><span>创建新角色</span>`;
      }
    }
    $('gemmaCharFormId').value = isEdit ? char.id : '';
    $('gemmaCharFormName').value = isEdit ? char.name : '';
    $('gemmaCharFormAvatar').value = isEdit ? (char.avatar || '') : '';
    $('gemmaCharFormTag').value = isEdit ? (char.tag || '') : '';
    $('gemmaCharFormDesc').value = isEdit ? (char.desc || '') : '';
    $('gemmaCharFormPlaceholder').value = isEdit ? (char.placeholder || '') : '';
    $('gemmaCharFormSys').value = isEdit ? (char.system_prompt || '') : '';

    updateAvatarPreview(isEdit ? (char.avatar || '') : '');

    const delBtn = $('gemmaCharFormDelete');
    if (delBtn) {
      delBtn.classList.toggle('hidden', !isEdit || isBuiltin);
    }
    const resetBtn = $('gemmaCharFormReset');
    if (resetBtn) {
      resetBtn.classList.toggle('hidden', !isEdit || !isBuiltin);
    }

    if (typeof modal.showModal === 'function') modal.showModal();
    else modal.setAttribute('open', '');
    $('gemmaCharFormName').focus();
  }

  function closeCharModal() {
    const modal = $('gemmaCharModal');
    if (!modal) return;
    if (typeof modal.close === 'function') modal.close();
    else modal.removeAttribute('open');
  }

  async function saveCustomCharacter(event) {
    event.preventDefault();
    const existingId = $('gemmaCharFormId').value.trim();
    const existingChar = existingId ? CHARACTERS[existingId] : null;
    const isBuiltin = !!existingChar?.builtin;
    const id = existingId || ('custom_' + newId().slice(0, 12));
    const name = $('gemmaCharFormName').value.trim();
    if (!name) return;
    const avatar = $('gemmaCharFormAvatar').value.trim() || (isBuiltin && existingChar?.avatar) || '';
    if (!avatar) {
      alert('请先点击「选择/更换头像」选择一张图片并完成裁剪');
      return;
    }
    const charData = {
      id,
      name,
      avatar,
      tag: $('gemmaCharFormTag').value.trim(),
      desc: $('gemmaCharFormDesc').value.trim(),
      placeholder: $('gemmaCharFormPlaceholder').value.trim(),
      system_prompt: $('gemmaCharFormSys').value.trim(),
      builtin: isBuiltin,
      is_customized: true,
    };

    try {
      const saved = await api('gemma/characters', {
        method: 'POST',
        body: JSON.stringify(charData),
      });
      CHARACTERS[id] = {...charData, ...(saved || {})};
      try {
        localStorage.setItem('lstudio-gemma-all-characters', JSON.stringify(Object.values(CHARACTERS)));
      } catch (_) {}
      setActiveCharacter(id, chatId);
      closeCharModal();
      renderCharacterGrid();
      renderTurns(state?.turns || [], false);
      refresh();
    } catch (err) {
      alert('保存角色失败：' + err.message);
    }
  }

  async function resetDefaultCharacter() {
    const id = $('gemmaCharFormId').value.trim();
    if (!id || !CHARACTERS[id] || !CHARACTERS[id].builtin) return;
    if (!confirm(`确定要将「${CHARACTERS[id].name}」恢复为预设默认的人设、头像与提示词吗？`)) return;

    try {
      const res = await api('gemma/characters/' + id + '/reset', {method: 'POST'});
      if (res && res.id) {
        CHARACTERS[id] = {...res, builtin: true, is_customized: false};
      } else if (DEFAULT_CHARACTERS[id]) {
        CHARACTERS[id] = {...DEFAULT_CHARACTERS[id]};
      }
      try {
        localStorage.setItem('lstudio-gemma-all-characters', JSON.stringify(Object.values(CHARACTERS)));
      } catch (_) {}
      setActiveCharacter(id, chatId);
      closeCharModal();
      renderCharacterGrid();
      renderTurns(state?.turns || [], false);
      refresh();
    } catch (err) {
      alert('恢复默认设置失败：' + err.message);
    }
  }

  async function deleteCustomCharacter() {
    const id = $('gemmaCharFormId').value.trim();
    if (!id || !CHARACTERS[id] || CHARACTERS[id].builtin) return;
    if (!confirm(`确定要删除角色「${CHARACTERS[id].name}」吗？`)) return;

    try {
      await api('gemma/characters/' + id, {method: 'DELETE'});
      delete CHARACTERS[id];
      try {
        localStorage.setItem('lstudio-gemma-all-characters', JSON.stringify(Object.values(CHARACTERS)));
      } catch (_) {}
      if (getActiveCharacter(chatId).id === id) {
        setActiveCharacter('assistant', chatId);
      }
      closeCharModal();
      renderCharacterGrid();
      refresh();
    } catch (err) {
      alert('删除角色失败：' + err.message);
    }
  }

  function renderCharacterGrid() {
    const grid = $('gemmaCharacterGrid');
    if (!grid) return;
    const activeChar = getActiveCharacter(chatId);

    const cards = [];

    // 1. Add character card
    const addCard = node('button', 'gemma-char-card gemma-char-add-card');
    addCard.type = 'button';
    addCard.title = '创建你的自定义角色';
    addCard.innerHTML = `
      <div class="gemma-add-card-inner">
        <div class="gemma-add-icon-wrap">
          <svg viewBox="0 0 24 24" width="22" height="22" fill="none" stroke="currentColor" stroke-width="2.5"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg>
        </div>
        <strong class="gemma-add-title">创建自定义角色</strong>
        <span class="gemma-add-sub">设定专属立绘、人设与提示词</span>
      </div>
    `;
    addCard.onclick = () => openCharModal();
    cards.push(addCard);

    // 2. Character cards
    Object.values(CHARACTERS).forEach(char => {
      const isSelected = char.id === activeChar.id;
      const card = node('button', 'gemma-char-card' + (isSelected ? ' active' : ''));
      card.type = 'button';
      card.setAttribute('role', 'radio');
      card.setAttribute('aria-checked', String(isSelected));
      card.dataset.charId = char.id;

      const avatarUrl = getCharAvatar(char);
      if (avatarUrl) {
        const cover = node('div', 'gemma-char-cover');
        const img = document.createElement('img');
        img.src = avatarUrl;
        img.alt = `${char.name} 方形头像`;
        img.decoding = 'async';
        img.onerror = () => { cover.style.display = 'none'; };
        const overlay = node('div', 'gemma-char-cover-overlay');
        cover.append(img, overlay);
        card.append(cover);
      }

      const body = node('div', 'gemma-char-content');
      const header = node('div', 'gemma-char-header');
      if (!avatarUrl) {
        const avatar = createAvatarNode(char, 'gemma-char-avatar');
        header.append(avatar);
      }

      const meta = node('div', 'gemma-char-meta');
      const titleRow = node('div', 'gemma-char-title-row');
      const name = node('span', 'gemma-char-name', char.name);
      const tag = node('span', 'gemma-char-tag', char.tag || '');
      titleRow.append(name, tag);
      meta.append(titleRow);

      const check = node('div', 'gemma-char-indicator');
      check.innerHTML = `<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2.5"><polyline points="20 6 9 17 4 12"/></svg>`;

      header.append(meta, check);
      const desc = node('p', 'gemma-char-desc', char.desc);
      body.append(header, desc);

      // Edit button for character (both builtin and custom)
      const editBtn = node('button', 'gemma-char-edit-btn');
      editBtn.type = 'button';
      editBtn.title = `编辑「${char.name}」的提示词、头像与人设`;
      editBtn.setAttribute('aria-label', `编辑角色：${char.name}`);
      editBtn.innerHTML = `<svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 20h9"/><path d="M16.5 3.5a2.121 2.121 0 0 1 3 3L7 19l-4 1 1-4L16.5 3.5z"/></svg>`;
      editBtn.onclick = e => {
        e.stopPropagation();
        openCharModal(char);
      };
      card.append(editBtn);

      card.append(body);

      card.onclick = () => {
        setActiveCharacter(char.id, chatId);
        prompt.placeholder = char.placeholder || `向 ${char.name} 提问...`;
        prompt.focus();
      };

      cards.push(card);
    });

    grid.replaceChildren(...cards);
  }

  try {
    const saved = localStorage.getItem('lstudio-gemma-chat');
    if (/^[a-f0-9]{32}$/.test(saved || '')) chatId = saved;
    const variant = localStorage.getItem('lstudio-gemma-variant');
    const radio = [...root.querySelectorAll('[name="gemmaVariant"]')].find(input => input.value === variant);
    if (radio) radio.checked = true;
    queueMicrotask(() => updateVariantGpuRecommendation());
  } catch (_) {}

  function node(tag, className, text) {
    const el = document.createElement(tag);
    el.className = className;
    if (text) el.textContent = text;
    return el;
  }

  function escapeHtml(str) {
    return String(str || '')
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
  }

  function copyTextToClipboard(text, btn) {
    if (!navigator.clipboard?.writeText) return;
    navigator.clipboard.writeText(text).then(() => {
      const origHtml = btn.innerHTML;
      btn.classList.add('copied');
      btn.innerHTML = `<svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="2.5"><polyline points="20 6 9 17 4 12"/></svg><span>已复制</span>`;
      setTimeout(() => {
        btn.classList.remove('copied');
        btn.innerHTML = origHtml;
      }, 2000);
    }).catch(() => {});
  }

  function renderMarkdown(rawText, isRunning, charName = '') {
    if (!rawText) {
      if (isRunning) {
        return `<div class="gemma-thinking-wrap" role="status" aria-label="${escapeHtml(charName || '角色')}正在思考">
          <div class="gemma-thinking-dots">
            <span class="gemma-thinking-dot"></span>
            <span class="gemma-thinking-dot"></span>
            <span class="gemma-thinking-dot"></span>
          </div>
          <span class="gemma-thinking-label">${escapeHtml(charName || '角色')} 正在思考…</span>
        </div>`;
      }
      return '';
    }
    let text = rawText;
    const blocks = [];

    // Fenced code blocks
    const codeBlockRegex = /```([a-zA-Z0-9_\-\+]*)\n?([\s\S]*?)```/g;
    text = text.replace(codeBlockRegex, (_, lang, code) => {
      const id = blocks.length;
      blocks.push({lang: (lang || 'code').trim(), code});
      return `\x00CODE_${id}\x00`;
    });

    // Handle unclosed code block during streaming
    const unclosedMatch = text.match(/```([a-zA-Z0-9_\-\+]*)\n?([\s\S]*)$/);
    if (unclosedMatch) {
      const id = blocks.length;
      blocks.push({lang: (unclosedMatch[1] || 'code').trim(), code: unclosedMatch[2]});
      text = text.slice(0, unclosedMatch.index) + `\x00CODE_${id}\x00`;
    }

    let html = escapeHtml(text);

    // Inline code `code`
    html = html.replace(/`([^`\n]+)`/g, '<code class="gemma-inline-code">$1</code>');

    // Bold & Italic
    html = html.replace(/\*\*\*([^*]+)\*\*\*/g, '<strong><em>$1</em></strong>');
    html = html.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
    html = html.replace(/\*([^*]+)\*/g, '<em>$1</em>');

    // Headers
    html = html.replace(/^#### (.*$)/gm, '<h4>$1</h4>');
    html = html.replace(/^### (.*$)/gm, '<h3>$1</h3>');
    html = html.replace(/^## (.*$)/gm, '<h2>$1</h2>');
    html = html.replace(/^# (.*$)/gm, '<h1>$1</h1>');

    // Blockquotes
    html = html.replace(/^> (.*$)/gm, '<blockquote>$1</blockquote>');

    // Horizontal rules
    html = html.replace(/^---$/gm, '<hr style="border:0;border-top:1px solid rgba(255,255,255,0.08);margin:1rem 0;">');

    // Lists
    html = html.replace(/^[\*\-] (.*$)/gm, '<li>$1</li>');
    html = html.replace(/^\d+\. (.*$)/gm, '<li>$1</li>');
    html = html.replace(/((?:<li>[\s\S]*?<\/li>\n?)+)/g, '<ul>$1</ul>');

    // Tables
    html = html.replace(/((?:\|[^\n]+\|\r?\n)+)/g, tableText => {
      const rows = tableText.trim().split('\n').map(r => r.trim()).filter(Boolean);
      if (rows.length < 2 || !rows[1].includes('---')) return tableText;
      const parseRow = (r, tag) => {
        const cells = r.split('|').slice(1, -1);
        return '<tr>' + cells.map(c => `<${tag}>${c.trim()}</${tag}>`).join('') + '</tr>';
      };
      const headerHtml = `<thead>${parseRow(rows[0], 'th')}</thead>`;
      const bodyHtml = `<tbody>${rows.slice(2).map(r => parseRow(r, 'td')).join('')}</tbody>`;
      return `<table>${headerHtml}${bodyHtml}</table>`;
    });

    // Paragraphs
    const paras = html.split(/\n{2,}/).map(p => {
      const trimmed = p.trim();
      if (!trimmed) return '';
      if (/^<(h\d|ul|ol|table|blockquote|hr|\x00CODE_)/.test(trimmed)) return trimmed;
      return `<p>${trimmed.replace(/\n/g, '<br>')}</p>`;
    }).filter(Boolean);
    html = paras.join('\n');

    // Restore Code Blocks
    html = html.replace(/\x00CODE_(\d+)\x00/g, (_, id) => {
      const block = blocks[Number(id)];
      if (!block) return '';
      const escaped = escapeHtml(block.code);
      let b64 = '';
      try {
        b64 = btoa(unescape(encodeURIComponent(block.code)));
      } catch (_) {}
      return `<div class="gemma-code-block">
        <div class="gemma-code-header">
          <span class="gemma-code-lang">${escapeHtml(block.lang)}</span>
          <button type="button" class="gemma-code-copy-btn" data-code="${b64}">
            <svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="2"><rect x="9" y="9" width="13" height="13" rx="2" ry="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>
            <span>复制代码</span>
          </button>
        </div>
        <pre><code>${escaped}</code></pre>
      </div>`;
    });

    if (isRunning) {
      if (html.endsWith('</p>')) {
        html = html.slice(0, -4) + '<span class="gemma-typing-cursor"></span></p>';
      } else {
        html += '<span class="gemma-typing-cursor"></span>';
      }
    }
    return html;
  }

  function setCurrentChat(id) {
    chatId = id;
    prompt.value = drafts.get(id) || '';
    try { localStorage.setItem('lstudio-gemma-chat', id); } catch (_) {}
    const activeChar = getActiveCharacter(id);
    if (!prompt.value) prompt.placeholder = activeChar.placeholder;
    renderCharacterGrid();
    updateComposer();
  }

  function selectChat(id, focus = true) {
    closeChatMenu();
    drafts.set(chatId, prompt.value);
    setCurrentChat(id);
    $('gemmaHistoryToggle')?.setAttribute('aria-expanded', 'false');
    $('gemmaChatList')?.parentElement.classList.remove('gemma-history-open');
    actionError = '';
    currentRenderedChatId = null;
    $('gemmaMessages').replaceChildren();
    if (state) state = {...state, turns: []};
    render(true);
    refresh();
    if (focus) prompt.focus();
  }

  function closeChatMenu() {
    if (chatMenu?.matches(':popover-open')) chatMenu.hidePopover();
  }

  function toggleChatMenu(chat, trigger) {
    const same = menuTarget?.trigger === trigger;
    closeChatMenu();
    if (same) return;
    menuTarget = {chat, trigger};
    trigger.setAttribute('aria-expanded', 'true');
    removeChat.disabled = !chat.can_remove;
    removeChat.title = chat.can_remove ? '' : '请先停止回复';
    chatMenu.showPopover();
    const bounds = trigger.getBoundingClientRect();
    const width = chatMenu.offsetWidth;
    const height = chatMenu.offsetHeight;
    const below = bounds.bottom + 4;
    chatMenu.style.left = `${Math.max(8, Math.min(bounds.right - width, innerWidth - width - 8))}px`;
    chatMenu.style.top = `${below + height <= innerHeight - 8 ? below : Math.max(8, bounds.top - height - 4)}px`;
    if (!removeChat.disabled) removeChat.focus({preventScroll: true});
  }

  function renderChats(chats) {
    const countBadge = $('gemmaHistoryCount');
    if (countBadge) countBadge.textContent = String(chats.length);
    const query = ($('gemmaChatSearch')?.value || '').trim().toLowerCase();
    const filtered = query ? chats.filter(c => c.title.toLowerCase().includes(query)) : chats;

    const signature = JSON.stringify([chatId, query, filtered.map(chat => [chat.id, chat.title, chat.can_remove])]);
    if (signature === listSignature) return;
    listSignature = signature;
    closeChatMenu();

    const list = $('gemmaChatList');
    if (!filtered.length) {
      const emptyNote = node('div', 'p-3 text-center text-xs opacity-50', query ? '未找到匹配对话' : '暂无对话');
      list.replaceChildren(emptyNote);
      return;
    }

    list.replaceChildren(...filtered.map(chat => {
      const row = node('div', 'gemma-chat-row');
      const button = node('button', 'btn btn-sm btn-ghost gemma-chat-select');
      button.type = 'button';
      button.title = chat.title;
      if (chat.id === chatId) button.setAttribute('aria-current', 'true');

      const char = getCharacterByIdOrName(chat.character_id, chat.character_name);
      let icon;
      if (char) {
        icon = createAvatarNode(char, 'gemma-chat-avatar-icon flex-none');
      } else {
        icon = node('span', 'gemma-chat-avatar-icon flex-none');
        icon.innerHTML = `<svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg>`;
      }
      button.append(icon, node('span', '', chat.title));
      button.onclick = () => selectChat(chat.id);

      const more = node('button', 'btn btn-xs btn-ghost gemma-chat-more', '⋯');
      more.type = 'button';
      more.title = '聊天选项';
      more.setAttribute('aria-label', `聊天选项：${chat.title}`);
      more.setAttribute('aria-haspopup', 'menu');
      more.setAttribute('aria-expanded', 'false');
      more.setAttribute('popovertarget', chatMenu.id);
      more.onclick = event => {
        event.preventDefault();
        toggleChatMenu(chat, more);
      };
      row.append(button, more);
      return row;
    }));
  }

  let currentRenderedChatId = null;

  function renderTurns(turns, jump) {
    const scroll = $('gemmaScroll');
    const follow = jump || scroll.scrollHeight - scroll.scrollTop - scroll.clientHeight < 120;
    $('gemmaEmpty').hidden = turns.length > 0;

    const container = $('gemmaMessages');
    if (currentRenderedChatId !== chatId) {
      currentRenderedChatId = chatId;
      container.replaceChildren();
    }

    const existingArticles = new Map();
    for (const child of Array.from(container.children)) {
      if (child.dataset?.turnId) {
        existingArticles.set(child.dataset.turnId, child);
      }
    }

    const turnIds = new Set(turns.map(t => String(t.id)));

    // Clean up removed turns
    for (const [id, el] of existingArticles) {
      if (!turnIds.has(id)) {
        const isTemp = id.startsWith('temp_');
        const hasMatch = isTemp && turns.some(t => t.prompt === el.dataset.prompt);
        if (!hasMatch) {
          el.remove();
          existingArticles.delete(id);
        }
      }
    }

    turns.forEach((turn, index) => {
      const turnId = String(turn.id);
      let article = existingArticles.get(turnId);

      // Match temp turn to confirmed backend turn by prompt or turnId
      if (!article) {
        for (const [oldId, el] of existingArticles) {
          if (oldId.startsWith('temp_') && (el.dataset.prompt === turn.prompt || el.dataset.turnId === turnId)) {
            article = el;
            article.dataset.turnId = turnId;
            existingArticles.delete(oldId);
            existingArticles.set(turnId, el);
            break;
          }
        }
      }

      const char = getCharacterByIdOrName(turn.character_id, turn.character_name) || getActiveCharacter(chatId);
      const text = turn.reply || turn.draft || '';
      const unfinished = ['cancelled', 'error', 'interrupted', 'discarded'].includes(turn.status);
      const isRunning = turn.status === 'running' || turn.status === 'queued';

      if (!article) {
        article = node('article', 'gemma-turn');
        article.dataset.turnId = turnId;
        article.dataset.prompt = turn.prompt || '';
        article.dataset.charId = char.id;

        const user = node('div', 'gemma-user');
        if (Array.isArray(turn.images) && turn.images.length > 0) {
          const gallery = node('div', 'gemma-user-images');
          for (const imgUrl of turn.images) {
            const img = node('img', 'gemma-user-image');
            img.src = imgUrl;
            img.alt = '用户上传图片';
            img.loading = 'lazy';
            img.onclick = () => window.open(imgUrl, '_blank');
            gallery.append(img);
          }
          user.append(gallery);
        }
        if (turn.prompt) {
          const textEl = node('div', 'gemma-user-text', turn.prompt);
          user.append(textEl);
        }
        user.setAttribute('aria-label', '你');
        article.append(user);

        const reply = node('div', 'gemma-reply');
        const header = node('div', 'gemma-reply-header');
        const avatar = createAvatarNode(char, 'gemma-reply-avatar');
        const meta = node('span', 'gemma-reply-meta');
        const label = node('strong', 'gemma-reply-label', char.name);
        meta.append(label);
        header.append(avatar, meta);

        const pill = node('span', 'gemma-metrics-pill');
        pill.hidden = true;
        header.append(pill);
        reply.append(header);

        const body = node('div', 'gemma-markdown-body');
        reply.append(body);

        const actions = node('div', 'gemma-reply-actions');
        actions.hidden = true;
        const copyBtn = node('button', 'gemma-action-btn gemma-reply-copy-btn');
        copyBtn.type = 'button';
        copyBtn.innerHTML = `<svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="2"><rect x="9" y="9" width="13" height="13" rx="2" ry="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg><span>复制回答</span>`;
        copyBtn.onclick = () => {
          const curText = article.dataset.text || '';
          copyTextToClipboard(curText, copyBtn);
        };
        actions.append(copyBtn);
        reply.append(actions);

        const note = node('div', 'gemma-reply-note');
        note.hidden = true;
        reply.append(note);

        article.append(reply);

        const refNode = container.children[index] || null;
        container.insertBefore(article, refNode);
        existingArticles.set(turnId, article);
      }

      // Update in-place only if text or status changed
      if (article.dataset.text !== text || article.dataset.status !== turn.status) {
        article.dataset.text = text;
        article.dataset.status = turn.status;

        const body = article.querySelector('.gemma-markdown-body');
        if (body) {
          body.innerHTML = renderMarkdown(text, isRunning, char.name);
        }

        const actions = article.querySelector('.gemma-reply-actions');
        if (actions) {
          actions.hidden = !text || isRunning;
        }

        const note = article.querySelector('.gemma-reply-note');
        if (note) {
          if (unfinished && !turn.reply) {
            note.hidden = false;
            note.replaceChildren();
            let noteText = '这条消息未完成';
            if (turn.status === 'cancelled') {
              noteText = '已停止回复';
            } else if (turn.message) {
              noteText = turn.message;
            }
            note.append(node('span', '', noteText));
            if (!turn.can_stop) {
              const retry = node('button', 'btn btn-xs btn-ghost text-primary', '重新编辑');
              retry.type = 'button';
              retry.onclick = () => { prompt.value = turn.prompt; prompt.focus(); updateComposer(); };
              note.append(retry);
            }
          } else {
            note.hidden = true;
          }
        }
      }

      // Keep character identity synced only if character changed
      if (article.dataset.charId !== char.id) {
        article.dataset.charId = char.id;
        const avatarEl = article.querySelector('.gemma-reply-avatar');
        if (avatarEl) {
          updateAvatarNode(avatarEl, char);
        }
        const labelEl = article.querySelector('.gemma-reply-label');
        if (labelEl && labelEl.textContent !== char.name) {
          labelEl.textContent = char.name;
        }
      }

      const pill = article.querySelector('.gemma-metrics-pill');
      if (pill) {
        if (turn.metrics?.tokens_per_second) {
          const speed = turn.metrics.tokens_per_second.toFixed(1);
          pill.textContent = `⚡ ${speed} tok/s`;
          pill.hidden = false;
        } else {
          pill.hidden = true;
        }
      }
    });

    if (follow) scroll.scrollTop = scroll.scrollHeight;
  }

  function currentTurn() {
    if (activeStreamAbort) return {status: 'running', can_stop: true};
    return state?.turns.find(turn => pendingStates.has(turn.status) || turn.can_stop);
  }

  function updateComposer() {
    const turn = currentTurn();
    const disabled = state?.state !== 'ready' || !!state.close_error || action === 'close';
    const locked = !!turn || action === 'send';
    const isMultimodal = state?.multimodal !== false;

    if (!isMultimodal && pendingImages.length > 0) {
      pendingImages = [];
      updateImagePreviews();
    }

    const hasPrompt = !!prompt.value.trim();
    const hasImages = isMultimodal && Array.isArray(pendingImages) && pendingImages.length > 0;
    $('gemmaSend').disabled = disabled || locked || (!hasPrompt && !hasImages);
    $('gemmaStop').hidden = !turn;
    $('gemmaStop').disabled = disabled || turn?.status === 'cancelling';
    prompt.disabled = disabled;

    const attachBtn = $('gemmaAttachBtn');
    if (attachBtn) {
      attachBtn.hidden = !isMultimodal;
      attachBtn.disabled = disabled || locked || !isMultimodal;
      attachBtn.title = isMultimodal
        ? '上传图片或截图 (支持粘贴、拖拽、选择图片)'
        : '当前模型仅支持纯文本，不支持图片输入';
    }

    // Auto-grow height
    prompt.style.height = 'auto';
    prompt.style.height = `${Math.min(Math.max(prompt.scrollHeight, 40), 180)}px`;

    const charCount = $('gemmaCharCount');
    if (charCount) charCount.textContent = `${prompt.value.length} / 12,000`;
    const clearBtn = $('gemmaClearPrompt');
    if (clearBtn) clearBtn.hidden = !prompt.value;

    if (!prompt.value) {
      const activeChar = getActiveCharacter(chatId);
      prompt.placeholder = activeChar.placeholder;
    }
  }

  function updateLoadingSteps(message, events) {
    const text = ((message || '') + ' ' + (events?.map(e => e.message).join(' ') || '')).toLowerCase();
    const stepGpu = $('gemmaStepGpu');
    const stepTunnel = $('gemmaStepTunnel');
    const stepWeights = $('gemmaStepWeights');
    const stepReady = $('gemmaStepReady');
    if (!stepGpu) return;

    stepGpu.classList.add('active');
    stepTunnel?.classList.toggle('active', text.includes('隧道') || text.includes('cloudflare') || text.includes('端口') || text.includes('模型') || text.includes('权重'));
    stepWeights?.classList.toggle('active', text.includes('下载') || text.includes('加载') || text.includes('权重') || text.includes('准备') || text.includes('18829'));
    stepReady?.classList.toggle('active', text.includes('就绪') || text.includes('成功') || text.includes('完成'));
  }

  function render(jump = false) {
    const status = action === 'open' ? 'loading' : action === 'close' ? 'closing' : state?.state;
    const ready = status === 'ready';
    $('gemmaSetup').hidden = !status || ready || status === 'closing' || status === 'loading';
    $('gemmaLoading').hidden = status !== 'loading' && status !== 'closing';
    $('gemmaChat').hidden = !ready;
    $('gemmaClose').hidden = !ready && status !== 'loading' && status !== 'closing' &&
      !state?.has_runtime && status !== 'error';
    $('gemmaClose').disabled = status === 'closing' || !!action || !!activeStreamAbort;
    $('gemmaOpen').disabled = !!action || !!state?.close_error;
    $('gemmaLoadingTitle').textContent = status === 'closing' ? '正在关闭应用' : '正在开启应用';

    const loadingNote = status === 'closing' ? '正在停止大模型任务并检查显卡' :
      state?.position ? `等待中 · 队列第 ${state.position} 位` :
        (action === 'open' ? '正在提交部署请求' : state?.message || '正在读取部署状态');
    $('gemmaLoadingStatus').textContent = loadingNote;

    const MODEL_NAMES = {
      native: 'Gemma 4 31B 原生版',
      heretic: 'Gemma 4 31B 破甲版',
      gpt_oss_120b: 'GPT-OSS 120B',
    };
    const currentModel = MODEL_NAMES[state?.variant] || '大模型对话';
    const isMultimodal = state?.multimodal !== false;
    const modeTag = isMultimodal ? '多模态图文' : '纯文本';
    if ($('gemmaTitle')) {
      $('gemmaTitle').textContent = ready ? currentModel : '大模型对话';
    }
    if ($('gemmaComposerHint')) {
      $('gemmaComposerHint').textContent = ready
        ? `${currentModel} (${modeTag}) · Enter 发送 · Shift + Enter 换行`
        : 'Enter 发送 · Shift + Enter 换行';
    }

    const logs = (status === 'loading' || status === 'error' || status === 'closing') ? state?.events || [] : [];
    updateLoadingSteps(loadingNote, logs);

    const nextLogSignature = JSON.stringify(logs);
    if (nextLogSignature !== logSignature) {
      logSignature = nextLogSignature;
      $('gemmaLoadLog').replaceChildren(...logs.map(entry => {
        const item = node('li', 'gemma-log-item');
        const time = node('time', 'gemma-log-time', entry.time);
        item.append(time, document.createTextNode(entry.message));
        return item;
      }));
      const logBox = $('gemmaLoadLog');
      logBox.scrollTop = logBox.scrollHeight;
    }

    const activeChar = getActiveCharacter(chatId);
    if (ready) {
      const avatarUrl = getCharAvatar(activeChar);
      $('gemmaSubtitle').innerHTML = `<span class="gemma-subtitle-badge"><img src="${escapeHtml(avatarUrl)}" class="gemma-subtitle-avatar" alt=""><span>${escapeHtml(activeChar.name)}</span></span>`;
    } else {
      $('gemmaSubtitle').textContent = 'Colab · 文本聊天';
    }

    // Update Status Pill in Header
    const statusBadge = $('gemmaStatusBadge');
    const statusText = $('gemmaStatusText');
    if (statusBadge && statusText) {
      statusBadge.className = 'gemma-status-pill ' + (
        ready ? 'status-ready' :
        (status === 'loading' || status === 'closing') ? 'status-loading' :
        status === 'error' ? 'status-error' : 'status-closed'
      );
      statusText.textContent = (
        ready ? `就绪 · ${state?.gpu || 'L4'}` :
        status === 'loading' ? '正在部署…' :
        status === 'closing' ? '正在关闭…' :
        status === 'error' ? '连接异常' : '未连接'
      );
    }

    // When session is active or starting, clear dismissed notice so future session ends can alert again
    if (status === 'loading' || status === 'ready') {
      if (dismissedNotice) {
        dismissedNotice = '';
        try { sessionStorage.removeItem('lstudio-gemma-dismissed-notice'); } catch (_) {}
      }
    }

    const rawMessage = actionError || loadError || state?.close_error || (status === 'error' ? state.message : '');
    let displayMessage = rawMessage;
    if (rawMessage && (rawMessage.includes('503') || rawMessage.includes('暂不可用') || rawMessage.includes('切换显卡')) && !rawMessage.includes('重新开启')) {
      displayMessage = `${rawMessage}。建议在下方切换显卡（推荐 L4）后重新点击「开启应用」。`;
    }
    // A session that ended on its own (idle, runtime lost, restart) is information, not an error.
    const notice = !displayMessage && status === 'closed' ? state?.notice || '' : '';

    const isError = !!displayMessage;
    const isNotice = !displayMessage && !!notice;
    const shouldShowError = isError && displayMessage !== dismissedError;
    const shouldShowNotice = isNotice && notice !== dismissedNotice;

    const banner = $('gemmaError');
    const textEl = $('gemmaErrorText');
    const iconEl = $('gemmaErrorIcon');

    if (banner) {
      if (shouldShowError || shouldShowNotice) {
        const msg = shouldShowError ? displayMessage : notice;
        if (textEl) textEl.textContent = msg;
        else banner.textContent = msg;

        banner.classList.toggle('alert-error', shouldShowError);
        banner.classList.toggle('alert-info', shouldShowNotice);
        banner.classList.remove('gemma-error-fading');
        banner.hidden = false;

        if (iconEl) {
          if (shouldShowNotice) {
            iconEl.innerHTML = '<circle cx="12" cy="12" r="10"></circle><line x1="12" y1="16" x2="12" y2="12"></line><line x1="12" y1="8" x2="12.01" y2="8"></line>';
          } else {
            iconEl.innerHTML = '<circle cx="12" cy="12" r="10"></circle><line x1="12" y1="8" x2="12" y2="12"></line><line x1="12" y1="16" x2="12.01" y2="16"></line>';
          }
        }

        if (shouldShowNotice) {
          if (!noticeDismissTimer) {
            noticeDismissTimer = setTimeout(() => {
              noticeDismissTimer = null;
              dismissedNotice = notice;
              try { sessionStorage.setItem('lstudio-gemma-dismissed-notice', notice); } catch (_) {}
              dismissBanner();
            }, 6000);
          }
        } else if (noticeDismissTimer) {
          clearTimeout(noticeDismissTimer);
          noticeDismissTimer = null;
        }
      } else {
        if (!banner.classList.contains('gemma-error-fading')) {
          banner.hidden = true;
        }
      }
    }

    const queueHint = $('gemmaQueueHint');
    if (queueHint) {
      const waiting = ready ? state?.waiting_others || 0 : 0;
      const deadline = ready && waiting ? state?.idle_deadline : null;
      const idleText = deadline ? `，闲置至 ${new Date(deadline * 1000).toLocaleTimeString()} 将自动结束会话` : '';
      queueHint.hidden = !waiting;
      queueHint.textContent = waiting
        ? `聊天正在独占显卡，另有 ${waiting} 个其他应用的任务在排队；用完请点「关闭应用」让它们继续${idleText}。`
        : '';
    }

    if (ready) {
      renderCharacterGrid();
      renderChats(state.chats);
      renderTurns(state.turns, jump);
    }
    updateComposer();
  }

  async function refresh() {
    const stamp = ++request;
    fetching = true;
    try {
      const next = await api('gemma?chat_id=' + chatId);
      if (stamp !== request) return;
      if (next.characters) syncCharacters(next.characters);
      if (state?.chats.some(chat => chat.id === chatId) && !next.chats.some(chat => chat.id === chatId)) {
        drafts.delete(chatId);
        setCurrentChat(newId());
      }
      const opened = state?.state === 'loading' && next.state === 'ready';
      state = next;
      loadError = '';
      render(opened);
      if (opened && !root.hidden && $('gemmaDebugPanel').hidden) prompt.focus();
    } catch (error) {
      if (stamp !== request) return;
      loadError = error.message;
      render();
    } finally {
      if (stamp === request) fetching = false;
    }
  }

  async function perform(name, operation) {
    if (action) return;
    action = name;
    actionError = '';
    dismissedError = '';
    render();
    try {
      await operation();
      await refresh();
    } catch (error) {
      actionError = error.message;
    } finally {
      action = null;
      render();
    }
  }

  async function streamSend(currentChatId, value, existing, images = []) {
    if (activeStreamAbort || action) return;
    const isMultimodal = state?.multimodal !== false;
    if (!isMultimodal && Array.isArray(images) && images.length > 0) {
      actionError = '当前模型仅支持纯文本对话，不支持图片输入。如需图文对话请切换至 Gemma 4 模型。';
      render();
      return;
    }
    action = 'send';
    actionError = '';
    dismissedError = '';
    activeStreamAbort = new AbortController();

    const activeChar = getActiveCharacter(currentChatId);
    const tempTurn = {
      id: 'temp_' + Date.now(),
      prompt: value,
      images: images,
      variant: state?.variant || 'native',
      status: 'running',
      stage: 'chat',
      message: `${activeChar.name} 正在回复`,
      reply: null,
      draft: '',
      can_stop: true,
      metrics: null,
      character_id: activeChar.id,
      character_name: activeChar.name,
    };
    if (state && Array.isArray(state.turns)) {
      state.turns.push(tempTurn);
    }
    updateComposer();
    render(true);

    try {
      const localKey = (typeof KEY !== 'undefined' ? KEY : $('apiKey')?.value) || '';
      const resp = await fetch(`/api/gemma/chats/${currentChatId}/stream`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'X-LStudio-Key': localKey,
        },
        body: JSON.stringify({
          prompt: value,
          images: images,
          existing_chat: existing,
          character_id: activeChar.id,
          character_name: activeChar.name,
          system_prompt: activeChar.system_prompt,
        }),
        signal: activeStreamAbort.signal,
      });

      if (!resp.ok) {
        const err = await resp.json().catch(() => ({}));
        throw new Error(err.detail || '发送消息失败');
      }

      drafts.delete(currentChatId);
      if (chatId === currentChatId && prompt.value.trim() === value) {
        prompt.value = '';
        updateComposer();
      }

      const reader = resp.body.getReader();
      const decoder = new TextDecoder('utf-8');
      let buffer = '';
      let draftText = '';
      let streamRaf = null;

      function scheduleStreamRender() {
        if (streamRaf) return;
        streamRaf = requestAnimationFrame(() => {
          streamRaf = null;
          renderTurns(state.turns, false);
        });
      }

      while (true) {
        const {value: chunk, done} = await reader.read();
        if (done) break;
        buffer += decoder.decode(chunk, {stream: true});
        const blocks = buffer.split('\n\n');
        buffer = blocks.pop();
        for (const block of blocks) {
          for (const line of block.split('\n')) {
            if (line.startsWith('data: ')) {
              const dataStr = line.slice(6).trim();
              if (dataStr === '[DONE]') break;
              try {
                const parsed = JSON.parse(dataStr);
                if (parsed.error) {
                  throw new Error(parsed.error.message || '模型回复失败');
                }
                const delta = parsed.choices?.[0]?.delta?.content;
                if (delta) {
                  draftText += delta;
                  tempTurn.draft = draftText;
                  scheduleStreamRender();
                }
                if (parsed.timings?.predicted_per_second) {
                  tempTurn.metrics = {tokens_per_second: parsed.timings.predicted_per_second};
                }
              } catch (e) {
                if (e.message && !e.message.includes('JSON')) {
                  throw e;
                }
              }
            }
          }
        }
      }
    } catch (err) {
      if (err.name === 'AbortError') {
        tempTurn.status = 'cancelled';
        tempTurn.message = '已停止回复';
      } else {
        actionError = err.message;
        tempTurn.status = 'error';
        tempTurn.message = err.message;
      }
    } finally {
      activeStreamAbort = null;
      action = null;
      renderTurns(state.turns, false);
      await refresh();
    }
  }

  // Code block copy delegation
  root.addEventListener('click', event => {
    const codeBtn = event.target.closest('.gemma-code-copy-btn');
    if (codeBtn) {
      event.preventDefault();
      const b64 = codeBtn.getAttribute('data-code');
      if (b64) {
        try {
          const code = decodeURIComponent(escape(atob(b64)));
          copyTextToClipboard(code, codeBtn);
        } catch (_) {}
      }
    }
  });


  // Clear Prompt Button
  if ($('gemmaClearPrompt')) {
    $('gemmaClearPrompt').onclick = () => {
      prompt.value = '';
      prompt.focus();
      updateComposer();
    };
  }

  // Sidebar Desktop Toggle
  const sidebarBtn = $('gemmaSidebarToggle');
  if (sidebarBtn) {
    sidebarBtn.onclick = () => {
      const chat = $('gemmaChat');
      const collapsed = chat.classList.toggle('gemma-sidebar-collapsed');
      sidebarBtn.setAttribute('aria-expanded', String(!collapsed));
    };
  }

  // Header New Chat
  const headerNewBtn = $('gemmaHeaderNew');
  if (headerNewBtn) {
    headerNewBtn.onclick = () => selectChat(newId());
  }

  // Chat Filter Search
  const searchInput = $('gemmaChatSearch');
  if (searchInput) {
    searchInput.oninput = () => {
      if (state?.chats) renderChats(state.chats);
    };
  }

  function updateVariantGpuRecommendation() {
    const variant = root.querySelector('[name="gemmaVariant"]:checked')?.value || 'native';
    const gpuSelect = $('gemmaGpu');
    if (!gpuSelect) return;
    if (variant === 'gpt_oss_120b') {
      if (gpuSelect.value === 'L4' || gpuSelect.value === 'T4') {
        gpuSelect.value = 'A100';
        gpuSelect.dispatchEvent(new Event('change'));
      }
    }
    window.Workspace?.syncGpuSelects?.();
  }

  root.querySelectorAll('[name="gemmaVariant"]').forEach(input => {
    input.addEventListener('change', () => {
      try { localStorage.setItem('lstudio-gemma-variant', input.value); } catch (_) {}
      updateVariantGpuRecommendation();
    });
  });

  $('gemmaOpenForm').onsubmit = async event => {
    event.preventDefault();
    const variant = root.querySelector('[name="gemmaVariant"]:checked').value;
    const gpu = $('gemmaGpu').value;
    if (variant === 'gpt_oss_120b' && (gpu === 'L4' || gpu === 'T4')) {
      actionError = 'GPT-OSS 120B 模型（~63GB 权重）需要至少 80GB 显存，不支持 L4 (24GB) / T4 (16GB)，请在下方切换至 A100 (80GB) 或 H100！';
      render();
      return;
    }
    try { localStorage.setItem('lstudio-gemma-variant', variant); } catch (_) {}
    const selected = $('endpoint')?.selectedOptions[0];
    const endpoint = selected?.value && (selected.dataset.targetGpu || $('gpu').value) === gpu ? selected.value : null;
    await perform('open', async () => {
      const placement = await window.Workspace.chooseQueuePlacement({targetAppId: 'gemma_chat', targetGpu: gpu});
      if (placement) await api('gemma/open?placement=' + placement, {
        method: 'POST', body: JSON.stringify({variant, gpu, endpoint})
      });
    });
  };

  function requestCloseApp() {
    const waiting = state?.waiting_others || 0;
    const isRunning = state?.state === 'loading';

    let msg = '关闭应用将释放 Colab 显卡并退出当前模型部署。如需继续聊天需要重新开启。确认关闭吗？';
    if (isRunning) {
      msg = '当前大模型正在加载或排队中。确认停止开启并关闭应用吗？';
    } else if (waiting > 0) {
      msg = `当前有 ${waiting} 个其他应用的任务正在排队等待显卡。确认关闭应用并将显卡释放给排队任务吗？`;
    }

    const modal = $('gemmaCloseConfirmModal');
    if (modal && typeof modal.showModal === 'function') {
      const textEl = $('gemmaCloseConfirmText');
      if (textEl) textEl.textContent = msg;
      modal.showModal();
    } else {
      if (confirm(msg)) {
        perform('close', () => api('gemma/close', {method: 'POST'}));
      }
    }
  }

  $('gemmaClose').onclick = requestCloseApp;
  $('gemmaCloseConfirmCancel')?.addEventListener('click', () => {
    const modal = $('gemmaCloseConfirmModal');
    if (modal && typeof modal.close === 'function') modal.close();
  });
  $('gemmaCloseConfirmProceed')?.addEventListener('click', () => {
    const modal = $('gemmaCloseConfirmModal');
    if (modal && typeof modal.close === 'function') modal.close();
    perform('close', () => api('gemma/close', {method: 'POST'}));
  });
  $('gemmaNew').onclick = () => selectChat(newId());
  $('gemmaHistoryToggle').onclick = () => {
    closeChatMenu();
    const open = $('gemmaChatList').parentElement.classList.toggle('gemma-history-open');
    $('gemmaHistoryToggle').setAttribute('aria-expanded', String(open));
  };
  chatMenu.addEventListener('beforetoggle', event => {
    if (event.newState !== 'closed') return;
    menuTarget?.trigger.setAttribute('aria-expanded', 'false');
    menuTarget = null;
  });
  $('gemmaChatList').parentElement.addEventListener('scroll', closeChatMenu, true);
  window.addEventListener('resize', closeChatMenu);
  removeChat.onclick = async () => {
    const chat = menuTarget?.chat;
    if (!chat?.can_remove) return;
    closeChatMenu();
    const selected = chat.id === chatId;
    await perform('remove', async () => {
      await api('gemma/chats/' + chat.id, {method: 'DELETE'});
      drafts.delete(chat.id);
      if (chatId === chat.id) setCurrentChat(newId());
    });
    if (selected && chatId !== chat.id && !root.hidden) prompt.focus();
  };
  $('gemmaStop').onclick = () => {
    if (activeStreamAbort) {
      activeStreamAbort.abort();
      activeStreamAbort = null;
      updateComposer();
      return;
    }
    const turn = currentTurn();
    if (turn) perform('stop', () => api(`jobs/${turn.id}/cancel`, {method: 'POST'}));
  };
  let pendingImages = [];

  function updateImagePreviews() {
    const wrap = $('gemmaImagePreviewWrap');
    if (!wrap) return;
    if (!pendingImages.length) {
      wrap.hidden = true;
      wrap.replaceChildren();
      updateComposer();
      return;
    }
    wrap.hidden = false;
    wrap.replaceChildren();
    pendingImages.forEach((imgData, idx) => {
      const item = node('div', 'gemma-img-preview-item');
      const img = node('img');
      img.src = imgData;
      img.alt = '待发送图片';
      const removeBtn = node('button', 'gemma-img-preview-del', '✕');
      removeBtn.type = 'button';
      removeBtn.title = '移除图片';
      removeBtn.onclick = (e) => {
        e.stopPropagation();
        pendingImages.splice(idx, 1);
        updateImagePreviews();
      };
      item.append(img, removeBtn);
      wrap.append(item);
    });
    updateComposer();
  }

  function addImagesFromFiles(files) {
    const isMultimodal = state?.multimodal !== false;
    if (!isMultimodal) {
      actionError = '当前模型仅支持纯文本对话，不支持图片输入。如需图文对话请切换至 Gemma 4 模型。';
      render();
      return;
    }
    for (const file of files) {
      if (!file || !file.type.startsWith('image/')) continue;
      if (file.size > 20 * 1024 * 1024) {
        actionError = '单张图片大小不能超过 20MB';
        render();
        continue;
      }
      const reader = new FileReader();
      reader.onload = () => {
        if (reader.result) {
          pendingImages.push(reader.result);
          updateImagePreviews();
        }
      };
      reader.readAsDataURL(file);
    }
  }

  const attachBtn = $('gemmaAttachBtn');
  const fileInput = $('gemmaFileInput');
  if (attachBtn && fileInput) {
    attachBtn.onclick = () => fileInput.click();
    fileInput.onchange = () => {
      if (fileInput.files?.length) {
        addImagesFromFiles(fileInput.files);
        fileInput.value = '';
      }
    };
  }

  const inputWrap = $('gemmaInputWrap');
  if (inputWrap) {
    inputWrap.addEventListener('dragover', e => {
      const isMultimodal = state?.multimodal !== false;
      if (!isMultimodal) return;
      if (e.dataTransfer?.types?.includes('Files')) {
        e.preventDefault();
        inputWrap.classList.add('gemma-dragover');
      }
    });
    inputWrap.addEventListener('dragleave', e => {
      inputWrap.classList.remove('gemma-dragover');
    });
    inputWrap.addEventListener('drop', e => {
      inputWrap.classList.remove('gemma-dragover');
      const files = Array.from(e.dataTransfer?.files || []).filter(f => f.type.startsWith('image/'));
      if (files.length > 0) {
        e.preventDefault();
        const isMultimodal = state?.multimodal !== false;
        if (!isMultimodal) {
          actionError = '当前模型仅支持纯文本对话，不支持图片拖拽输入。如需图文对话请切换至 Gemma 4 模型。';
          render();
          return;
        }
        addImagesFromFiles(files);
      }
    });
  }

  function handlePaste(e) {
    const items = e.clipboardData?.items;
    if (!items) return;
    const imageFiles = [];
    for (const item of items) {
      if (item.type.startsWith('image/')) {
        const file = item.getAsFile();
        if (file) imageFiles.push(file);
      }
    }
    if (imageFiles.length > 0) {
      const isMultimodal = state?.multimodal !== false;
      if (!isMultimodal) {
        e.preventDefault();
        actionError = '当前模型仅支持纯文本对话，不支持粘贴图片。如需图文对话请切换至 Gemma 4 模型。';
        render();
        return;
      }
      e.preventDefault();
      addImagesFromFiles(imageFiles);
    }
  }
  prompt.addEventListener('paste', handlePaste);

  $('gemmaComposer').onsubmit = event => {
    event.preventDefault();
    const value = prompt.value.trim();
    if ((!value && !pendingImages.length) || $('gemmaSend').disabled) return;
    const id = chatId;
    const existing = state?.chats?.some(chat => chat.id === id) || false;
    const imagesToSend = [...pendingImages];
    pendingImages = [];
    updateImagePreviews();
    streamSend(id, value, existing, imagesToSend);
  };
  prompt.addEventListener('input', updateComposer);
  prompt.addEventListener('keydown', event => {
    if (event.key !== 'Enter' || event.shiftKey || event.isComposing || event.keyCode === 229) return;
    event.preventDefault();
    if (!$('gemmaSend').disabled) $('gemmaComposer').requestSubmit();
  });

  // Avatar selection & Interactive Square Cropper
  const avatarFileBtn = $('gemmaSelectAvatarBtn');
  const avatarFileInput = $('gemmaCharAvatarFileInput');
  if (avatarFileBtn && avatarFileInput) {
    avatarFileBtn.onclick = () => {
      avatarFileInput.value = '';
      avatarFileInput.click();
    };
    avatarFileInput.onchange = () => {
      const file = avatarFileInput.files?.[0];
      if (!file) return;
      if (!file.type.startsWith('image/')) {
        alert('请选择有效的图片文件');
        return;
      }
      const reader = new FileReader();
      reader.onload = (e) => {
        const img = new Image();
        img.onload = () => {
          openCropModal(img);
        };
        img.src = e.target.result;
      };
      reader.readAsDataURL(file);
    };
  }

  let cropImg = null;
  let cropScale = 1;
  let cropMinScale = 1;
  let cropX = 0;
  let cropY = 0;
  let isDraggingCrop = false;
  let cropDragStartX = 0;
  let cropDragStartY = 0;
  let cropDragOrigX = 0;
  let cropDragOrigY = 0;

  const CROP_BOX_SIZE = 260;
  const CROP_CANVAS_SIZE = 300;
  const CROP_OFFSET = (CROP_CANVAS_SIZE - CROP_BOX_SIZE) / 2; // 20px

  function clampCrop() {
    if (!cropImg) return;
    const scaledW = cropImg.width * cropScale;
    const scaledH = cropImg.height * cropScale;
    const maxNormX = Math.max(0, (scaledW - CROP_BOX_SIZE) / 2);
    const maxNormY = Math.max(0, (scaledH - CROP_BOX_SIZE) / 2);
    cropX = Math.max(-maxNormX, Math.min(maxNormX, cropX));
    cropY = Math.max(-maxNormY, Math.min(maxNormY, cropY));
  }

  function drawCropStage() {
    const canvas = $('gemmaCropCanvas');
    if (!canvas || !cropImg) return;
    const ctx = canvas.getContext('2d');
    if (!ctx) return;

    ctx.clearRect(0, 0, CROP_CANVAS_SIZE, CROP_CANVAS_SIZE);

    const scaledW = cropImg.width * cropScale;
    const scaledH = cropImg.height * cropScale;
    const centerX = CROP_CANVAS_SIZE / 2;
    const centerY = CROP_CANVAS_SIZE / 2;
    const drawX = centerX + cropX - scaledW / 2;
    const drawY = centerY + cropY - scaledH / 2;

    // 1. Draw scaled image
    ctx.drawImage(cropImg, drawX, drawY, scaledW, scaledH);

    // 2. Draw outer semi-transparent mask around crop window
    ctx.fillStyle = 'rgba(0, 0, 0, 0.58)';
    ctx.fillRect(0, 0, CROP_CANVAS_SIZE, CROP_OFFSET);
    ctx.fillRect(0, CROP_OFFSET + CROP_BOX_SIZE, CROP_CANVAS_SIZE, CROP_OFFSET);
    ctx.fillRect(0, CROP_OFFSET, CROP_OFFSET, CROP_BOX_SIZE);
    ctx.fillRect(CROP_OFFSET + CROP_BOX_SIZE, CROP_OFFSET, CROP_OFFSET, CROP_BOX_SIZE);

    // 3. Draw crop box border
    ctx.strokeStyle = '#38bdf8';
    ctx.lineWidth = 2;
    ctx.strokeRect(CROP_OFFSET, CROP_OFFSET, CROP_BOX_SIZE, CROP_BOX_SIZE);

    // 4. Rule-of-thirds grid
    ctx.strokeStyle = 'rgba(255, 255, 255, 0.22)';
    ctx.lineWidth = 1;
    ctx.beginPath();
    const third = CROP_BOX_SIZE / 3;
    ctx.moveTo(CROP_OFFSET + third, CROP_OFFSET);
    ctx.lineTo(CROP_OFFSET + third, CROP_OFFSET + CROP_BOX_SIZE);
    ctx.moveTo(CROP_OFFSET + third * 2, CROP_OFFSET);
    ctx.lineTo(CROP_OFFSET + third * 2, CROP_OFFSET + CROP_BOX_SIZE);
    ctx.moveTo(CROP_OFFSET, CROP_OFFSET + third);
    ctx.lineTo(CROP_OFFSET + CROP_BOX_SIZE, CROP_OFFSET + third);
    ctx.moveTo(CROP_OFFSET, CROP_OFFSET + third * 2);
    ctx.lineTo(CROP_OFFSET + CROP_BOX_SIZE, CROP_OFFSET + third * 2);
    ctx.stroke();

    // 5. Circular guide outline (for chat avatar preview)
    ctx.save();
    ctx.setLineDash([4, 4]);
    ctx.strokeStyle = 'rgba(255, 255, 255, 0.5)';
    ctx.lineWidth = 1.5;
    ctx.beginPath();
    ctx.arc(centerX, centerY, CROP_BOX_SIZE / 2, 0, Math.PI * 2);
    ctx.stroke();
    ctx.restore();
  }

  function openCropModal(img) {
    cropImg = img;
    cropMinScale = Math.max(CROP_BOX_SIZE / img.width, CROP_BOX_SIZE / img.height);
    cropScale = cropMinScale;
    cropX = 0;
    cropY = 0;

    const slider = $('gemmaCropZoomSlider');
    if (slider) {
      slider.min = '1';
      slider.max = '3';
      slider.step = '0.01';
      slider.value = '1';
    }

    const modal = $('gemmaCropModal');
    if (modal) {
      if (typeof modal.showModal === 'function') modal.showModal();
      else modal.setAttribute('open', '');
    }
    requestAnimationFrame(drawCropStage);
  }

  function closeCropModal() {
    const modal = $('gemmaCropModal');
    if (!modal) return;
    if (typeof modal.close === 'function') modal.close();
    else modal.removeAttribute('open');
    cropImg = null;
  }

  function applyZoomRatio(ratio) {
    if (!cropImg) return;
    const clampedRatio = Math.max(1, Math.min(3, ratio));
    cropScale = cropMinScale * clampedRatio;
    const slider = $('gemmaCropZoomSlider');
    if (slider) slider.value = String(clampedRatio);
    clampCrop();
    drawCropStage();
  }

  const cropCanvas = $('gemmaCropCanvas');
  if (cropCanvas) {
    function startDrag(clientX, clientY) {
      if (!cropImg) return;
      isDraggingCrop = true;
      cropDragStartX = clientX;
      cropDragStartY = clientY;
      cropDragOrigX = cropX;
      cropDragOrigY = cropY;
      cropCanvas.style.cursor = 'grabbing';
    }
    function moveDrag(clientX, clientY) {
      if (!isDraggingCrop || !cropImg) return;
      const dx = clientX - cropDragStartX;
      const dy = clientY - cropDragStartY;
      cropX = cropDragOrigX + dx;
      cropY = cropDragOrigY + dy;
      clampCrop();
      drawCropStage();
    }
    function endDrag() {
      if (!isDraggingCrop) return;
      isDraggingCrop = false;
      cropCanvas.style.cursor = 'grab';
    }

    cropCanvas.onmousedown = (e) => {
      e.preventDefault();
      startDrag(e.clientX, e.clientY);
    };
    window.addEventListener('mousemove', (e) => {
      if (isDraggingCrop) moveDrag(e.clientX, e.clientY);
    });
    window.addEventListener('mouseup', () => {
      if (isDraggingCrop) endDrag();
    });

    cropCanvas.ontouchstart = (e) => {
      if (e.touches.length === 1) {
        e.preventDefault();
        startDrag(e.touches[0].clientX, e.touches[0].clientY);
      }
    };
    cropCanvas.ontouchmove = (e) => {
      if (isDraggingCrop && e.touches.length === 1) {
        e.preventDefault();
        moveDrag(e.touches[0].clientX, e.touches[0].clientY);
      }
    };
    cropCanvas.ontouchend = () => {
      if (isDraggingCrop) endDrag();
    };

    cropCanvas.onwheel = (e) => {
      e.preventDefault();
      const slider = $('gemmaCropZoomSlider');
      if (!slider) return;
      const current = parseFloat(slider.value) || 1;
      const step = e.deltaY < 0 ? 0.08 : -0.08;
      applyZoomRatio(current + step);
    };
  }

  const zoomSlider = $('gemmaCropZoomSlider');
  if (zoomSlider) {
    zoomSlider.oninput = () => {
      applyZoomRatio(parseFloat(zoomSlider.value) || 1);
    };
  }
  $('gemmaCropZoomIn')?.addEventListener('click', () => {
    const cur = parseFloat($('gemmaCropZoomSlider')?.value) || 1;
    applyZoomRatio(cur + 0.15);
  });
  $('gemmaCropZoomOut')?.addEventListener('click', () => {
    const cur = parseFloat($('gemmaCropZoomSlider')?.value) || 1;
    applyZoomRatio(cur - 0.15);
  });
  $('gemmaCropResetBtn')?.addEventListener('click', () => {
    cropX = 0;
    cropY = 0;
    applyZoomRatio(1);
  });

  $('gemmaCropCancelBtn')?.addEventListener('click', closeCropModal);
  $('gemmaCropCloseBtn')?.addEventListener('click', closeCropModal);

  $('gemmaCropConfirmBtn')?.addEventListener('click', () => {
    if (!cropImg) {
      closeCropModal();
      return;
    }
    const scaledW = cropImg.width * cropScale;
    const scaledH = cropImg.height * cropScale;
    const drawX = (CROP_CANVAS_SIZE / 2) + cropX - (scaledW / 2);
    const drawY = (CROP_CANVAS_SIZE / 2) + cropY - (scaledH / 2);

    const sx = Math.max(0, (CROP_OFFSET - drawX) / cropScale);
    const sy = Math.max(0, (CROP_OFFSET - drawY) / cropScale);
    const sw = Math.min(cropImg.width - sx, CROP_BOX_SIZE / cropScale);
    const sh = Math.min(cropImg.height - sy, CROP_BOX_SIZE / cropScale);

    const outCanvas = document.createElement('canvas');
    outCanvas.width = 256;
    outCanvas.height = 256;
    const outCtx = outCanvas.getContext('2d');
    outCtx.imageSmoothingEnabled = true;
    outCtx.imageSmoothingQuality = 'high';
    outCtx.drawImage(cropImg, sx, sy, sw, sh, 0, 0, 256, 256);

    const dataUrl = outCanvas.toDataURL('image/jpeg', 0.86);
    $('gemmaCharFormAvatar').value = dataUrl;
    updateAvatarPreview(dataUrl);
    closeCropModal();
  });

  $('gemmaCharForm')?.addEventListener('submit', saveCustomCharacter);
  $('gemmaCharFormDelete')?.addEventListener('click', deleteCustomCharacter);
  $('gemmaCharFormReset')?.addEventListener('click', resetDefaultCharacter);
  $('gemmaModalClose')?.addEventListener('click', closeCharModal);
  $('gemmaModalCancel')?.addEventListener('click', closeCharModal);

  $('gemmaErrorClose')?.addEventListener('click', () => {
    if (noticeDismissTimer) {
      clearTimeout(noticeDismissTimer);
      noticeDismissTimer = null;
    }
    const currentNotice = state?.notice || '';
    if (currentNotice) {
      dismissedNotice = currentNotice;
      try { sessionStorage.setItem('lstudio-gemma-dismissed-notice', currentNotice); } catch (_) {}
    }
    const rawMessage = actionError || loadError || state?.close_error || (state?.status === 'error' ? state.message : '');
    if (rawMessage) {
      dismissedError = rawMessage;
      actionError = '';
      loadError = '';
    }
    dismissBanner();
  });

  window.GemmaChat = {refresh};
  if (window.Workspace?.page === 'gemma') refresh();
  setInterval(() => {
    if (!fetching && !root.hidden && !document.hidden && !activeStreamAbort) refresh();
  }, 950);
})();
