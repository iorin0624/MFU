/// <reference types="vite/client" />

interface Window {
  MFUAdminPasskey?: {
    authorize(action: string): Promise<string>;
  };
}
