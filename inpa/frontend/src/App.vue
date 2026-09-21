<script setup lang="ts">
import { RouterView } from 'vue-router'
</script>

<template>
  <main class="app-shell">
    <header class="site-header">
      <RouterLink to="/" class="brand">INPA</RouterLink>
      <nav aria-label="主なメニュー">
        <RouterLink to="/login">ログイン</RouterLink>
        <RouterLink to="/register">新規登録</RouterLink>
      </nav>
    </header>
    <RouterView />
  </main>
</template>
