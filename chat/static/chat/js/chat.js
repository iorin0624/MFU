(() => {
  const root = document.querySelector('[data-event-id]');
  if (!root) return;

  const eventId = Number(root.dataset.eventId);
  const csrfToken = root.dataset.csrf;
  const vapidKey = root.dataset.vapid;
  const meId = root.dataset.me || '';
  const myDisplayName = root.dataset.displayName || '';
  const reactionEmojis = ['💕', '👍', '😆', '😭', '😢', '🫶'];
  const msgBox = document.getElementById('messages');
  const form = document.getElementById('chat-form');
  const bodyInput = document.getElementById('chat-body');
  const pushBtn = document.getElementById('enable-push');

  const fmtDate = new Intl.DateTimeFormat('ja-JP', {
    timeZone: 'Asia/Tokyo',
    year: 'numeric',
    month: 'numeric',
    day: 'numeric',
    weekday: 'short',
  });
  const fmtTime = new Intl.DateTimeFormat('ja-JP', {
    timeZone: 'Asia/Tokyo',
    hour: 'numeric',
    minute: '2-digit',
    hour12: false,
  });

  const socket = io({ transports: ['websocket', 'polling'] });
  let forceLogoutHandled = false;
  socket.on('force_logout', (payload) => {
    if (forceLogoutHandled) return;
    forceLogoutHandled = true;
    try { socket.disconnect(); } catch (_error) {}
    const redirectUrl = String(payload?.redirect || '/external-login/');
    window.location.replace(redirectUrl);
  });
  socket.emit('chat_join', { event_id: eventId });

  socket.on('chat_message', (msg) => {
    renderMessage(msg);
    msgBox.scrollTop = msgBox.scrollHeight;
  });
  socket.on('chat_error', (d) => alert(d.error || '送信失敗'));
  socket.on('chat_reaction_update', (payload) => applyReactionUpdate(payload));


  msgBox?.addEventListener('click', (e) => {
    const reactionBtn = e.target.closest('.chat-reaction-emoji');
    if (!reactionBtn) return;
    const row = reactionBtn.closest('.chat-row');
    const messageId = Number(row?.dataset.messageId || 0);
    const emoji = reactionBtn.dataset.emoji || '';
    if (!messageId || !reactionEmojis.includes(emoji)) return;
    socket.emit('chat_react', { event_id: eventId, message_id: messageId, emoji });
  });

  form?.addEventListener('submit', (e) => {
    e.preventDefault();
    socket.emit('chat_send', { event_id: eventId, body: bodyInput.value });
    bodyInput.value = '';
  });

  pushBtn?.addEventListener('click', async () => {
    if (!('serviceWorker' in navigator) || !('PushManager' in window)) {
      alert('この端末はPush通知に非対応です');
      return;
    }
    const reg = await navigator.serviceWorker.ready;
    const sub = await reg.pushManager.subscribe({
      userVisibleOnly: true,
      applicationServerKey: vapidKey ? urlBase64ToUint8Array(vapidKey) : undefined,
    });
    const res = await fetch('/chat/api/push/subscribe', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        csrf_token: csrfToken,
        sw_scope: new URL(reg.scope).pathname || '/',
        ...sub.toJSON(),
      }),
    });
    if (!res.ok) {
      alert('購読登録に失敗しました');
      return;
    }
    alert('通知を有効化しました');
  });

  function renderMessage(msg) {
    const createdAt = msg.created_at_iso ? new Date(msg.created_at_iso) : new Date();
    const dateLabel = Number.isNaN(createdAt.getTime())
      ? (msg.created_at_jst_date_label || '')
      : formatDateLabel(createdAt);
    const timeLabel = Number.isNaN(createdAt.getTime())
      ? (msg.created_at_jst_time_hm || '')
      : formatTimeLabel(createdAt);

    appendDateDividerIfNeeded(dateLabel);

    const isMe = (msg.sender_id && meId && String(msg.sender_id) === String(meId))
      || (!msg.sender_id && msg.sender_display_name && myDisplayName && msg.sender_display_name === myDisplayName);

    const row = document.createElement('div');
    row.className = `chat-row ${isMe ? 'me' : 'other'}`;
    row.dataset.messageId = String(msg.id || '');
    if (msg.sender_id) row.dataset.senderId = msg.sender_id;
    if (msg.created_at_iso) row.dataset.createdAt = msg.created_at_iso;

    if (!isMe) {
      const sender = document.createElement('div');
      sender.className = 'chat-sender';
      sender.textContent = msg.sender_display_name || 'Unknown';
      row.appendChild(sender);
    }

    const wrap = document.createElement('div');
    wrap.className = 'chat-bubble-wrap';

    const bubble = document.createElement('div');
    bubble.className = 'chat-bubble';
    bubble.innerHTML = msg.body || '';

    const time = document.createElement('div');
    time.className = 'chat-time';
    time.textContent = timeLabel;

    wrap.appendChild(bubble);
    wrap.appendChild(time);
    row.appendChild(wrap);

    const reactionBox = document.createElement('div');
    reactionBox.className = 'chat-reaction-box';
    reactionBox.dataset.myReaction = msg.my_reaction || '';
    const panel = document.createElement('div');
    panel.className = 'chat-reaction-panel';
    for (const emoji of reactionEmojis) {
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'chat-reaction-emoji';
      btn.dataset.emoji = emoji;
      btn.textContent = emoji;
      panel.appendChild(btn);
    }
    const summary = document.createElement('div');
    summary.className = 'chat-reaction-summary';
    reactionBox.appendChild(panel);
    reactionBox.appendChild(summary);
    row.appendChild(reactionBox);
    renderReactionSummary(row, msg.reactions_summary || []);

    msgBox.appendChild(row);
  }

  function appendDateDividerIfNeeded(dateLabel) {
    if (!dateLabel) return;
    const dividers = msgBox.querySelectorAll('.chat-date-divider span');
    const lastDivider = dividers.length ? dividers[dividers.length - 1] : null;
    if (lastDivider?.textContent === dateLabel) return;

    const divider = document.createElement('div');
    divider.className = 'chat-date-divider';
    divider.dataset.dateLabel = dateLabel;

    const label = document.createElement('span');
    label.textContent = dateLabel;
    divider.appendChild(label);
    msgBox.appendChild(divider);
  }

  function formatDateLabel(date) {
    const parts = fmtDate.formatToParts(date);
    const get = (type) => parts.find((p) => p.type === type)?.value || '';
    return `${get('year')}/${get('month')}/${get('day')}(${get('weekday')})`;
  }

  function formatTimeLabel(date) {
    return fmtTime.format(date);
  }



  function applyReactionUpdate(payload) {
    const messageId = Number(payload?.message_id || 0);
    if (!messageId) return;
    const row = msgBox.querySelector(`.chat-row[data-message-id="${messageId}"]`);
    if (!row) return;
    const changed = payload?.changed || {};
    if (meId && changed.actor_type && `${changed.actor_type}:${changed.actor_id}` === meId) {
      const box = row.querySelector('.chat-reaction-box');
      if (box) box.dataset.myReaction = changed.emoji || '';
    }
    renderReactionSummary(row, payload?.reactions || []);
  }

  function renderReactionSummary(row, reactions) {
    const box = row.querySelector('.chat-reaction-box');
    const summary = box?.querySelector('.chat-reaction-summary');
    if (!summary) return;
    summary.innerHTML = '';
    for (const item of reactions || []) {
      if (!reactionEmojis.includes(item?.emoji || '') || Number(item?.count || 0) <= 0) continue;
      const chip = document.createElement('span');
      chip.className = 'chat-reaction-chip';
      chip.textContent = `${item.emoji} ${item.count}`;
      summary.appendChild(chip);
    }
    const myReaction = box?.dataset.myReaction || '';
    box?.querySelectorAll('.chat-reaction-emoji').forEach((btn) => {
      btn.classList.toggle('is-active', btn.dataset.emoji === myReaction);
    });
  }

  function urlBase64ToUint8Array(base64String) {
    const padding = '='.repeat((4 - (base64String.length % 4)) % 4);
    const base64 = (base64String + padding).replace(/-/g, '+').replace(/_/g, '/');
    const rawData = window.atob(base64);
    return Uint8Array.from([...rawData].map((c) => c.charCodeAt(0)));
  }
})();
