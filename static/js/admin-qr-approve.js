(() => {
  "use strict";
  const csrf = document.getElementById("qr-root")?.dataset.csrf || "";
  const token = location.hash.slice(1) || sessionStorage.getItem("mfuAdminQrToken") || "";
  if (location.hash.slice(1)) sessionStorage.setItem("mfuAdminQrToken", token);
  // Remove the secret from the address bar and browser history immediately.
  history.replaceState(null, "", location.pathname + location.search);
  const error = document.getElementById("qr-error");
  const details = document.getElementById("qr-details");
  const result = document.getElementById("qr-result");
  const showError = text => { error.textContent = text; error.classList.remove("d-none"); details.classList.add("d-none"); };
  const showDecisionError = text => { error.textContent = text; error.classList.remove("d-none"); details.classList.remove("d-none"); };
  const post = async (url, body) => {
    const response = await fetch(url, {
      method: "POST", credentials: "same-origin",
      headers: {"Content-Type": "application/json", "X-CSRF-Token": csrf},
      body: JSON.stringify({...body, csrf_token: csrf}),
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) {
      const err = new Error(data.error || "処理に失敗しました。");
      err.status = response.status;
      throw err;
    }
    return data;
  };
  const verifyPasskey = async () => {
    if (!window.PublicKeyCredential || !navigator.credentials) {
      throw new Error("この端末ではパスキーを利用できません。");
    }
    const options = await post("/webauthn/auth/options", {
      username: "admin", purpose: "admin_qr_approval", qr_token: token,
    });
    const credential = await navigator.credentials.get({
      publicKey: PasskeyUtils.decodeRequestOptions(options),
    });
    await post("/webauthn/auth/verify", {
      username: "admin",
      purpose: "admin_qr_approval",
      qr_token: token,
      credential: PasskeyUtils.serializeAuthenticationCredential(credential),
    });
  };
  const load = async () => {
    if (token.length < 40) throw new Error("QRコードが不正です。");
    const data = await post("/auth/admin/qr/details", {token});
    document.getElementById("desktop-ip").textContent = data.desktop_ip || "不明";
    document.getElementById("desktop-ua").textContent = data.desktop_user_agent || "不明";
    document.getElementById("created-at").textContent = data.created_at || "";
    document.getElementById("expires-at").textContent = data.expires_at || "";
    details.classList.remove("d-none");
  };
  document.querySelectorAll("[data-decision]").forEach(button => button.addEventListener("click", async () => {
    try {
      error.classList.add("d-none");
      document.querySelectorAll("[data-decision]").forEach(item => { item.disabled = true; });
      if (button.dataset.decision === "approve") await verifyPasskey();
      const data = await post("/auth/admin/qr/decision", {token, action: button.dataset.decision});
      sessionStorage.removeItem("mfuAdminQrToken");
      details.classList.add("d-none");
      result.textContent = data.approved ? "ログインを承認しました。この画面を閉じてください。" : "ログインを拒否しました。";
      result.classList.remove("d-none");
    } catch (e) {
      document.querySelectorAll("[data-decision]").forEach(item => { item.disabled = false; });
      showDecisionError(e.name === "NotAllowedError" ? "パスキー認証がキャンセルされました。" : e.message);
    }
  }));
  load().catch(e => {
    if (e.status === 401) {
      sessionStorage.setItem("mfuAdminQrToken", token);
      location.href = "/login?next=/auth/admin/qr/approve";
      return;
    }
    showError(e.message);
  });
})();
