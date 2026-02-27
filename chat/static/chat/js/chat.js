(() => {
  const root = document.querySelector('[data-event-id]');
  if (!root) return;

  const eventId = Number(root.dataset.eventId);
  const csrfToken = root.dataset.csrf;
  const vapidKey = root.dataset.vapid;
  const msgBox = document.getElementById('messages');
  const form = document.getElementById('chat-form');
  const bodyInput = document.getElementById('chat-body');
  const pushBtn = document.getElementById('enable-push');

  const socket = io({ transports: ['websocket', 'polling'] });
  socket.emit('chat_join', { event_id: eventId });

  socket.on('chat_message', (m) => {
    const div = document.createElement('div');
    div.className = 'mb-2';
    div.innerHTML = `<strong>${m.sender_display_name}</strong> <small class="text-muted">${m.created_at}</small><br>${m.body}`;
    msgBox.appendChild(div);
    msgBox.scrollTop = msgBox.scrollHeight;
  });
  socket.on('chat_error', (d) => alert(d.error || '送信失敗'));

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
      body: JSON.stringify({ csrf_token: csrfToken, ...sub.toJSON() }),
    });
    if (!res.ok) {
      alert('購読登録に失敗しました');
      return;
    }
    alert('通知を有効化しました');
  });

  function urlBase64ToUint8Array(base64String) {
    const padding = '='.repeat((4 - (base64String.length % 4)) % 4);
    const base64 = (base64String + padding).replace(/-/g, '+').replace(/_/g, '/');
    const rawData = window.atob(base64);
    return Uint8Array.from([...rawData].map((c) => c.charCodeAt(0)));
  }
})();
