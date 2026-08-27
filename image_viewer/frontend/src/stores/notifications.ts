import { defineStore } from 'pinia';
import { ref } from 'vue';

export const useNotificationStore = defineStore('image-viewer-notifications', () => {
  const message = ref('');
  const error = ref(false);
  let timer = 0;

  function show(text: string, isError = false, duration = 3200) {
    window.clearTimeout(timer);
    message.value = text;
    error.value = isError;
    if (!isError) timer = window.setTimeout(clear, duration);
  }

  function clear() {
    window.clearTimeout(timer);
    message.value = '';
    error.value = false;
  }

  return { message, error, show, clear };
});
