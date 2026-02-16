(function () {
  function fallbackCopy(text) {
    const ta = document.createElement("textarea");
    ta.value = text;
    ta.setAttribute("readonly", "readonly");
    ta.style.position = "fixed";
    ta.style.top = "-9999px";
    ta.style.left = "-9999px";
    document.body.appendChild(ta);
    ta.focus();
    ta.select();
    ta.setSelectionRange(0, ta.value.length);
    const ok = document.execCommand("copy");
    document.body.removeChild(ta);
    if (!ok) {
      throw new Error("execCommand failed");
    }
  }

  async function copyText(text) {
    if (!text) {
      throw new Error("empty");
    }

    if (navigator.clipboard && window.isSecureContext) {
      try {
        await navigator.clipboard.writeText(text);
        return;
      } catch (_err) {
        // fall back below
      }
    }

    fallbackCopy(text);
  }

  function showFeedback(message, isError) {
    const container = document.getElementById("copy-feedback");
    if (!container || !window.bootstrap || !window.bootstrap.Toast) {
      alert(message);
      return;
    }

    const wrap = document.createElement("div");
    wrap.className = "toast align-items-center text-white " + (isError ? "bg-danger" : "bg-success") + " border-0";
    wrap.role = "alert";
    wrap.ariaLive = "assertive";
    wrap.ariaAtomic = "true";
    wrap.innerHTML =
      '<div class="d-flex">' +
      '<div class="toast-body">' + message + '</div>' +
      '<button type="button" class="btn-close btn-close-white me-2 m-auto" data-bs-dismiss="toast" aria-label="Close"></button>' +
      '</div>';
    container.appendChild(wrap);

    const toast = new window.bootstrap.Toast(wrap, { delay: 1800 });
    wrap.addEventListener("hidden.bs.toast", function () {
      wrap.remove();
    });
    toast.show();
  }

  document.addEventListener("click", async function (event) {
    const btn = event.target.closest(".js-copy");
    if (!btn) return;

    event.preventDefault();
    const text = btn.getAttribute("data-copy-text") || "";
    try {
      await copyText(text);
      showFeedback("コピーしました。", false);
    } catch (_err) {
      showFeedback("コピーに失敗しました。", true);
    }
  });
})();
