import { defineConfig } from 'vite';

export default defineConfig({
  build: { target: 'es2022', sourcemap: false, chunkSizeWarningLimit: 800 },
  worker: { format: 'es' },
});
