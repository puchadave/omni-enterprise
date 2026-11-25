import template from './omnicore-chat-workflow.html.twig';

const { Mixin } = Shopware;

Shopware.Component.register('omnicore-chat-workflow', {
    template,
    inject: ['acl'],
    mixins: [Mixin.getByName('notification')],
    data() {
        return {
            isLoading: false,
            isExecuting: false,
            prompt: '',
            skusInput: '',
            plan: null,
            pollTimer: null,
        };
    },
    computed: {
        canRead() { return this.acl.can('omnicore_connector:read'); },
        canExecute() { return this.acl.can('omnicore_connector:import'); },
        diffItems() { return (this.plan?.diff?.items) || []; },
    },
    beforeUnmount() { this.stopPolling(); },
    methods: {
        async client() { return Shopware.Application.getContainer('init').httpClient; },
        stopPolling() { if (this.pollTimer) { clearInterval(this.pollTimer); this.pollTimer = null; } },
        startPolling() { this.stopPolling(); if (!this.plan?.plan_id) return; this.pollTimer = setInterval(() => this.refreshPlan(), 3000); },
        parseSkus() { return this.skusInput.split(/[\s,;]+/).map(s => s.trim()).filter(Boolean); },
        async createPlan() {
            if (!this.canRead || !this.prompt.trim()) return;
            this.isLoading = true;
            try {
                const c = await this.client();
                const resp = await c.post('/_action/omnicore/chat/plan', { message: this.prompt, skus: this.parseSkus() });
                this.plan = resp.data.plan;
                this.createNotificationSuccess({ message: this.$tc('omnicore-chat.messages.planCreated') });
            } catch (e) {
                this.createNotificationError({ message: e?.response?.data?.message || e.message || 'Plan failed' });
            } finally { this.isLoading = false; }
        },
        async refreshPlan() {
            if (!this.plan?.plan_id) return;
            try {
                const c = await this.client();
                const resp = await c.get(`/_action/omnicore/chat/plans/${encodeURIComponent(this.plan.plan_id)}`);
                this.plan = resp.data.plan;
                if (['completed','failed','rolled_back'].includes(this.plan.status)) this.stopPolling();
            } catch (_) {}
        },
        async executePlan() {
            if (!this.canExecute || !this.plan?.plan_id) return;
            this.isExecuting = true;
            try {
                const c = await this.client();
                const resp = await c.post(`/_action/omnicore/chat/plans/${encodeURIComponent(this.plan.plan_id)}/execute`, { idempotency_key: this.plan.idempotency_key });
                this.plan = resp.data.plan;
                this.createNotificationSuccess({ message: this.$tc('omnicore-chat.messages.executeQueued') });
                this.startPolling();
            } catch (e) {
                this.createNotificationError({ message: e?.response?.data?.message || e.message || 'Execute failed' });
            } finally { this.isExecuting = false; }
        },
        async rollbackPlan() {
            if (!this.canExecute || !this.plan?.plan_id) return;
            try {
                const c = await this.client();
                const resp = await c.post(`/_action/omnicore/chat/plans/${encodeURIComponent(this.plan.plan_id)}/rollback`, {});
                this.plan = resp.data.plan;
                this.createNotificationSuccess({ message: this.$tc('omnicore-chat.messages.rollbackDone') });
            } catch (e) {
                this.createNotificationError({ message: e?.response?.data?.message || e.message || 'Rollback failed' });
            }
        },
        statusVariant(status) {
            const s = String(status||'').toLower();
            if (['completed','ready','succeeded'].includes(s)) return 'success';
            if (['failed','blocked','rolled_back'].includes(s)) return 'danger';
            if (['queued_for_execution','executing','awaiting_confirmation','draft','incomplete'].includes(s)) return 'warning';
            return 'info';
        },
    },
});
