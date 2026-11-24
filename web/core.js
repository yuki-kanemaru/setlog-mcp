/* 共通のAPI・状態・表示ユーティリティ（デスクトップ表示とスマホ表示で共有する） */
'use strict';

const Setlog = (() => {
  const TOKEN = document.querySelector('meta[name="setlog-token"]').content;
  const EMOJI = ['✨', '❤️', '🥺', '😍', '😂', '👏', '🔥', '👍'];

  const state = {
    me: '',
    profile: null,
    collections: [],
    current: null,
    date: '',
    days: [],
    posts: [],
    nextBefore: null,
  };

  async function api(path, options = {}) {
    const { method = 'GET', body = null, headers = {} } = options;
    const init = { method, headers: { ...headers } };
    if (method !== 'GET') init.headers['x-setlog-token'] = TOKEN;
    if (body !== null) {
      init.headers['Content-Type'] = 'application/json';
      init.body = JSON.stringify(body);
    }
    const response = await fetch(path, init);
    let payload = null;
    try { payload = await response.json(); } catch { payload = null; }
    if (!response.ok) {
      const message = (payload && (payload.error || payload.message)) || `HTTP ${response.status}`;
      const error = new Error(message);
      error.status = response.status;
      throw error;
    }
    return payload;
  }

  function toast(message, kind = '') {
    const el = document.getElementById('toast');
    if (!el) return;
    el.textContent = message;
    el.className = 'toast' + (kind ? ` is-${kind}` : '');
    clearTimeout(toast._timer);
    toast._timer = setTimeout(() => el.classList.add('hidden'), kind === 'error' ? 6000 : 3200);
  }

  function escapeHtml(value) {
    return String(value || '')
      .replaceAll('&', '&amp;')
      .replaceAll('<', '&lt;')
      .replaceAll('>', '&gt;')
      .replaceAll('"', '&quot;');
  }

  function timeOf(iso) {
    if (!iso) return '';
    const date = new Date(iso);
    if (Number.isNaN(date.getTime())) return '';
    return `${String(date.getHours()).padStart(2, '0')}:${String(date.getMinutes()).padStart(2, '0')}`;
  }

  function aggregateReactions(list) {
    const map = new Map();
    for (const reaction of list || []) {
      const entry = map.get(reaction.text) || [];
      entry.push(reaction.user_id);
      map.set(reaction.text, entry);
    }
    return Array.from(map.entries());
  }

  async function boot() {
    const [options, status, collections] = await Promise.all([
      api('/api/options'),
      api('/api/status'),
      api('/api/collections'),
    ]);
    state.days = options.days;
    state.me = collections.me;
    state.collections = collections.collections;
    state.profile = status.profile || collections.profile;
    return { options, status, collections };
  }

  function feedUrls(collectionId, post) {
    const base = `/api/media/${collectionId}/${post.message_id}`;
    return {
      poster: post.poster_url || post.posterUrl || undefined,
      video: post.video_url || post.videoUrl || base,
      audio: post.audio_url || post.audioUrl || `${base}/audio`,
    };
  }

  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

  async function pollTask(taskId, onTick) {
    for (let index = 0; index < 900; index += 1) {
      const record = await api(`/api/tasks/${taskId}`);
      if (record.status === 'done') return record.result;
      if (record.status === 'failed') throw new Error(record.error || '処理に失敗しました。');
      if (onTick) onTick(record, index);
      await sleep(700);
    }
    throw new Error('時間がかかっています。状態を確認してください（自動再送はしません）。');
  }

  function uploadWithProgress(file, onProgress) {
    return new Promise((resolve, reject) => {
      const xhr = new XMLHttpRequest();
      xhr.open('POST', `/api/uploads?name=${encodeURIComponent(file.name || 'upload.mp4')}`);
      xhr.setRequestHeader('x-setlog-token', TOKEN);
      xhr.setRequestHeader('Content-Type', 'application/octet-stream');
      xhr.upload.onprogress = (event) => {
        if (event.lengthComputable && onProgress) onProgress(event.loaded / event.total);
      };
      xhr.onload = () => {
        let payload = null;
        try { payload = JSON.parse(xhr.responseText); } catch { payload = null; }
        if (xhr.status >= 200 && xhr.status < 300) resolve(payload);
        else reject(new Error((payload && payload.error) || `アップロードに失敗しました (${xhr.status})`));
      };
      xhr.onerror = () => reject(new Error('アップロード中に通信が切れました。'));
      xhr.send(file);
    });
  }

  return {
    TOKEN,
    EMOJI,
    state,
    api,
    toast,
    escapeHtml,
    timeOf,
    aggregateReactions,
    boot,
    feedUrls,
    sleep,
    pollTask,
    uploadWithProgress,
  };
})();
