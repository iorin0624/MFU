(() => {
  "use strict";
  const root = document.getElementById("layer-upload-detail");
  const buttons = Array.from(document.querySelectorAll(".layer-zip-download"));
  if (!root || !buttons.length) return;

  const showStatus = (status, message, kind = "info") => {
    if (!status) return;
    status.textContent = message;
    status.className = `alert alert-${kind} py-2`;
  };

  buttons.forEach(button => button.addEventListener("click", async () => {
    const status = document.getElementById(button.dataset.statusTarget || "layer-zip-status-all");
    const replyUuid = button.dataset.replyUuid || "";
    button.disabled = true;
    showStatus(status, "ZIPを準備しています… 0%", "info");
    try {
      const response = await fetch(root.dataset.zipPrepareUrl, {
        method: "POST",
        credentials: "same-origin",
        headers: {
          "Content-Type": "application/json",
          "X-CSRF-Token": root.dataset.csrf || "",
        },
        body: JSON.stringify({
          csrf_token: root.dataset.csrf || "",
          reply_uuid: replyUuid,
        }),
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok || !data.ok) {
        throw new Error(data.error || "ZIPの準備を開始できませんでした。");
      }

      await MFUZipDownload.waitUntilReady({
        key: data.key,
        onProgress: progress => {
          const percent = Number(progress.percent || 0);
          const done = Number(progress.processed_files || 0);
          const total = Number(progress.total_files || data.file_count || 0);
          showStatus(status, `ZIPを準備しています… ${percent}%（${done}/${total}枚）`, "info");
        },
      });
      showStatus(status, "ZIPの準備が完了しました。ダウンロードを開始します。", "success");
      MFUZipDownload.startDownload(data.download_url);
    } catch (error) {
      showStatus(status, error.message || "ZIPの作成に失敗しました。", "danger");
    } finally {
      button.disabled = false;
    }
  }));
})();
