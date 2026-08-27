import { defineStore } from 'pinia';
import { ref } from 'vue';
import type { DownloadImageItem, DownloadVideoItem } from '@/types';

export const useDownloaderStore = defineStore('image-viewer-downloader', () => {
  const imageUrl = ref('');
  const imageIdentifier = ref('');
  const imageJobId = ref('');
  const images = ref<DownloadImageItem[]>([]);
  const videoUrl = ref('');
  const videoIdentifier = ref('');
  const videoJobId = ref('');
  const videos = ref<DownloadVideoItem[]>([]);

  function setImages(values: {
    url?: string; identifier?: string; jobId?: string; images?: DownloadImageItem[];
  }) {
    if (values.url !== undefined) imageUrl.value = values.url;
    imageIdentifier.value = values.identifier || '';
    imageJobId.value = values.jobId || '';
    images.value = values.images || [];
  }

  function setVideos(values: {
    url?: string; identifier?: string; jobId?: string; videos?: DownloadVideoItem[];
  }) {
    if (values.url !== undefined) videoUrl.value = values.url;
    videoIdentifier.value = values.identifier || '';
    videoJobId.value = values.jobId || '';
    videos.value = values.videos || [];
  }

  return {
    imageUrl, imageIdentifier, imageJobId, images,
    videoUrl, videoIdentifier, videoJobId, videos,
    setImages, setVideos,
  };
});
