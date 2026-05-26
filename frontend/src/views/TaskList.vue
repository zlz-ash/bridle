<template>
  <div>
    <el-page-header title="Tasks" />

    <el-row :gutter="16" style="margin-top: 16px; margin-bottom: 16px">
      <el-col :span="8">
        <el-input v-model="newTitle" placeholder="Task title" clearable />
      </el-col>
      <el-col :span="8">
        <el-input v-model="newGoal" placeholder="Goal (optional)" clearable />
      </el-col>
      <el-col :span="4">
        <el-button type="primary" @click="createTask" :loading="creating">Create</el-button>
      </el-col>
    </el-row>

    <el-table :data="tasks" stripe v-loading="loading" style="width: 100%">
      <el-table-column prop="id" label="ID" width="100">
        <template #default="{ row }">
          <el-text size="small" type="info">{{ row.id.slice(0, 8) }}</el-text>
        </template>
      </el-table-column>
      <el-table-column prop="title" label="Title" min-width="200" />
      <el-table-column prop="status" label="Status" width="120">
        <template #default="{ row }">
          <el-tag :type="statusTag(row.status)" size="small">{{ row.status }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column prop="created_at" label="Created" width="180">
        <template #default="{ row }">
          {{ formatTime(row.created_at) }}
        </template>
      </el-table-column>
      <el-table-column label="Actions" width="200">
        <template #default="{ row }">
          <el-button size="small" @click="viewPlan(row.id)">Plan</el-button>
          <el-button size="small" @click="viewGraph(row.id)">Graph</el-button>
        </template>
      </el-table-column>
    </el-table>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { taskApi, type Task } from '../api'

const router = useRouter()
const tasks = ref<Task[]>([])
const loading = ref(false)
const creating = ref(false)
const newTitle = ref('')
const newGoal = ref('')

onMounted(async () => {
  await loadTasks()
})

async function loadTasks() {
  loading.value = true
  try {
    tasks.value = await taskApi.list()
  } catch (e: any) {
    ElMessage.error('Failed to load tasks: ' + (e.message || e))
  } finally {
    loading.value = false
  }
}

async function createTask() {
  if (!newTitle.value.trim()) {
    ElMessage.warning('Title is required')
    return
  }
  creating.value = true
  try {
    await taskApi.create({ title: newTitle.value.trim(), goal: newGoal.value.trim() || undefined })
    newTitle.value = ''
    newGoal.value = ''
    ElMessage.success('Task created')
    await loadTasks()
  } catch (e: any) {
    ElMessage.error('Failed to create task: ' + (e.message || e))
  } finally {
    creating.value = false
  }
}

function viewPlan(_taskId: string) {
  router.push('/plan')
}

function viewGraph(_taskId: string) {
  router.push({ path: '/plan', query: { taskId: _taskId } })
}

function statusTag(status: string): '' | 'success' | 'warning' | 'danger' | 'info' {
  const map: Record<string, '' | 'success' | 'warning' | 'danger' | 'info'> = {
    created: 'info',
    planned: 'warning',
    running: '',
    completed: 'success',
    failed: 'danger',
  }
  return map[status] || 'info'
}

function formatTime(iso: string): string {
  if (!iso) return ''
  return new Date(iso).toLocaleString()
}
</script>
