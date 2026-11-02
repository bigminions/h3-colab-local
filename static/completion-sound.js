(function (root) {
  'use strict';
  const PREF = 'h3-completion-sound-enabled-v1';
  const NOTIFIED = 'h3-completion-sound-notified-v1';
  const NOTIFIED_ERRORS = 'h3-error-sound-notified-v1';

  class CompletionSound {
    constructor(storage, createAudio, notifier) {
      this.storage = storage;
      this.createAudio = createAudio;
      this.notifier = notifier !== undefined ? notifier : (
        typeof window !== 'undefined' && 'Notification' in window ? window.Notification : null
      );
      this.context = null;
      this.initialized = false;
      this.seen = new Set();
      this.seenErrors = new Set();
      try { this.enabled = storage.getItem(PREF) !== 'off'; }
      catch (_) { this.enabled = true; }
    }

    setEnabled(value) {
      this.enabled = !!value;
      try { this.storage.setItem(PREF, value ? 'on' : 'off'); } catch (_) {}
      if (this.enabled) this.unlock();
    }

    unlock() {
      if (!this.enabled) return;
      try {
        if (!this.context) this.context = this.createAudio();
        if (this.context?.state === 'suspended') this.context.resume().catch(() => {});
      } catch (_) { /* Blocked audio must not interrupt generation. */ }
      try {
        if (this.notifier && typeof this.notifier.requestPermission === 'function' && this.notifier.permission === 'default') {
          const p = this.notifier.requestPermission();
          if (p && typeof p.catch === 'function') p.catch(() => {});
        }
      } catch (_) { /* Permission request failure must not block. */ }
    }

    play() {
      if (!this.enabled || !this.context) return;
      const ring = () => {
        if (!this.enabled || this.context.state !== 'running') return;
        const start = this.context.currentTime + .02;
        // Ascending chime: C5 (523.25), E5 (659.25), G5 (783.99)
        // Louder peak gain 0.35 (elevated from 0.09) with resonant envelope
        for (const [offset, frequency, duration] of [[0, 523.25, .18], [.20, 659.25, .18], [.40, 783.99, .35]]) {
          const tone = this.context.createOscillator(), gain = this.context.createGain();
          tone.type = 'sine';
          tone.frequency.setValueAtTime(frequency, start + offset);
          gain.gain.setValueAtTime(0, start + offset);
          gain.gain.linearRampToValueAtTime(.35, start + offset + .015);
          gain.gain.exponentialRampToValueAtTime(.001, start + offset + duration);
          tone.connect(gain);
          gain.connect(this.context.destination);
          tone.onended = () => { tone.disconnect(); gain.disconnect(); };
          tone.start(start + offset);
          tone.stop(start + offset + duration + .03);
        }
      };
      try {
        if (this.context.state === 'suspended') this.context.resume().then(ring).catch(() => {});
        else ring();
      } catch (_) {}
    }

    playError() {
      if (!this.enabled || !this.context) return;
      const alert = () => {
        if (!this.enabled || this.context.state !== 'running') return;
        const start = this.context.currentTime + .02;
        // Distinct descending warning chime: A4 (440), F4 (349.23), C4 (261.63)
        // Uses triangle wave for richer harmonic presence and unmistakable alert signature
        for (const [offset, frequency, duration] of [[0, 440, .18], [.20, 349.23, .18], [.40, 261.63, .35]]) {
          const tone = this.context.createOscillator(), gain = this.context.createGain();
          tone.type = 'triangle';
          tone.frequency.setValueAtTime(frequency, start + offset);
          gain.gain.setValueAtTime(0, start + offset);
          gain.gain.linearRampToValueAtTime(.35, start + offset + .015);
          gain.gain.exponentialRampToValueAtTime(.001, start + offset + duration);
          tone.connect(gain);
          gain.connect(this.context.destination);
          tone.onended = () => { tone.disconnect(); gain.disconnect(); };
          tone.start(start + offset);
          tone.stop(start + offset + duration + .03);
        }
      };
      try {
        if (this.context.state === 'suspended') this.context.resume().then(alert).catch(() => {});
        else alert();
      } catch (_) {}
    }

    notify(title, body, tag) {
      if (!this.enabled || !this.notifier) return;
      const send = () => {
        try {
          if (this.notifier.permission === 'granted') {
            const n = new this.notifier(title, {
              body: body || '',
              tag: tag || undefined,
              renotify: !!tag,
            });
            if (typeof window !== 'undefined') {
              n.onclick = () => {
                try { window.focus(); } catch (_) {}
                try { n.close(); } catch (_) {}
              };
            }
          }
        } catch (_) {}
      };
      if (this.notifier.permission === 'granted') {
        send();
      } else if (this.notifier.permission === 'default' && typeof this.notifier.requestPermission === 'function') {
        try {
          const p = this.notifier.requestPermission();
          if (p && typeof p.then === 'function') {
            p.then(res => { if (res === 'granted') send(); }).catch(() => {});
          }
        } catch (_) {}
      }
    }

    formatPrompt(job, max = 40) {
      const p = job?.creation?.prompt || job?.config?.prompt || job?.prompt || '';
      return p.length > max ? p.slice(0, max) + '…' : p;
    }

    update(jobs) {
      if (!Array.isArray(jobs)) return;
      const locallyReady = j => (j.local_file !== undefined ? j.local_file : !!j.file) && !j.local_download_error && ['done','warning'].includes(j.status);
      const isFailed = j => j && (
        j.status === 'error' ||
        j.status === 'interrupted' ||
        (!!j.local_download_error && !locallyReady(j))
      );

      const completed = jobs.filter(j => locallyReady(j) && (!j.batch_id || j.batch_complete &&
        jobs.filter(item => item.batch_id === j.batch_id).every(locallyReady)));
      const failed = jobs.filter(isFailed);

      let stored = [];
      try { stored = JSON.parse(this.storage.getItem(NOTIFIED) || '[]'); } catch (_) {}
      if (!Array.isArray(stored)) stored = [];
      const notified = new Set(stored.filter(id => typeof id === 'string'));

      let storedErrors = [];
      try { storedErrors = JSON.parse(this.storage.getItem(NOTIFIED_ERRORS) || '[]'); } catch (_) {}
      if (!Array.isArray(storedErrors)) storedErrors = [];
      const notifiedErrors = new Set(storedErrors.filter(id => typeof id === 'string'));

      let fresh = false;
      const freshCompleted = [];
      for (const job of completed) {
        const id = job.batch_id || job.id;
        if (this.initialized && !this.seen.has(id) && !notified.has(id)) {
          fresh = true;
          freshCompleted.push(job);
        }
        this.seen.add(id); notified.add(id);
      }

      let freshError = false;
      const freshFailed = [];
      for (const job of failed) {
        const id = job.id;
        if (this.initialized && !this.seenErrors.has(id) && !notifiedErrors.has(id)) {
          freshError = true;
          freshFailed.push(job);
        }
        this.seenErrors.add(id); notifiedErrors.add(id);
      }

      this.initialized = true;
      try { this.storage.setItem(NOTIFIED, JSON.stringify([...notified].slice(-100))); } catch (_) {}
      try { this.storage.setItem(NOTIFIED_ERRORS, JSON.stringify([...notifiedErrors].slice(-100))); } catch (_) {}

      if (freshError) {
        this.playError();
        const firstErr = freshFailed[0];
        const reason = firstErr?.failure_reason || firstErr?.message || firstErr?.local_download_error || '任务执行或保存异常';
        const prompt = this.formatPrompt(firstErr, 36);
        const promptInfo = prompt ? `\n提示词：“${prompt}”` : '';
        this.notify('H3 视频生成失败', `原因：${reason}${promptInfo}`, 'h3-error-' + (firstErr?.id || 'error'));
        if (fresh) setTimeout(() => this.play(), 800);
      } else if (fresh) {
        this.play();
      }

      if (fresh && !freshError) {
        const firstComp = freshCompleted[0];
        if (firstComp?.batch_id) {
          const total = firstComp.batch?.total || firstComp.batch_total || freshCompleted.length;
          const prompt = this.formatPrompt(firstComp, 36);
          const promptInfo = prompt ? `\n提示词：“${prompt}”` : '';
          this.notify('H3 视频生成完成', `整批共 ${total} 个视频已全部完成并保存。${promptInfo}`, 'h3-batch-' + firstComp.batch_id);
        } else if (firstComp) {
          const title = firstComp.creation?.title;
          const prompt = this.formatPrompt(firstComp, 40);
          const file = firstComp.file ? `（${firstComp.file}）` : '';
          const msg = title ? `《${title}》已完成并保存。` : (prompt ? `“${prompt}” 已完成并保存。` : `视频已成功完成并保存${file}。`);
          this.notify('H3 视频生成完成', msg, 'h3-done-' + firstComp.id);
        }
      }
    }
  }

  if (typeof module !== 'undefined' && module.exports) module.exports = CompletionSound;
  else root.H3CompletionSound = CompletionSound;
})(typeof window !== 'undefined' ? window : globalThis);
