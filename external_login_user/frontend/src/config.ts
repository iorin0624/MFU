import type { RuntimeConfig } from '@/types';

const element = document.getElementById('event-portal-config');
if (!element?.textContent) throw new Error('event portal config is missing');

export const runtimeConfig = JSON.parse(element.textContent) as RuntimeConfig;
