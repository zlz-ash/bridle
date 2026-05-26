<template>
  <div>
    <el-page-header @back="router.push('/plan')" title="Back to Plan">
      <template #content>Node Report</template>
    </el-page-header>

    <div v-if="loading" style="margin-top: 24px">
      <el-skeleton :rows="5" animated />
    </div>

    <div v-else-if="!report">
      <el-empty description="Report not found" />
    </div>

    <div v-else style="margin-top: 16px">
      <!-- Summary -->
      <el-descriptions title="Summary" :column="3" border>
        <el-descriptions-item label="Total Runs">{{ report.summary.total_runs }}</el-descriptions-item>
        <el-descriptions-item label="Completed">
          <el-text type="success">{{ report.summary.completed_runs }}</el-text>
        </el-descriptions-item>
        <el-descriptions-item label="Failed">
          <el-text type="danger">{{ report.summary.failed_runs }}</el-text>
        </el-descriptions-item>
        <el-descriptions-item label="Evidence Count">{{ report.summary.evidence_count }}</el-descriptions-item>
        <el-descriptions-item label="Missing Evidence">
          <el-text :type="report.summary.missing_evidence_count > 0 ? 'warning' : 'success'">
            {{ report.summary.missing_evidence_count }}
          </el-text>
        </el-descriptions-item>
      </el-descriptions>

      <!-- Node info -->
      <h3 style="margin-top: 24px">Node</h3>
      <el-descriptions :column="2" border>
        <el-descriptions-item label="Title">{{ report.node.title }}</el-descriptions-item>
        <el-descriptions-item label="Status">
          <el-tag size="small">{{ report.node.status }}</el-tag>
        </el-descriptions-item>
        <el-descriptions-item label="Goal" :span="2">{{ report.node.goal }}</el-descriptions-item>
      </el-descriptions>

      <!-- Runs -->
      <h3 style="margin-top: 24px">Recent Runs</h3>
      <el-table :data="report.runs" stripe size="small">
        <el-table-column prop="id" label="ID" width="100">
          <template #default="{ row }">{{ row.id.slice(0, 8) }}</template>
        </el-table-column>
        <el-table-column prop="status" label="Status" width="100" />
        <el-table-column prop="exit_code" label="Exit" width="80" />
        <el-table-column prop="duration_ms" label="Duration" width="100">
          <template #default="{ row }">{{ row.duration_ms ? `${row.duration_ms}ms` : '—' }}</template>
        </el-table-column>
        <el-table-column prop="started_at" label="Started">
          <template #default="{ row }">{{ formatTime(row.started_at) }}</template>
        </el-table-column>
      </el-table>

      <!-- Baseline -->
      <h3 style="margin-top: 24px">Baseline Run</h3>
      <el-text v-if="!report.baseline_run" type="info">No successful baseline run</el-text>
      <el-descriptions v-else :column="2" border size="small">
        <el-descriptions-item label="Run ID">{{ report.baseline_run.id.slice(0, 8) }}</el-descriptions-item>
        <el-descriptions-item label="Duration">{{ report.baseline_run.duration_ms }}ms</el-descriptions-item>
      </el-descriptions>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { nodeApi, type NodeReport } from '../api'

const route = useRoute()
const router = useRouter()
const report = ref<NodeReport | null>(null)
const loading = ref(false)

onMounted(async () => {
  const nodeId = route.params.nodeId as string
  loading.value = true
  try {
    report.value = await nodeApi.report(nodeId)
  } catch (e: any) {
    if (e.response?.status !== 404) {
      ElMessage.error('Failed to load report: ' + (e.message || e))
    }
  } finally {
    loading.value = false
  }
})

function formatTime(iso: string): string {
  if (!iso) return ''
  return new Date(iso).toLocaleString()
}
</script>
