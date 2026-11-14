import { defineStore } from 'pinia'

export type Role = '管理员' | '编辑' | '只读'

export const useSessionStore = defineStore('session', {
  state: () => ({
    operator: '值班管理员',
    role: '管理员' as Role,
    shiftLabel: '白班 08:00-20:00',
    scope: '指标口径与分析实验平台',
  }),
  getters: {
    canOperate: (state) => state.role !== '只读',
    canAdmin: (state) => state.role === '管理员',
    identity: (state) => ({
      operator: state.operator,
      role: state.role,
    }),
  },
  actions: {
    setShift(label: string) {
      this.shiftLabel = label
    },
    setOperator(operator: string) {
      this.operator = operator.trim() || '值班管理员'
    },
    setRole(role: Role) {
      this.role = role
    },
  },
})
