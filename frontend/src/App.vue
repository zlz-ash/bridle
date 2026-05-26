<template>
  <el-container style="min-height: 100vh">
    <el-aside width="200px">
      <el-menu :default-active="route.path" router>
        <el-menu-item index="/tasks">
          <el-icon><List /></el-icon>
          <span>Tasks</span>
        </el-menu-item>
        <el-menu-item index="/plan">
          <el-icon><Share /></el-icon>
          <span>Current Plan</span>
        </el-menu-item>
      </el-menu>
    </el-aside>
    <el-main>
      <router-view />
    </el-main>
  </el-container>
</template>

<script setup lang="ts">
import { useRoute } from 'vue-router'
import { List, Share } from '@element-plus/icons-vue'

const route = useRoute()
</script>

<style>
body {
  margin: 0;
  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', Arial, sans-serif;
}
.el-aside {
  background-color: #f5f7fa;
  border-right: 1px solid #e4e7ed;
}
.el-menu {
  border-right: none;
}
</style>
