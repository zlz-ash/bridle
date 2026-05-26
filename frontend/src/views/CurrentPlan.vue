<template>
  <div>
    <el-page-header title="Current Plan" />

    <div v-if="loading" style="margin-top: 24px">
      <el-skeleton :rows="5" animated />
    </div>

    <div v-else-if="!plan" style="margin-top: 24px">
      <el-empty description="No active plan. Import a plan via CLI or API." />
    </div>

    <div v-else style="margin-top: 16px">
      <el-descriptions :column="2" border>
        <el-descriptions-item label="Plan ID">{{ plan.id.slice(0, 8) }}</el-descriptions-item>
        <el-descriptions-item label="Status">
          <el-tag type="success" size="small">{{ plan.status }}</el-tag>
        </el-descriptions-item>
        <el-descriptions-item label="Goal" :span="2">{{ plan.goal }}</el-descriptions-item>
        <el-descriptions-item label="Task ID">{{ plan.task_id.slice(0, 8) }}</el-descriptions-item>
        <el-descriptions-item label="Created">{{ formatTime(plan.created_at) }}</el-descriptions-item>
      </el-descriptions>

      <div style="margin-top: 16px">
        <el-button type="primary" size="small" @click="showPatchDialog = true">Patch (Partial Update)</el-button>
        <el-button type="warning" size="small" @click="showReplaceDialog = true">Replace (Full)</el-button>
        <el-button size="small" @click="loadSummary">View Summary</el-button>
      </div>

      <div v-if="plan.aggregate_files?.length" style="margin-top: 16px">
        <h3>Aggregate Files</h3>
        <el-table :data="plan.aggregate_files" size="small" stripe style="width: 100%">
          <el-table-column prop="target_path" label="Target" min-width="180" />
          <el-table-column prop="merge_strategy" label="Merge" width="120" />
          <el-table-column prop="owner" label="Owner" width="140" />
          <el-table-column label="Contributors" min-width="200">
            <template #default="{ row }">
              <el-tag v-for="nodeId in row.contributors" :key="nodeId" size="small" style="margin: 2px">
                {{ nodeId }}
              </el-tag>
            </template>
          </el-table-column>
        </el-table>
      </div>

      <h3 style="margin-top: 24px">Nodes</h3>
      <el-table :data="plan.nodes" stripe style="width: 100%">
        <el-table-column prop="order" label="#" width="50" />
        <el-table-column prop="title" label="Title" min-width="200">
          <template #default="{ row }">
            <el-link type="primary" @click="router.push(`/nodes/${row.id}`)">{{ row.title }}</el-link>
          </template>
        </el-table-column>
        <el-table-column prop="plan_node_id" label="Node ID" width="120">
          <template #default="{ row }">
            <el-text size="small" type="info">{{ row.plan_node_id }}</el-text>
          </template>
        </el-table-column>
        <el-table-column prop="node_type" label="Type" width="160" />
        <el-table-column prop="status" label="Status" width="140">
          <template #default="{ row }">
            <el-tag :type="nodeStatusTag(row.status)" size="small">{{ row.status }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="Dependencies" width="140">
          <template #default="{ row }">
            <span v-if="row.depends_on.length === 0">—</span>
            <el-tag v-for="dep in row.depends_on" :key="dep" size="small" style="margin-right: 4px">
              {{ dep }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="Container Boundary" min-width="220" class-name="boundary-column">
          <template #default="{ row }">
            <div class="boundary-tags">
              <el-tag type="success" size="small">write {{ row.write_set?.length || row.files?.length || 0 }}</el-tag>
              <el-tag type="info" size="small">read {{ row.read_set?.length || 0 }}</el-tag>
              <el-tag type="warning" size="small">aggregate {{ row.conflict_contributions?.length || 0 }}</el-tag>
            </div>
          </template>
        </el-table-column>
        <el-table-column label="Interfaces" width="200">
          <template #default="{ row }">
            <div v-if="row.interfaces?.exposes?.length || row.interfaces?.consumes?.length">
              <el-tag v-for="exp in row.interfaces.exposes" :key="'exp-'+exp.name"
                type="success" size="small" style="margin: 2px"
              >↑ {{ exp.name }}</el-tag>
              <el-tag v-for="con in row.interfaces.consumes" :key="'con-'+con.node_id+'-'+con.interface_name"
                size="small" style="margin: 2px"
              >↓ {{ con.interface_name }}@{{ con.node_id }}</el-tag>
            </div>
            <el-text v-else type="info" size="small">—</el-text>
          </template>
        </el-table-column>
        <el-table-column label="Actions" width="200">
          <template #default="{ row }">
            <el-button size="small" @click="router.push(`/runs/${row.id}`)">Runs</el-button>
            <el-button size="small" @click="router.push(`/reports/${row.id}`)">Report</el-button>
          </template>
        </el-table-column>
      </el-table>

      <!-- Patch Dialog -->
      <el-dialog v-model="showPatchDialog" title="Patch Current Plan" width="600px">
        <el-form label-width="120px">
          <el-form-item label="Remove Node IDs">
            <el-input v-model="patchForm.remove_node_ids" placeholder="n1,n2 (comma separated)" />
          </el-form-item>
        </el-form>
        <template #footer>
          <el-button @click="showPatchDialog = false">Cancel</el-button>
          <el-button type="primary" @click="doPatch" :loading="patching">Apply Patch</el-button>
        </template>
      </el-dialog>

      <!-- Replace Dialog -->
      <el-dialog v-model="showReplaceDialog" title="Replace Current Plan" width="600px">
        <el-form label-width="80px">
          <el-form-item label="New Goal">
            <el-input v-model="replaceForm.goal" placeholder="New plan goal" />
          </el-form-item>
        </el-form>
        <template #footer>
          <el-button @click="showReplaceDialog = false">Cancel</el-button>
          <el-button type="warning" @click="doReplace" :loading="replacing">Replace</el-button>
        </template>
      </el-dialog>

      <!-- Summary Dialog -->
      <el-dialog v-model="showSummaryDialog" title="Plan Replacement Summary" width="600px">
        <div v-if="summaryData">
          <el-descriptions :column="2" border size="small">
            <el-descriptions-item label="Goal">{{ summaryData.goal }}</el-descriptions-item>
            <el-descriptions-item label="Replaced At">{{ formatTime(summaryData.replaced_at) }}</el-descriptions-item>
            <el-descriptions-item label="Nodes">{{ summaryData.node_count }}</el-descriptions-item>
            <el-descriptions-item label="Completed">
              <el-text type="success">{{ summaryData.completed_count }}</el-text>
            </el-descriptions-item>
            <el-descriptions-item label="Failed">
              <el-text type="danger">{{ summaryData.failed_count }}</el-text>
            </el-descriptions-item>
            <el-descriptions-item label="Status">{{ summaryData.final_status }}</el-descriptions-item>
          </el-descriptions>
        </div>
        <el-empty v-else description="No summary available" />
      </el-dialog>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { planApi, type PlanCurrent, type PlanSummary } from '../api'

const router = useRouter()
const plan = ref<PlanCurrent | null>(null)
const loading = ref(false)
const showPatchDialog = ref(false)
const showReplaceDialog = ref(false)
const showSummaryDialog = ref(false)
const patching = ref(false)
const replacing = ref(false)
const summaryData = ref<PlanSummary | null>(null)

const patchForm = ref({ remove_node_ids: '' })
const replaceForm = ref({ goal: '' })

onMounted(async () => {
  await loadPlan()
})

async function loadPlan() {
  loading.value = true
  try {
    plan.value = await planApi.current()
  } catch (e: any) {
    if (e.response?.status !== 404) {
      ElMessage.error('Failed to load plan: ' + (e.message || e))
    }
  } finally {
    loading.value = false
  }
}

async function doPatch() {
  patching.value = true
  try {
    const removeIds = patchForm.value.remove_node_ids
      .split(',')
      .map(s => s.trim())
      .filter(s => s.length > 0)
    const data: Record<string, unknown> = {}
    if (removeIds.length > 0) data.remove_node_ids = removeIds
    await planApi.patchPlan(data)
    ElMessage.success('Plan patched')
    showPatchDialog.value = false
    patchForm.value.remove_node_ids = ''
    await loadPlan()
  } catch (e: any) {
    ElMessage.error('Patch failed: ' + (e.response?.data?.detail || e.message || e))
  } finally {
    patching.value = false
  }
}

async function doReplace() {
  if (!replaceForm.value.goal.trim()) {
    ElMessage.warning('Goal is required')
    return
  }
  replacing.value = true
  try {
    await planApi.replacePlan({
      goal: replaceForm.value.goal.trim(),
      aggregate_files: plan.value?.aggregate_files || [],
      nodes: plan.value?.nodes?.map(n => ({
        id: n.plan_node_id,
        title: n.title,
        goal: n.goal,
        node_type: n.node_type,
        depends_on: n.depends_on,
        files: n.files,
        tests: n.tests,
        metrics: n.metrics,
        constraints: n.constraints,
        review_checks: n.review_checks,
        expected_outputs: n.expected_outputs,
        interfaces: n.interfaces,
        read_set: n.read_set,
        write_set: n.write_set,
        readonly_context: n.readonly_context,
        conflict_contributions: n.conflict_contributions,
        container_policy: n.container_policy,
      })) || [],
    })
    ElMessage.success('Plan replaced')
    showReplaceDialog.value = false
    replaceForm.value.goal = ''
    await loadPlan()
  } catch (e: any) {
    ElMessage.error('Replace failed: ' + (e.response?.data?.detail || e.message || e))
  } finally {
    replacing.value = false
  }
}

async function loadSummary() {
  try {
    summaryData.value = await planApi.summary()
    showSummaryDialog.value = true
  } catch (e: any) {
    if (e.response?.status === 404) {
      ElMessage.info('No replacement summary available yet')
    } else {
      ElMessage.error('Failed to load summary: ' + (e.message || e))
    }
  }
}

function nodeStatusTag(status: string): '' | 'success' | 'warning' | 'danger' | 'info' {
  const map: Record<string, '' | 'success' | 'warning' | 'danger' | 'info'> = {
    pending: 'info', blocked: 'warning', ready: '', running: '',
    completed: 'success', failed: 'danger', failed_retryable: 'danger',
    missing_evidence: 'warning', needs_review: 'warning', needs_review_retryable: 'warning',
    archived: 'info',
  }
  return map[status] || 'info'
}

function formatTime(iso: string): string {
  if (!iso) return ''
  return new Date(iso).toLocaleString()
}
</script>

<style scoped>
.boundary-tags {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
  max-width: 100%;
  word-break: break-word;
  overflow-wrap: anywhere;
}

:deep(.boundary-column .cell) {
  overflow: hidden;
  white-space: normal;
}

:deep(.boundary-column .el-tag) {
  max-width: 100%;
  overflow: hidden;
  text-overflow: ellipsis;
}

@media (max-width: 768px) {
  .boundary-tags {
    flex-direction: column;
    align-items: flex-start;
  }

  :deep(.boundary-column) {
    min-width: 0 !important;
  }
}
</style>
