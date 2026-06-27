(() => {
    const apiUrl = "/mock";
    const createFolderUrl = "/mock";
    const uploadImagesUrl = "/mock";
    const pasteImagesUrl = "/mock";
    const renameEntryUrl = "/mock";
    const deleteEntryUrl = "/mock";
    const moveEntryUrl = "/mock";
    const illustrationUrl = "/mock";
    const illustrationSaveUrl = "/mock";
    const illustrationJobUrlBase = "/mock";
    const illustrationJobSaveUrlBase = "/mock";
    const instagramFetchUrl = "/mock";
    const instagramSaveUrl = "/mock";
    const instagramJobUrlBase = "/mock";
    const instagramNextNumberUrl = "/mock";
    const videoFetchUrl = "/mock";
    const videoSaveUrl = "/mock";
    const videoJobUrlBase = "/mock";
    const thumbnailJobUrlBase = "/mock";
    const csrfToken = document.querySelector('meta[name="csrf-token"]')?.content || '';
    const instagramSettingsKey = 'mfu.imageViewer.instagramSettings';
    const videoDownloaderSettingsKey = 'mfu.imageViewer.videoDownloaderSettings';
    const videoSettingsKey = 'mfu.imageViewer.videoSettings';
    const illustrationSettingsKey = 'mfu.imageViewer.illustrationSettings';
    const defaultIllustrationPrompt = '画像を詳細なアニメの美意識で再構成してください。 表情豊かな瞳、なめらかな網掛けセルの色使い、はっきりした線画を使用します。アニメのシーンに典型的な身ぶりと雰囲気で、心情と登場人物の存在を強調してください。 服とアクセサリーを参考にしてイラストを描いてください。背景は白地で、人物は全身を描いてください。 服と靴の装飾はできるだけ綺麗にこだわってください。 顔は、20代女性を生成して置き換えてください。 生成が完了したら完了したと報告をください。';
    const state = {
      images: [],
      folders: [''],
      currentFolder: '',
      windows: new Map(),
      activeId: 'explorer',
      z: 10,
      nextViewer: 1,
      nextExplorer: 1,
      selectedPath: '',
      fitByWindow: new Map(),
      zoomByWindow: new Map(),
      wheelLockByWindow: new Map(),
      sortByFolder: {},
      viewSizeByFolder: {},
      resetFileScrollNext: false,
      selectedEntry: null,
      contextEntry: null,
      thumbObserver: null,
      thumbHydrateRaf: 0,
      thumbSettleTimer: 0,
      explorerClones: new Map(),
      instagram: { shortcode: '', jobId: '', images: [] },
      videoDownloader: { identifier: '', jobId: '', videos: [] }
    };

    const desktopGrid = document.getElementById('desktopGrid');
    const explorerWindow = document.getElementById('explorerWindow');
    const folderList = document.getElementById('folderList');
    const fileGrid = document.getElementById('fileGrid');
    const pathBox = document.getElementById('pathBox');
    const sortBtn = document.getElementById('sortBtn');
    const thumbBtn = document.getElementById('thumbBtn');
    const regenThumbBtn = document.getElementById('regenThumbBtn');
    const viewButtons = Array.from(document.querySelectorAll('[data-view-size]'));
    const taskList = document.getElementById('taskList');
    const clock = document.getElementById('clock');
    const uploadBatchMaxBytes = 80 * 1024 * 1024;
    const uploadBatchMaxFiles = 20;

    const icons = {
      explorer: 'EXP',
      viewer: 'IMG',
      instagram: 'IG',
      video: 'MOV',
      downloader: 'VD',
      progress: '%'
    };

    async function fetchJson(url, options = {}) {
      const headers = new Headers(options.headers || {});
      headers.set('Accept', 'application/json');
      if (options.body && !(options.body instanceof FormData) && !headers.has('Content-Type')) headers.set('Content-Type', 'application/json');
      if (csrfToken && !headers.has('X-CSRF-Token')) headers.set('X-CSRF-Token', csrfToken);
      const resp = await fetch(url, { ...options, headers });
      const contentType = resp.headers.get('content-type') || '';
      if (!contentType.includes('application/json')) {
        const text = await resp.text();
        const preview = text.replace(/\s+/g, ' ').slice(0, 80);
        if (resp.status === 413) {
          throw new Error('アップロード容量が大きすぎます。1ファイルが大きすぎる可能性があります。');
        }
        throw new Error(`Non-JSON response (${resp.status}) ${preview}`);
      }
      const data = await resp.json();
      if (!resp.ok || data.ok === false) throw new Error(data.error || data.message || `HTTP ${resp.status}`);
      return data;
    }

    function sleep(ms) {
      return new Promise(resolve => setTimeout(resolve, ms));
    }

    async function waitInstagramJob(jobId, onTick) {
      const url = instagramJobUrlBase.replace('__JOB_ID__', encodeURIComponent(jobId));
      const startedAt = Date.now();
      let count = 0;
      while (Date.now() - startedAt < 45000) {
        await sleep(count < 3 ? 700 : 1200);
        count += 1;
        const data = await fetchJson(url);
        if (onTick) onTick(count, data);
        if (data.status === 'done') return data;
        if (data.status === 'error') throw new Error(data.error || '取得に失敗しました。');
      }
      throw new Error('取得がタイムアウトしました。もう一度お試しください。');
    }

    async function waitVideoJob(jobId, onTick) {
      const url = videoJobUrlBase.replace('__JOB_ID__', encodeURIComponent(jobId));
      const startedAt = Date.now();
      let count = 0;
      while (Date.now() - startedAt < 120000) {
        await sleep(count < 3 ? 700 : 1200);
        count += 1;
        if (onTick) onTick(count);
        const data = await fetchJson(url);
        if (data.status === 'done') return data;
        if (data.status === 'error') throw new Error(data.error || '取得に失敗しました。');
      }
      throw new Error('取得がタイムアウトしました。もう一度お試しください。');
    }

    async function waitIllustrationJob(jobId, onTick) {
      const url = illustrationJobUrlBase.replace('__JOB_ID__', encodeURIComponent(jobId));
      const startedAt = Date.now();
      let count = 0;
      while (true) {
        await sleep(count < 3 ? 900 : 2500);
        count += 1;
        if (onTick) onTick(count, Date.now() - startedAt);
        const data = await fetchJson(url);
        if (data.status === 'done') return data;
        if (data.status === 'error') throw new Error(data.error || '生成に失敗しました。');
      }
    }

    function formatBytes(bytes) {
      const n = Number(bytes || 0);
      if (n < 1024) return `${n} B`;
      if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
      return `${(n / 1024 / 1024).toFixed(1)} MB`;
    }

    function folderLabel(folder) {
      return folder ? folder.split('/').pop() : 'uploads';
    }

    function compareNaturalText(a, b) {
      const ax = String(a || '').match(/\d+|\D+/g) || [''];
      const bx = String(b || '').match(/\d+|\D+/g) || [''];
      const len = Math.max(ax.length, bx.length);
      for (let i = 0; i < len; i += 1) {
        const av = ax[i] || '';
        const bv = bx[i] || '';
        const an = /^\d+$/.test(av);
        const bn = /^\d+$/.test(bv);
        if (an && bn) {
          const diff = Number(av) - Number(bv);
          if (diff) return diff;
          if (av.length !== bv.length) return av.length - bv.length;
          continue;
        }
        const diff = av.localeCompare(bv, 'ja', { numeric: true, sensitivity: 'base' });
        if (diff) return diff;
      }
      return 0;
    }

    function compareFolderPath(a, b) {
      if ((a || '') === (b || '')) return 0;
      if (!a) return -1;
      if (!b) return 1;
      const ap = String(a).split('/');
      const bp = String(b).split('/');
      const len = Math.max(ap.length, bp.length);
      for (let i = 0; i < len; i += 1) {
        if (ap[i] === undefined) return -1;
        if (bp[i] === undefined) return 1;
        const diff = compareNaturalText(ap[i], bp[i]);
        if (diff) return diff;
      }
      return 0;
    }

    function setCurrentFolder(folder) {
      if (folder === state.currentFolder) return;
      state.currentFolder = folder;
      state.selectedPath = '';
      state.selectedEntry = null;
      state.resetFileScrollNext = true;
      renderExplorer();
    }

    function loadSortState() {
      try {
        const raw = localStorage.getItem('imageViewerSortByFolder');
        state.sortByFolder = raw ? JSON.parse(raw) : {};
      } catch (_) {
        state.sortByFolder = {};
      }
    }

    function saveSortState() {
      try {
        localStorage.setItem('imageViewerSortByFolder', JSON.stringify(state.sortByFolder));
      } catch (_) {}
    }

    function sortDirection(folder = state.currentFolder) {
      return state.sortByFolder[folder || ''] === 'desc' ? 'desc' : 'asc';
    }

    function setSortDirection(folder, direction) {
      state.sortByFolder[folder || ''] = direction === 'desc' ? 'desc' : 'asc';
      saveSortState();
    }

    function loadViewSizeState() {
      try {
        const raw = localStorage.getItem('imageViewerViewSizeByFolder');
        state.viewSizeByFolder = raw ? JSON.parse(raw) : {};
      } catch (_) {
        state.viewSizeByFolder = {};
      }
    }

    function saveViewSizeState() {
      try {
        localStorage.setItem('imageViewerViewSizeByFolder', JSON.stringify(state.viewSizeByFolder));
      } catch (_) {}
    }

    function viewSize(folder = state.currentFolder) {
      const value = state.viewSizeByFolder[folder || ''];
      return ['xl', 'lg', 'md', 'sm'].includes(value) ? value : 'lg';
    }

    function setViewSize(folder, size) {
      state.viewSizeByFolder[folder || ''] = ['xl', 'lg', 'md', 'sm'].includes(size) ? size : 'lg';
      saveViewSizeState();
    }

    function activate(id) {
      const win = state.windows.get(id);
      if (!win) return;
      win.el.classList.remove('minimized');
      win.minimized = false;
      state.activeId = id;
      win.el.style.zIndex = String(++state.z);
      renderTasks();
      document.querySelectorAll('.window').forEach(el => el.classList.toggle('active', el.dataset.windowId === id));
    }

    function minimize(id) {
      const win = state.windows.get(id);
      if (!win) return;
      win.el.classList.add('minimized');
      win.minimized = true;
      if (state.activeId === id) {
        const next = Array.from(state.windows.values()).filter(w => !w.minimized && w.id !== id).pop();
        if (next) activate(next.id);
      }
      renderTasks();
      if (win.kind === 'viewer' && state.fitByWindow.get(id)) requestAnimationFrame(() => applyZoom(id));
    }

    function toggleMaximize(id) {
      const win = state.windows.get(id);
      if (!win) return;
      activate(id);
      const el = win.el;
      if (!win.maximized) {
        win.restoreRect = {
          left: el.style.left || `${el.offsetLeft}px`,
          top: el.style.top || `${el.offsetTop}px`,
          width: el.style.width || `${el.offsetWidth}px`,
          height: el.style.height || `${el.offsetHeight}px`
        };
        el.style.left = '0px';
        el.style.top = '0px';
        el.style.width = '100vw';
        el.style.height = 'calc(100vh - var(--taskbar-h))';
        win.maximized = true;
        el.querySelector('[data-action="maximize"]')?.setAttribute('title', '元に戻す');
      } else {
        const rect = win.restoreRect || {};
        el.style.left = rect.left || '26px';
        el.style.top = rect.top || '24px';
        el.style.width = rect.width || '';
        el.style.height = rect.height || '';
        win.maximized = false;
        el.querySelector('[data-action="maximize"]')?.setAttribute('title', '最大化');
      }
      renderTasks();
      if (win.kind === 'viewer' && state.fitByWindow.get(id)) {
        requestAnimationFrame(() => applyZoom(id));
      }
    }

    function closeWindow(id) {
      if (id === 'explorer') {
        minimize(id);
        return;
      }
      const win = state.windows.get(id);
      if (!win) return;
      win.el.querySelectorAll('video').forEach(video => video.pause());
      win.el.remove();
      if (win.kind === 'explorer' && id !== 'explorer') {
        state.explorerClones.delete(id);
      }
      state.windows.delete(id);
      state.fitByWindow.delete(id);
      state.zoomByWindow.delete(id);
      state.wheelLockByWindow.delete(id);
      if (state.activeId === id) activate('explorer');
      renderTasks();
    }

    function renderTasks() {
      taskList.innerHTML = '';
      for (const win of state.windows.values()) {
        const btn = document.createElement('button');
        btn.type = 'button';
        btn.className = `task${state.activeId === win.id && !win.minimized ? ' active' : ''}${win.minimized ? ' minimized' : ''}`;
        btn.title = win.title;
        btn.innerHTML = `<span>${icons[win.kind] || icons.viewer}</span><span></span>`;
        btn.lastElementChild.textContent = win.title;
        btn.addEventListener('click', () => {
          if (state.activeId === win.id && !win.minimized) minimize(win.id);
          else activate(win.id);
        });
        taskList.appendChild(btn);
      }
    }

    function registerWindow(id, el, title, kind) {
      state.windows.set(id, { id, el, title, kind, minimized: false });
      el.dataset.windowId = id;
      el.style.zIndex = String(++state.z);
      wireWindow(el);
      activate(id);
    }

    function wireWindow(win) {
      win.addEventListener('pointerdown', () => activate(win.dataset.windowId));
      win.querySelectorAll('[data-action]').forEach(btn => {
        btn.addEventListener('click', (event) => {
          event.stopPropagation();
          const id = win.dataset.windowId;
          const action = btn.dataset.action;
          if (action === 'minimize') minimize(id);
          if (action === 'close') closeWindow(id);
          if (action === 'maximize') toggleMaximize(id);
        });
      });

      const handle = win.querySelector('[data-drag-handle]');
      if (handle) {
        handle.addEventListener('dblclick', (event) => {
          if (event.target.closest('button')) return;
          event.preventDefault();
          toggleMaximize(win.dataset.windowId);
        });
        handle.addEventListener('pointerdown', (event) => {
          if (event.target.closest('button')) return;
          activate(win.dataset.windowId);
          const winState = state.windows.get(win.dataset.windowId);
          if (winState?.maximized) toggleMaximize(win.dataset.windowId);
          const rect = win.getBoundingClientRect();
          const startX = event.clientX;
          const startY = event.clientY;
          const offsetX = startX - rect.left;
          const offsetY = startY - rect.top;
          handle.setPointerCapture(event.pointerId);
          const move = (e) => {
            const maxLeft = window.innerWidth - 80;
            const maxTop = window.innerHeight - 80;
            const left = Math.max(0, Math.min(maxLeft, e.clientX - offsetX));
            const top = Math.max(0, Math.min(maxTop, e.clientY - offsetY));
            win.style.left = `${left}px`;
            win.style.top = `${top}px`;
          };
          const up = () => {
            handle.removeEventListener('pointermove', move);
            handle.removeEventListener('pointerup', up);
            handle.removeEventListener('pointercancel', up);
          };
          handle.addEventListener('pointermove', move);
          handle.addEventListener('pointerup', up);
          handle.addEventListener('pointercancel', up);
        });
      }

      const resize = win.querySelector('[data-resize-handle]');
      if (resize) {
        resize.addEventListener('pointerdown', (event) => {
          activate(win.dataset.windowId);
          const winState = state.windows.get(win.dataset.windowId);
          if (winState?.maximized) toggleMaximize(win.dataset.windowId);
          event.stopPropagation();
          const rect = win.getBoundingClientRect();
          const startX = event.clientX;
          const startY = event.clientY;
          resize.setPointerCapture(event.pointerId);
          const move = (e) => {
            const w = Math.max(280, rect.width + e.clientX - startX);
            const h = Math.max(190, rect.height + e.clientY - startY);
            win.style.width = `${Math.min(w, window.innerWidth - rect.left)}px`;
            win.style.height = `${Math.min(h, window.innerHeight - rect.top - 48)}px`;
          };
          const up = () => {
            resize.removeEventListener('pointermove', move);
            resize.removeEventListener('pointerup', up);
            resize.removeEventListener('pointercancel', up);
            const id = win.dataset.windowId;
            if (state.windows.get(id)?.kind === 'viewer' && state.fitByWindow.get(id)) {
              requestAnimationFrame(() => applyZoom(id));
            }
          };
          resize.addEventListener('pointermove', move);
          resize.addEventListener('pointerup', up);
          resize.addEventListener('pointercancel', up);
        });
      }
    }

    function imagesInCurrentFolder() {
      return imagesForFolder(state.currentFolder);
    }

    function imagesForFolder(folder) {
      const direction = sortDirection(folder);
      return state.images
        .filter(img => (img.folder || '') === (folder || ''))
        .sort((a, b) => {
          const result = String(a.name || '').localeCompare(String(b.name || ''), 'ja', { numeric: true, sensitivity: 'base' });
          return direction === 'desc' ? -result : result;
        });
    }

    function renderFolders() {
      folderList.innerHTML = '';
      [...state.folders].sort(compareFolderPath).forEach(folder => {
        const btn = document.createElement('button');
        btn.type = 'button';
        btn.className = `folder${folder === state.currentFolder ? ' active' : ''}`;
        btn.dataset.folder = folder;
        btn.draggable = Boolean(folder);
        btn.style.paddingLeft = `${8 + Math.max(0, folder.split('/').length - 1) * 12}px`;
        btn.innerHTML = `<span class="label"></span>`;
        btn.lastElementChild.textContent = folderLabel(folder);
        btn.title = folder || 'uploads';
        btn.addEventListener('click', () => {
          setCurrentFolder(folder);
        });
        btn.addEventListener('contextmenu', (event) => {
          showContextMenu(event, { type: 'folder', path: folder });
        });
        btn.addEventListener('dragstart', (event) => {
          if (!folder) {
            event.preventDefault();
            return;
          }
          setSelectedFolder(folder);
          setDragEntry(event, { type: 'folder', path: folder });
        });
        folderList.appendChild(btn);
      });
    }

    function updateSelectedFileElement() {
      fileGrid.querySelectorAll('.file').forEach(el => {
        el.classList.toggle('selected', el.dataset.path === state.selectedPath);
      });
    }

    function setSelectedFile(path) {
      state.selectedPath = path;
      state.selectedEntry = { type: 'file', path };
      updateSelectedFileElement();
    }

    function setSelectedFolder(folder) {
      state.selectedEntry = { type: 'folder', path: folder || '' };
      state.selectedPath = '';
      updateSelectedFileElement();
    }

    function entryName(entry) {
      if (!entry) return '';
      if (entry.type === 'folder') return folderLabel(entry.path);
      const item = state.images.find(img => img.path === entry.path);
      return item?.name || String(entry.path || '').split('/').pop() || '';
    }

    function isRootFolderEntry(entry) {
      return entry?.type === 'folder' && !entry.path;
    }

    function getDragEntry(event) {
      const raw = event.dataTransfer?.getData('application/x-mfu-image-viewer-entry') || '';
      if (!raw) return null;
      try {
        const entry = JSON.parse(raw);
        if (!entry || !['file', 'folder'].includes(entry.type)) return null;
        return { type: entry.type, path: String(entry.path || '') };
      } catch (_) {
        return null;
      }
    }

    function setDragEntry(event, entry) {
      event.dataTransfer.effectAllowed = 'move';
      event.dataTransfer.setData('application/x-mfu-image-viewer-entry', JSON.stringify(entry));
      event.dataTransfer.setData('text/plain', entry.path || 'uploads');
    }

    function closeContextMenu() {
      document.querySelectorAll('.context-menu').forEach(menu => menu.remove());
      state.contextEntry = null;
    }

    function showContextMenu(event, entry) {
      event.preventDefault();
      event.stopPropagation();
      closeContextMenu();
      state.contextEntry = entry;
      if (entry.type === 'file') setSelectedFile(entry.path);
      else setSelectedFolder(entry.path);
      const menu = document.createElement('div');
      menu.className = 'context-menu open';
      const canOpen = entry.type === 'file';
      const canIllustrate = entry.type === 'file' && /\.(jpe?g|png|gif|webp)$/i.test(entry.path || '');
      const locked = isRootFolderEntry(entry);
      menu.innerHTML = `
        ${canOpen ? '<button type="button" data-menu-action="open">開く</button><div class="separator"></div>' : ''}
        ${canOpen ? '<button type="button" data-menu-action="copy">コピー</button><div class="separator"></div>' : ''}
        ${canIllustrate ? '<button type="button" data-menu-action="illustrate">OpenAIでイラスト化</button><div class="separator"></div>' : ''}
        <button type="button" data-menu-action="rename" ${locked ? 'disabled' : ''}>名前の変更</button>
        <button type="button" data-menu-action="move" ${locked ? 'disabled' : ''}>移動...</button>
        <button type="button" data-menu-action="delete" ${locked ? 'disabled' : ''}>削除</button>
      `;
      document.body.appendChild(menu);
      const x = Math.min(event.clientX, window.innerWidth - menu.offsetWidth - 8);
      const y = Math.min(event.clientY, window.innerHeight - menu.offsetHeight - 8);
      menu.style.left = `${Math.max(4, x)}px`;
      menu.style.top = `${Math.max(4, y)}px`;
      menu.addEventListener('click', (clickEvent) => {
        const button = clickEvent.target.closest('button[data-menu-action]');
        if (!button || button.disabled) return;
        const action = button.dataset.menuAction;
        closeContextMenu();
        if (action === 'open') openSelectedEntry();
        if (action === 'copy') copyEntryToClipboard(entry).catch(err => alert(err.message || 'コピーに失敗しました。'));
        if (action === 'illustrate') openIllustrationDialog(entry);
        if (action === 'rename') renameEntry(entry);
        if (action === 'move') chooseMoveEntry(entry);
        if (action === 'delete') deleteEntry(entry);
      });
    }

    function openDialog(title, bodyHtml) {
      const id = `dialog-${Date.now()}-${Math.random().toString(16).slice(2)}`;
      const el = document.createElement('article');
      el.className = 'xp-dialog';
      el.dataset.dialogId = id;
      el.style.left = `${Math.max(110, Math.round((window.innerWidth - 420) / 2))}px`;
      el.style.top = `${Math.max(72, Math.round((window.innerHeight - 210) / 2))}px`;
      el.innerHTML = `
        <header class="titlebar" data-drag-handle>
          <div class="title-icon" aria-hidden="true">?</div>
          <div class="title-text"></div>
          <div class="win-actions">
            <button class="win-btn close" type="button" data-dialog-action="cancel" title="閉じる">X</button>
          </div>
        </header>
        <div class="xp-dialog-body">${bodyHtml}</div>
      `;
      el.querySelector('.title-text').textContent = title;
      desktopGrid.appendChild(el);
      wireDialogDrag(el);
      return el;
    }

    function wireDialogDrag(dialog) {
      const handle = dialog.querySelector('[data-drag-handle]');
      if (!handle) return;
      handle.addEventListener('pointerdown', (event) => {
        if (event.target.closest('button')) return;
        event.preventDefault();
        const start = { x: event.clientX, y: event.clientY, left: dialog.offsetLeft, top: dialog.offsetTop };
        handle.setPointerCapture(event.pointerId);
        const move = (moveEvent) => {
          dialog.style.left = `${Math.max(0, start.left + moveEvent.clientX - start.x)}px`;
          dialog.style.top = `${Math.max(0, start.top + moveEvent.clientY - start.y)}px`;
        };
        const up = () => {
          handle.removeEventListener('pointermove', move);
          handle.removeEventListener('pointerup', up);
          handle.removeEventListener('pointercancel', up);
        };
        handle.addEventListener('pointermove', move);
        handle.addEventListener('pointerup', up);
        handle.addEventListener('pointercancel', up);
      });
    }

    function dialogPrompt(title, message, value = '') {
      return new Promise(resolve => {
        const dialog = openDialog(title, `
          <div>${message}</div>
          <input class="dialog-input" type="text">
          <div class="xp-dialog-actions">
            <button type="button" data-dialog-action="ok">OK</button>
            <button type="button" data-dialog-action="cancel">キャンセル</button>
          </div>
        `);
        const input = dialog.querySelector('.dialog-input');
        input.value = value;
        input.focus();
        input.select();
        const finish = (result) => {
          dialog.remove();
          resolve(result);
        };
        dialog.addEventListener('click', (event) => {
          const action = event.target.closest('[data-dialog-action]')?.dataset.dialogAction;
          if (action === 'ok') finish(input.value.trim());
          if (action === 'cancel') finish(null);
        });
        input.addEventListener('keydown', (event) => {
          if (event.key === 'Enter') finish(input.value.trim());
          if (event.key === 'Escape') finish(null);
        });
      });
    }

    function dialogConfirm(title, message) {
      return new Promise(resolve => {
        const dialog = openDialog(title, `
          <div>${message}</div>
          <div class="xp-dialog-actions">
            <button type="button" data-dialog-action="ok">OK</button>
            <button type="button" data-dialog-action="cancel">キャンセル</button>
          </div>
        `);
        const finish = (result) => {
          dialog.remove();
          resolve(result);
        };
        dialog.addEventListener('click', (event) => {
          const action = event.target.closest('[data-dialog-action]')?.dataset.dialogAction;
          if (action === 'ok') finish(true);
          if (action === 'cancel') finish(false);
        });
      });
    }

    function dialogMoveDestination(entry) {
      return new Promise(resolve => {
        const options = [...state.folders]
          .sort(compareFolderPath)
          .filter(folder => !(entry.type === 'folder' && folder === entry.path))
          .map(folder => `<option value="${folder.replace(/"/g, '&quot;')}">${folder || 'uploads'}</option>`)
          .join('');
        const dialog = openDialog('移動', `
          <div>${entryName(entry)} の移動先を選択してください。</div>
          <select class="dialog-select">${options}</select>
          <div class="xp-dialog-actions">
            <button type="button" data-dialog-action="ok">移動</button>
            <button type="button" data-dialog-action="cancel">キャンセル</button>
          </div>
        `);
        const select = dialog.querySelector('.dialog-select');
        select.value = state.currentFolder || '';
        select.focus();
        const finish = (result) => {
          dialog.remove();
          resolve(result);
        };
        dialog.addEventListener('click', (event) => {
          const action = event.target.closest('[data-dialog-action]')?.dataset.dialogAction;
          if (action === 'ok') finish(select.value);
          if (action === 'cancel') finish(null);
        });
      });
    }

    function loadIllustrationSettings() {
      try {
        const raw = localStorage.getItem(illustrationSettingsKey);
        return raw ? JSON.parse(raw) : {};
      } catch (_) {
        return {};
      }
    }

    function saveIllustrationSettings(settings) {
      localStorage.setItem(illustrationSettingsKey, JSON.stringify(settings));
    }

    function openIllustrationDialog(entry = state.selectedEntry) {
      if (!entry || entry.type !== 'file') return;
      const image = state.images.find(img => img.path === entry.path);
      if (!image || image.mediaType === 'video') {
        alert('画像ファイルを選択してください。');
        return;
      }
      const settings = loadIllustrationSettings();
      const dialog = openDialog('OpenAIでイラスト化', `
        <div>${image.name} をイラスト化します。</div>
        <label>保存先フォルダー<input class="illustration-folder" type="text"></label>
        <label>モデル
          <select class="illustration-model">
            <option value="gpt-image-1.5">gpt-image-1.5（安定）</option>
            <option value="gpt-image-2">gpt-image-2（最新）</option>
            <option value="gpt-image-1">gpt-image-1</option>
            <option value="gpt-image-1-mini">gpt-image-1-mini</option>
          </select>
        </label>
        <label>品質
          <select class="illustration-quality">
            <option value="medium">標準</option>
            <option value="high">高品質</option>
            <option value="low">高速</option>
            <option value="auto">自動</option>
          </select>
        </label>
        <label>プロンプト<textarea class="illustration-prompt"></textarea></label>
        <div class="ig-status illustration-status"></div>
        <div class="xp-dialog-actions">
          <button type="button" data-dialog-action="generate">生成</button>
          <button type="button" data-dialog-action="cancel">キャンセル</button>
        </div>
      `);
      const folderInput = dialog.querySelector('.illustration-folder');
      const modelInput = dialog.querySelector('.illustration-model');
      const qualityInput = dialog.querySelector('.illustration-quality');
      const promptInput = dialog.querySelector('.illustration-prompt');
      const status = dialog.querySelector('.illustration-status');
      const generateBtn = dialog.querySelector('[data-dialog-action="generate"]');
      folderInput.value = settings.folder ?? state.currentFolder ?? image.folder ?? '';
      modelInput.value = settings.model || 'gpt-image-1.5';
      qualityInput.value = settings.quality || 'medium';
      promptInput.value = settings.prompt || defaultIllustrationPrompt;
      promptInput.focus();

      dialog.addEventListener('click', async (event) => {
        const action = event.target.closest('[data-dialog-action]')?.dataset.dialogAction;
        if (action === 'cancel') {
          dialog.remove();
          return;
        }
        if (action !== 'generate') return;
        generateBtn.disabled = true;
        status.textContent = '生成ジョブを開始しています...';
        const folder = folderInput.value || '';
        const model = modelInput.value || 'gpt-image-1.5';
        const quality = qualityInput.value || 'medium';
        const prompt = promptInput.value || defaultIllustrationPrompt;
        saveIllustrationSettings({ folder, model, quality, prompt });
        try {
          const data = await fetchJson(illustrationUrl, {
            method: 'POST',
            body: JSON.stringify({ path: image.path, folder, model, quality, prompt })
          });
          const result = await waitIllustrationJob(data.jobId, (count, elapsedMs) => {
            const minutes = Math.floor(elapsedMs / 60000);
            const seconds = Math.floor((elapsedMs % 60000) / 1000);
            status.textContent = `生成中... ${minutes}:${String(seconds).padStart(2, '0')}`;
          });
          showIllustrationPreview(dialog, data.jobId, result, folder);
        } catch (err) {
          status.textContent = err.message || '生成に失敗しました。';
          generateBtn.disabled = false;
        }
      });
    }

    function showIllustrationPreview(dialog, jobId, result, folder) {
      const previewUrl = result.generated?.previewUrl;
      const model = result.generated?.model || '';
      const quality = result.generated?.quality || '';
      const body = dialog.querySelector('.xp-dialog-body');
      body.innerHTML = `
        <div>生成結果を確認してください。保存すると指定フォルダーへ連番保存します。</div>
        <div class="illustration-preview">${previewUrl ? `<img src="${previewUrl}" alt="生成プレビュー">` : 'プレビューを取得できません。'}</div>
        <div class="ig-status">モデル: ${model} / 品質: ${quality}</div>
        <div class="xp-dialog-actions">
          <button type="button" data-preview-action="save">保存</button>
          <button type="button" data-preview-action="cancel">キャンセル</button>
        </div>
      `;
      const saveBtn = body.querySelector('[data-preview-action="save"]');
      const cancelBtn = body.querySelector('[data-preview-action="cancel"]');
      const status = body.querySelector('.ig-status');
      cancelBtn.addEventListener('click', () => dialog.remove());
      saveBtn.addEventListener('click', async () => {
        saveBtn.disabled = true;
        status.textContent = '保存中...';
        try {
          const saveUrl = illustrationJobSaveUrlBase.replace('__JOB_ID__', encodeURIComponent(jobId)) || illustrationSaveUrl;
          const data = await fetchJson(saveUrl, {
            method: 'POST',
            body: JSON.stringify({ jobId, folder })
          });
          status.textContent = `保存しました: ${data.saved?.name || ''}`;
          await loadImages();
          if (data.saved?.path) {
            state.currentFolder = data.saved.path.includes('/') ? data.saved.path.split('/').slice(0, -1).join('/') : '';
            renderAllExplorers();
            setSelectedFile(data.saved.path);
          }
          setTimeout(() => dialog.remove(), 900);
        } catch (err) {
          status.textContent = err.message || '保存に失敗しました。';
          saveBtn.disabled = false;
        }
      });
    }

    async function renameEntry(entry = state.selectedEntry) {
      if (!entry || isRootFolderEntry(entry)) return;
      const currentName = entryName(entry);
      const nextName = await dialogPrompt('名前の変更', '新しい名前を入力してください。', currentName);
      if (!nextName || nextName === currentName) return;
      const data = await fetchJson(renameEntryUrl, {
        method: 'POST',
        body: JSON.stringify({ type: entry.type, path: entry.path, name: nextName })
      });
      await loadImages();
      if (entry.type === 'file') setSelectedFile(data.path || entry.path);
      else setCurrentFolder(data.folder || data.path || '');
    }

    async function deleteEntry(entry = state.selectedEntry) {
      if (!entry || isRootFolderEntry(entry)) return;
      const ok = await dialogConfirm('削除の確認', `${entryName(entry)} を削除します。よろしいですか？`);
      if (!ok) return;
      await fetchJson(deleteEntryUrl, {
        method: 'POST',
        body: JSON.stringify({ type: entry.type, path: entry.path })
      });
      if (entry.type === 'folder' && (state.currentFolder === entry.path || state.currentFolder.startsWith(`${entry.path}/`))) {
        state.currentFolder = '';
      }
      state.selectedPath = '';
      state.selectedEntry = null;
      await loadImages();
    }

    async function moveEntry(entry, destination) {
      if (!entry || isRootFolderEntry(entry)) return;
      const data = await fetchJson(moveEntryUrl, {
        method: 'POST',
        body: JSON.stringify({ type: entry.type, path: entry.path, destination })
      });
      await loadImages();
      if (entry.type === 'file') {
        state.currentFolder = data.folder || destination || '';
        renderAllExplorers();
        setSelectedFile(data.path || '');
      } else {
        setCurrentFolder(data.folder || data.path || destination || '');
      }
    }

    async function chooseMoveEntry(entry = state.selectedEntry) {
      if (!entry || isRootFolderEntry(entry)) return;
      const destination = await dialogMoveDestination(entry);
      if (destination === null) return;
      await moveEntry(entry, destination);
    }

    function selectedFileItem(entry = state.selectedEntry) {
      if (!entry || entry.type !== 'file') return null;
      return state.images.find(img => img.path === entry.path) || null;
    }

    function notifyClipboard(message) {
      const previous = pathBox.textContent;
      pathBox.textContent = message;
      setTimeout(() => {
        if (pathBox.textContent === message) {
          pathBox.textContent = `/mnt/mfu/image_viewer_uploads${state.currentFolder ? '/' + state.currentFolder : ''}`;
        } else if (!pathBox.textContent) {
          pathBox.textContent = previous;
        }
      }, 1400);
    }

    async function blobToPngBlob(blob) {
      if (blob.type === 'image/png') return blob;
      if ('createImageBitmap' in window) {
        const bitmap = await createImageBitmap(blob);
        const canvas = document.createElement('canvas');
        canvas.width = bitmap.width;
        canvas.height = bitmap.height;
        const ctx = canvas.getContext('2d');
        ctx.drawImage(bitmap, 0, 0);
        bitmap.close?.();
        return await new Promise((resolve, reject) => {
          canvas.toBlob(result => result ? resolve(result) : reject(new Error('画像変換に失敗しました。')), 'image/png');
        });
      }
      return await new Promise((resolve, reject) => {
        const url = URL.createObjectURL(blob);
        const img = new Image();
        img.onload = () => {
          try {
            const canvas = document.createElement('canvas');
            canvas.width = img.naturalWidth || img.width;
            canvas.height = img.naturalHeight || img.height;
            canvas.getContext('2d').drawImage(img, 0, 0);
            canvas.toBlob(result => {
              URL.revokeObjectURL(url);
              result ? resolve(result) : reject(new Error('画像変換に失敗しました。'));
            }, 'image/png');
          } catch (err) {
            URL.revokeObjectURL(url);
            reject(err);
          }
        };
        img.onerror = () => {
          URL.revokeObjectURL(url);
          reject(new Error('画像を読み込めませんでした。'));
        };
        img.src = url;
      });
    }

    async function copyEntryToClipboard(entry = state.selectedEntry) {
      const item = selectedFileItem(entry);
      if (!item) throw new Error('コピーするファイルを選択してください。');
      if (!navigator.clipboard) throw new Error('このブラウザではクリップボードを利用できません。');
      const fileUrl = new URL(item.url, window.location.href).href;
      const isImage = (item.mediaType || 'image') === 'image' && /\.(jpe?g|png|gif|webp)$/i.test(item.name || item.path || '');
      if (isImage && window.ClipboardItem && navigator.clipboard.write) {
        try {
          const response = await fetch(item.url, { credentials: 'same-origin', cache: 'no-store' });
          if (!response.ok) throw new Error(`HTTP ${response.status}`);
          const blob = await response.blob();
          const pngBlob = await blobToPngBlob(blob);
          await navigator.clipboard.write([new ClipboardItem({ 'image/png': pngBlob })]);
          notifyClipboard(`${item.name} を画像としてコピーしました。`);
          return;
        } catch (_) {
          // Fall through to URL copy when image clipboard is blocked or unsupported for the source.
        }
      }
      await navigator.clipboard.writeText(fileUrl);
      notifyClipboard(`${item.name} のURLをコピーしました。`);
    }

    function openSelectedEntry() {
      const entry = state.selectedEntry;
      if (!entry) return;
      if (entry.type === 'folder') {
        setCurrentFolder(entry.path);
        return;
      }
      const item = state.images.find(img => img.path === entry.path);
      if (item) openMedia(item);
    }

    function observeThumbnail(el, url) {
      if (!state.thumbObserver) {
        state.thumbObserver = new IntersectionObserver((entries) => {
          entries.forEach(entry => {
            if (!entry.isIntersecting) return;
            const thumb = entry.target;
            hydrateThumbnail(thumb);
            state.thumbObserver.unobserve(thumb);
          });
        }, { root: fileGrid, rootMargin: '1800px 0px' });
      }
      el.dataset.bgUrl = url;
      state.thumbObserver.observe(el);
    }

    function hydrateThumbnail(thumb) {
      const imageUrl = thumb?.dataset?.bgUrl;
      if (!imageUrl) return;
      thumb.style.backgroundImage = `url(${JSON.stringify(imageUrl)})`;
      thumb.removeAttribute('data-bg-url');
    }

    function hydrateThumbnailsInRange(buffer) {
      const viewport = fileGrid.getBoundingClientRect();
      const minY = viewport.top - buffer;
      const maxY = viewport.bottom + buffer;
      const visible = [];
      const nearby = [];
      fileGrid.querySelectorAll('.thumb[data-bg-url]').forEach(thumb => {
        const rect = thumb.getBoundingClientRect();
        if (rect.bottom < minY || rect.top > maxY) return;
        if (rect.bottom >= viewport.top && rect.top <= viewport.bottom) visible.push(thumb);
        else nearby.push(thumb);
      });
      [...visible, ...nearby].forEach(thumb => {
        hydrateThumbnail(thumb);
        if (state.thumbObserver) state.thumbObserver.unobserve(thumb);
      });
    }

    function hydrateVisibleThumbnails() {
      state.thumbHydrateRaf = 0;
      hydrateThumbnailsInRange(1600);
    }

    function hydrateCurrentViewportThumbnails() {
      hydrateThumbnailsInRange(0);
    }

    function scheduleHydrateVisibleThumbnails() {
      if (state.thumbHydrateRaf) return;
      state.thumbHydrateRaf = requestAnimationFrame(hydrateVisibleThumbnails);
    }

    function scheduleHydrateAfterScrollSettles() {
      clearTimeout(state.thumbSettleTimer);
      state.thumbSettleTimer = setTimeout(() => {
        hydrateCurrentViewportThumbnails();
        scheduleHydrateVisibleThumbnails();
      }, 80);
    }

    function renderFiles() {
      if (state.thumbObserver) state.thumbObserver.disconnect();
      fileGrid.innerHTML = '';
      const images = imagesInCurrentFolder();
      pathBox.textContent = `/mnt/mfu/image_viewer_uploads${state.currentFolder ? '/' + state.currentFolder : ''}`;
      sortBtn.textContent = sortDirection() === 'desc' ? '逆順' : '昇順';
      sortBtn.title = sortDirection() === 'desc' ? '名前の逆順' : '名前順';
      const size = viewSize();
      fileGrid.classList.remove('view-xl', 'view-lg', 'view-md', 'view-sm');
      fileGrid.classList.add(`view-${size}`);
      viewButtons.forEach(btn => btn.classList.toggle('active', btn.dataset.viewSize === size));
      if (!images.length) {
        const empty = document.createElement('div');
        empty.className = 'empty';
        empty.textContent = 'このフォルダーに画像がありません。ここにドロップ、または追加してください。';
        fileGrid.appendChild(empty);
        resetFileScrollIfNeeded();
        return;
      }
      images.forEach(img => {
        const btn = document.createElement('button');
        btn.type = 'button';
        btn.className = `file${state.selectedPath === img.path ? ' selected' : ''}`;
        btn.dataset.path = img.path;
        btn.draggable = true;
        btn.title = img.path;
        btn.innerHTML = `
          <div class="thumb" aria-hidden="true"></div>
          <div class="file-name"></div>
          <div class="file-meta"></div>
        `;
        const thumb = btn.querySelector('.thumb');
        if (img.mediaType === 'video') {
          if (img.thumbUrl) {
            observeThumbnail(thumb, img.thumbUrl);
          } else {
            thumb.classList.add('video-thumb');
          }
        } else {
          observeThumbnail(thumb, img.thumbUrl || img.url);
        }
        btn.querySelector('.file-name').textContent = img.name;
        btn.querySelector('.file-meta').textContent = formatBytes(img.size);
        btn.addEventListener('click', () => {
          setSelectedFile(img.path);
        });
        btn.addEventListener('dblclick', () => {
          const latest = state.images.find(item => item.path === img.path) || img;
          openMedia(latest);
        });
        btn.addEventListener('contextmenu', (event) => {
          showContextMenu(event, { type: 'file', path: img.path });
        });
        btn.addEventListener('dragstart', (event) => {
          setSelectedFile(img.path);
          setDragEntry(event, { type: 'file', path: img.path });
        });
        fileGrid.appendChild(btn);
      });
      scheduleHydrateVisibleThumbnails();
      resetFileScrollIfNeeded();
    }

    function resetFileScrollIfNeeded() {
      if (!state.resetFileScrollNext) return;
      state.resetFileScrollNext = false;
      fileGrid.scrollTop = 0;
      fileGrid.scrollLeft = 0;
      requestAnimationFrame(() => {
        fileGrid.scrollTop = 0;
        fileGrid.scrollLeft = 0;
        hydrateCurrentViewportThumbnails();
      });
    }

    function renderExplorer() {
      renderFolders();
      renderFiles();
      renderTasks();
    }

    function renderAllExplorers() {
      renderExplorer();
      for (const id of state.explorerClones.keys()) {
        renderExplorerClone(id);
      }
    }

    function explorerTitle(folder) {
      return `エクスプローラー - /mnt/mfu/image_viewer_uploads${folder ? '/' + folder : ''}`;
    }

    function openExplorerWindow(folder = state.currentFolder || '') {
      const id = `explorer-${state.nextExplorer++}`;
      const el = document.createElement('article');
      el.className = 'window explorer';
      el.style.left = `${120 + (state.nextExplorer % 5) * 28}px`;
      el.style.top = `${88 + (state.nextExplorer % 5) * 24}px`;
      el.style.width = '860px';
      el.style.height = '560px';
      el.innerHTML = `
        <header class="titlebar" data-drag-handle>
          <div class="title-icon" aria-hidden="true">EXP</div>
          <div class="title-text"></div>
          <div class="win-actions">
            <button class="win-btn" type="button" data-action="minimize" title="最小化">_</button>
            <button class="win-btn" type="button" data-action="maximize" title="最大化">□</button>
            <button class="win-btn close" type="button" data-action="close" title="閉じる">X</button>
          </div>
        </header>
        <div class="explorer-body">
          <aside class="sidebar">
            <div class="side-title">フォルダー</div>
            <div class="clone-folder-list"></div>
          </aside>
          <section class="filepane">
            <div class="toolbar">
              <div class="pathbox clone-pathbox"></div>
              <button type="button" data-clone-action="refresh" title="更新">更新</button>
              <button type="button" data-clone-action="up" title="上へ">上へ</button>
              <button type="button" data-clone-action="sort" title="名前順 / 逆順">昇順</button>
              <button type="button" data-clone-size="xl" title="特大表示">特大</button>
              <button type="button" data-clone-size="lg" title="大表示">大</button>
              <button type="button" data-clone-size="md" title="中表示">中</button>
              <button type="button" data-clone-size="sm" title="小表示">小</button>
              <button type="button" data-clone-action="open" title="選択画像を開く">開く</button>
            </div>
            <div class="files clone-file-grid" aria-label="画像一覧"></div>
          </section>
        </div>
        <div class="resize-handle" data-resize-handle></div>
      `;
      desktopGrid.appendChild(el);
      state.explorerClones.set(id, {
        id,
        el,
        currentFolder: folder,
        selectedPath: '',
        resetScroll: true,
        refs: {
          folderList: el.querySelector('.clone-folder-list'),
          fileGrid: el.querySelector('.clone-file-grid'),
          pathBox: el.querySelector('.clone-pathbox'),
          title: el.querySelector('.title-text')
        }
      });
      registerWindow(id, el, 'エクスプローラー', 'explorer');
      wireExplorerClone(id);
      renderExplorerClone(id);
    }

    function setExplorerCloneFolder(id, folder) {
      const ex = state.explorerClones.get(id);
      if (!ex || ex.currentFolder === folder) return;
      ex.currentFolder = folder;
      ex.selectedPath = '';
      ex.resetScroll = true;
      renderExplorerClone(id);
    }

    function renderExplorerClone(id) {
      const ex = state.explorerClones.get(id);
      if (!ex) return;
      const { folderList: cloneFolders, fileGrid: cloneGrid, pathBox: clonePath, title } = ex.refs;
      const folder = ex.currentFolder || '';
      title.textContent = explorerTitle(folder);
      clonePath.textContent = `/mnt/mfu/image_viewer_uploads${folder ? '/' + folder : ''}`;
      const sortButton = ex.el.querySelector('[data-clone-action="sort"]');
      if (sortButton) sortButton.textContent = sortDirection(folder) === 'desc' ? '逆順' : '昇順';
      ex.el.querySelectorAll('[data-clone-size]').forEach(btn => btn.classList.toggle('active', btn.dataset.cloneSize === viewSize(folder)));

      cloneFolders.innerHTML = '';
      [...state.folders].sort(compareFolderPath).forEach(nextFolder => {
        const btn = document.createElement('button');
        btn.type = 'button';
        btn.className = `folder${nextFolder === folder ? ' active' : ''}`;
        btn.dataset.folder = nextFolder;
        btn.draggable = Boolean(nextFolder);
        btn.style.paddingLeft = `${8 + Math.max(0, nextFolder.split('/').length - 1) * 12}px`;
        btn.innerHTML = `<span class="label"></span>`;
        btn.lastElementChild.textContent = folderLabel(nextFolder);
        btn.title = nextFolder || 'uploads';
        btn.addEventListener('click', () => setExplorerCloneFolder(id, nextFolder));
        btn.addEventListener('contextmenu', (event) => showContextMenu(event, { type: 'folder', path: nextFolder }));
        btn.addEventListener('dragstart', (event) => {
          if (!nextFolder) {
            event.preventDefault();
            return;
          }
          setDragEntry(event, { type: 'folder', path: nextFolder });
        });
        cloneFolders.appendChild(btn);
      });

      const size = viewSize(folder);
      cloneGrid.classList.remove('view-xl', 'view-lg', 'view-md', 'view-sm');
      cloneGrid.classList.add(`view-${size}`);
      cloneGrid.innerHTML = '';
      const images = imagesForFolder(folder);
      if (!images.length) {
        const empty = document.createElement('div');
        empty.className = 'empty';
        empty.textContent = 'このフォルダーに画像がありません。';
        cloneGrid.appendChild(empty);
      } else {
        images.forEach(img => {
          const btn = document.createElement('button');
          btn.type = 'button';
          btn.className = `file${ex.selectedPath === img.path ? ' selected' : ''}`;
          btn.dataset.path = img.path;
          btn.draggable = true;
          btn.title = img.path;
          btn.innerHTML = `
            <div class="thumb" aria-hidden="true"></div>
            <div class="file-name"></div>
            <div class="file-meta"></div>
          `;
          const thumb = btn.querySelector('.thumb');
          if (img.mediaType === 'video' && !img.thumbUrl) {
            thumb.classList.add('video-thumb');
          } else {
            thumb.style.backgroundImage = `url(${JSON.stringify(img.thumbUrl || img.url)})`;
          }
          btn.querySelector('.file-name').textContent = img.name;
          btn.querySelector('.file-meta').textContent = formatBytes(img.size);
          btn.addEventListener('click', () => {
            ex.selectedPath = img.path;
            state.selectedEntry = { type: 'file', path: img.path };
            cloneGrid.querySelectorAll('.file').forEach(file => file.classList.toggle('selected', file.dataset.path === img.path));
          });
          btn.addEventListener('dblclick', () => openMedia(state.images.find(item => item.path === img.path) || img));
          btn.addEventListener('contextmenu', (event) => {
            ex.selectedPath = img.path;
            showContextMenu(event, { type: 'file', path: img.path });
          });
          btn.addEventListener('dragstart', (event) => setDragEntry(event, { type: 'file', path: img.path }));
          cloneGrid.appendChild(btn);
        });
      }
      if (ex.resetScroll) {
        ex.resetScroll = false;
        cloneGrid.scrollTop = 0;
        cloneGrid.scrollLeft = 0;
      }
      renderTasks();
    }

    function wireExplorerClone(id) {
      const ex = state.explorerClones.get(id);
      if (!ex) return;
      const { folderList: cloneFolders, fileGrid: cloneGrid } = ex.refs;
      ex.el.querySelector('[data-clone-action="refresh"]').addEventListener('click', () => loadImages());
      ex.el.querySelector('[data-clone-action="up"]').addEventListener('click', () => {
        const parent = (ex.currentFolder || '').split('/').slice(0, -1).join('/');
        setExplorerCloneFolder(id, parent);
      });
      ex.el.querySelector('[data-clone-action="sort"]').addEventListener('click', () => {
        setSortDirection(ex.currentFolder, sortDirection(ex.currentFolder) === 'desc' ? 'asc' : 'desc');
        renderExplorerClone(id);
      });
      ex.el.querySelectorAll('[data-clone-size]').forEach(btn => {
        btn.addEventListener('click', () => {
          setViewSize(ex.currentFolder, btn.dataset.cloneSize);
          renderExplorerClone(id);
        });
      });
      ex.el.querySelector('[data-clone-action="open"]').addEventListener('click', () => {
        const selected = state.images.find(img => img.path === ex.selectedPath);
        if (selected) openMedia(selected);
      });
      cloneGrid.addEventListener('dragover', (event) => {
        if (!hasInternalDrag(event) && !fileListFromDrag(event).length && !event.dataTransfer?.types?.includes('Files')) return;
        event.preventDefault();
        if (hasInternalDrag(event)) event.dataTransfer.dropEffect = 'move';
        cloneGrid.classList.add('drag-over');
      });
      cloneGrid.addEventListener('dragleave', (event) => {
        if (!cloneGrid.contains(event.relatedTarget)) cloneGrid.classList.remove('drag-over');
      });
      cloneGrid.addEventListener('drop', (event) => {
        event.preventDefault();
        cloneGrid.classList.remove('drag-over');
        const entry = getDragEntry(event);
        if (entry) {
          moveEntry(entry, ex.currentFolder).catch(err => alert(err.message || '移動に失敗しました。'));
          return;
        }
        uploadFilesToFolder(event.dataTransfer.files, ex.currentFolder).catch(err => alert(err.message || 'アップロードに失敗しました。'));
      });
      cloneFolders.addEventListener('dragover', (event) => {
        const folderButton = event.target.closest('.folder');
        if (!folderButton || (!hasInternalDrag(event) && !event.dataTransfer?.types?.includes('Files'))) return;
        event.preventDefault();
        if (hasInternalDrag(event)) event.dataTransfer.dropEffect = 'move';
        folderButton.classList.add('drag-over');
      });
      cloneFolders.addEventListener('dragleave', (event) => {
        event.target.closest('.folder')?.classList.remove('drag-over');
      });
      cloneFolders.addEventListener('drop', (event) => {
        const folderButton = event.target.closest('.folder');
        if (!folderButton) return;
        event.preventDefault();
        folderButton.classList.remove('drag-over');
        const entry = getDragEntry(event);
        const destination = folderButton.dataset.folder || '';
        if (entry) {
          moveEntry(entry, destination).catch(err => alert(err.message || '移動に失敗しました。'));
          return;
        }
        uploadFilesToFolder(event.dataTransfer.files, destination).catch(err => alert(err.message || 'アップロードに失敗しました。'));
      });
    }

    async function loadImages() {
      fileGrid.innerHTML = '<div class="empty">読み込み中...</div>';
      const data = await fetchJson(apiUrl);
      state.images = data.images || [];
      state.folders = (data.folders && data.folders.length ? data.folders : ['']).sort(compareFolderPath);
      if (!state.folders.includes(state.currentFolder)) {
        state.currentFolder = '';
        state.resetFileScrollNext = true;
      }
      if (state.selectedEntry?.type === 'file' && !state.images.some(img => img.path === state.selectedEntry.path)) {
        state.selectedEntry = null;
        state.selectedPath = '';
      }
      if (state.selectedEntry?.type === 'folder' && !state.folders.includes(state.selectedEntry.path)) {
        state.selectedEntry = null;
      }
      for (const ex of state.explorerClones.values()) {
        if (!state.folders.includes(ex.currentFolder)) {
          ex.currentFolder = '';
          ex.resetScroll = true;
        }
        if (ex.selectedPath && !state.images.some(img => img.path === ex.selectedPath)) {
          ex.selectedPath = '';
        }
      }
      renderAllExplorers();
    }

    async function createFolderInCurrentFolder() {
      const parent = state.currentFolder || '';
      const parentLabel = `/mnt/mfu/image_viewer_uploads${parent ? '/' + parent : ''}`;
      const name = prompt(`作成先: ${parentLabel}\nフォルダー名:`);
      if (!name) return;
      const data = await fetchJson(createFolderUrl, {
        method: 'POST',
        body: JSON.stringify({ parent, name })
      });
      state.currentFolder = data.folder || parent;
      await loadImages();
    }

    async function uploadFilesToFolder(files, folder = state.currentFolder) {
      const imageFiles = Array.from(files || []).filter(file => file.type.startsWith('image/') || file.type.startsWith('video/') || /\.(jpe?g|png|gif|webp|mp4|webm|mov|m4v)$/i.test(file.name));
      if (!imageFiles.length) {
        alert('アップロードできる画像または動画ファイルがありません。');
        return;
      }
      const batches = uploadBatches(imageFiles);
      let savedCount = 0;
      let skippedCount = 0;
      let failedCount = 0;
      fileGrid.classList.add('drag-over');
      try {
        for (let i = 0; i < batches.length; i += 1) {
          pathBox.textContent = `アップロード中... ${i + 1}/${batches.length}`;
          const form = new FormData();
          form.append('folder', folder || '');
          batches[i].forEach(file => form.append('files', file, file.name));
          const data = await fetchJson(uploadImagesUrl, { method: 'POST', body: form });
          savedCount += (data.saved || []).length;
          skippedCount += (data.skipped || []).length;
          failedCount += (data.errors || []).length;
        }
      } finally {
        fileGrid.classList.remove('drag-over');
      }
      state.currentFolder = folder || '';
      await loadImages();
      const message = `${savedCount}件アップロードしました。${skippedCount ? ` ${skippedCount}件スキップ。` : ''}${failedCount ? ` ${failedCount}件エラー。` : ''}`;
      alert(message);
    }

    async function pasteClipboardFiles(files, folder = state.currentFolder) {
      const imageFiles = Array.from(files || []).filter(file => file.type.startsWith('image/') || /\.(jpe?g|png|gif|webp)$/i.test(file.name));
      if (!imageFiles.length) {
        alert('貼り付けできる画像がありません。');
        return;
      }
      pathBox.textContent = '貼り付け保存中...';
      const form = new FormData();
      form.append('folder', folder || '');
      imageFiles.forEach((file, index) => {
        const fallbackName = file.type === 'image/jpeg' ? `clipboard-${index + 1}.jpg` : file.type === 'image/webp' ? `clipboard-${index + 1}.webp` : file.type === 'image/gif' ? `clipboard-${index + 1}.gif` : `clipboard-${index + 1}.png`;
        form.append('files', file, file.name || fallbackName);
      });
      const data = await fetchJson(pasteImagesUrl, { method: 'POST', body: form });
      state.currentFolder = folder || '';
      await loadImages();
      const saved = data.saved || [];
      if (saved.length) {
        setSelectedFile(saved[saved.length - 1].path);
      }
      const skipped = (data.skipped || []).length;
      const failed = (data.errors || []).length;
      alert(`${saved.length}件保存しました。${skipped ? ` ${skipped}件スキップ。` : ''}${failed ? ` ${failed}件エラー。` : ''}`);
    }

    async function readClipboardImageFiles() {
      if (!navigator.clipboard || !navigator.clipboard.read) return [];
      const items = await navigator.clipboard.read();
      const files = [];
      for (const item of items) {
        const imageType = (item.types || []).find(type => type.startsWith('image/'));
        if (!imageType) continue;
        const blob = await item.getType(imageType);
        const ext = imageType === 'image/jpeg' ? 'jpg' : imageType.split('/')[1] || 'png';
        files.push(new File([blob], `clipboard.${ext}`, { type: imageType }));
      }
      return files;
    }

    async function pasteFromClipboard() {
      const files = await readClipboardImageFiles();
      await pasteClipboardFiles(files, state.currentFolder);
    }

    function uploadBatches(files) {
      const batches = [];
      let current = [];
      let currentBytes = 0;
      files.forEach(file => {
        const size = Number(file.size || 0);
        if (current.length && (current.length >= uploadBatchMaxFiles || currentBytes + size > uploadBatchMaxBytes)) {
          batches.push(current);
          current = [];
          currentBytes = 0;
        }
        current.push(file);
        currentBytes += size;
      });
      if (current.length) batches.push(current);
      return batches;
    }


    function currentFolderImagesFor(image) {
      return imagesForFolder(image.folder || '').filter(item => (item.mediaType || 'image') === 'image');
    }

    function openMedia(item) {
      if ((item.mediaType || 'image') === 'video') openVideoPlayer(item);
      else openViewer(item);
    }

    function neighborImage(image, delta) {
      const list = currentFolderImagesFor(image);
      const index = list.findIndex(item => item.path === image.path);
      if (index < 0) return image;
      const nextIndex = index + delta;
      if (nextIndex < 0 || nextIndex >= list.length) return image;
      return list[nextIndex] || image;
    }

    function setViewerImage(id, image) {
      const win = state.windows.get(id);
      if (!win) return;
      win.image = image;
      win.title = image.name;
      win.el.querySelector('.title-text').textContent = image.name;
      const img = win.el.querySelector('.stage img');
      img.src = image.url;
      img.alt = image.name;
      img.onload = () => applyZoom(id);
      win.el.querySelector('[data-status-name]').textContent = image.path;
      win.el.querySelector('[data-status-size]').textContent = formatBytes(image.size);
      renderTasks();
    }

    function applyZoom(id) {
      const win = state.windows.get(id);
      if (!win) return;
      const stage = win.el.querySelector('.stage');
      const img = stage.querySelector('img');
      const fit = state.fitByWindow.get(id);
      const zoom = state.zoomByWindow.get(id) || 100;
      const naturalW = img.naturalWidth || 0;
      const naturalH = img.naturalHeight || 0;
      if (fit && naturalW > 0 && naturalH > 0) {
        const stageRect = stage.getBoundingClientRect();
        const availableW = Math.max(1, stageRect.width - 36);
        const availableH = Math.max(1, stageRect.height - 36);
        const scale = Math.min(availableW / naturalW, availableH / naturalH, 4);
        img.style.width = `${Math.max(1, Math.floor(naturalW * scale))}px`;
        img.style.height = `${Math.max(1, Math.floor(naturalH * scale))}px`;
        win.el.querySelector('.zoom-label').textContent = `${Math.round(scale * 100)}%`;
      } else {
        img.style.width = `${zoom}%`;
        img.style.height = 'auto';
        win.el.querySelector('.zoom-label').textContent = `${zoom}%`;
      }
    }

    function openViewer(image) {
      const id = `viewer-${state.nextViewer++}`;
      const index = state.nextViewer;
      const left = Math.min(140 + index * 28, window.innerWidth - 360);
      const top = Math.min(70 + index * 24, window.innerHeight - 260);
      const el = document.createElement('article');
      el.className = 'window viewer';
      el.style.left = `${Math.max(16, left)}px`;
      el.style.top = `${Math.max(16, top)}px`;
      el.innerHTML = `
        <header class="titlebar" data-drag-handle>
          <div class="title-icon" aria-hidden="true">画像</div>
          <div class="title-text"></div>
          <div class="win-actions">
            <button class="win-btn" type="button" data-action="minimize" title="最小化">_</button>
            <button class="win-btn" type="button" data-action="maximize" title="最大化">□</button>
            <button class="win-btn close" type="button" data-action="close" title="閉じる">X</button>
          </div>
        </header>
        <div class="content">
          <div class="viewer-toolbar">
            <button type="button" data-tool="prev" title="前の画像">前</button>
            <button type="button" data-tool="next" title="次の画像">次</button>
            <button type="button" data-tool="out" title="縮小">-</button>
            <span class="zoom-label">Fit</span>
            <button type="button" data-tool="in" title="拡大">+</button>
            <button type="button" data-tool="fit" title="画面に合わせる">Fit</button>
            <button type="button" data-tool="actual" title="100%">100%</button>
          </div>
          <div class="stage fit"><img alt=""></div>
          <div class="viewer-status"><span data-status-name></span><span data-status-size></span></div>
        </div>
        <div class="resize-handle" data-resize-handle></div>
      `;
      desktopGrid.appendChild(el);
      state.fitByWindow.set(id, true);
      state.zoomByWindow.set(id, 100);
      registerWindow(id, el, image.name, 'viewer');
      setViewerImage(id, image);
      applyZoom(id);

      el.querySelectorAll('[data-tool]').forEach(btn => {
        btn.addEventListener('click', () => {
          const tool = btn.dataset.tool;
          const win = state.windows.get(id);
          if (!win || !win.image) return;
          if (tool === 'prev') setViewerImage(id, neighborImage(win.image, -1));
          if (tool === 'next') setViewerImage(id, neighborImage(win.image, 1));
          if (tool === 'fit') state.fitByWindow.set(id, true);
          if (tool === 'actual') {
            state.fitByWindow.set(id, false);
            state.zoomByWindow.set(id, 100);
          }
          if (tool === 'in' || tool === 'out') {
            state.fitByWindow.set(id, false);
            const current = state.zoomByWindow.get(id) || 100;
            const next = tool === 'in' ? current + 25 : current - 25;
            state.zoomByWindow.set(id, Math.max(25, Math.min(400, next)));
          }
          applyZoom(id);
        });
      });

      el.querySelector('.stage').addEventListener('wheel', (event) => {
        const win = state.windows.get(id);
        if (!win || !win.image) return;
        event.preventDefault();
        const now = Date.now();
        const lockedUntil = state.wheelLockByWindow.get(id) || 0;
        if (now < lockedUntil || Math.abs(event.deltaY) < 8) return;
        state.wheelLockByWindow.set(id, now + 180);
        setViewerImage(id, neighborImage(win.image, event.deltaY > 0 ? 1 : -1));
      }, { passive: false });
    }

    function formatDuration(seconds) {
      const total = Math.max(0, Math.floor(Number(seconds) || 0));
      const h = Math.floor(total / 3600);
      const m = Math.floor((total % 3600) / 60);
      const s = total % 60;
      if (h > 0) return `${h}:${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`;
      return `${m}:${String(s).padStart(2, '0')}`;
    }

    function loadVideoSettings() {
      try {
        const raw = localStorage.getItem(videoSettingsKey);
        const settings = raw ? JSON.parse(raw) : {};
        const volume = Math.max(0, Math.min(1, Number(settings.volume ?? 1)));
        return { volume: Number.isFinite(volume) ? volume : 1, muted: Boolean(settings.muted) };
      } catch (_) {
        return { volume: 1, muted: false };
      }
    }

    function saveVideoSettings(video) {
      try {
        localStorage.setItem(videoSettingsKey, JSON.stringify({
          volume: Math.max(0, Math.min(1, Number(video.volume || 0))),
          muted: Boolean(video.muted)
        }));
      } catch (_) {}
    }

    function openVideoPlayer(videoItem) {
      const id = `video-${state.nextViewer++}`;
      const index = state.nextViewer;
      const left = Math.min(170 + index * 28, window.innerWidth - 420);
      const top = Math.min(90 + index * 24, window.innerHeight - 300);
      const el = document.createElement('article');
      el.className = 'window video-player';
      el.style.left = `${Math.max(16, left)}px`;
      el.style.top = `${Math.max(16, top)}px`;
      el.innerHTML = `
        <header class="titlebar" data-drag-handle>
          <div class="title-icon" aria-hidden="true">VID</div>
          <div class="title-text"></div>
          <div class="win-actions">
            <button class="win-btn" type="button" data-action="minimize" title="最小化">_</button>
            <button class="win-btn" type="button" data-action="maximize" title="最大化">□</button>
            <button class="win-btn close" type="button" data-action="close" title="閉じる">X</button>
          </div>
        </header>
        <div class="video-content">
          <div class="video-stage"><video playsinline preload="metadata"></video></div>
          <input class="video-seek" type="range" min="0" max="1000" value="0">
          <div class="video-toolbar">
            <button type="button" data-video-tool="play">再生</button>
            <button type="button" data-video-tool="back10">10秒戻し</button>
            <button type="button" data-video-tool="fwd10">10秒飛ばし</button>
            <button type="button" data-video-tool="fwd30">30秒飛ばし</button>
            <button type="button" data-video-tool="mute">ミュート</button>
            <label class="video-volume">音量
              <input class="video-volume-slider" type="range" min="0" max="100" value="100">
              <span class="video-volume-value">100%</span>
            </label>
            <span class="video-time">0:00 / 0:00</span>
          </div>
        </div>
        <div class="resize-handle" data-resize-handle></div>
      `;
      desktopGrid.appendChild(el);
      registerWindow(id, el, videoItem.name, 'video');
      const video = el.querySelector('video');
      const seek = el.querySelector('.video-seek');
      const playBtn = el.querySelector('[data-video-tool="play"]');
      const muteBtn = el.querySelector('[data-video-tool="mute"]');
      const volumeSlider = el.querySelector('.video-volume-slider');
      const volumeValue = el.querySelector('.video-volume-value');
      const timeLabel = el.querySelector('.video-time');
      el.querySelector('.title-text').textContent = videoItem.name;
      video.src = videoItem.url;
      const videoSettings = loadVideoSettings();
      video.volume = videoSettings.volume;
      video.muted = videoSettings.muted;

      const updateVideoUi = () => {
        const duration = Number.isFinite(video.duration) ? video.duration : 0;
        const current = Number.isFinite(video.currentTime) ? video.currentTime : 0;
        seek.value = duration > 0 ? String(Math.round(current / duration * 1000)) : '0';
        playBtn.textContent = video.paused ? '再生' : '一時停止';
        muteBtn.textContent = video.muted || video.volume === 0 ? 'ミュート解除' : 'ミュート';
        volumeSlider.value = String(Math.round(video.volume * 100));
        volumeValue.textContent = `${Math.round(video.volume * 100)}%`;
        timeLabel.textContent = `${formatDuration(current)} / ${formatDuration(duration)}`;
      };
      const jump = (seconds) => {
        const duration = Number.isFinite(video.duration) ? video.duration : 0;
        const next = Math.max(0, Math.min(duration || Number.MAX_SAFE_INTEGER, video.currentTime + seconds));
        video.currentTime = next;
        updateVideoUi();
      };

      el.querySelectorAll('[data-video-tool]').forEach(btn => {
        btn.addEventListener('click', async () => {
          const tool = btn.dataset.videoTool;
          if (tool === 'play') {
            if (video.paused) {
              try { await video.play(); } catch (_) {}
            } else {
              video.pause();
            }
          }
          if (tool === 'back10') jump(-10);
          if (tool === 'fwd10') jump(10);
          if (tool === 'fwd30') jump(30);
          if (tool === 'mute') {
            video.muted = !(video.muted || video.volume === 0);
            if (!video.muted && video.volume === 0) video.volume = 0.5;
            saveVideoSettings(video);
          }
          updateVideoUi();
        });
      });
      seek.addEventListener('input', () => {
        const duration = Number.isFinite(video.duration) ? video.duration : 0;
        if (duration > 0) video.currentTime = Number(seek.value || 0) / 1000 * duration;
        updateVideoUi();
      });
      volumeSlider.addEventListener('input', () => {
        const value = Math.max(0, Math.min(100, Number(volumeSlider.value || 0)));
        video.volume = value / 100;
        video.muted = value === 0;
        saveVideoSettings(video);
        updateVideoUi();
      });
      ['loadedmetadata', 'timeupdate', 'play', 'pause', 'ended', 'volumechange'].forEach(eventName => {
        video.addEventListener(eventName, updateVideoUi);
      });
      video.addEventListener('click', () => {
        if (video.paused) video.play().catch(() => {});
        else video.pause();
      });
      updateVideoUi();
    }

    function openInstagramWindow() {
      const existing = state.windows.get('instagram');
      if (existing) {
        activate('instagram');
        return;
      }
      const el = document.createElement('article');
      el.className = 'window instagram-window';
      el.style.left = '96px';
      el.style.top = '72px';
      el.innerHTML = `
        <header class="titlebar" data-drag-handle>
          <div class="title-icon" aria-hidden="true">IG</div>
          <div class="title-text">Instagram / X Image Downloader</div>
          <div class="win-actions">
            <button class="win-btn" type="button" data-action="minimize" title="最小化">_</button>
            <button class="win-btn" type="button" data-action="maximize" title="最大化">□</button>
            <button class="win-btn close" type="button" data-action="close" title="閉じる">X</button>
          </div>
        </header>
        <div class="instagram-body">
          <div>
            <div class="ig-form">
              <label for="igUrl">URL</label>
              <input id="igUrl" type="text" placeholder="https://www.instagram.com/p/... / https://x.com/.../status/...">
              <button type="button" id="igFetchBtn">取得</button>
            </div>
            <div class="ig-options">
              <label>保存先フォルダー<input id="igFolder" type="text" value="instagram" placeholder="instagram"></label>
              <label>連番開始<input id="igStart" type="number" min="1" value="1"></label>
              <label>桁数<input id="igDigits" type="number" min="1" max="6" value="3"></label>
            </div>
          </div>
          <div class="ig-grid" id="igGrid">
            <div class="empty">InstagramまたはXの投稿URLを入力して取得してください。</div>
          </div>
          <div class="ig-actions">
            <button type="button" id="igSelectAll">全選択</button>
            <button type="button" id="igClearAll">全解除</button>
            <button type="button" id="igInvert">反転</button>
            <button type="button" id="igSaveBtn">選択画像を保存</button>
            <span class="ig-status" id="igStatus"></span>
          </div>
        </div>
        <div class="resize-handle" data-resize-handle></div>
      `;
      desktopGrid.appendChild(el);
      registerWindow('instagram', el, 'Instagram / X Image Downloader', 'instagram');
      wireInstagramWindow(el);
    }

    function openProgressWindow(title) {
      const id = `progress-${Date.now()}`;
      const el = document.createElement('article');
      el.className = 'window progress-window';
      el.style.left = '180px';
      el.style.top = '130px';
      el.innerHTML = `
        <header class="titlebar" data-drag-handle>
          <div class="title-icon" aria-hidden="true">%</div>
          <div class="title-text"></div>
          <div class="win-actions">
            <button class="win-btn" type="button" data-action="minimize" title="最小化">_</button>
            <button class="win-btn close" type="button" data-action="close" title="閉じる">X</button>
          </div>
        </header>
        <div class="progress-body">
          <div class="progress-title"></div>
          <div class="progress-bar"><div class="progress-fill"></div></div>
          <div class="progress-detail"></div>
        </div>
        <div class="resize-handle" data-resize-handle></div>
      `;
      el.querySelector('.title-text').textContent = title;
      el.querySelector('.progress-title').textContent = title;
      desktopGrid.appendChild(el);
      registerWindow(id, el, title, 'progress');
      return { id, el };
    }

    function updateProgressWindow(el, job) {
      const total = Number(job.total || 0);
      const processed = Number(job.processed || 0);
      const percent = total > 0 ? Math.min(100, Math.round(processed / total * 100)) : 0;
      el.querySelector('.progress-fill').style.width = `${percent}%`;
      const statusText = job.status === 'done' ? '完了' : job.status === 'error' ? 'エラー' : job.status === 'waiting' ? '待機中' : '処理中';
      el.querySelector('.progress-detail').textContent =
        `${statusText}: ${processed}/${total || '?'} (${percent}%) / 作成 ${job.created || 0}・スキップ ${job.skipped || 0}・失敗 ${job.failed || 0}`;
    }

    async function waitThumbnailJob(jobId, progressEl) {
      const url = thumbnailJobUrlBase.replace('__JOB_ID__', encodeURIComponent(jobId));
      while (true) {
        await sleep(700);
        const job = await fetchJson(url);
        updateProgressWindow(progressEl, job);
        if (job.status === 'done') return job;
        if (job.status === 'error') throw new Error(job.error || 'サムネイル生成に失敗しました。');
      }
    }

    function setInstagramStatus(el, text) {
      el.querySelector('#igStatus').textContent = text || '';
    }

    function renderInstagramItems(el) {
      const grid = el.querySelector('#igGrid');
      grid.innerHTML = '';
      if (!state.instagram.images.length) {
        grid.innerHTML = '<div class="empty">画像がありません。</div>';
        return;
      }
      state.instagram.images.forEach(item => {
        const card = document.createElement('div');
        card.className = 'ig-item selected';
        card.innerHTML = `
          <div class="ig-thumb"></div>
          <label><input type="checkbox" checked data-ig-index="${item.index}"><span></span></label>
        `;
        const thumb = card.querySelector('.ig-thumb');
        if (item.previewReady && item.previewUrl) {
          thumb.style.backgroundImage = `url(${JSON.stringify(item.previewUrl)})`;
        } else if (item.previewError) {
          thumb.textContent = '取得失敗';
        } else {
          thumb.textContent = '取得中...';
        }
        card.querySelector('span').textContent = item.filename || `${item.index}`;
        card.querySelector('input').addEventListener('change', (event) => {
          card.classList.toggle('selected', event.target.checked);
        });
        grid.appendChild(card);
      });
    }

    function selectedInstagramIndexes(el) {
      return Array.from(el.querySelectorAll('[data-ig-index]:checked')).map(input => Number(input.dataset.igIndex));
    }

    function loadInstagramSettings() {
      try {
        const raw = localStorage.getItem(instagramSettingsKey);
        return raw ? JSON.parse(raw) : {};
      } catch (_) {
        return {};
      }
    }

    function saveInstagramSettings(el) {
      const settings = {
        folder: el.querySelector('#igFolder')?.value || 'instagram',
        digits: el.querySelector('#igDigits')?.value || '3'
      };
      localStorage.setItem(instagramSettingsKey, JSON.stringify(settings));
    }

    function applyInstagramSettings(el) {
      const settings = loadInstagramSettings();
      el.querySelector('#igFolder').value = settings.folder || 'instagram';
      el.querySelector('#igDigits').value = settings.digits || '3';
    }

    function debounce(fn, delay = 300) {
      let timer = 0;
      return (...args) => {
        clearTimeout(timer);
        timer = setTimeout(() => fn(...args), delay);
      };
    }

    async function updateInstagramNextNumber(el) {
      const params = new URLSearchParams({
        folder: el.querySelector('#igFolder').value || ''
      });
      const data = await fetchJson(`${instagramNextNumberUrl}?${params.toString()}`);
      el.querySelector('#igStart').value = data.nextNumber || 1;
      return data;
    }

    function wireInstagramWindow(el) {
      applyInstagramSettings(el);
      updateInstagramNextNumber(el).catch(err => setInstagramStatus(el, err.message || '連番を取得できませんでした。'));
      const debouncedNextNumber = debounce(() => {
        saveInstagramSettings(el);
        updateInstagramNextNumber(el).catch(err => setInstagramStatus(el, err.message || '連番を取得できませんでした。'));
      }, 350);
      ['#igFolder', '#igDigits'].forEach(selector => {
        el.querySelector(selector).addEventListener('input', debouncedNextNumber);
        el.querySelector(selector).addEventListener('change', debouncedNextNumber);
      });

      el.querySelector('#igFetchBtn').addEventListener('click', async () => {
        const url = el.querySelector('#igUrl').value.trim();
        const fetchBtn = el.querySelector('#igFetchBtn');
        fetchBtn.disabled = true;
        setInstagramStatus(el, '取得中...');
        try {
          const data = await fetchJson(instagramFetchUrl, {
            method: 'POST',
            body: JSON.stringify({ url })
          });
          setInstagramStatus(el, '投稿画像を取得中...');
          const result = data.jobId
            ? await waitInstagramJob(data.jobId, (count, job) => {
                const total = Number(job.total || 0);
                const processed = Number(job.processed || 0);
                const downloaded = Number(job.downloaded || 0);
                const failed = Number(job.failed || 0);
                if (Array.isArray(job.images) && job.images.length) {
                  state.instagram.shortcode = job.shortcode;
                  state.instagram.jobId = data.jobId || '';
                  state.instagram.images = job.images;
                  renderInstagramItems(el);
                }
                setInstagramStatus(el, total
                  ? `画像をダウンロード中... ${processed}/${total}（成功 ${downloaded}・失敗 ${failed}）`
                  : `投稿画像を取得中... ${count}`);
              })
            : data;
          state.instagram.shortcode = result.shortcode;
          state.instagram.jobId = data.jobId || '';
          state.instagram.images = result.images || [];
          saveInstagramSettings(el);
          await updateInstagramNextNumber(el);
          renderInstagramItems(el);
          setInstagramStatus(el, `${state.instagram.images.length}枚取得しました。`);
        } catch (err) {
          state.instagram.images = [];
          renderInstagramItems(el);
          setInstagramStatus(el, err.message || '取得に失敗しました。');
        } finally {
          fetchBtn.disabled = false;
        }
      });

      el.querySelector('#igSelectAll').addEventListener('click', () => {
        el.querySelectorAll('[data-ig-index]').forEach(input => {
          input.checked = true;
          input.closest('.ig-item')?.classList.add('selected');
        });
      });
      el.querySelector('#igClearAll').addEventListener('click', () => {
        el.querySelectorAll('[data-ig-index]').forEach(input => {
          input.checked = false;
          input.closest('.ig-item')?.classList.remove('selected');
        });
      });
      el.querySelector('#igInvert').addEventListener('click', () => {
        el.querySelectorAll('[data-ig-index]').forEach(input => {
          input.checked = !input.checked;
          input.closest('.ig-item')?.classList.toggle('selected', input.checked);
        });
      });
      el.querySelector('#igSaveBtn').addEventListener('click', async () => {
        const saveBtn = el.querySelector('#igSaveBtn');
        saveBtn.disabled = true;
        setInstagramStatus(el, '保存中...');
        try {
          const data = await fetchJson(instagramSaveUrl, {
            method: 'POST',
            body: JSON.stringify({
              shortcode: state.instagram.shortcode,
              jobId: state.instagram.jobId,
              images: state.instagram.images,
              selected: selectedInstagramIndexes(el),
              folder: el.querySelector('#igFolder').value,
              startNumber: el.querySelector('#igStart').value,
              digits: el.querySelector('#igDigits').value
            })
          });
          setInstagramStatus(el, `${(data.saved || []).length}枚保存しました。`);
          saveInstagramSettings(el);
          await loadImages();
          await updateInstagramNextNumber(el);
        } catch (err) {
          setInstagramStatus(el, err.message || '保存に失敗しました。');
        } finally {
          saveBtn.disabled = false;
        }
      });
    }

    function openVideoDownloaderWindow() {
      const existing = state.windows.get('video-downloader');
      if (existing) {
        activate('video-downloader');
        return;
      }
      const el = document.createElement('article');
      el.className = 'window instagram-window';
      el.style.left = '128px';
      el.style.top = '96px';
      el.innerHTML = `
        <header class="titlebar" data-drag-handle>
          <div class="title-icon" aria-hidden="true">VD</div>
          <div class="title-text">Instagram / X Video Downloader</div>
          <div class="win-actions">
            <button class="win-btn" type="button" data-action="minimize" title="最小化">_</button>
            <button class="win-btn" type="button" data-action="maximize" title="最大化">□</button>
            <button class="win-btn close" type="button" data-action="close" title="閉じる">X</button>
          </div>
        </header>
        <div class="instagram-body">
          <div>
            <div class="ig-form">
              <label for="vdUrl">URL</label>
              <input id="vdUrl" type="text" placeholder="https://www.instagram.com/reel/... / https://x.com/.../status/...">
              <button type="button" id="vdFetchBtn">取得</button>
            </div>
            <div class="ig-options" style="grid-template-columns:minmax(0, 1fr);">
              <label>保存先フォルダー<input id="vdFolder" type="text" value="video" placeholder="video"></label>
            </div>
          </div>
          <div class="ig-grid" id="vdGrid">
            <div class="empty">InstagramまたはXの投稿URLを入力して動画を取得してください。</div>
          </div>
          <div class="ig-actions">
            <button type="button" id="vdSelectAll">全選択</button>
            <button type="button" id="vdClearAll">全解除</button>
            <button type="button" id="vdInvert">反転</button>
            <button type="button" id="vdSaveBtn">選択動画を保存</button>
            <span class="ig-status" id="vdStatus"></span>
          </div>
        </div>
        <div class="resize-handle" data-resize-handle></div>
      `;
      desktopGrid.appendChild(el);
      registerWindow('video-downloader', el, 'Instagram / X Video Downloader', 'downloader');
      wireVideoDownloaderWindow(el);
    }

    function setVideoDownloaderStatus(el, text) {
      el.querySelector('#vdStatus').textContent = text || '';
    }

    function renderVideoDownloaderItems(el) {
      const grid = el.querySelector('#vdGrid');
      grid.innerHTML = '';
      if (!state.videoDownloader.videos.length) {
        grid.innerHTML = '<div class="empty">動画がありません。</div>';
        return;
      }
      state.videoDownloader.videos.forEach(item => {
        const card = document.createElement('div');
        card.className = 'ig-item selected';
        card.innerHTML = `
          <div class="video-download-thumb">VIDEO</div>
          <label><input type="checkbox" checked data-vd-index="${item.index}"><span></span></label>
        `;
        card.querySelector('span').textContent = item.filename || `${item.index}`;
        card.querySelector('input').addEventListener('change', (event) => {
          card.classList.toggle('selected', event.target.checked);
        });
        grid.appendChild(card);
      });
    }

    function selectedVideoDownloaderIndexes(el) {
      return Array.from(el.querySelectorAll('[data-vd-index]:checked')).map(input => Number(input.dataset.vdIndex));
    }

    function loadVideoDownloaderSettings() {
      try {
        const raw = localStorage.getItem(videoDownloaderSettingsKey);
        return raw ? JSON.parse(raw) : {};
      } catch (_) {
        return {};
      }
    }

    function saveVideoDownloaderSettings(el) {
      localStorage.setItem(videoDownloaderSettingsKey, JSON.stringify({
        folder: el.querySelector('#vdFolder')?.value || 'video'
      }));
    }

    function applyVideoDownloaderSettings(el) {
      const settings = loadVideoDownloaderSettings();
      el.querySelector('#vdFolder').value = settings.folder || 'video';
    }

    function wireVideoDownloaderWindow(el) {
      applyVideoDownloaderSettings(el);
      el.querySelector('#vdFolder').addEventListener('change', () => saveVideoDownloaderSettings(el));
      el.querySelector('#vdFolder').addEventListener('input', () => saveVideoDownloaderSettings(el));

      el.querySelector('#vdFetchBtn').addEventListener('click', async () => {
        const url = el.querySelector('#vdUrl').value.trim();
        const fetchBtn = el.querySelector('#vdFetchBtn');
        fetchBtn.disabled = true;
        setVideoDownloaderStatus(el, '取得中...');
        try {
          const data = await fetchJson(videoFetchUrl, {
            method: 'POST',
            body: JSON.stringify({ url })
          });
          setVideoDownloaderStatus(el, '投稿動画を取得中...');
          const result = data.jobId
            ? await waitVideoJob(data.jobId, (count) => setVideoDownloaderStatus(el, `投稿動画を取得中... ${count}`))
            : data;
          state.videoDownloader.identifier = result.identifier;
          state.videoDownloader.jobId = data.jobId || '';
          state.videoDownloader.videos = result.videos || [];
          saveVideoDownloaderSettings(el);
          renderVideoDownloaderItems(el);
          setVideoDownloaderStatus(el, `${state.videoDownloader.videos.length}件取得しました。`);
        } catch (err) {
          state.videoDownloader.videos = [];
          renderVideoDownloaderItems(el);
          setVideoDownloaderStatus(el, err.message || '取得に失敗しました。');
        } finally {
          fetchBtn.disabled = false;
        }
      });

      el.querySelector('#vdSelectAll').addEventListener('click', () => {
        el.querySelectorAll('[data-vd-index]').forEach(input => {
          input.checked = true;
          input.closest('.ig-item')?.classList.add('selected');
        });
      });
      el.querySelector('#vdClearAll').addEventListener('click', () => {
        el.querySelectorAll('[data-vd-index]').forEach(input => {
          input.checked = false;
          input.closest('.ig-item')?.classList.remove('selected');
        });
      });
      el.querySelector('#vdInvert').addEventListener('click', () => {
        el.querySelectorAll('[data-vd-index]').forEach(input => {
          input.checked = !input.checked;
          input.closest('.ig-item')?.classList.toggle('selected', input.checked);
        });
      });
      el.querySelector('#vdSaveBtn').addEventListener('click', async () => {
        const saveBtn = el.querySelector('#vdSaveBtn');
        saveBtn.disabled = true;
        setVideoDownloaderStatus(el, '保存中...');
        try {
          const data = await fetchJson(videoSaveUrl, {
            method: 'POST',
            body: JSON.stringify({
              jobId: state.videoDownloader.jobId,
              videos: state.videoDownloader.videos,
              selected: selectedVideoDownloaderIndexes(el),
              folder: el.querySelector('#vdFolder').value
            })
          });
          setVideoDownloaderStatus(el, `${(data.saved || []).length}件保存しました。`);
          saveVideoDownloaderSettings(el);
          await loadImages();
        } catch (err) {
          setVideoDownloaderStatus(el, err.message || '保存に失敗しました。');
        } finally {
          saveBtn.disabled = false;
        }
      });
    }

    function fileListFromDrag(event) {
      return Array.from(event.dataTransfer?.files || []);
    }

    function hasInternalDrag(event) {
      return Array.from(event.dataTransfer?.types || []).includes('application/x-mfu-image-viewer-entry');
    }

    function wireUploadInteractions() {
      const uploadInput = document.getElementById('uploadInput');
      document.getElementById('newFolderBtn').addEventListener('click', () => {
        createFolderInCurrentFolder().catch(err => alert(err.message || 'フォルダーを作成できませんでした。'));
      });
      document.getElementById('uploadBtn').addEventListener('click', () => {
        uploadInput.value = '';
        uploadInput.click();
      });
      document.getElementById('pasteBtn').addEventListener('click', () => {
        pasteFromClipboard().catch(err => alert(err.message || '貼り付け保存に失敗しました。'));
      });
      uploadInput.addEventListener('change', () => {
        uploadFilesToFolder(uploadInput.files, state.currentFolder).catch(err => alert(err.message || 'アップロードに失敗しました。'));
      });

      fileGrid.addEventListener('dragover', (event) => {
        if (!hasInternalDrag(event) && !fileListFromDrag(event).length && !event.dataTransfer?.types?.includes('Files')) return;
        event.preventDefault();
        if (hasInternalDrag(event)) event.dataTransfer.dropEffect = 'move';
        fileGrid.classList.add('drag-over');
      });
      fileGrid.addEventListener('dragleave', (event) => {
        if (!fileGrid.contains(event.relatedTarget)) fileGrid.classList.remove('drag-over');
      });
      fileGrid.addEventListener('drop', (event) => {
        event.preventDefault();
        fileGrid.classList.remove('drag-over');
        const entry = getDragEntry(event);
        if (entry) {
          moveEntry(entry, state.currentFolder).catch(err => alert(err.message || '移動に失敗しました。'));
          return;
        }
        uploadFilesToFolder(event.dataTransfer.files, state.currentFolder).catch(err => alert(err.message || 'アップロードに失敗しました。'));
      });

      folderList.addEventListener('dragover', (event) => {
        const folderButton = event.target.closest('.folder');
        if (!folderButton || (!hasInternalDrag(event) && !event.dataTransfer?.types?.includes('Files'))) return;
        event.preventDefault();
        if (hasInternalDrag(event)) event.dataTransfer.dropEffect = 'move';
        folderButton.classList.add('drag-over');
      });
      folderList.addEventListener('dragleave', (event) => {
        event.target.closest('.folder')?.classList.remove('drag-over');
      });
      folderList.addEventListener('drop', (event) => {
        const folderButton = event.target.closest('.folder');
        if (!folderButton) return;
        event.preventDefault();
        folderButton.classList.remove('drag-over');
        const entry = getDragEntry(event);
        if (entry) {
          moveEntry(entry, folderButton.dataset.folder || '').catch(err => alert(err.message || '移動に失敗しました。'));
          return;
        }
        uploadFilesToFolder(event.dataTransfer.files, folderButton.dataset.folder || '').catch(err => alert(err.message || 'アップロードに失敗しました。'));
      });

      document.addEventListener('paste', (event) => {
        const target = event.target;
        if (target && target.closest && target.closest('input, textarea, [contenteditable="true"]')) return;
        const files = [];
        for (const item of Array.from(event.clipboardData?.items || [])) {
          if (!item.type.startsWith('image/')) continue;
          const file = item.getAsFile();
          if (file) files.push(file);
        }
        if (!files.length) return;
        event.preventDefault();
        pasteClipboardFiles(files, state.currentFolder).catch(err => alert(err.message || '貼り付け保存に失敗しました。'));
      });
    }


    document.getElementById('refreshBtn').addEventListener('click', loadImages);
    wireUploadInteractions();
    fileGrid.addEventListener('scroll', () => {
      hydrateCurrentViewportThumbnails();
      scheduleHydrateAfterScrollSettles();
    }, { passive: true });

    async function runThumbnailBuild(button, force) {
      const originalText = button.textContent;
      const folder = state.currentFolder || '';
      const folderName = folderLabel(folder);
      thumbBtn.disabled = true;
      regenThumbBtn.disabled = true;
      button.textContent = force ? '再生成中' : '作成中';
      const progress = openProgressWindow(`${force ? 'サムネイル再生成' : 'サムネイル作成'} - ${folderName}`);
      try {
        const data = await fetchJson("/mock", {
          method: 'POST',
          body: JSON.stringify({ force, folder })
        });
        const result = data.jobId ? await waitThumbnailJob(data.jobId, progress.el) : data;
        button.textContent = `${result.created || 0}件作成`;
        await loadImages();
        setTimeout(() => { button.textContent = originalText; }, 1200);
      } catch (err) {
        progress.el.querySelector('.progress-detail').textContent = err.message || 'サムネイル生成に失敗しました。';
        button.textContent = '失敗';
        setTimeout(() => { button.textContent = originalText; }, 1800);
      } finally {
        thumbBtn.disabled = false;
        regenThumbBtn.disabled = false;
      }
    }

    thumbBtn.addEventListener('click', () => {
      runThumbnailBuild(thumbBtn, false);
    });
    regenThumbBtn.addEventListener('click', () => {
      runThumbnailBuild(regenThumbBtn, true);
    });
    sortBtn.addEventListener('click', () => {
      setSortDirection(state.currentFolder, sortDirection() === 'desc' ? 'asc' : 'desc');
      renderAllExplorers();
    });
    viewButtons.forEach(btn => {
      btn.addEventListener('click', () => {
        setViewSize(state.currentFolder, btn.dataset.viewSize);
        renderAllExplorers();
      });
    });
    document.getElementById('tileBtn').addEventListener('click', () => {
      const selected = imagesInCurrentFolder().find(img => img.path === state.selectedPath);
      if (selected) openMedia(selected);
    });
    document.getElementById('showExplorerBtn').addEventListener('click', () => openExplorerWindow(state.currentFolder));
    document.getElementById('instagramDesktopIcon').addEventListener('dblclick', openInstagramWindow);
    document.getElementById('videoDownloaderDesktopIcon').addEventListener('dblclick', openVideoDownloaderWindow);

    const browserFullscreenIcon = document.getElementById('browserFullscreenIcon');
    const browserFullscreenLabel = document.getElementById('browserFullscreenLabel');
    const taskbarFullscreenBtn = document.getElementById('taskbarFullscreenBtn');
    function refreshBrowserFullscreenIcon() {
      const fullscreen = Boolean(document.fullscreenElement);
      browserFullscreenLabel.textContent = fullscreen ? '解除' : '全画面';
      taskbarFullscreenBtn.textContent = fullscreen ? '戻す' : 'FS';
      taskbarFullscreenBtn.title = fullscreen ? '全画面を解除' : '全画面';
    }
    async function toggleBrowserFullscreen() {
      try {
        if (document.fullscreenElement) {
          await document.exitFullscreen();
        } else {
          await document.documentElement.requestFullscreen();
        }
      } catch (_) {
        setTimeout(() => alert('ブラウザが全画面表示を許可していません。'), 0);
      } finally {
        refreshBrowserFullscreenIcon();
      }
    }
    browserFullscreenIcon.addEventListener('dblclick', toggleBrowserFullscreen);
    taskbarFullscreenBtn.addEventListener('click', toggleBrowserFullscreen);
    document.addEventListener('fullscreenchange', refreshBrowserFullscreenIcon);
    refreshBrowserFullscreenIcon();
    document.addEventListener('click', closeContextMenu);
    document.addEventListener('contextmenu', (event) => {
      if (!event.target.closest('.file, .folder')) closeContextMenu();
    });

    document.addEventListener('keydown', (event) => {
      const target = event.target;
      if (target && target.closest && target.closest('input, textarea, select, [contenteditable="true"]')) return;
      const activeWindow = state.windows.get(state.activeId);
      if (activeWindow?.kind === 'explorer') {
        if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'c') {
          event.preventDefault();
          copyEntryToClipboard().catch(err => alert(err.message || 'コピーに失敗しました。'));
          return;
        }
        if (event.key === 'F2') {
          event.preventDefault();
          renameEntry().catch(err => alert(err.message || '名前変更に失敗しました。'));
          return;
        }
        if (event.key === 'Delete') {
          event.preventDefault();
          deleteEntry().catch(err => alert(err.message || '削除に失敗しました。'));
          return;
        }
        if (event.key === 'Enter') {
          event.preventDefault();
          openSelectedEntry();
          return;
        }
      }
      const win = activeWindow;
      if (!win || win.kind !== 'viewer' || !win.image) return;
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'c') {
        event.preventDefault();
        copyEntryToClipboard({ type: 'file', path: win.image.path }).catch(err => alert(err.message || 'コピーに失敗しました。'));
        return;
      }
      if (event.key === 'ArrowLeft') {
        setViewerImage(win.id, neighborImage(win.image, -1));
        event.preventDefault();
      }
      if (event.key === 'ArrowRight') {
        setViewerImage(win.id, neighborImage(win.image, 1));
        event.preventDefault();
      }
      if (event.key === 'Escape') closeWindow(win.id);
    });

    function updateClock() {
      const now = new Date();
      clock.textContent = now.toLocaleTimeString('ja-JP', { hour: '2-digit', minute: '2-digit' });
    }

    loadSortState();
    loadViewSizeState();
    registerWindow('explorer', explorerWindow, 'エクスプローラー', 'explorer');
    updateClock();
    setInterval(updateClock, 15000);
    loadImages().catch(err => {
      fileGrid.innerHTML = `<div class="empty">${err.message || '読み込みに失敗しました。'}</div>`;
    });

    if ('serviceWorker' in navigator) {
      window.addEventListener('load', () => {
        navigator.serviceWorker.register("/mock", {
          scope: "/mock"
        }).catch(() => {});
      });
    }
  })();