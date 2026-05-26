import { createRouter, createWebHistory } from 'vue-router'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    {
      path: '/',
      redirect: '/tasks',
    },
    {
      path: '/tasks',
      name: 'tasks',
      component: () => import('../views/TaskList.vue'),
    },
    {
      path: '/plan',
      name: 'plan',
      component: () => import('../views/CurrentPlan.vue'),
    },
    {
      path: '/nodes/:id',
      name: 'node',
      component: () => import('../views/NodeDetail.vue'),
    },
    {
      path: '/runs/:nodeId',
      name: 'runs',
      component: () => import('../views/RunHistory.vue'),
    },
    {
      path: '/reports/:nodeId',
      name: 'report',
      component: () => import('../views/ReportView.vue'),
    },
  ],
})

export default router
