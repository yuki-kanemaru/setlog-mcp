/* setlog PC — 画面ロジック（フレームワークなし） */
'use strict';

const TOKEN = document.querySelector('meta[name="setlog-token"]').content;
const $ = (id) => document.getElementById(id);
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
  viewer: null,
  replyCounts: new Map(),
  upload: null,
  prepared: null,
};

/* ---------------- API ---------------- */

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
  const el = $('toast');
  el.textContent = message;
  el.className = 'toast' + (kind ? ` is-${kind}` : '');
  clearTimeout(toast._timer);
  toast._timer = setTimeout(() => el.classList.add('hidden'), kind === 'error' ? 6000 : 3200);
}

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

function formatTime(iso) {
  if (!iso) return '';
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return '';
  return `${String(date.getHours()).padStart(2, '0')}:${String(date.getMinutes()).padStart(2, '0')}`;
}

function escapeHtml(value) {
  return String(value || '')
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;');
}

/* ---------------- 起動 ---------------- */

async function boot() {
  try {
    const [options, status, collections] = await Promise.all([
      api('/api/options'),
      api('/api/status'),
      api('/api/collections'),
    ]);
    state.days = options.days;
    state.me = collections.me;
    state.collections = collections.collections;
    state.profile = status.profile || collections.profile;
    state.date = '';
    $('who').textContent = state.profile ? state.profile.name || '' : '';
    if (!status.authenticated) {
      $('who').textContent = '未認証';
      toast('setlogの認証を確認できません。時間をおいて更新してください。', 'error');
    }
    renderDays();
    renderCollections();
    renderFoot(options);
    if (state.collections.length) {
      const recent = [...state.collections].sort((a, b) => (b.last_read || 0) - (a.last_read || 0))[0];
      selectCollection(recent.id);
    } else {
      $('grid').innerHTML = '';
      $('empty').classList.remove('hidden');
      $('empty').textContent = '参加しているログがありません。';
    }
  } catch (error) {
    toast(`読み込みに失敗しました: ${error.message}`, 'error');
  }
}

function renderFoot(options) {
  const ffmpeg = options && options.ffmpeg ? '動画の変換: 利用可' : '動画の変換: 未検出';
  $('side-foot').innerHTML = `${state.collections.length}件のログ<br>${ffmpeg}`;
}

function renderDays() {
  const bar = $('daybar');
  bar.innerHTML = '';
  const items = [{ date: '', label: '最新', weekday: '履歴' }, ...state.days.map((day) => ({ date: day.date, label: day.label, weekday: day.weekday }))];
  for (const item of items) {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'day' + (item.date === state.date ? ' is-active' : '');
    button.setAttribute('aria-pressed', item.date === state.date ? 'true' : 'false');
    button.innerHTML = `<span class="day__label">${escapeHtml(item.label)}</span><span class="day__weekday">${escapeHtml(item.weekday)}</span>`;
    button.onclick = () => {
      state.date = item.date;
      renderDays();
      loadFeed(item.date ? { date: item.date } : {});
    };
    bar.appendChild(button);
  }
}

function renderCollections() {
  const list = $('collections');
  list.innerHTML = '';
  for (const item of state.collections) {
    const li = document.createElement('li');
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'log-item' + (state.current === item.id ? ' is-active' : '');
    button.dataset.id = item.id;
    if (state.current === item.id) button.setAttribute('aria-current', 'true');
    const initial = (item.name || '?').trim().charAt(0) || '?';
    const people = item.participants ? `${item.participants + 1}人` : 'ひとり';
    button.innerHTML = `<span class="log-item__avatar" aria-hidden="true">${escapeHtml(initial)}</span>
      <span class="log-item__text"><span class="log-item__name">${escapeHtml(item.name)}</span><span class="log-item__meta">${people}</span></span>`;
    button.onclick = () => selectCollection(item.id);
    li.appendChild(button);
    list.appendChild(li);
  }
}

function selectCollection(id) {
  state.current = id;
  state.posts = [];
  state.nextBefore = null;
  for (const el of document.querySelectorAll('.log-item')) {
    const active = el.dataset.id === id;
    el.classList.toggle('is-active', active);
    if (active) el.setAttribute('aria-current', 'true');
    else el.removeAttribute('aria-current');
  }
  const item = state.collections.find((entry) => entry.id === id);
  $('collection-title').textContent = item ? item.name : '';
  $('collection-meta').textContent = item ? `${item.participants + 1}人のログ` : '';
  loadFeed(state.date ? { date: state.date } : {});
}

/* ---------------- 一覧 ---------------- */

async function loadFeed({ date = '', before = null, append = false } = {}) {
  if (!state.current) return;
  const params = new URLSearchParams({ collection: state.current, limit: '60' });
  if (date) params.set('date', date);
  if (before) params.set('before', String(before));
  if (!append) {
    $('grid').innerHTML = '';
    for (let index = 0; index < 6; index += 1) {
      const skeleton = document.createElement('div');
      skeleton.className = 'skeleton';
      skeleton.setAttribute('aria-hidden', 'true');
      $('grid').appendChild(skeleton);
    }
  }
  try {
    const payload = await api(`/api/feed?${params.toString()}`);
    state.posts = append ? state.posts.concat(payload.posts) : payload.posts;
    state.nextBefore = payload.next_before;
    renderGrid();
  } catch (error) {
    $('grid').innerHTML = '';
    $('empty').classList.remove('hidden');
    $('empty').textContent = error.status === 403
      ? 'この画面から開き直してください。'
      : `読み込みに失敗しました: ${error.message}`;
  }
}

function renderGrid() {
  const grid = $('grid');
  const chatlist = $('chatlist');
  grid.innerHTML = '';
  chatlist.innerHTML = '';

  const counts = new Map();
  for (const post of state.posts) {
    if (post.is_reply && post.parent_id) counts.set(post.parent_id, (counts.get(post.parent_id) || 0) + 1);
  }
  state.replyCounts = counts;

  const visible = state.posts.filter((post) => !post.is_reply);
  const media = visible.filter((post) => post.kind !== 'chat');
  const chats = visible.filter((post) => post.kind === 'chat');

  $('empty').classList.toggle('hidden', visible.length > 0);
  if (!visible.length) {
    $('empty').textContent = state.date ? 'この日の投稿はまだありません。' : 'まだ投稿がありません。';
  }

  for (const post of media) grid.appendChild(videoCard(post));

  $('chat-heading').classList.toggle('hidden', chats.length === 0);
  $('chat-count').textContent = chats.length ? `(${chats.length})` : '';
  for (const post of chats) {
    const li = document.createElement('li');
    li.appendChild(chatRow(post));
    chatlist.appendChild(li);
  }

  $('btn-more').classList.toggle('hidden', !state.nextBefore);
}

function videoCard(post) {
  const card = document.createElement('button');
  card.type = 'button';
  card.className = 'card';
  card.dataset.id = post.message_id;

  const replies = state.replyCounts.get(post.message_id) || 0;
  const badges = [];
  if (post.unread) badges.push('<span class="pill pill--new">新着</span>');
  if (replies) badges.push(`<span class="pill">返信 ${replies}</span>`);

  const poster = post.poster_url
    ? `<img src="${post.poster_url}" alt="" loading="lazy">`
    : `<span class="card__tile">${post.has_audio ? '音声' : post.has_image ? '画像' : 'メディア'}</span>`;
  const caption = (post.media.flatMap((item) => item.captions || []).join(' ') || '').trim();
  const reactionChips = aggregateReactions(post.reactions)
    .slice(0, 3)
    .map(([text, users]) => `<span class="pill">${escapeHtml(text)} ${users.length}</span>`)
    .join('');

  card.innerHTML = `<span class="card__poster">${poster}<span class="card__badges">${badges.join('')}</span></span>
    <span class="card__body">
      <span class="card__top">
        <span class="card__sender">${escapeHtml(post.sender_name || '')}</span>
        <span class="card__time">${formatTime(post.posted_at)}</span>
      </span>
      ${caption ? `<span class="card__caption">${escapeHtml(caption)}</span>` : ''}
      ${reactionChips ? `<span class="card__reactions">${reactionChips}</span>` : ''}
    </span>`;
  card.onclick = () => openViewer(post);
  return card;
}

function chatRow(post) {
  const row = document.createElement('button');
  row.type = 'button';
  row.className = 'chat' + (post.unread ? ' is-unread' : '');
  row.dataset.id = post.message_id;
  const body = post.text || (post.text_error ? '（復号できませんでした）' : '');
  row.innerHTML = `<span class="chat__sender">${escapeHtml(post.sender_name || '')}</span>
    <span class="chat__text">${escapeHtml(body)}</span>
    <span class="chat__time">${formatTime(post.posted_at)}</span>`;
  row.onclick = () => openViewer(post);
  return row;
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

/* ---------------- 詳細 ---------------- */

async function openViewer(post) {
  state.viewer = post;
  openModal('viewer');
  $('viewer-title').textContent = post.sender_name || '投稿';
  $('v-time').textContent = new Date(post.timestamp_ms).toLocaleString('ja-JP');
  $('v-caption').textContent = post.kind === 'chat'
    ? post.text || ''
    : (post.media.flatMap((item) => item.captions || []).join('\n') || '');
  $('v-delete').classList.toggle('hidden', !post.is_mine);

  const video = $('viewer-video');
  const audio = $('viewer-audio');
  const fallback = $('viewer-fallback');
  fallback.classList.add('hidden');
  video.classList.add('hidden');
  video.removeAttribute('src');
  audio.classList.add('hidden');
  audio.removeAttribute('src');
  if (post.video_url) {
    video.classList.remove('hidden');
    video.src = post.video_url;
    video.load();
    video.play().catch(() => {});
  } else if (post.audio_url) {
    audio.classList.remove('hidden');
    audio.src = post.audio_url;
    audio.play().catch(() => {});
  } else if (post.kind !== 'chat') {
    fallback.textContent = 'この投稿に再生できるメディアはありません。';
    fallback.classList.remove('hidden');
  }

  renderViewerReactions(post);
  $('v-reply-count').textContent = '';
  $('v-replies').innerHTML = '<p class="reply__body">読み込み中…</p>';
  try {
    const thread = await api(`/api/thread?collection=${encodeURIComponent(state.current)}&parent=${encodeURIComponent(post.message_id)}`);
    renderThread(thread.replies);
  } catch (error) {
    $('v-replies').innerHTML = `<p class="reply__body">返信を読み込めません: ${escapeHtml(error.message)}</p>`;
  }
}

function renderThread(replies) {
  const box = $('v-replies');
  box.innerHTML = '';
  $('v-reply-count').textContent = replies.length ? `(${replies.length})` : '';
  if (!replies.length) {
    const empty = document.createElement('p');
    empty.className = 'reply__body';
    empty.textContent = 'まだ返信はありません。';
    box.appendChild(empty);
    return;
  }
  for (const reply of replies) {
    const el = document.createElement('article');
    el.className = 'reply';
    el.innerHTML = `<p class="reply__head"><span>${escapeHtml(reply.sender_name || '')}</span><span>${formatTime(reply.posted_at)}</span></p>
      <p class="reply__body">${escapeHtml(reply.text || '')}</p>`;
    box.appendChild(el);
  }
  box.scrollTop = box.scrollHeight;
}

function renderViewerReactions(post) {
  const box = $('v-reactions');
  box.innerHTML = '';
  const grouped = aggregateReactions(post.reactions);
  for (const [text, users] of grouped) {
    const mine = users.includes(state.me);
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'reaction' + (mine ? ' is-mine' : '');
    button.setAttribute('aria-pressed', mine ? 'true' : 'false');
    button.innerHTML = `<span>${escapeHtml(text)}</span><span class="reaction__count">${users.length}</span>`;
    button.onclick = () => toggleReaction(post, text, mine);
    box.appendChild(button);
  }
  const picker = document.createElement('div');
  picker.className = 'reaction-picker';
  for (const emoji of EMOJI) {
    const mine = (post.own_reactions || []).includes(emoji);
    const button = document.createElement('button');
    button.type = 'button';
    button.textContent = emoji;
    button.setAttribute('aria-label', `リアクション ${emoji}`);
    button.setAttribute('aria-pressed', mine ? 'true' : 'false');
    if (mine) button.style.borderColor = 'var(--accent)';
    button.onclick = () => toggleReaction(post, emoji, mine);
    picker.appendChild(button);
  }
  box.appendChild(picker);
}

async function toggleReaction(post, text, mine) {
  try {
    const result = await api('/api/reactions', {
      method: 'POST',
      body: { collection_id: state.current, message_id: post.message_id, text, remove: Boolean(mine) },
    });
    post.reactions = result.reactions;
    post.own_reactions = result.reactions.filter((item) => item.user_id === state.me).map((item) => item.text);
    renderViewerReactions(post);
    const card = state.posts.find((item) => item.message_id === post.message_id);
    if (card) card.reactions = result.reactions;
    renderGrid();
  } catch (error) {
    toast(`リアクションを更新できません: ${error.message}`, 'error');
  }
}

/* ---------------- 投稿 ---------------- */

function openComposer() {
  state.upload = null;
  state.prepared = null;
  const result = $('preview');
  result.classList.add('hidden');
  result.innerHTML = '';
  $('btn-send').classList.add('hidden');
  $('btn-send').disabled = false;
  $('btn-prepare').disabled = false;
  $('composer-status').textContent = '';
  $('upload-wrap').classList.add('hidden');
  $('upload-bar').style.width = '0%';
  $('local-preview').classList.add('hidden');
  $('local-preview').removeAttribute('src');
  $('drop-hint').classList.remove('hidden');
  $('comment').value = '';
  $('file').value = '';
  const select = $('dest');
  select.innerHTML = '';
  for (const item of state.collections) {
    const option = document.createElement('option');
    option.value = item.id;
    option.textContent = `${item.name}（${item.participants + 1}人）`;
    if (item.id === state.current) option.selected = true;
    select.appendChild(option);
  }
  openModal('composer');
}

function chooseFile(file) {
  if (!file) return;
  state.upload = file;
  state.prepared = null;
  $('drop-hint').classList.add('hidden');
  const preview = $('local-preview');
  preview.classList.remove('hidden');
  preview.src = URL.createObjectURL(file);
  preview.play().catch(() => {});
  $('btn-send').classList.add('hidden');
  $('preview').classList.add('hidden');
}

function uploadWithProgress(file, onProgress) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open('POST', `/api/uploads?name=${encodeURIComponent(file.name || 'upload.mp4')}`);
    xhr.setRequestHeader('x-setlog-token', TOKEN);
    xhr.setRequestHeader('Content-Type', 'application/octet-stream');
    xhr.upload.onprogress = (event) => {
      if (event.lengthComputable) onProgress(event.loaded / event.total);
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

async function preparePost() {
  if (!state.upload) {
    toast('動画を選んでください。', 'error');
    return;
  }
  $('btn-prepare').disabled = true;
  $('btn-send').classList.add('hidden');
  try {
    $('composer-status').textContent = 'アップロード中';
    $('upload-wrap').classList.remove('hidden');
    const uploaded = await uploadWithProgress(state.upload, (ratio) => {
      $('upload-bar').style.width = `${Math.round(ratio * 100)}%`;
    });
    $('upload-bar').style.width = '100%';
    $('composer-status').textContent = '準備中（暗号化）';
    const task = await api('/api/jobs/prepare', {
      method: 'POST',
      body: {
        collection_id: $('dest').value,
        upload_id: uploaded.upload_id,
        comment: $('comment').value,
      },
    });
    const result = await pollTask(task.task_id);
    state.prepared = result;
    const box = $('preview');
    box.classList.remove('hidden');
    box.innerHTML = `<p><strong>準備ID</strong> ${escapeHtml(result['準備ID'])}</p>
      <p><strong>投稿先</strong> ${escapeHtml(result['投稿先'] || '')}（${result['受信者数']}人）</p>
      <p><strong>動画</strong> ${escapeHtml(result['動画'] || '')}</p>
      ${result['コメント'] ? `<p><strong>コメント</strong> ${escapeHtml(result['コメント'])}</p>` : ''}
      <p class="warn">まだ送信していません。内容を確認して送信してください。</p>`;
    $('btn-send').classList.remove('hidden');
    $('composer-status').textContent = '準備ができました';
  } catch (error) {
    $('composer-status').textContent = '';
    toast(`準備できません: ${error.message}`, 'error');
    $('btn-prepare').disabled = false;
  }
}

async function sendPost() {
  if (!state.prepared) return;
  const jobId = state.prepared['準備ID'];
  $('btn-send').disabled = true;
  $('btn-prepare').disabled = true;
  try {
    $('composer-status').textContent = '送信中（このまま待ってください）';
    const task = await api(`/api/jobs/${encodeURIComponent(jobId)}/send`, { method: 'POST' });
    const result = await pollTask(task.task_id);
    $('composer-status').textContent = '送信しました';
    const box = $('preview');
    box.innerHTML = `<p class="ok">投稿しました</p>
      <p><strong>投稿ID</strong> ${escapeHtml(result['投稿ID'] || '')}</p>
      <p><strong>投稿先</strong> ${escapeHtml(result['投稿先'] || '')}</p>
      ${result['コメント'] ? `<p><strong>コメント</strong> ${escapeHtml(result['コメント'])}</p>` : ''}
      <p class="note">${escapeHtml(result['状態'] || '')}</p>`;
    toast('投稿しました。', 'ok');
    await loadFeed(state.date ? { date: state.date } : {});
    setTimeout(() => $('composer').classList.add('hidden'), 1800);
  } catch (error) {
    $('composer-status').textContent = '';
    $('btn-send').disabled = true;
    toast(`送信できませんでした: ${error.message}（再送せず状態を確認してください）`, 'error');
    $('preview').innerHTML += '<p class="warn">結果が不明です。自動再送はしていません。</p>';
  }
}

/* ---------------- 容量 ---------------- */

async function openCache() {
  const payload = await api('/api/cache');
  const cache = payload.cache;
  $('cache-body').innerHTML = `<p><strong>キャッシュ</strong><br>${escapeHtml(cache.dir || '未作成')}</p>
    <p><strong>使用量</strong> ${(cache.bytes / 1024 / 1024).toFixed(1)} MB / ${(cache.limit / 1024 / 1024).toFixed(0)} MB（${cache.files}件）</p>
    <p><strong>動画の保存先</strong><br>${escapeHtml(payload.save_dir || '')}</p>`;
  openModal('cache-modal');
}

/* ---------------- 操作 ---------------- */

let lastFocus = null;

function openModal(id) {
  lastFocus = document.activeElement;
  const modal = $(id);
  modal.classList.remove('hidden');
  const first = modal.querySelector('[data-close], input, video, button');
  if (first) first.focus();
}

function closeModals() {
  for (const id of ['viewer', 'composer', 'cache-modal']) $(id).classList.add('hidden');
  const video = $('viewer-video');
  video.pause();
  video.removeAttribute('src');
  const audio = $('viewer-audio');
  audio.pause();
  audio.removeAttribute('src');
  if (lastFocus && document.contains(lastFocus)) lastFocus.focus();
  lastFocus = null;
}

function bind() {
  for (const el of document.querySelectorAll('[data-close]')) el.onclick = closeModals;
  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape') closeModals();
  });

  $('btn-refresh').onclick = () => {
    if (state.current) loadFeed(state.date ? { date: state.date } : {});
    toast('更新しました。', 'ok');
  };
  $('btn-cache').onclick = () => openCache().catch((error) => toast(error.message, 'error'));
  $('btn-clear-cache').onclick = async () => {
    try {
      const result = await api('/api/cache/clear', { method: 'POST' });
      toast(`${result.removed}件のキャッシュを削除しました。`, 'ok');
      closeModals();
    } catch (error) {
      toast(error.message, 'error');
    }
  };
  $('btn-post').onclick = openComposer;
  $('btn-prepare').onclick = preparePost;
  $('btn-send').onclick = sendPost;
  $('btn-more').onclick = () => {
    if (state.nextBefore) loadFeed({ before: state.nextBefore, append: true });
  };
  $('btn-prev').onclick = () => shiftDay(-1);
  $('btn-next').onclick = () => shiftDay(1);

  $('file').onchange = (event) => chooseFile(event.target.files[0]);
  const drop = $('drop');
  drop.addEventListener('dragover', (event) => {
    event.preventDefault();
    drop.classList.add('is-dragging');
  });
  drop.addEventListener('dragleave', () => drop.classList.remove('is-dragging'));
  drop.addEventListener('drop', (event) => {
    event.preventDefault();
    drop.classList.remove('is-dragging');
    const file = event.dataTransfer.files[0];
    if (file) chooseFile(file);
  });

  $('v-reply-form').onsubmit = async (event) => {
    event.preventDefault();
    const input = $('v-reply-input');
    const text = input.value.trim();
    if (!text || !state.viewer) return;
    input.disabled = true;
    try {
      await api('/api/replies', {
        method: 'POST',
        body: { collection_id: state.current, text, parent_id: state.viewer.message_id },
      });
      input.value = '';
      const thread = await api(`/api/thread?collection=${encodeURIComponent(state.current)}&parent=${encodeURIComponent(state.viewer.message_id)}`);
      renderThread(thread.replies);
      await loadFeed(state.date ? { date: state.date } : {});
    } catch (error) {
      toast(`返信できません: ${error.message}`, 'error');
    } finally {
      input.disabled = false;
      input.focus();
    }
  };

  $('v-delete').onclick = async () => {
    if (!state.viewer) return;
    if (!window.confirm('この投稿を削除しますか？（取り消せません）')) return;
    try {
      await api('/api/messages/delete', {
        method: 'POST',
        body: { collection_id: state.current, message_id: state.viewer.message_id },
      });
      toast('削除しました。', 'ok');
      closeModals();
      await loadFeed(state.date ? { date: state.date } : {});
    } catch (error) {
      toast(`削除できません: ${error.message}`, 'error');
    }
  };

  $('viewer-video').onerror = () => {
    $('viewer-fallback').textContent = 'この動画を再生できませんでした（変換中か、形式が非対応です）。';
    $('viewer-fallback').classList.remove('hidden');
  };
}

function shiftDay(delta) {
  if (state.date === '') {
    if (delta > 0) return;
    state.date = state.days[0].date;
    renderDays();
    loadFeed({ date: state.date });
    return;
  }
  const index = state.days.findIndex((day) => day.date === state.date);
  const next = index + delta;
  if (next < -1 || next >= state.days.length) return;
  if (next === -1) {
    state.date = '';
    renderDays();
    loadFeed({});
    return;
  }
  state.date = state.days[next].date;
  renderDays();
  loadFeed({ date: state.date });
}

bind();
boot();
