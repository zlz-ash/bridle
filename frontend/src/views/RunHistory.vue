<template>
  <div>
    <el-page-header @back="router.push('/plan')" title="Back to Plan">
      <template #content>Run History</template>
    </el-page-header>

    <div v-if="loading" style="margin-top: 24px">
      <el-skeleton :rows="5" animated />
    </div>

    <div v-else style="margin-top: 16px">
      <el-table :data="runs" stripe style="width: 100%" class="run-history-table">
        <el-table-column prop="id" label="Run ID" width="100">
          <template #default="{ row }">
            <el-text size="small" type="info">{{ row.id.slice(0, 8) }}</el-text>
          </template>
        </el-table-column>
        <el-table-column prop="status" label="Status" width="120">
          <template #default="{ row }">
            <el-tag :type="row.status === 'completed' ? 'success' : row.status === 'failed' ? 'danger' : 'info'" size="small">
              {{ row.status }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="exit_code" label="Exit Code" width="100">
          <template #default="{ row }">
            <el-text v-if="row.exit_code !== null" :type="row.exit_code === 0 ? 'success' : 'danger'">
              {{ row.exit_code }}
            </el-text>
            <el-text v-else type="info">—</el-text>
          </template>
        </el-table-column>
        <el-table-column prop="duration_ms" label="Duration" width="120">
          <template #default="{ row }">
            {{ row.duration_ms !== null ? `${row.duration_ms}ms` : '—' }}
          </template>
        </el-table-column>
        <el-table-column label="Tests / Metrics" min-width="160">
          <template #default="{ row }">
            <div v-if="row.test_summary || row.metrics_summary" class="container-diagnostics">
              <el-text v-if="row.test_summary" size="small">{{ row.test_summary }}</el-text>
              <el-text v-if="row.metrics_summary" size="small" type="info">{{ row.metrics_summary }}</el-text>
            </div>
            <el-text v-else type="info">—</el-text>
          </template>
        </el-table-column>
        <el-table-column label="Container" min-width="260">
          <template #default="{ row }">
            <div v-if="row.container_id || row.container_health || row.container_error || row.container_logs_summary || row.diagnostic_path" class="container-diagnostics">
              <el-text v-if="row.container_id" size="small" type="info">{{ shortId(row.container_id) }}</el-text>
              <el-tag v-if="row.container_health" :type="row.container_health === 'healthy' ? 'success' : 'warning'" size="small">
                {{ row.container_health }}
              </el-tag>
              <el-tooltip
                v-if="row.container_logs_summary"
                :content="row.container_logs_summary"
                placement="top"
                :show-after="200"
              >
                <el-text size="small" class="logs-summary">{{ truncate(row.container_logs_summary, 80) }}</el-text>
              </el-tooltip>
              <el-tooltip
                v-if="row.container_error || row.error_code"
                :content="row.container_error || row.error_code || ''"
                placement="top"
                :show-after="200"
              >
                <el-text size="small" type="danger" class="error-text">{{ truncate(row.container_error || row.error_code || '', 80) }}</el-text>
              </el-tooltip>
              <el-tooltip
                v-if="row.diagnostic_path"
                :content="row.diagnostic_path"
                placement="top"
                :show-after="200"
              >
                <el-text size="small" type="info" class="logs-summary">{{ truncate(row.diagnostic_path, 60) }}</el-text>
              </el-tooltip>
            </div>
            <el-text v-else type="info">—</el-text>
          </template>
        </el-table-column>
        <el-table-column prop="started_at" label="Started" width="180">
          <template #default="{ row }">
            {{ formatTime(row.started_at) }}
          </template>
        </el-table-column>
        <el-table-column prop="finished_at" label="Finished" width="180">
          <template #default="{ row }">
            {{ row.finished_at ? formatTime(row.finished_at) : '—' }}
          </template>
        </el-table-column>
      </el-table>

      <el-empty v-if="runs.length === 0 && !loading" description="No runs yet for this node" />
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { nodeApi, type Run } from '../api'

const route = useRoute()
const router = useRouter()
const runs = ref<Run[]>([])
const loading = ref(false)

onMounted(async () => {
  const nodeId = route.params.nodeId as string
  loading.value = true
  try {
    runs.value = await nodeApi.runs(nodeId)
  } catch (e: any) {
    ElMessage.error('Failed to load runs: ' + (e.message || e))
  } finally {
    loading.value = false
  }
})

function formatTime(iso: string): string {
  if (!iso) return ''
  return new Date(iso).toLocaleString()
}

function shortId(id: string): string {
  return id.length > 12 ? id.slice(0, 12) : id
}

function truncate(text: string, max: number): string {
  if (!text) return ''
  return text.length > max ? text.slice(0, max) + '…' : text
}
</script>

<style scoped>
.container-diagnostics {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  align-items: center;
  max-width: 100%;
}

:deep(.run-history-table .cell) {
  word-break: break-word;
  overflow-wrap: anywhere;
}

.logs-summary,
.error-text {
  overflow-wrap: anywhere;
  word-break: break-word;
  max-width: 100%;
}
</style>
