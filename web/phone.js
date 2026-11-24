/* setlog PC — スマホ版に寄せた表示（core.js のAPIを共有する） */
'use strict';

(() => {
  if (document.documentElement.dataset.view !== 'phone') return;

  const $ = (id) => document.getElementById(id);
  const { api, toast, escapeHtml, EMOJI, state, timeOf, aggregateReactions } = Setlog;

  const view = {
    page: 'home',        // home | detail | camera
    collectionId: null,
    collectionName: '',
    date: '',
    media: [],           // 再生対象（動画・音声）
    index: 0,
    upload: null,
    prepared: null,
  };

  /* ---------- 画面切り替え ---------- */

  function showPage(page) {
    view.page = page;
    for (const id of ['p-home', 'p-detail', 'p-camera']) {
      $(id).classList.toggle('hidden', id !== `p-${page}`);
    }
    for (const dot of document.querySelectorAll('.switcher__dot')) {
      dot.classList.toggle('is-active', dot.dataset.page === page || (page === 'detail' && dot.dataset.page === 'home'));
    }
    $('p-back').classList.toggle('hidden', page !== 'detail');
  }

  function setBar(title, meta) {
    $('p-title').textContent = title;
    $('p-meta').textContent = meta || '';
  }

  /* ---------- ホーム（ログ一覧） ---------- */

  function renderHome() {
    const list = $('p-logs');
    list.innerHTML = '';
    const rows = [...state.collections].sort((a, b) => (b.last_read || 0) - (a.last_read || 0));
    $('p-logs-empty').classList.toggle('hidden', rows.length > 0);
    for (const item of rows) {
      const li = document.createElement('li');
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'log-row';
      const people = item.participants ? `${item.participants + 1}人` : 'ひとり';
      button.innerHTML = `<span class="log-row__text">
          <span class="log-row__name">${escapeHtml(item.name)}</span>
          <span class="log-row__meta">${people}</span>
        </span>
        <svg class="log-row__arrow" viewBox="0 0 24 24" width="16" height="16" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M9 6l6 6-6 6"/></svg>`;
      button.onclick = () => openDetail(item);
      li.appendChild(button);
      list.appendChild(li);
    }
    setBar('ログ', `${rows.length}件`);
    showPage('home');
  }

  /* ---------- ログ詳細（日別） ---------- */

  async function openDetail(item) {
    view.collectionId = item.id;
    view.collectionName = item.name;
    view.date = '';
    setBar(item.name, `${item.participants + 1}人`);
    showPage('detail');
    renderDays();
    await loadDetail();
  }

  function renderDays() {
    const strip = $('p-days');
    strip.innerHTML = '';
    const items = [
      { date: '', label: '最新' },
      ...state.days.map((day) => ({ date: day.date, label: day.label })),
    ];
    for (const item of items) {
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'day-chip' + (item.date === view.date ? ' is-active' : '');
      button.textContent = item.label;
      button.setAttribute('aria-pressed', item.date === view.date ? 'true' : 'false');
      button.onclick = () => {
        view.date = item.date;
        renderDays();
        loadDetail();
      };
      strip.appendChild(button);
    }
  }

  async function loadDetail() {
    const grid = $('p-thumbs');
    grid.innerHTML = '<div class="thumb thumb--skeleton"></div><div class="thumb thumb--skeleton"></div><div class="thumb thumb--skeleton"></div><div class="thumb thumb--skeleton"></div>';
    const params = new URLSearchParams({ collection: view.collectionId, limit: '60' });
    if (view.date) params.set('date', view.date);
    try {
      const payload = await api(`/api/feed?${params.toString()}`);
      const posts = payload.posts.filter((post) => post.kind !== 'chat' && !post.is_reply);
      view.media = posts.filter((post) => post.video_url || post.audio_url);
      grid.innerHTML = '';
      $('p-thumbs-empty').classList.toggle('hidden', posts.length > 0);
      for (const post of posts) grid.appendChild(thumb(post));
      setBar(view.collectionName, `${posts.length}件`);
    } catch (error) {
      grid.innerHTML = '';
      $('p-thumbs-empty').classList.remove('hidden');
      $('p-thumbs-empty').textContent = `読み込みに失敗しました: ${error.message}`;
    }
  }

  function thumb(post) {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'thumb';
    const image = post.poster_url
      ? `<img src="${post.poster_url}" alt="" loading="lazy">`
      : `<span class="thumb__fallback">${post.has_audio ? '音声' : 'メディア'}</span>`;
    button.innerHTML = `${image}<span class="thumb__time">${timeOf(post.posted_at)}</span>`;
    button.onclick = () => openPlayer(post);
    return button;
  }

  /* ---------- プレイヤー ---------- */

  function openPlayer(post) {
    const index = Math.max(0, view.media.findIndex((item) => item.message_id === post.message_id));
    view.index = index;
    renderPlayer();
    $('p-player').classList.remove('hidden');
  }

  function closePlayer() {
    const video = $('p-video');
    video.pause();
    video.removeAttribute('src');
    const audio = $('p-audio');
    audio.pause();
    audio.removeAttribute('src');
    $('p-player').classList.add('hidden');
  }

  function renderPlayer() {
    const post = view.media[view.index];
    if (!post) return;
    const video = $('p-video');
    const audio = $('p-audio');
    video.classList.add('hidden');
    audio.classList.add('hidden');
    video.removeAttribute('src');
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
    }

    $('p-counter').textContent = `${view.index + 1} / ${view.media.length}`;
    $('p-who').textContent = post.sender_name || '';
    const caption = post.media.flatMap((item) => item.captions || []).join('\n') || post.text || '';
    $('p-caption').textContent = caption;
    renderReactions(post);
  }

  function renderReactions(post) {
    const box = $('p-reactions');
    box.innerHTML = '';
    for (const [text, users] of aggregateReactions(post.reactions)) {
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
      button.onclick = () => toggleReaction(post, emoji, mine);
      picker.appendChild(button);
    }
    box.appendChild(picker);
  }

  async function toggleReaction(post, text, mine) {
    try {
      const result = await api('/api/reactions', {
        method: 'POST',
        body: { collection_id: view.collectionId, message_id: post.message_id, text, remove: Boolean(mine) },
      });
      post.reactions = result.reactions;
      post.own_reactions = result.reactions
        .filter((item) => item.user_id === state.me)
        .map((item) => item.text);
      renderReactions(post);
    } catch (error) {
      toast(`リアクションを更新できません: ${error.message}`, 'error');
    }
  }

  function step(delta) {
    const next = view.index + delta;
    if (next < 0 || next >= view.media.length) return;
    view.index = next;
    renderPlayer();
  }

  /* ---------- 投稿シート ---------- */

  function openComposer() {
    view.upload = null;
    view.prepared = null;
    $('p-file').value = '';
    $('p-comment').value = '';
    $('p-local').classList.add('hidden');
    $('p-local').removeAttribute('src');
    $('p-result').classList.add('hidden');
    $('p-result').innerHTML = '';
    $('p-status').textContent = '';
    $('p-send').classList.add('hidden');
    $('p-prepare').disabled = false;
    const select = $('p-dest');
    select.innerHTML = '';
    for (const item of state.collections) {
      const option = document.createElement('option');
      option.value = item.id;
      option.textContent = `${item.name}（${item.participants + 1}人）`;
      if (item.id === view.collectionId) option.selected = true;
      select.appendChild(option);
    }
    if (!state.collections.length) select.innerHTML = '<option value="">ログがありません</option>';
    $('p-composer').classList.remove('hidden');
  }

  function closeComposer() {
    $('p-composer').classList.add('hidden');
  }

  async function prepareUpload() {
    if (!view.upload) {
      toast('動画を選んでください。', 'error');
      return;
    }
    $('p-prepare').disabled = true;
    try {
      $('p-status').textContent = 'アップロード中';
      const uploaded = await Setlog.uploadWithProgress(view.upload, () => {});
      $('p-status').textContent = '準備中（暗号化）';
      const task = await api('/api/jobs/prepare', {
        method: 'POST',
        body: { collection_id: $('p-dest').value, upload_id: uploaded.upload_id, comment: $('p-comment').value },
      });
      const result = await Setlog.pollTask(task.task_id);
      view.prepared = result;
      $('p-result').classList.remove('hidden');
      $('p-result').innerHTML = `<p><strong>準備ID</strong> ${escapeHtml(result['準備ID'])}</p>
        <p><strong>投稿先</strong> ${escapeHtml(result['投稿先'] || '')}（${result['受信者数']}人）</p>
        ${result['コメント'] ? `<p><strong>コメント</strong> ${escapeHtml(result['コメント'])}</p>` : ''}
        <p class="warn">まだ送信していません。</p>`;
      $('p-send').classList.remove('hidden');
      $('p-status').textContent = '準備ができました';
    } catch (error) {
      $('p-status').textContent = '';
      toast(`準備できません: ${error.message}`, 'error');
      $('p-prepare').disabled = false;
    }
  }

  async function sendUpload() {
    if (!view.prepared) return;
    $('p-send').disabled = true;
    try {
      $('p-status').textContent = '送信中';
      const task = await api(`/api/jobs/${encodeURIComponent(view.prepared['準備ID'])}/send`, { method: 'POST' });
      const result = await Setlog.pollTask(task.task_id);
      $('p-result').innerHTML = `<p class="ok">投稿しました</p>
        <p><strong>投稿ID</strong> ${escapeHtml(result['投稿ID'] || '')}</p>`;
      $('p-status').textContent = '';
      toast('投稿しました。', 'ok');
      if (view.collectionId) await loadDetail();
      setTimeout(closeComposer, 1400);
    } catch (error) {
      $('p-send').disabled = true;
      toast(`送信できませんでした: ${error.message}（再送せず状態を確認してください）`, 'error');
    }
  }

  /* ---------- 操作 ---------- */

  function bind() {
    $('p-view').onclick = () => {
      localStorage.setItem('setlog:view', 'desktop');
      location.reload();
    };
    $('p-back').onclick = () => renderHome();
    $('p-player-back').onclick = closePlayer;
    $('p-composer-close').onclick = closeComposer;
    $('p-pick').onclick = openComposer;
    $('p-prepare').onclick = prepareUpload;
    $('p-send').onclick = sendUpload;

    $('p-file').onchange = (event) => {
      const file = event.target.files[0];
      if (!file) return;
      view.upload = file;
      const preview = $('p-local');
      preview.classList.remove('hidden');
      preview.src = URL.createObjectURL(file);
      preview.play().catch(() => {});
    };

    for (const dot of document.querySelectorAll('.switcher__dot')) {
      dot.onclick = () => {
        if (dot.dataset.page === 'camera') {
          setBar('カメラ', '');
          showPage('camera');
        } else if (view.collectionId) {
          setBar(view.collectionName, `${view.media.length}件`);
          showPage('detail');
        } else {
          renderHome();
        }
      };
    }

    $('p-reply-form').onsubmit = async (event) => {
      event.preventDefault();
      const input = $('p-reply-input');
      const text = input.value.trim();
      const post = view.media[view.index];
      if (!text || !post) return;
      input.disabled = true;
      try {
        await api('/api/replies', {
          method: 'POST',
          body: { collection_id: view.collectionId, text, parent_id: post.message_id },
        });
        input.value = '';
        toast('返信しました。', 'ok');
      } catch (error) {
        toast(`返信できません: ${error.message}`, 'error');
      } finally {
        input.disabled = false;
        input.focus();
      }
    };

    // 上下スワイプで前後の投稿へ
    let startY = null;
    const player = $('p-player');
    player.addEventListener('pointerdown', (event) => { startY = event.clientY; });
    player.addEventListener('pointerup', (event) => {
      if (startY === null) return;
      const delta = event.clientY - startY;
      startY = null;
      if (Math.abs(delta) < 48) return;
      step(delta < 0 ? 1 : -1);
    });
    document.addEventListener('keydown', (event) => {
      if ($('p-player').classList.contains('hidden')) return;
      if (event.key === 'ArrowDown') step(1);
      if (event.key === 'ArrowUp') step(-1);
      if (event.key === 'Escape') closePlayer();
    });
  }

  /* ---------- 起動 ---------- */

  async function start() {
    bind();
    try {
      const { status } = await Setlog.boot();
      if (!status.authenticated) toast('setlogの認証を確認できません。時間をおいて更新してください。', 'error');
      renderHome();
    } catch (error) {
      toast(`読み込みに失敗しました: ${error.message}`, 'error');
    }
  }

  start();
})();
