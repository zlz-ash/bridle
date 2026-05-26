<template>
  <div>
    <el-page-header @back="router.push('/plan')" title="Back to Plan">
      <template #content>Node Detail</template>
    </el-page-header>

    <div v-if="loading" style="margin-top: 24px">
      <el-skeleton :rows="5" animated />
    </div>

    <div v-else-if="!node">
      <el-empty description="Node not found (not in current plan or does not exist)" />
    </div>

    <div v-else style="margin-top: 16px">
      <el-descriptions :column="2" border>
        <el-descriptions-item label="ID">{{ node.id }}</el-descriptions-item>
        <el-descriptions-item label="Status">
          <el-tag :type="statusTag(node.status)" size="small">{{ node.status }}</el-tag>
        </el-descriptions-item>
        <el-descriptions-item label="Title" :span="2">{{ node.title }}</el-descriptions-item>
        <el-descriptions-item label="Goal" :span="2">{{ node.goal }}</el-descriptions-item>
        <el-descriptions-item label="Type">{{ node.node_type }}</el-descriptions-item>
        <el-descriptions-item label="Order">{{ node.order }}</el-descriptions-item>
        <el-descriptions-item label="Dependencies">
          <span v-if="node.depends_on.length === 0">None</span>
          <el-tag v-for="dep in node.depends_on" :key="dep" size="small" style="margin-right: 4px">
            {{ dep }}
          </el-tag>
        </el-descriptions-item>
        <el-descriptions-item label="Plan ID">{{ node.plan_id.slice(0, 8) }}</el-descriptions-item>
      </el-descriptions>

      <el-divider />

      <h3>Files</h3>
      <el-tag v-for="f in node.files" :key="f" style="margin: 4px">{{ f }}</el-tag>
      <el-text v-if="node.files.length === 0" type="info">No files</el-text>

      <h3 style="margin-top: 16px">Tests</h3>
      <el-tag v-for="t in node.tests" :key="t" type="warning" style="margin: 4px">{{ t }}</el-tag>
      <el-text v-if="node.tests.length === 0" type="info">No tests</el-text>

      <h3 style="margin-top: 16px">Metrics</h3>
      <pre style="background: #f5f7fa; padding: 12px; border-radius: 4px">{{ JSON.stringify(node.metrics, null, 2) }}</pre>

      <h3 style="margin-top: 16px">Constraints</h3>
      <pre style="background: #f5f7fa; padding: 12px; border-radius: 4px">{{ JSON.stringify(node.constraints, null, 2) }}</pre>

      <h3 style="margin-top: 16px">Review Checks</h3>
      <el-tag v-for="rc in node.review_checks" :key="rc" type="success" style="margin: 4px">{{ rc }}</el-tag>
      <el-text v-if="node.review_checks.length === 0" type="info">No review checks</el-text>

      <h3 style="margin-top: 16px">Interfaces — Exposes</h3>
      <div v-if="node.interfaces?.exposes?.length">
        <div v-for="exp in node.interfaces.exposes" :key="exp.name"
          style="background: #f0f9eb; padding: 12px; border-radius: 4px; margin: 8px 0">
          <strong>{{ exp.name }}</strong>
          <div v-if="exp.fields?.length" style="margin-top: 6px">
            <el-text size="small" type="info">Fields:</el-text>
            <el-tag v-for="f in exp.fields" :key="f.name" size="small" style="margin: 2px">
              {{ f.name }}: {{ f.type }}{{ f.required ? ' *' : '' }}
            </el-tag>
          </div>
          <div v-if="exp.endpoints?.length" style="margin-top: 6px">
            <el-text size="small" type="info">Endpoints:</el-text>
            <el-tag v-for="ep in exp.endpoints" :key="ep.name" size="small" style="margin: 2px">
              {{ ep.method }} {{ ep.path }}
            </el-tag>
          </div>
        </div>
      </div>
      <el-text v-else type="info">No interfaces exposed</el-text>

      <h3 style="margin-top: 16px">Interfaces — Consumes</h3>
      <div v-if="node.interfaces?.consumes?.length">
        <div v-for="con in node.interfaces.consumes" :key="con.node_id + con.interface_name"
          style="background: #fdf6ec; padding: 12px; border-radius: 4px; margin: 8px 0">
          <strong>{{ con.interface_name }}</strong> <el-text size="small" type="info">from {{ con.node_id }}</el-text>
          <div v-if="con.fields?.length" style="margin-top: 6px">
            <el-text size="small" type="info">Fields:</el-text>
            <el-tag v-for="f in con.fields" :key="f" size="small" style="margin: 2px">{{ f }}</el-tag>
          </div>
          <div v-if="con.endpoints?.length" style="margin-top: 6px">
            <el-text size="small" type="info">Endpoints:</el-text>
            <el-tag v-for="ep in con.endpoints" :key="ep" size="small" style="margin: 2px">{{ ep }}</el-tag>
          </div>
        </div>
      </div>
      <el-text v-else type="info">No interfaces consumed</el-text>

      <div style="margin-top: 24px">
        <el-button type="primary" @click="router.push(`/runs/${node.id}`)">View Runs</el-button>
        <el-button @click="router.push(`/reports/${node.id}`)">View Report</el-button>
      </div>

      <el-divider />

      <h3>Agent Proposals</h3>
      <div style="margin-bottom: 12px; display: flex; gap: 8px; align-items: flex-start">
        <el-input
          v-model="instruction"
          placeholder="What should the agent do for this node?"
          style="flex: 1"
          :disabled="generating"
          @keyup.enter="generateProposal"
        />
        <el-button type="primary" :loading="generating" @click="generateProposal">
          Generate Proposal
        </el-button>
      </div>
      <el-text size="small" type="info">
        Dry-run only: proposals are not applied and do not modify files.
      </el-text>

      <div v-if="proposals.length > 0" style="margin-top: 16px">
        <div v-for="p in proposals" :key="p.id"
          style="background: #f5f7fa; padding: 16px; border-radius: 8px; margin-bottom: 12px">

          <div style="display: flex; justify-content: space-between; margin-bottom: 8px">
            <el-tag size="small" type="info">{{ p.status }}</el-tag>
            <el-text size="small" type="info">{{ new Date(p.created_at).toLocaleString() }}</el-text>
          </div>

          <el-text size="small" type="info">Instruction:</el-text>
          <p style="margin: 4px 0 8px 0">{{ p.instruction }}</p>

          <el-text size="small" type="info">Summary:</el-text>
          <p style="margin: 4px 0 8px 0">{{ p.proposal.summary }}</p>

          <div v-if="p.proposal.file_patches.length > 0" style="margin-top: 8px">
            <el-text size="small" style="font-weight: bold">File Patches:</el-text>
            <div v-for="fp in p.proposal.file_patches" :key="fp.path"
              style="background: #fff; padding: 8px; border-radius: 4px; margin: 6px 0">
              <el-tag size="small" :type="fp.change_type === 'modify' ? 'warning' : fp.change_type === 'add' ? 'success' : 'danger'">
                {{ fp.change_type }}
              </el-tag>
              <el-text style="margin-left: 8px" size="small">{{ fp.path }}</el-text>
              <pre style="margin-top: 4px; font-size: 12px; background: #f0f0f0; padding: 4px; border-radius: 2px; overflow-x: auto">{{ fp.diff }}</pre>
            </div>
          </div>

          <div v-if="p.proposal.tests_to_run.length > 0" style="margin-top: 8px">
            <el-text size="small" style="font-weight: bold">Tests to Run:</el-text>
            <el-tag v-for="t in p.proposal.tests_to_run" :key="t" size="small" type="warning" style="margin: 2px 4px">{{ t }}</el-tag>
          </div>

          <details style="margin-top: 8px">
            <summary style="cursor: pointer; font-size: 13px; color: #909399">
              Allowed Files ({{ p.allowed_files.length }}) &amp; Context
            </summary>
            <div style="margin-top: 4px; font-size: 13px">
              <el-text size="small" type="info">Allowed Files:</el-text>
              <div>
                <el-tag v-for="f in p.allowed_files" :key="f" size="small" style="margin: 2px">{{ f }}</el-tag>
              </div>
            </div>
          </details>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { nodeApi, proposalApi, type PlanNode, type ProposalRecord } from '../api'

const route = useRoute()
const router = useRouter()
const node = ref<PlanNode | null>(null)
const loading = ref(false)
const proposals = ref<ProposalRecord[]>([])
const instruction = ref('')
const generating = ref(false)

onMounted(async () => {
  const nodeId = route.params.id as string
  loading.value = true
  try {
    node.value = await nodeApi.get(nodeId)
    proposals.value = await proposalApi.list(nodeId)
  } catch (e: any) {
    if (e.response?.status !== 404) {
      ElMessage.error('Failed to load node: ' + (e.message || e))
    }
  } finally {
    loading.value = false
  }
})

async function generateProposal() {
  if (!node.value || !instruction.value.trim()) return
  generating.value = true
  try {
    const p = await proposalApi.create(node.value.id, { instruction: instruction.value.trim() })
    proposals.value.unshift(p)
    instruction.value = ''
    ElMessage.success('Proposal generated (dry-run)')
  } catch (e: any) {
    const msg = e.response?.data?.message || e.message || 'Unknown error'
    ElMessage.error('Failed to generate proposal: ' + msg)
  } finally {
    generating.value = false
  }
}

function statusTag(status: string): '' | 'success' | 'warning' | 'danger' | 'info' {
  const map: Record<string, '' | 'success' | 'warning' | 'danger' | 'info'> = {
    pending: 'info', blocked: 'warning', ready: '', running: '',
    completed: 'success', failed: 'danger', failed_retryable: 'danger',
    missing_evidence: 'warning', needs_review: 'warning', needs_review_retryable: 'warning',
    archived: 'info',
  }
  return map[status] || 'info'
}
</script>
