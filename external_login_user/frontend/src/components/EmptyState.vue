<script setup lang="ts">
withDefaults(defineProps<{ icon?: string; title: string; text?: string }>(), { icon: '📭', text: '' });
</script>

<template>
  <section class="empty-state">
    <span class="empty-icon" aria-hidden="true">{{ icon }}</span>
    <h2>{{ title }}</h2>
    <p v-if="text">{{ text }}</p>
    <slot></slot>
  </section>
</template>
