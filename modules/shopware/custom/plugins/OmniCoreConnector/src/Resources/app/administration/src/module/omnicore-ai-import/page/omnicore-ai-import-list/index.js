import template from './omnicore-ai-import-list.html.twig';

const { Mixin } = Shopware;

Shopware.Component.register('omnicore-ai-import-list', {
    template,

    inject: ['acl'],

    mixins: [Mixin.getByName('notification')],

    data() {
        return {
            isLoading: false,
            isImporting: false,
            candidates: [],
            total: 0,
            selection: {},
            activeJob: null,
            jobItems: [],
            detailItem: null,
            pollTimer: null,
            filter: {
                search: '',
                status: '',
                minScore: 55,
                limit: 50,
            },
        };
    },

    computed: {
        canRead() { return this.acl.can('omnicore_connector:read'); },
        canImport() { return this.acl.can('omnicore_connector:import'); },
        canUpdate() { return this.acl.can('omnicore_connector:update'); },
        selectedSkus() { return Object.keys(this.selection).filter((k) => this.selection[k]); },
        columns() {
            return [
                { property: 'sku', label: 'SKU', primary: true },
                { property: 'title', label: this.$tc('omnicore-ai-import.columns.title') },
                { property: 'selection_score', label: this.$tc('omnicore-ai-import.columns.score') },
                { property: 'lifecycle_status', label: this.$tc('omnicore-ai-import.columns.status') },
                { property: 'shopware_product_id', label: this.$tc('omnicore-ai-import.columns.shopwareId') },
                { property: 'last_error', label: this.$tc('omnicore-ai-import.columns.error') },
            ];
        },
    },

    created() {
        this.loadCandidates();
    },

    beforeUnmount() {
        if (this.pollTimer) clearInterval(this.pollTimer);
    },

    methods: {
        async httpClient() {
            return Shopware.Application.getContainer('init').httpClient;
        },

        async loadCandidates() {
            if (!this.canRead) return;
            this.isLoading = true;
            try {
                const client = await this.httpClient();
                const params = {
                    limit: this.filter.limit,
                    offset: 0,
                    search: this.filter.search || undefined,
                    status: this.filter.status || undefined,
                    min_score: this.filter.minScore,
                };
                const resp = await client.get('/_action/omnicore/ai-import/candidates', { params });
                this.candidates = resp.data.items || [];
                this.total = resp.data.total || 0;
            } catch (e) {
                this.createNotificationError({ message: this.errorMessage(e, 'loadFailed') });
            } finally {
                this.isLoading = false;
            }
        },

        async refreshSelection() {
            if (!this.canUpdate) return;
            this.isLoading = true;
            try {
                const client = await this.httpClient();
                await client.post('/_action/omnicore/ai-import/refresh', { limit: this.filter.limit, minScore: this.filter.minScore });
                this.createNotificationSuccess({ message: this.$tc('omnicore-ai-import.messages.refreshSuccess') });
                await this.loadCandidates();
            } catch (e) {
                this.createNotificationError({ message: this.errorMessage(e, 'refreshFailed') });
            } finally {
                this.isLoading = false;
            }
        },

        onSelectionChange(selection) {
            this.selection = selection || {};
        },

        async importSelected() {
            const skus = this.selectedSkus;
            if (!skus.length) {
                this.createNotificationWarning({ message: this.$tc('omnicore-ai-import.messages.noSelection') });
                return;
            }
            await this.createJob({ mode: 'selected', skus, confirm: true });
        },

        async importAllMatching() {
            if (!window.confirm(this.$tc('omnicore-ai-import.messages.confirmFilterImport'))) return;
            await this.createJob({ mode: 'filter', skus: [], confirm: true, filters: {
                status: this.filter.status || undefined,
                search: this.filter.search || undefined,
                min_score: this.filter.minScore,
                limit: this.filter.limit,
            } });
        },

        async createJob(payload) {
            if (!this.canImport) return;
            this.isImporting = true;
            try {
                const client = await this.httpClient();
                const resp = await client.post('/_action/omnicore/ai-import/jobs', payload);
                this.activeJob = resp.data.job;
                this.createNotificationSuccess({ message: this.$tc('omnicore-ai-import.messages.jobCreated') });
                this.startPolling();
                await this.loadJobItems();
            } catch (e) {
                this.createNotificationError({ message: this.errorMessage(e, 'jobFailed') });
            } finally {
                this.isImporting = false;
            }
        },

        startPolling() {
            if (this.pollTimer) clearInterval(this.pollTimer);
            this.pollTimer = setInterval(() => this.pollJob(), 4000);
            this.pollJob();
        },

        async pollJob() {
            if (!this.activeJob || !this.activeJob.id) return;
            try {
                const client = await this.httpClient();
                const resp = await client.get(`/_action/omnicore/ai-import/jobs/${this.activeJob.id}`);
                this.activeJob = resp.data.job;
                await this.loadJobItems();
                if (['completed', 'completed_with_errors', 'failed'].includes(this.activeJob.status)) {
                    clearInterval(this.pollTimer);
                    this.pollTimer = null;
                    await this.loadCandidates();
                }
            } catch (e) {
                clearInterval(this.pollTimer);
                this.pollTimer = null;
                this.createNotificationError({ message: this.errorMessage(e, 'pollFailed') });
            }
        },

        async loadJobItems() {
            if (!this.activeJob || !this.activeJob.id) return;
            try {
                const client = await this.httpClient();
                const resp = await client.get(`/_action/omnicore/ai-import/jobs/${this.activeJob.id}/items`, { params: { limit: 100 } });
                this.jobItems = resp.data.items || [];
            } catch (_) { /* non-fatal */ }
        },

        async retryFailed() {
            if (!this.activeJob || !this.activeJob.id) return;
            await this.createRetry(this.activeJob.id);
        },

        async createRetry(jobId) {
            if (!this.canImport) return;
            this.isImporting = true;
            try {
                const client = await this.httpClient();
                const resp = await client.post(`/_action/omnicore/ai-import/jobs/${jobId}/retry-failed`);
                this.activeJob = resp.data.job;
                this.createNotificationSuccess({ message: this.$tc('omnicore-ai-import.messages.retryCreated') });
                this.startPolling();
            } catch (e) {
                this.createNotificationError({ message: this.errorMessage(e, 'retryFailed') });
            } finally {
                this.isImporting = false;
            }
        },

        async openDetail(item) {
            try {
                const client = await this.httpClient();
                const resp = await client.get(`/_action/omnicore/ai-import/candidates/${encodeURIComponent(item.sku)}`);
                this.detailItem = resp.data.item || item;
            } catch (e) {
                this.detailItem = item;
                this.createNotificationWarning({ message: this.errorMessage(e, 'detailFailed') });
            }
        },

        closeDetail() { this.detailItem = null; },

        errorMessage(e, key) {
            const detail = e?.response?.data?.message || e?.response?.data?.error || e?.message;
            return detail || this.$tc(`omnicore-ai-import.messages.${key}`);
        },
    },
});
