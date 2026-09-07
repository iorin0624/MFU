import { fileURLToPath, URL } from 'node:url';
import { resolve } from 'node:path';
import { defineConfig } from 'vite';
import vue from '@vitejs/plugin-vue';

export default defineConfig({
  base: '/static/external_login_vue/',
  plugins: [vue()],
  resolve: {
    alias: { '@': fileURLToPath(new URL('./src', import.meta.url)) },
  },
  build: {
    // process.cwd() avoids a duplicated UNC share segment when this repository
    // is opened through the Windows Y: mapped drive.
    outDir: resolve(process.cwd(), '../../static/external_login_vue'),
    emptyOutDir: true,
    cssCodeSplit: true,
    rollupOptions: {
      input: {
        'event-portal': fileURLToPath(new URL('./src/main.ts', import.meta.url)),
        'public-upload-viewer': fileURLToPath(new URL('./src/public-upload-main.ts', import.meta.url)),
      },
      output: {
        entryFileNames: '[name].js',
        chunkFileNames: 'chunks/[name]-[hash].js',
        assetFileNames: (assetInfo) =>
          assetInfo.names?.some((name) => name.includes('public-upload'))
            ? 'public-upload-viewer.css'
            : assetInfo.names?.some((name) => name.endsWith('.css'))
            ? 'event-portal.css'
            : 'assets/[name]-[hash][extname]',
      },
    },
  },
});
