(() => {
  "use strict";

  const csrf = () => document.querySelector('meta[name="csrf-token"]')?.content || "";

  const post = async (url, body) => {
    const response = await fetch(url, {
      method: "POST",
      credentials: "same-origin",
      headers: {"Content-Type": "application/json", "X-CSRF-Token": csrf()},
      body: JSON.stringify({...body, csrf_token: csrf()}),
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) {
      const error = new Error(data.message || data.error || "パスキー認証に失敗しました。");
      error.status = response.status;
      throw error;
    }
    return data;
  };

  const authorize = async (action) => {
    if (!action) throw new Error("操作内容を特定できませんでした。");
    if (!window.PublicKeyCredential || !navigator.credentials || !window.PasskeyUtils) {
      throw new Error("この端末ではパスキーを利用できません。");
    }
    const options = await post("/webauthn/auth/options", {
      username: "admin", purpose: "admin_action", action,
    });
    const credential = await navigator.credentials.get({
      publicKey: PasskeyUtils.decodeRequestOptions(options),
    });
    const result = await post("/webauthn/auth/verify", {
      username: "admin", purpose: "admin_action", action,
      credential: PasskeyUtils.serializeAuthenticationCredential(credential),
    });
    if (!result.token) throw new Error("操作許可を取得できませんでした。");
    return result.token;
  };

  const putToken = (form, token) => {
    let input = form.querySelector('input[name="admin_passkey_token"]');
    if (!input) {
      input = document.createElement("input");
      input.type = "hidden";
      input.name = "admin_passkey_token";
      form.appendChild(input);
    }
    input.value = token;
  };

  const fieldChanged = (field) => {
    const initial = field.dataset.adminInitial;
    if (initial !== undefined) return String(field.value) !== initial;
    if (field instanceof HTMLInputElement || field instanceof HTMLTextAreaElement) {
      return field.value !== field.defaultValue;
    }
    if (field instanceof HTMLSelectElement) {
      const original = Array.from(field.options).find(option => option.defaultSelected)?.value ?? "";
      return field.value !== original;
    }
    return false;
  };

  const shouldProtect = (form, submitter) => {
    const requiredField = submitter?.dataset.adminPasskeyIfField || form.dataset.adminPasskeyIfField;
    if (requiredField) {
      const field = form.elements.namedItem(requiredField);
      return Boolean(field && String(field.value || "").trim());
    }
    const changedFields = submitter?.dataset.adminPasskeyIfChanged || form.dataset.adminPasskeyIfChanged;
    if (changedFields) {
      return changedFields.split(",").some(name => {
        const field = form.elements.namedItem(name.trim());
        return field && fieldChanged(field);
      });
    }
    return true;
  };

  document.addEventListener("submit", (event) => {
    const form = event.target;
    if (!(form instanceof HTMLFormElement)) return;
    const submitter = event.submitter;
    const action = submitter?.dataset.adminPasskeyAction || form.dataset.adminPasskeyAction;
    if (!action || form.dataset.adminPasskeyAuthorized === "1" || !shouldProtect(form, submitter)) {
      if (form.dataset.adminPasskeyAuthorized === "1") delete form.dataset.adminPasskeyAuthorized;
      return;
    }

    event.preventDefault();
    event.stopImmediatePropagation();
    const buttons = Array.from(form.querySelectorAll('button, input[type="submit"]'));
    buttons.forEach(button => { button.disabled = true; });
    authorize(action).then(token => {
      putToken(form, token);
      form.dataset.adminPasskeyAuthorized = "1";
      buttons.forEach(button => { button.disabled = false; });
      form.requestSubmit(submitter || undefined);
    }).catch(error => {
      buttons.forEach(button => { button.disabled = false; });
      if (error?.name !== "NotAllowedError") window.alert(error.message || "パスキー認証に失敗しました。");
    });
  }, true);

  window.MFUAdminPasskey = {authorize};
})();
