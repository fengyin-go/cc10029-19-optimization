<template>
  <section class="page" data-module="targetsite">
    <header class="page-head">
      <div>
        <h2>采集目标管理</h2>
        <p class="page-desc">维护采集目标站点的入口、协议约束和字符编码配置，可按多条件筛选并快照采集目标。</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" data-testid="open-create" type="button" @click="openCreate">登记目标站点</button>
        <button class="btn" data-testid="export" type="button" @click="exportRows">导出采集目标清单</button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <form class="filter-bar" @submit.prevent="queryFirstPage">
      <label v-for="field in filterFields" :key="field" class="filter-item">
        <span>{{ field }}</span>
        <input v-model="filters[field]" :data-testid="`filter-${field}`" :placeholder="`按${field}检索`" />
      </label>
      <label class="filter-item">
        <span>请求方法</span>
        <input v-model="filters['请求方法']" data-testid="filter-请求方法" placeholder="按请求方法检索" />
      </label>
      <label class="filter-item">
        <span>字符编码</span>
        <input v-model="filters['字符编码']" data-testid="filter-字符编码" placeholder="按字符编码检索" />
      </label>
      <label class="filter-item">
        <span>状态</span>
        <select v-model="status" data-testid="filter-status">
          <option value="">全部状态</option>
          <option v-for="item in statuses" :key="item" :value="item">{{ item }}</option>
        </select>
      </label>
      <button class="btn" data-testid="query" type="submit">查询</button>
      <button class="btn ghost" data-testid="reset" type="button" @click="resetFilters">重置条件</button>
      <button class="btn primary" data-testid="snapshot-create" type="button" :disabled="snapshotting" @click="createSnapshot">
        {{ snapshotting ? '快照生成中…' : '筛选并快照采集目标' }}
      </button>
    </form>

    <div v-if="snapshot" class="snapshot-panel" data-testid="snapshot-panel">
      <div class="snapshot-head">
        <strong>{{ snapshot.快照编号 }}</strong>
        <span class="snapshot-meta" data-testid="snapshot-meta">
          版本 v{{ snapshot.版本号 }} · 状态 {{ snapshot.状态 }} · 本站点集合 {{ snapshot.站点总数 }} 个站点 · 数据版本
          {{ snapshot.数据版本 }}
        </span>
        <span class="snapshot-meta" data-testid="snapshot-fingerprint">
          条件指纹 {{ snapshot.条件指纹.slice(0, 16) }}…
        </span>
        <button class="link" data-testid="snapshot-close" type="button" @click="closeSnapshot">退出快照翻页</button>
      </div>
      <p v-if="snapshot.冲突站点.length" class="snapshot-conflict" data-testid="snapshot-conflict">
        站点编号冲突已收敛：
        <span v-for="item in snapshot.冲突站点" :key="item.站点编号">
          {{ item.站点编号 }} 选用主键 {{ item.选用主键 }}（{{ item.选用状态 }}），舍弃主键
          {{ item.舍弃主键.join('、') }}；
        </span>
      </p>
      <div class="snapshot-conditions">
        <span v-for="(value, key) in snapshot.筛选条件" :key="key" class="condition-chip">{{ key }}={{ value }}</span>
        <span v-if="!Object.keys(snapshot.筛选条件).length" class="condition-chip">无条件（全部站点）</span>
      </div>
      <details class="snapshot-bindings">
        <summary data-testid="snapshot-bindings-summary">
          站点选择器绑定台账 {{ bindings.length }} 条（口径：绑定范围不超出快照站点）
        </summary>
        <table class="data-table">
          <thead>
            <tr><th>绑定编号</th><th>站点主键</th><th>站点编号</th><th>规则编号</th><th>台账状态</th></tr>
          </thead>
          <tbody>
            <tr v-for="binding in bindings" :key="String(binding.id)" :data-testid="`binding-${binding.id}`">
              <td>{{ binding.绑定编号 }}</td>
              <td>{{ binding.站点主键 }}</td>
              <td>{{ binding.站点编号 ?? '—' }}</td>
              <td>{{ binding.规则编号 ?? '未匹配' }}</td>
              <td>{{ binding.台账状态 }}</td>
            </tr>
            <tr v-if="!bindings.length"><td colspan="5" class="empty-state">该快照暂无绑定记录</td></tr>
          </tbody>
        </table>
      </details>
    </div>

    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in columns" :key="column">{{ column }}</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)" :data-testid="`row-${row.id}`" :data-status="String(row.status)">
          <td v-for="column in columns" :key="column">{{ row[column] ?? '—' }}</td>
          <td class="row-actions">
            <button
              v-for="action in actions"
              :key="action"
              class="link"
              type="button"
              :data-testid="`action-${row.id}-${action}`"
              :disabled="snapshot !== null"
              :title="snapshot !== null ? '快照内站点集合已冻结，请退出快照翻页后再执行动作' : undefined"
              @click="runAction(action, row)"
            >
              {{ action }}
            </button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="columns.length + 1" class="empty-state">暂无采集目标数据，可先登记目标站点</td>
        </tr>
      </tbody>
    </table>

    <div class="pager" data-testid="pager">
      <button class="btn ghost" data-testid="page-prev" type="button" :disabled="page <= 1" @click="gotoPage(page - 1)">
        上一页
      </button>
      <span data-testid="page-info">第 {{ page }} / {{ pageCount }} 页 · 本页 {{ rows.length }} 条 · 共 {{ total }} 条</span>
      <button
        class="btn ghost"
        data-testid="page-next"
        type="button"
        :disabled="page >= pageCount"
        @click="gotoPage(page + 1)"
      >
        下一页
      </button>
      <label class="pager-size">
        每页
        <select v-model.number="size" data-testid="page-size" @change="queryFirstPage">
          <option v-for="option in pageSizes" :key="option" :value="option">{{ option }}</option>
        </select>
        条
      </label>
    </div>

    <section class="snapshot-history" data-testid="snapshot-history">
      <header class="snapshot-history-head">
        <strong>筛选快照版本</strong>
        <span class="snapshot-meta">条件变化后旧快照仍可回看；切换或回退都会推进当前数据版本。</span>
        <button class="link" data-testid="snapshot-refresh" type="button" @click="loadSnapshots">刷新快照列表</button>
      </header>
      <table class="data-table">
        <thead>
          <tr><th>快照编号</th><th>版本</th><th>状态</th><th>站点数</th><th>条件指纹</th><th>操作</th></tr>
        </thead>
        <tbody>
          <tr v-for="item in snapshotHistory" :key="String(item.id)" :data-testid="`snapshot-row-${item.id}`">
            <td>{{ item.快照编号 }}</td>
            <td>v{{ item.版本号 }}</td>
            <td>{{ item.状态 }}</td>
            <td>{{ item.站点总数 }}</td>
            <td class="mono">{{ item.条件指纹.slice(0, 12) }}…</td>
            <td class="row-actions">
              <button class="link" :data-testid="`snapshot-view-${item.id}`" type="button" @click="openSnapshot(item.id, 1)">
                回看
              </button>
              <button
                class="link"
                :data-testid="`snapshot-switch-${item.id}`"
                type="button"
                :disabled="item.状态 === '生效'"
                @click="switchSnapshot(item.id, 'switch')"
              >
                切换新版本
              </button>
              <button
                class="link"
                :data-testid="`snapshot-rollback-${item.id}`"
                type="button"
                :disabled="item.状态 === '生效'"
                @click="switchSnapshot(item.id, 'rollback')"
              >
                回退到此版本
              </button>
            </td>
          </tr>
          <tr v-if="!snapshotHistory.length">
            <td colspan="6" class="empty-state">尚未生成筛选快照，可先按条件筛选并快照采集目标</td>
          </tr>
        </tbody>
      </table>
    </section>

    <div v-if="showCreate" class="modal-backdrop" data-testid="create-modal">
      <form class="modal" @submit.prevent="submitCreate">
        <h3>登记目标站点</h3>
        <label v-for="field in columns" :key="field" class="create-field">
          <span>{{ field }}</span>
          <input v-model="createValues[field]" :data-testid="`create-${field}`" required />
        </label>
        <div class="page-actions">
          <button class="btn" type="button" @click="showCreate = false">取消</button>
          <button class="btn primary" data-testid="submit-create" type="submit" :disabled="creating">
            {{ creating ? '提交中…' : '确认登记' }}
          </button>
        </div>
      </form>
    </div>

    <footer class="page-foot">
      <span>共 {{ total }} 条采集目标记录</span>
      <span v-if="errorMessage" class="error-text" data-testid="error-message">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, string | number | null>
type Binding = Record<string, string | number | null>
type Snapshot = {
  id: number
  快照编号: string
  条件指纹: string
  筛选条件: Record<string, string>
  站点主键: number[]
  站点总数: number
  版本号: number
  数据版本: number
  状态: string
  冲突站点: { 站点编号: string; 选用主键: number; 选用状态: string; 舍弃主键: number[] }[]
  创建时间: string
}

const ENDPOINT = '/api/targetsite'
const columns = ["站点编号", "站点名称", "入口URL", "请求方法", "请求头模板", "字符编码", "重试上限", "抓取间隔"]
const actions = ["提交校验", "启用配置", "停用配置"]
const statuses = ["草稿", "待校验", "已启用", "已停用"]
const metricNames = ["目标站点总数", "待校验目标站点", "已启用目标站点"]
const pageSizes = [10, 20, 50]

const rows = ref<Row[]>([])
const total = ref(0)
const errorMessage = ref('')
const filters = ref<Record<string, string>>({})
const status = ref('')
const filterFields = columns.slice(0, 3)
const showCreate = ref(false)
const creating = ref(false)
const createValues = ref<Record<string, string>>({})

const page = ref(1)
const size = ref(20)
const pageCount = computed(() => Math.max(1, Math.ceil(total.value / size.value)))

const snapshot = ref<Snapshot | null>(null)
const bindings = ref<Binding[]>([])
const snapshotting = ref(false)
const snapshotHistory = ref<Snapshot[]>([])

const stats = computed(() => [
  { label: metricNames[0], value: total.value },
  { label: metricNames[1], value: rows.value.filter((row) => row.status === statuses[1]).length },
  { label: metricNames[2], value: rows.value.filter((row) => row.status === statuses[statuses.length - 1]).length },
])

function activeFilters(): Record<string, string> {
  return Object.fromEntries(
    Object.entries(filters.value).filter(([, value]) => String(value ?? '').trim() !== ''),
  )
}

function resetFilters() {
  filters.value = {}
  status.value = ''
  void queryFirstPage()
}

async function exportRows() {
  errorMessage.value = ''
  const params = new URLSearchParams(activeFilters())
  if (status.value) params.set('status', status.value)
  try {
    const response = await request(`${ENDPOINT}/export?${params.toString()}`)
    if (!response.ok) throw new Error('导出失败，请稍后重试')
    const url = URL.createObjectURL(await response.blob())
    const link = document.createElement('a')
    link.href = url
    link.download = 'targetsite.csv'
    link.click()
    URL.revokeObjectURL(url)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '导出失败'
  }
}

function openCreate() {
  createValues.value = Object.fromEntries(columns.map((field) => [field, '']))
  errorMessage.value = ''
  showCreate.value = true
}

async function submitCreate() {
  creating.value = true
  errorMessage.value = ''
  try {
    const response = await request(ENDPOINT, {
      method: 'POST', body: JSON.stringify(createValues.value),
    })
    const payload = await response.json()
    if (!response.ok || !payload.ok) throw new Error(payload.message || '登记失败')
    showCreate.value = false
    snapshot.value = null
    bindings.value = []
    await Promise.all([reload(), loadSnapshots()])
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '登记失败'
  } finally {
    creating.value = false
  }
}

async function runAction(action: string, row: Row) {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/actions`, {
      method: 'POST',
      body: JSON.stringify({ action }),
    })
    if (!response.ok) {
      throw new Error('采集目标动作未生效，请稍后重试')
    }
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '采集目标操作失败'
  }
}

function queryFirstPage() {
  // 手动查询回到实时列表口径；要看固定站点集合请重新“筛选并快照采集目标”。
  snapshot.value = null
  bindings.value = []
  page.value = 1
  return reload()
}

async function reload() {
  errorMessage.value = ''
  if (snapshot.value) {
    await openSnapshot(snapshot.value.id, page.value)
    return
  }
  const params = new URLSearchParams(activeFilters())
  if (status.value) params.set('status', status.value)
  params.set('page', String(page.value))
  params.set('size', String(size.value))
  try {
    const response = await request(`${ENDPOINT}?${params.toString()}`)
    if (!response.ok) {
      throw new Error('目标站点列表读取失败')
    }
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '采集目标列表读取失败'
  }
}

function gotoPage(target: number) {
  const bounded = Math.min(Math.max(1, target), pageCount.value)
  page.value = bounded
  void reload()
}

async function createSnapshot() {
  snapshotting.value = true
  errorMessage.value = ''
  try {
    const body = {
      values: activeFilters(),
      status: status.value || null,
      request_id: `snap-${Date.now()}-${Math.random().toString(16).slice(2)}`,
    }
    const response = await request(`${ENDPOINT}/snapshots`, {
      method: 'POST',
      body: JSON.stringify(body),
    })
    const payload = await response.json()
    if (!response.ok || !payload.ok) throw new Error(payload.message || '快照生成失败')
    const created = payload.entry.snapshot as Snapshot
    await Promise.all([openSnapshot(created.id, 1), loadSnapshots()])
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '快照生成失败'
  } finally {
    snapshotting.value = false
  }
}

async function openSnapshot(snapshotId: number, targetPage: number) {
  errorMessage.value = ''
  try {
    const response = await request(
      `${ENDPOINT}/snapshots/${snapshotId}?page=${targetPage}&size=${size.value}`,
    )
    if (!response.ok) throw new Error('筛选快照读取失败')
    const payload = await response.json()
    snapshot.value = {
      id: payload.id,
      快照编号: payload.快照编号,
      条件指纹: payload.条件指纹,
      筛选条件: payload.筛选条件 ?? {},
      站点主键: payload.站点主键 ?? [],
      站点总数: payload.站点总数 ?? 0,
      版本号: payload.版本号,
      数据版本: payload.数据版本,
      状态: payload.状态,
      冲突站点: payload.冲突站点 ?? [],
      创建时间: payload.创建时间,
    }
    bindings.value = payload.bindings ?? []
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
    page.value = payload.page ?? targetPage
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '筛选快照读取失败'
  }
}

function closeSnapshot() {
  snapshot.value = null
  bindings.value = []
  page.value = 1
  void reload()
}

async function loadSnapshots() {
  try {
    const response = await request(`${ENDPOINT}/snapshots?size=50`)
    if (!response.ok) return
    const payload = await response.json()
    snapshotHistory.value = payload.items ?? []
  } catch {
    snapshotHistory.value = []
  }
}

async function switchSnapshot(snapshotId: number, mode: 'switch' | 'rollback') {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/snapshots/${snapshotId}/${mode}`, { method: 'POST' })
    const payload = await response.json()
    if (!response.ok || !payload.ok) throw new Error(payload.message || '快照版本切换失败')
    await Promise.all([loadSnapshots(), openSnapshot(snapshotId, 1)])
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '快照版本切换失败'
  }
}

onMounted(async () => {
  await Promise.all([reload(), loadSnapshots()])
})
</script>

<style scoped>
.snapshot-panel {
  border: 1px solid #bfdbfe;
  background: #eff6ff;
  border-radius: 8px;
  padding: 12px;
  margin-bottom: 12px;
  font-size: 13px;
}
.snapshot-head { display: flex; flex-wrap: wrap; gap: 12px; align-items: center; }
.snapshot-meta { color: #475569; font-size: 12px; }
.snapshot-conflict { color: #b45309; margin: 8px 0 0; }
.snapshot-conditions { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 8px; }
.condition-chip { background: #fff; border: 1px solid #bfdbfe; border-radius: 999px; padding: 2px 10px; font-size: 12px; }
.snapshot-bindings { margin-top: 10px; }
.snapshot-bindings summary { cursor: pointer; color: #1f6feb; font-size: 12px; }
.snapshot-bindings .data-table { margin-top: 8px; }
.pager { display: flex; align-items: center; gap: 12px; margin-top: 10px; font-size: 13px; color: #475569; }
.pager-size select { padding: 4px 6px; border: 1px solid var(--border); border-radius: 6px; }
.snapshot-history { margin-top: 16px; }
.snapshot-history-head { display: flex; flex-wrap: wrap; gap: 12px; align-items: center; margin-bottom: 8px; }
.mono { font-family: monospace; font-size: 12px; }
.filter-item select { padding: 6px; border: 1px solid var(--border); border-radius: 6px; }
</style>
