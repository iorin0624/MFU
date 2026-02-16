(function () {
  /**
   * テキストをクリップボードにコピー
   */
  async function copyText(text) {
    if (!text) throw new Error("empty");
    if (navigator.clipboard && window.isSecureContext) {
      try {
        await navigator.clipboard.writeText(text);
        return;
      } catch (err) {
        console.error("Clipboard API failed, falling back:", err);
      }
    }
    // フォールバック
    const ta = document.createElement("textarea");
    ta.value = text;
    ta.setAttribute("readonly", "readonly");
    ta.style.position = "fixed";
    ta.style.top = "-9999px";
    document.body.appendChild(ta);
    ta.focus();
    ta.select();
    const ok = document.execCommand("copy");
    document.body.removeChild(ta);
    if (!ok) throw new Error("execCommand failed");
  }

  /**
   * フィードバックの表示
   */
  function showFeedback(message, isError) {
    const container = document.getElementById("copy-feedback");
    if (!container || !window.bootstrap || !window.bootstrap.Toast) {
      alert(message);
      return;
    }
    const wrap = document.createElement("div");
    wrap.className = "toast align-items-center text-white " + (isError ? "bg-danger" : "bg-success") + " border-0";
    wrap.role = "alert";
    wrap.innerHTML = '<div class="d-flex"><div class="toast-body">' + message + '</div>' +
      '<button type="button" class="btn-close btn-close-white me-2 m-auto" data-bs-dismiss="toast"></button></div>';
    container.appendChild(wrap);
    const toast = new window.bootstrap.Toast(wrap, { delay: 1800 });
    wrap.addEventListener("hidden.bs.toast", () => wrap.remove());
    toast.show();
  }

  /**
   * 初期化処理
   */
  function init() {
    document.addEventListener("click", async (event) => {
      const btn = event.target.closest(".js-copy");
      if (!btn) return;
      event.preventDefault();
      const text = btn.getAttribute("data-copy-text") || "";
      try {
        await copyText(text);
        showFeedback("コピーしました。", false);
      } catch (err) {
        showFeedback("コピーに失敗しました。", true);
      }
    });
  }

  // DOMの読み込み完了を待って実行
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
