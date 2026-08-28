<script setup lang="ts">
import { ref } from 'vue';

const props = defineProps<{ targetUrl: string }>();
const copied = ref(false);

async function copyUrl() {
  copied.value = false;
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(props.targetUrl);
    } else {
      const input = document.createElement('textarea');
      input.value = props.targetUrl;
      input.style.position = 'fixed';
      input.style.left = '-9999px';
      document.body.appendChild(input);
      input.select();
      document.execCommand('copy');
      input.remove();
    }
    copied.value = true;
    window.setTimeout(() => { copied.value = false; }, 2500);
  } catch {
    window.prompt('下のURLをコピーしてください。', props.targetUrl);
  }
}
</script>

<template>
  <section class="inapp-album-notice" role="alert">
    <div class="inapp-album-icon">🌐</div>
    <h2>LINEの外部ブラウザーで開いてください</h2>
    <p>LINE内ブラウザーでは、アルバムの読み込みやダウンロードが正常に動作しない場合があります。</p>
    <ol>
      <li>LINE画面の右上にある <strong>「…」</strong> をタップ</li>
      <li><strong>「デフォルトのブラウザーで開く」</strong> または <strong>「ブラウザーで開く」</strong> を選択</li>
      <li>Safari / Chromeでアルバムを開く</li>
    </ol>
    <div class="inapp-album-url">{{ targetUrl }}</div>
    <button type="button" class="button primary" @click="copyUrl">
      {{ copied ? 'コピーしました' : 'アルバムURLをコピー' }}
    </button>
    <p class="inapp-album-help">外部ブラウザーでログインが求められた場合は、同じLINEアカウントでログインしてください。</p>
  </section>
</template>
