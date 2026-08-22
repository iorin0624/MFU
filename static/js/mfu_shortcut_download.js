(function (window, document) {
  'use strict';

  const CONFIG_URL = '/mobile-download/api/shortcut-config';
  let configPromise = null;
  let activeDialog = null;

  function isIOSDevice() {
    const ua = navigator.userAgent || '';
    const classicIOS = /iPad|iPhone|iPod/i.test(ua);
    const desktopModeIPad = navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1;
    return classicIOS || desktopModeIPad;
  }

  async function loadConfig(force) {
    if (!configPromise || force) {
      configPromise = fetch(CONFIG_URL, { credentials: 'same-origin', cache: 'no-store' })
        .then(async response => {
          const data = await response.json().catch(() => null);
          if (!response.ok || !data?.ok) throw new Error(data?.error || 'shortcut_config_failed');
          return data;
        });
    }
    return configPromise;
  }

  function addStyles() {
    if (document.getElementById('mfu-shortcut-dialog-style')) return;
    const style = document.createElement('style');
    style.id = 'mfu-shortcut-dialog-style';
    style.textContent = `
      .mfu-sc-overlay{position:fixed;inset:auto 0 0;z-index:2147483000;width:100%;height:100vh;height:100dvh;box-sizing:border-box;background:rgba(12,20,35,.58);display:flex;align-items:center;justify-content:center;padding:max(16px,env(safe-area-inset-top)) 16px max(20px,env(safe-area-inset-bottom))}
      .mfu-sc-dialog{width:min(440px,100%);max-height:calc(100dvh - max(32px,env(safe-area-inset-top)) - max(40px,env(safe-area-inset-bottom)));overflow:auto;overscroll-behavior:contain;-webkit-overflow-scrolling:touch;background:#fff;color:#172033;border-radius:18px;box-shadow:0 20px 60px rgba(0,0,0,.28);padding:24px;font-family:system-ui,-apple-system,"Yu Gothic",sans-serif}
      .mfu-sc-title{font-size:21px;line-height:1.45;margin:0 0 12px;font-weight:750}
      .mfu-sc-copy{white-space:pre-wrap;line-height:1.7;color:#526071;margin:0 0 16px}
      .mfu-sc-steps{white-space:pre-wrap;line-height:1.65;background:#f3f6fa;border-radius:10px;padding:12px 14px;margin:0 0 18px;color:#364255}
      .mfu-sc-status{display:flex;align-items:center;gap:10px;line-height:1.55;color:#364255;margin:4px 0 18px}
      .mfu-sc-spinner{width:20px;height:20px;border:3px solid #cbd8ea;border-right-color:#1267d6;border-radius:50%;animation:mfu-sc-spin .7s linear infinite;flex:0 0 auto}
      .mfu-sc-actions{position:sticky;bottom:-24px;display:grid;justify-items:center;gap:10px;margin:0 -24px -24px;padding:14px 24px max(24px,calc(env(safe-area-inset-bottom) + 10px));background:linear-gradient(to bottom,rgba(255,255,255,.92),#fff 14px);z-index:2}
      .mfu-sc-btn{appearance:none;display:block;width:min(100%,300px);box-sizing:border-box;border-radius:10px;padding:13px 16px;font-size:16px;font-weight:700;text-align:center;text-decoration:none;cursor:pointer;border:1px solid #1267d6;background:#1267d6;color:#fff}
      .mfu-sc-btn:hover{color:#fff;background:#0f5fc9}.mfu-sc-btn.secondary{background:#fff;color:#1267d6}.mfu-sc-btn.secondary:hover{background:#eef5ff;color:#0f5fc9}
      .mfu-sc-btn.close{background:#fff;color:#596474;border-color:#c8d0da}.mfu-sc-btn.close:hover{background:#f4f5f7;color:#313a47}
      .mfu-sc-btn[disabled]{opacity:.55;cursor:not-allowed}.mfu-sc-url-warning{font-size:14px;color:#a25b00;margin:0 0 12px}
      @keyframes mfu-sc-spin{to{transform:rotate(360deg)}}
      @media (max-width:480px){.mfu-sc-dialog{padding:20px}.mfu-sc-actions{bottom:-20px;margin:0 -20px -20px;padding-left:20px;padding-right:20px}}
    `;
    document.head.appendChild(style);
  }

  function buildDialog(config) {
    activeDialog?.remove();
    addStyles();
    const overlay = document.createElement('div');
    overlay.className = 'mfu-sc-overlay';
    overlay.setAttribute('role', 'dialog');
    overlay.setAttribute('aria-modal', 'true');
    overlay.setAttribute('aria-label', 'ショートカットの起動');
    overlay.innerHTML = `
      <div class="mfu-sc-dialog">
        <section data-state="launching">
          <h2 class="mfu-sc-title">ショートカットを確認しています</h2>
          <div class="mfu-sc-status"><span class="mfu-sc-spinner" aria-hidden="true"></span><span>「${escapeHTML(config.shortcut_name || 'MFU写真保存')}」を開いています…</span></div>
          <p class="mfu-sc-copy">ショートカットへ移動した場合は、そのまま保存完了までお待ちください。</p>
          <div class="mfu-sc-actions"><button type="button" class="mfu-sc-btn close" data-close>閉じる</button></div>
        </section>
        <section data-state="install" hidden>
          <h2 class="mfu-sc-title" data-title></h2>
          <p class="mfu-sc-copy" data-body></p>
          <div class="mfu-sc-steps" data-steps></div>
          <p class="mfu-sc-url-warning" data-url-warning hidden>ショートカット配布URLがまだ設定されていません。管理者へお問い合わせください。</p>
          <div class="mfu-sc-actions">
            <a class="mfu-sc-btn" data-install-link></a>
            <button type="button" class="mfu-sc-btn secondary" data-retry>もう一度起動</button>
            <button type="button" class="mfu-sc-btn close" data-close>閉じる</button>
          </div>
        </section>
      </div>`;
    overlay.querySelector('[data-title]').textContent = config.popup_title || 'ショートカットが必要です';
    overlay.querySelector('[data-body]').textContent = config.popup_body || '';
    overlay.querySelector('[data-steps]').textContent = config.install_steps || '';
    const installLink = overlay.querySelector('[data-install-link]');
    installLink.textContent = config.download_button_label || 'ショートカットを入手';
    if (config.download_url) {
      installLink.href = config.download_url;
    } else {
      installLink.removeAttribute('href');
      installLink.setAttribute('aria-disabled', 'true');
      installLink.addEventListener('click', event => event.preventDefault());
      overlay.querySelector('[data-url-warning]').hidden = false;
    }
    overlay.querySelectorAll('[data-close]').forEach(button => {
      button.addEventListener('click', () => overlay.remove());
    });
    document.body.appendChild(overlay);
    activeDialog = overlay;
    return overlay;
  }

  function escapeHTML(value) {
    return String(value || '').replace(/[&<>'"]/g, character => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;'
    })[character]);
  }

  async function readStatus(statusUrl) {
    if (!statusUrl) return { ok: false, started: false };
    try {
      const response = await fetch(statusUrl, { credentials: 'same-origin', cache: 'no-store' });
      return await response.json().catch(() => ({ ok: false, started: false }));
    } catch (_) {
      return { ok: false, started: false };
    }
  }

  async function launch(jobData) {
    if (!isIOSDevice()) return { started: false, unsupported: true };
    const config = await loadConfig();
    if (!config.enabled) return { started: false, disabled: true };
    if (!jobData?.shortcut_url) throw new Error('shortcut_url_missing');

    const dialog = buildDialog(config);
    const launching = dialog.querySelector('[data-state="launching"]');
    const install = dialog.querySelector('[data-state="install"]');
    let attemptId = 0;

    const showInstall = () => {
      launching.hidden = true;
      install.hidden = false;
    };

    const runAttempt = () => {
      attemptId += 1;
      const currentAttempt = attemptId;
      launching.hidden = false;
      install.hidden = true;
      const deadline = Date.now() + Number(config.detection_timeout_seconds || 10) * 1000;

      const check = async () => {
        if (currentAttempt !== attemptId || !dialog.isConnected) return;
        const status = await readStatus(jobData.shortcut_status_url);
        if (status?.started) {
          dialog.remove();
          return;
        }
        if (status?.expired) {
          showInstall();
          return;
        }
        if (Date.now() >= deadline && document.visibilityState === 'visible') {
          showInstall();
          return;
        }
        window.setTimeout(check, 900);
      };

      window.setTimeout(check, 900);
      window.location.assign(jobData.shortcut_url);
    };

    dialog.querySelector('[data-retry]').addEventListener('click', runAttempt);
    runAttempt();
    return { started: true, pending: true };
  }

  async function initializeButtons() {
    const buttons = Array.from(document.querySelectorAll('[data-mfu-shortcut-button]'));
    buttons.forEach(button => {
      button.hidden = true;
      button.style.setProperty('display', 'none');
    });
    if (!buttons.length || !isIOSDevice()) return;
    try {
      const config = await loadConfig();
      if (!config.enabled) return;
      buttons.forEach(button => {
        button.hidden = false;
        button.style.removeProperty('display');
      });
      window.dispatchEvent(new Event('resize'));
    } catch (error) {
      console.error('MFU shortcut config load failed:', error);
    }
  }

  window.MFUShortcutDownload = {
    isIOSDevice,
    loadConfig,
    launch,
    initializeButtons,
  };

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', initializeButtons, { once: true });
  } else {
    initializeButtons();
  }
})(window, document);
