<template>
  <div class="split">
    <section class="pane">
      <h2>可借物</h2>
      <div v-for="i in board.available" :key="i.id" class="item">
        <strong>{{ i.title }}</strong>
        <div class="muted">物主 {{ i.owner || '—' }}</div>
        <input v-model="forms[i.id].borrower" placeholder="借用人" />
        <input v-model="forms[i.id].due_date" placeholder="应还日 YYYY-MM-DD" />
        <button @click="lend(i.id)">借出通过</button>
      </div>
    </section>
    <section class="pane">
      <h2>在借 / 逾期</h2>
      <div v-for="l in [...board.overdue, ...board.active]" :key="l.id" class="item" :class="{ overdue: l.overdue }">
        <label>
          <input type="checkbox" class="chk" :value="l.id" v-model="selected" @change="preview = null" />
          <strong>{{ l.title }}</strong> → {{ l.borrower }}
        </label>
        <div class="muted">应还 {{ l.due_date }} {{ l.overdue ? '· 逾期' : '' }}</div>
        <button @click="ret(l.id)">归还</button>
      </div>
      <div v-if="selected.length">
        <button @click="dryRun" :disabled="busy">干跑预览（{{ selected.length }} 笔）</button>
      </div>
      <div v-if="preview" class="item preview">
        <div><strong>干跑：将回到可借栏 {{ preview.items.length }} 件</strong></div>
        <div v-for="i in preview.items" :key="i.loan_id">
          · {{ i.title }}（{{ i.borrower }}，应还 {{ i.due_date }}）
        </div>
        <div v-for="b in preview.blocked" :key="b.loan_id" class="blocked">
          · #{{ b.loan_id }} {{ b.reason === 'not_found' ? '记录不存在' : '未在借' }}，整单不会提交
        </div>
        <div class="row">
          <button @click="commit" :disabled="busy || preview.blocked.length > 0">确认归还</button>
          <button class="ghost" @click="preview = null" :disabled="busy">取消</button>
        </div>
      </div>
      <div v-if="err" class="item overdue">{{ err }}</div>
    </section>
  </div>
</template>
<script setup>
import { inject, reactive, ref, watch } from 'vue'
import { api } from '../api'
const board = inject('board')
const reload = inject('reloadBoard')
const forms = reactive({})
const selected = ref([])
const preview = ref(null)
const busy = ref(false)
const err = ref('')
watch(board, (b) => {
  for (const i of (b.available || [])) {
    if (!forms[i.id]) forms[i.id] = { borrower: '邻居', due_date: '2026-12-31' }
  }
}, { immediate: true, deep: true })
async function lend(id) {
  await api('/items/' + id + '/lend', { method: 'POST', body: JSON.stringify(forms[id]) })
  await reload()
}
async function ret(id) {
  await api('/loans/' + id + '/return', { method: 'POST', body: '{}' })
  selected.value = selected.value.filter(x => x !== id)
  preview.value = null
  await reload()
}
async function dryRun() {
  busy.value = true
  err.value = ''
  try {
    // 干跑只取预览，分栏与顶细条不动
    preview.value = await api('/returns/dry-run', {
      method: 'POST', body: JSON.stringify({ loan_ids: selected.value }),
    })
  } catch (e) {
    err.value = '干跑失败：' + e.message
  } finally {
    busy.value = false
  }
}
async function commit() {
  busy.value = true
  err.value = ''
  try {
    await api('/returns/commit', {
      method: 'POST',
      body: JSON.stringify({ token: preview.value.token, loan_ids: selected.value }),
    })
    selected.value = []
    preview.value = null
    await reload() // 分栏、物主栏、顶细条同一次刷新，不会各说各话
  } catch (e) {
    preview.value = null
    await reload()
    if (e.message.includes('stale_plan') || e.message.includes('blocked_present')) {
      await dryRun() // 整单未动，按最新在借重算预览
      err.value = '名单已变化，整单未提交；已按最新在借重跑干跑，请确认后再提交'
    } else {
      selected.value = []
      err.value = '归还失败，已全部回到提交前：' + e.message
    }
  } finally {
    busy.value = false
  }
}
</script>
