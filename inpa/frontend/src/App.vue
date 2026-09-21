<script setup lang="ts">
import { RouterView } from 'vue-router'
</script>

<template>
  <main class="app-shell">
    <header class="site-header">
      <RouterLink to="/" class="brand">INPA</RouterLink>
      <nav aria-label="主なメニュー">
        <RouterLink to="/login">ログイン</RouterLink>
        <RouterLink to="/profile">プロフィール</RouterLink>
        <RouterLink to="/visits">予定</RouterLink>
        <RouterLink to="/share">共有</RouterLink>
        <RouterLink to="/connections">つながり</RouterLink>
        <RouterLink to="/register">新規登録</RouterLink>
      </nav>
    </header>
    <RouterView />
  </main>
</template>
