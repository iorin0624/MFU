(() => {
  "use strict";

  // Keep one ordered gallery for every reply belonging to this parent UUID.
  const links = Array.from(document.querySelectorAll("#layer-image-gallery .layer-image-open"));
  const viewer = document.getElementById("layer-image-viewer");
  if (!links.length || !viewer || typeof viewer.showModal !== "function") return;

  const image = document.getElementById("layer-image-viewer-image");
  const imageError = document.getElementById("layer-image-viewer-image-error");
  const counter = document.getElementById("layer-image-viewer-counter");
  const reply = document.getElementById("layer-image-viewer-reply");
  const filename = document.getElementById("layer-image-viewer-filename");
  const copyButton = document.getElementById("layer-image-viewer-copy");
  const download = document.getElementById("layer-image-viewer-download");
  const status = document.getElementById("layer-image-viewer-status");
  let currentIndex = -1;
  let lastTrigger = null;
  let copying = false;

  const message = (value, isError = false) => {
    status.textContent = value;
    status.classList.toggle("is-error", isError);
  };

  const showImage = index => {
    currentIndex = (index + links.length) % links.length;
    const link = links[currentIndex];
    const name = link.dataset.filename || link.querySelector("img")?.alt || "画像";
    const url = link.href;
    counter.textContent = `${currentIndex + 1} / ${links.length}`;
    reply.textContent = `返信UUID: ${link.dataset.replyUuid || "不明"}`;
    filename.textContent = name;
    download.href = url;
    download.download = name;
    image.alt = name;
    image.hidden = true;
    imageError.hidden = true;
    copyButton.disabled = true;
    document.getElementById("layer-image-viewer-prev").disabled = links.length < 2;
    document.getElementById("layer-image-viewer-next").disabled = links.length < 2;
    message("画像を読み込み中…");
    image.src = url;
  };

  image.addEventListener("load", () => {
    image.hidden = false;
    imageError.hidden = true;
    copyButton.disabled = copying;
    message("");
  });
  image.addEventListener("error", () => {
    image.hidden = true;
    imageError.hidden = false;
    copyButton.disabled = true;
    message("画像を表示できません。元画像の保存をお試しください。", true);
  });

  links.forEach((link, index) => link.addEventListener("click", event => {
    event.preventDefault();
    lastTrigger = link;
    showImage(index);
    if (!viewer.open) viewer.showModal();
    document.getElementById("layer-image-viewer-close").focus();
  }));

  document.getElementById("layer-image-viewer-close").addEventListener("click", () => viewer.close());
  document.getElementById("layer-image-viewer-prev").addEventListener("click", () => showImage(currentIndex - 1));
  document.getElementById("layer-image-viewer-next").addEventListener("click", () => showImage(currentIndex + 1));
  viewer.addEventListener("click", event => {
    if (event.target === viewer) viewer.close();
  });
  viewer.addEventListener("close", () => {
    image.removeAttribute("src");
    currentIndex = -1;
    lastTrigger?.focus();
  });

  const imageAsPng = async url => {
    const response = await fetch(url, { credentials: "same-origin", cache: "no-store" });
    if (!response.ok) throw new Error(`画像の取得に失敗しました（HTTP ${response.status}）。`);
    if (!response.headers.get("content-type")?.toLowerCase().startsWith("image/")) {
      throw new Error("画像の取得に失敗しました。ログイン状態を確認してください。");
    }
    const bitmap = await createImageBitmap(await response.blob());
    try {
      const canvas = document.createElement("canvas");
      canvas.width = bitmap.width;
      canvas.height = bitmap.height;
      const context = canvas.getContext("2d");
      if (!context) throw new Error("画像をコピー用に変換できません。");
      context.drawImage(bitmap, 0, 0);
      return await new Promise((resolve, reject) => {
        canvas.toBlob(blob => blob ? resolve(blob) : reject(new Error("PNGへの変換に失敗しました。")), "image/png");
      });
    } finally {
      bitmap.close();
    }
  };

  const copyCurrentImage = async () => {
    if (currentIndex < 0 || copying || copyButton.disabled) return;
    if (!navigator.clipboard?.write || typeof ClipboardItem === "undefined" || typeof createImageBitmap !== "function") {
      message("このブラウザーは画像のクリップボードコピーに対応していません。", true);
      return;
    }
    const index = currentIndex;
    const url = links[index].href;
    copying = true;
    copyButton.disabled = true;
    message("画像をコピーしています…");
    try {
      // Construct ClipboardItem while the click/keydown still has user activation.
      const png = imageAsPng(url);
      png.catch(() => {});
      await navigator.clipboard.write([new ClipboardItem({ "image/png": png })]);
      if (viewer.open && currentIndex === index) message("画像をクリップボードにコピーしました。");
    } catch (error) {
      const reason = error?.name === "NotAllowedError"
        ? "クリップボードへの書き込みが拒否されました。ブラウザーの許可を確認してください。"
        : (error?.message || "画像をコピーできませんでした。");
      if (viewer.open && currentIndex === index) message(reason, true);
    } finally {
      copying = false;
      copyButton.disabled = image.hidden || !viewer.open;
    }
  };

  copyButton.addEventListener("click", copyCurrentImage);
  document.addEventListener("keydown", event => {
    if (!viewer.open) return;
    if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "c") {
      if (event.target?.closest?.("input, textarea, [contenteditable]")) return;
      event.preventDefault();
      copyCurrentImage();
    } else if (event.key === "ArrowLeft" && links.length > 1) {
      event.preventDefault();
      showImage(currentIndex - 1);
    } else if (event.key === "ArrowRight" && links.length > 1) {
      event.preventDefault();
      showImage(currentIndex + 1);
    }
  });
})();
