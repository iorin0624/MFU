(() => {
  "use strict";
  const form = document.getElementById("login-form");
  if (!form) return;
  const csrf = document.getElementById("csrf-token")?.value || "";
  const username = document.getElementById("username");
  const errorBox = document.getElementById("auth-error");
  const infoBox = document.getElementById("auth-info");
  const isPreauth = form.dataset.preauthActive === "true";
  const isAdminMfa = form.dataset.adminMfa === "true";
  const qrLoginEnabled = form.dataset.qrLoginEnabled === "true";
  let pollTimer = null;

  const show = (box, text) => {
    if (!box) return;
    box.textContent = text;
    box.classList.toggle("d-none", !text);
  };
  const clear = () => { show(errorBox, ""); show(infoBox, ""); };
  const post = async (url, body) => {
    const response = await fetch(url, {
      method: "POST",
      credentials: "same-origin",
      headers: {"Content-Type": "application/json", "X-CSRF-Token": csrf},
      body: JSON.stringify({...body, csrf_token: csrf}),
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.error || data.message || "認証処理に失敗しました。");
    return data;
  };

  const passkey = async () => {
    clear();
    const name = (username?.value || form.dataset.preauthUsername || "").trim();
    if (!name) throw new Error("ユーザー名を入力してください。");
    if (!window.PublicKeyCredential || !navigator.credentials) throw new Error("この端末ではパスキーを利用できません。");
    const options = await post("/webauthn/auth/options", {username: name});
    const credential = await navigator.credentials.get({publicKey: PasskeyUtils.decodeRequestOptions(options)});
    const result = await post("/webauthn/auth/verify", {
      username: name,
      credential: PasskeyUtils.serializeAuthenticationCredential(credential),
    });
    location.href = result.redirect || "/upload";
  };

  document.getElementById("initial-passkey")?.addEventListener("click", () => passkey().catch(e => show(errorBox, e.message)));
  document.getElementById("mfa-passkey")?.addEventListener("click", () => passkey().catch(e => show(errorBox, e.message)));
  document.getElementById("totp-verify")?.addEventListener("click", async () => {
    clear();
    try {
      const result = await post("/mfa/totp/verify", {
        username: (username?.value || form.dataset.preauthUsername || "").trim(),
        code: (document.getElementById("totp-code")?.value || "").trim(),
      });
      location.href = result.redirect || "/upload";
    } catch (e) { show(errorBox, e.message); }
  });
  document.getElementById("email-send")?.addEventListener("click", async () => {
    clear();
    try {
      await post("/mfa/email/send", {username: (username?.value || form.dataset.preauthUsername || "").trim()});
      show(infoBox, "メールOTPを送信しました。");
    } catch (e) { show(errorBox, e.message); }
  });
  document.getElementById("email-verify")?.addEventListener("click", async () => {
    clear();
    try {
      const result = await post("/mfa/email/verify", {
        username: (username?.value || form.dataset.preauthUsername || "").trim(),
        code: (document.getElementById("email-code")?.value || "").trim(),
      });
      location.href = result.redirect || "/upload";
    } catch (e) { show(errorBox, e.message); }
  });

  const pollQr = async () => {
    try {
      const data = await post("/auth/admin/qr/status", {});
      const status = document.getElementById("qr-status");
      if (data.status === "approved") { location.href = data.redirect || "/upload"; return; }
      if (data.status === "rejected") { show(status, "スマートフォンで拒否されました。"); return; }
      if (data.status === "expired" || data.status === "consumed") {
        show(status, "QRコードの有効期限が切れました。");
        document.getElementById("qr-reload")?.classList.remove("d-none");
        return;
      }
      pollTimer = setTimeout(pollQr, 1500);
    } catch (e) { show(document.getElementById("qr-status"), e.message); }
  };
  const createQr = async () => {
    if (pollTimer) clearTimeout(pollTimer);
    const reload = document.getElementById("qr-reload");
    reload?.classList.add("d-none");
    try {
      const data = await post("/auth/admin/qr/create", {});
      const image = document.getElementById("qr-image");
      image.src = data.qr_image;
      image.classList.remove("d-none");
      show(document.getElementById("qr-status"), "スマートフォンで読み取り、内容を確認して承認してください。");
      pollTimer = setTimeout(pollQr, 1000);
    } catch (e) { show(document.getElementById("qr-status"), e.message); }
  };
  document.getElementById("qr-reload")?.addEventListener("click", createQr);
  if (qrLoginEnabled) createQr();
})();
