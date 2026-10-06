<script setup lang="ts">
/**
 * 部门与职位维护（P2-13b）。
 *
 * --------------------------------------------------------------------------
 * 部门树这块最容易做错的两件事
 * --------------------------------------------------------------------------
 * 1. **改上级要防环**。把 A 的上级设成 A 的子孙，后端会拒绝（400），
 *    但前端必须**先**把那些选项从下拉里剔除 —— 否则用户会以为「系统不让我选
 *    一个明显合理的选项」。树形结构里「合理不合理」只有算过才知道，
 *    所以这里算一遍子孙，下拉里直接不出现它们。
 *
 * 2. **删部门/删职位的失败原因要说清**。后端会因为「还有下级 / 还有人挂着」
 *    拒绝删除，这个 400 是**预期内的业务结果**，不是错误 —— 展示时要有区别，
 *    否则用户会以为系统坏了。
 */
import { computed, onMounted, ref } from 'vue'
import ModalDialog from '@/components/ModalDialog.vue'
import ToastStack from '@/components/ToastStack.vue'
import { newToast, type ToastItem } from '@/components/ui'
import * as api from '@/api/admin'
import { SEQUENCE_LABEL, type DepartmentRow, type PositionRow, type Sequence } from '@/api/admin'
import { ApiError } from '@/api/http'

const departments = ref<DepartmentRow[]>([])
const positions = ref<PositionRow[]>([])
const users = ref<{ id: number; display_name: string; department_id: number | null }[]>([])
const toasts = ref<ToastItem[]>([])

function toast(kind: ToastItem['kind'], text: string) {
  const item = newToast(kind, text)
  toasts.value.push(item)
  setTimeout(() => dismiss(item.id), kind === 'ok' ? 2600 : 4200)
}
function dismiss(id: number) {
  toasts.value = toasts.value.filter((t) => t.id !== id)
}

async function load() {
  const [opt, list] = await Promise.all([api.fetchOptions(), api.fetchUsers({})])
  departments.value = opt.departments
  positions.value = opt.positions
  users.value = list.items.map((u) => ({
    id: u.id, display_name: u.display_name, department_id: u.department_id,
  }))
}

onMounted(load)

/** 扁平 → 带缩进的层级列表（表格比树控件更适合「一行一个操作」）。 */
const deptRows = computed(() => {
  const byId = new Map(departments.value.map((d) => [d.id, d]))
  const depthOf = (d: DepartmentRow): number => {
    let depth = 0
    let cur = d
    while (cur.parent_id != null && byId.has(cur.parent_id) && depth < 8) {
      cur = byId.get(cur.parent_id)!
      depth += 1
    }
    return depth
  }
  return departments.value
    .map((d) => ({ dept: d, depth: depthOf(d) }))
    .sort((a, b) => a.dept.sort_order - b.dept.sort_order || a.dept.id - b.dept.id)
})

function memberCount(deptId: number): number {
  return users.value.filter((u) => u.department_id === deptId).length
}
function leaderName(dept: DepartmentRow): string {
  if (dept.leader_user_id == null) return '—'
  return users.value.find((u) => u.id === dept.leader_user_id)?.display_name ?? `#${dept.leader_user_id}`
}
function parentName(dept: DepartmentRow): string {
  if (dept.parent_id == null) return '—'
  return departments.value.find((d) => d.id === dept.parent_id)?.name ?? `#${dept.parent_id}`
}

/** 自己 + 全部子孙：改上级时这些都不能选。 */
function forbiddenParents(deptId: number): Set<number> {
  const children = new Map<number | null, number[]>()
  for (const d of departments.value) {
    const list = children.get(d.parent_id) ?? []
    list.push(d.id)
    children.set(d.parent_id, list)
  }
  const out = new Set<number>([deptId])
  const stack = [deptId]
  while (stack.length) {
    const cur = stack.pop()!
    for (const child of children.get(cur) ?? []) {
      if (!out.has(child)) {
        out.add(child)
        stack.push(child)
      }
    }
  }
  return out
}

// ---------- 部门弹窗 ----------
const deptOpen = ref(false)
const deptEditing = ref<DepartmentRow | null>(null)
const deptForm = ref({ code: '', name: '', parent_id: null as number | null, leader_user_id: null as number | null, sort_order: 0 })
const deptError = ref('')
const deptSaving = ref(false)

const parentOptions = computed(() => {
  const forbidden = deptEditing.value ? forbiddenParents(deptEditing.value.id) : new Set<number>()
  return departments.value.filter((d) => !forbidden.has(d.id))
})

function openDeptCreate() {
  deptEditing.value = null
  deptForm.value = { code: '', name: '', parent_id: null, leader_user_id: null, sort_order: 0 }
  deptError.value = ''
  deptOpen.value = true
}
function openDeptEdit(d: DepartmentRow) {
  deptEditing.value = d
  deptForm.value = {
    code: d.code, name: d.name, parent_id: d.parent_id,
    leader_user_id: d.leader_user_id, sort_order: d.sort_order,
  }
  deptError.value = ''
  deptOpen.value = true
}
async function submitDept() {
  deptSaving.value = true
  deptError.value = ''
  try {
    if (deptEditing.value) {
      await api.updateDepartment(deptEditing.value.id, {
        name: deptForm.value.name,
        parent_id: deptForm.value.parent_id,
        leader_user_id: deptForm.value.leader_user_id,
        sort_order: deptForm.value.sort_order,
      })
      toast('ok', '部门已更新')
    } else {
      await api.createDepartment({
        code: deptForm.value.code,
        name: deptForm.value.name,
        parent_id: deptForm.value.parent_id,
        leader_user_id: deptForm.value.leader_user_id,
        sort_order: deptForm.value.sort_order,
      })
      toast('ok', `部门「${deptForm.value.name}」已创建`)
    }
    deptOpen.value = false
    await load()
  } catch (e) {
    deptError.value = e instanceof ApiError ? e.message : '保存失败'
  } finally {
    deptSaving.value = false
  }
}
async function removeDept(d: DepartmentRow) {
  try {
    await api.deleteDepartment(d.id)
    toast('ok', `部门「${d.name}」已删除`)
    await load()
  } catch (e) {
    // 这两条 400 是业务规则（有下级 / 有人），不是故障 —— 用 warn 而不是 error
    toast('warn', e instanceof ApiError ? e.message : '删除失败')
  }
}

// ---------- 职位弹窗 ----------
const posOpen = ref(false)
const posEditing = ref<PositionRow | null>(null)
const posForm = ref({ code: '', name: '', level: '', sequence: 'tech' as Sequence })
const posError = ref('')
const posSaving = ref(false)

function openPosCreate() {
  posEditing.value = null
  posForm.value = { code: '', name: '', level: '', sequence: 'tech' }
  posError.value = ''
  posOpen.value = true
}
function openPosEdit(p: PositionRow) {
  posEditing.value = p
  posForm.value = { code: p.code, name: p.name, level: p.level ?? '', sequence: p.sequence }
  posError.value = ''
  posOpen.value = true
}
async function submitPos() {
  posSaving.value = true
  posError.value = ''
  try {
    if (posEditing.value) {
      await api.updatePosition(posEditing.value.id, {
        code: posForm.value.code,
        name: posForm.value.name,
        level: posForm.value.level || null,
        sequence: posForm.value.sequence,
      })
      toast('ok', '职位已更新')
    } else {
      await api.createPosition({
        code: posForm.value.code,
        name: posForm.value.name,
        level: posForm.value.level || null,
        sequence: posForm.value.sequence,
      })
      toast('ok', `职位「${posForm.value.name}」已创建`)
    }
    posOpen.value = false
    await load()
  } catch (e) {
    posError.value = e instanceof ApiError ? e.message : '保存失败'
  } finally {
    posSaving.value = false
  }
}
async function removePos(p: PositionRow) {
  try {
    await api.deletePosition(p.id)
    toast('ok', `职位「${p.name}」已删除`)
    await load()
  } catch (e) {
    toast('warn', e instanceof ApiError ? e.message : '删除失败')
  }
}
</script>

<template>
  <div>
    <div class="head">
      <div>
        <h1 class="page-title">部门与职位</h1>
        <p class="page-sub">
          部门负责「谁归哪儿」，职位负责「是什么岗」—— 两者都与系统角色正交
        </p>
      </div>
      <button class="btn btn-primary" @click="openDeptCreate">＋ 新建部门</button>
    </div>

    <div class="card table-wrap">
      <table class="grid">
        <thead>
          <tr>
            <th>部门</th>
            <th>编码</th>
            <th>上级</th>
            <th>负责人</th>
            <th class="num">成员</th>
            <th class="num">排序</th>
            <th class="ops">操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="{ dept, depth } in deptRows" :key="dept.id">
            <td>
              <span :style="{ paddingLeft: `${depth * 16}px` }">
                <span v-if="depth" class="muted">{{ '└ ' }}</span>{{ dept.name }}
              </span>
            </td>
            <td class="mono">{{ dept.code }}</td>
            <td class="muted">{{ parentName(dept) }}</td>
            <td>{{ leaderName(dept) }}</td>
            <td class="num">{{ memberCount(dept.id) }}</td>
            <td class="num muted">{{ dept.sort_order }}</td>
            <td class="ops">
              <button class="btn btn-sm" @click="openDeptEdit(dept)">编辑</button>
              <button class="btn btn-sm btn-danger" @click="removeDept(dept)">删除</button>
            </td>
          </tr>
          <tr v-if="!deptRows.length">
            <td colspan="7" class="empty">还没有部门</td>
          </tr>
        </tbody>
      </table>
    </div>

    <div class="head second">
      <div>
        <h2 class="section-title">职位</h2>
        <p class="page-sub">职级挂在职位上（同岗位的人职级一般一致），不挂在员工身上</p>
      </div>
      <button class="btn" @click="openPosCreate">＋ 新建职位</button>
    </div>

    <div class="card table-wrap">
      <table class="grid">
        <thead>
          <tr>
            <th>职位</th>
            <th>编码</th>
            <th>职级</th>
            <th>序列</th>
            <th class="ops">操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="p in positions" :key="p.id">
            <td>{{ p.name }}</td>
            <td class="mono">{{ p.code }}</td>
            <td>{{ p.level || '—' }}</td>
            <td><span class="badge badge-user">{{ SEQUENCE_LABEL[p.sequence] }}</span></td>
            <td class="ops">
              <button class="btn btn-sm" @click="openPosEdit(p)">编辑</button>
              <button class="btn btn-sm btn-danger" @click="removePos(p)">删除</button>
            </td>
          </tr>
          <tr v-if="!positions.length">
            <td colspan="5" class="empty">还没有职位</td>
          </tr>
        </tbody>
      </table>
    </div>

    <!-- 部门 -->
    <ModalDialog
      :open="deptOpen"
      :title="deptEditing ? `编辑部门 · ${deptEditing.name}` : '新建部门'"
      :width="460"
      @close="deptOpen = false"
    >
      <label class="field">
        <span>编码</span>
        <input v-model="deptForm.code" class="input" :disabled="!!deptEditing" placeholder="OPS2" />
      </label>
      <label class="field">
        <span>名称</span>
        <input v-model="deptForm.name" class="input" />
      </label>
      <div class="two">
        <label class="field">
          <span>上级部门</span>
          <select v-model="deptForm.parent_id" class="select">
            <option :value="null">（无，作为一级部门）</option>
            <option v-for="d in parentOptions" :key="d.id" :value="d.id">{{ d.name }}</option>
          </select>
          <span v-if="deptEditing" class="hint">下拉里已经排除了它自己和它的下级（防环）</span>
        </label>
        <label class="field">
          <span>负责人</span>
          <select v-model="deptForm.leader_user_id" class="select">
            <option :value="null">（未指定）</option>
            <option v-for="u in users" :key="u.id" :value="u.id">{{ u.display_name }}</option>
          </select>
        </label>
      </div>
      <label class="field">
        <span>排序</span>
        <input v-model.number="deptForm.sort_order" class="input" type="number" />
      </label>
      <p v-if="deptError" class="form-error">{{ deptError }}</p>
      <template #footer>
        <button class="btn" @click="deptOpen = false">取消</button>
        <button class="btn btn-primary" :disabled="deptSaving" @click="submitDept">
          {{ deptSaving ? '保存中…' : '保存' }}
        </button>
      </template>
    </ModalDialog>

    <!-- 职位 -->
    <ModalDialog
      :open="posOpen"
      :title="posEditing ? `编辑职位 · ${posEditing.name}` : '新建职位'"
      :width="440"
      @close="posOpen = false"
    >
      <label class="field">
        <span>编码</span>
        <input v-model="posForm.code" class="input" placeholder="P9" />
      </label>
      <label class="field">
        <span>名称</span>
        <input v-model="posForm.name" class="input" />
      </label>
      <div class="two">
        <label class="field">
          <span>职级</span>
          <input v-model="posForm.level" class="input" placeholder="P6 / M2" />
        </label>
        <label class="field">
          <span>序列</span>
          <select v-model="posForm.sequence" class="select">
            <option v-for="(label, key) in SEQUENCE_LABEL" :key="key" :value="key">{{ label }}</option>
          </select>
        </label>
      </div>
      <p v-if="posError" class="form-error">{{ posError }}</p>
      <template #footer>
        <button class="btn" @click="posOpen = false">取消</button>
        <button class="btn btn-primary" :disabled="posSaving" @click="submitPos">
          {{ posSaving ? '保存中…' : '保存' }}
        </button>
      </template>
    </ModalDialog>

    <ToastStack :items="toasts" @dismiss="dismiss" />
  </div>
</template>

<style scoped>
.head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  margin-bottom: 16px;
}
.head.second {
  margin-top: 26px;
}
.section-title {
  font-size: 16px;
  font-weight: 600;
}
.table-wrap {
  overflow: auto;
}
.ops {
  white-space: nowrap;
}
.ops .btn {
  margin-right: 6px;
}
.two {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 12px;
}
.form-error {
  color: var(--danger);
  background: var(--danger-soft);
  border: 1px solid rgba(240, 97, 109, 0.3);
  border-radius: var(--radius-sm);
  padding: 8px 10px;
  font-size: 13px;
}
</style>
