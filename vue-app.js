const { createApp, ref, computed, onMounted, onUnmounted, watch, nextTick, markRaw } = Vue;

const app = createApp({
    setup() {
        // 状态变量
        const loading = ref(false);
        const showLoginDialog = ref(false);
        const showRefreshIndicator = ref(false);
        const showAdjustmentDialog = ref(false);
        const adjusting = ref(false);
        const isAuthenticated = ref(false);
        const currentUser = ref(null);
        const accessToken = ref(null);

        // 数据变量
        const users = ref([]);
        const currentAccountId = ref('');
        const showAliasDialog = ref(false);
        const showUserSortDialog = ref(false);
        const userSortList = ref([]);
        const aliasForm = ref({
            account_id: '',
            alias: ''
        });
        const positionSearch = ref('');
        const strategyFilter = ref('');
        const hideClearedPositions = ref(false);

        // 页面宽度控制
        const containerMaxWidth = ref(parseInt(localStorage.getItem('container_max_width') || '2000'));
        watch(containerMaxWidth, (val) => localStorage.setItem('container_max_width', val));
        const showClearPositionsDialog = ref(false);
        const showClearPositionsTag = ref(false);
        const showSetClearPasswordDialog = ref(false);
        const showUserManagementDialog = ref(false);
        const activeUserTab = ref('clear_password');
        const showConfigDialog = ref(false);
        const configAccountId = ref('');
        const configContent = ref('');
        const configLoadTime = ref('');
        const configLoading = ref(false);
        const configSaving = ref(false);
        const llmConfigForm = ref({
            api_url: '',
            api_key: '',
            model: ''
        });
        const llmConfigLoading = ref(false);
        const llmConfigSaving = ref(false);
        const researchBoardInput = ref('');
        const researchBoardRecords = ref([]);
        const researchBoardPage = ref(1);
        const researchBoardPageSize = ref(50);
        const researchBoardSort = ref({ prop: 'created_at', order: 'descending' });
        const onResearchBoardSortChange = ({ prop, order }) => {
            researchBoardSort.value = { prop, order };
            researchBoardPage.value = 1;
        };
        // 自己接管排序：自动刷新替换数据数组时排序不会丢失
        const researchBoardSortedRecords = computed(() => {
            const all = (researchBoardRecords.value || []).slice();
            const { prop, order } = researchBoardSort.value || {};
            if (!prop || !order) return all;
            const dir = order === 'ascending' ? 1 : -1;
            return all.sort((a, b) => {
                const x = a[prop], y = b[prop];
                const xe = (x === null || x === undefined || x === '');
                const ye = (y === null || y === undefined || y === '');
                if (xe && ye) return 0;
                if (xe) return 1;
                if (ye) return -1;
                const nx = Number(x), ny = Number(y);
                if (Number.isFinite(nx) && Number.isFinite(ny)) return (nx - ny) * dir;
                return String(x).localeCompare(String(y)) * dir;
            });
        });
        const researchBoardPagedRecords = computed(() => {
            const all = researchBoardSortedRecords.value || [];
            const size = researchBoardPageSize.value;
            const maxPage = Math.max(1, Math.ceil(all.length / size));
            const page = Math.min(researchBoardPage.value, maxPage);
            const start = (page - 1) * size;
            return all.slice(start, start + size);
        });
        const getTScanRowKey = (row) => `${row?.account_id || ''}_${row?.stock_code || ''}_${row?.action || ''}`;
        const isTScanRowHandled = (row) => Boolean(tScanHandledRows.value[getTScanRowKey(row)]);
        const isTScanRowQueued = (row) => Boolean(tScanQueuedRows.value[getTScanRowKey(row)]);
        const isTScanRowProcessing = (row) => Boolean(tScanProcessingRows.value[getTScanRowKey(row)]);
        const clearTScanRowState = (stateRef, key) => {
            const nextState = { ...stateRef.value };
            delete nextState[key];
            stateRef.value = nextState;
        };
        const markTScanRowHandled = (row, side) => {
            const key = getTScanRowKey(row);
            if (!key.trim()) return;
            tScanHandledRows.value = {
                ...tScanHandledRows.value,
                [key]: {
                    side,
                    submitted_at: Date.now()
                }
            };
        };
        const tScanVisibleRows = computed(() => {
            const activeRows = [];
            const handledRows = [];
            (tScanRows.value || []).forEach((row, index) => {
                const key = getTScanRowKey(row);
                const viewRow = {
                    ...row,
                    _scan_index: index,
                    is_handled: Boolean(tScanHandledRows.value[key]),
                    is_queued: Boolean(tScanQueuedRows.value[key]),
                    is_processing: Boolean(tScanProcessingRows.value[key])
                };
                if (viewRow.is_handled) {
                    handledRows.push(viewRow);
                } else {
                    activeRows.push(viewRow);
                }
            });
            const orderedRows = activeRows.concat(handledRows);
            if (!tScanOnlyCandidates.value) return orderedRows;
            return orderedRows.filter(row => row.is_candidate && !row.is_handled);
        });
        const researchBoardLoading = ref(false);
        const researchBoardParsing = ref(false);
        const researchBoardRepairing = ref(false);
        const showResearchBoardInputsDialog = ref(false);
        const researchBoardInputs = ref([]);
        const researchBoardInputsLoading = ref(false);
        const researchBoardTaskMessage = ref('');
        const showResearchBoardLargeDialog = ref(false);
        const showResearchBoardEditDialog = ref(false);
        const researchBoardEditing = ref(false);
        const showTScanDialog = ref(false);
        const tScanRows = ref([]);
        const tScanSummary = ref({});
        const tScanTradeDate = ref('');
        const tScanScanTime = ref('');
        const tScanLoading = ref(false);
        const tScanOnlyCandidates = ref(true);
        const tScanHandledRows = ref({});
        const tScanQueuedRows = ref({});
        const tScanProcessingRows = ref({});
        const tScanCommandQueue = ref([]);
        const tScanQueueRunning = ref(false);
        let tScanQueueCursor = 0;
        const researchBoardEditForm = ref({
            id: null,
            stock_name: '',
            stock_code: '',
            logic: '',
            target_market_value_yi: null,
            industry: '',
            concept: '',
            limit_up_reason: '',
            topic: ''
        });
        let researchBoardPollTimer = null;
        let researchBoardAutoTimer = null;

        // 买卖流水（账户真实成交）
        const tradeFlowRecords = ref([]);
        const tradeFlowPending = ref([]);      // 已下单未成交，可撤
        const cancellingOrders = ref(new Set());
        const tradeFlowSummary = ref({});
        const tradeFlowLoading = ref(false);
        const tradeFlowSide = ref('all');
        const tradeFlowDays = ref(7);
        const tradeFlowQuery = ref('');

        let tradeFlowTimer = null;


        // 记录板 K 线 AI 分析
        const showKlineAnalysisDialog = ref(false);
        const klineAnalysisLoading = ref(false);
        const klineAnalysisData = ref({
            stock_name: '',
            stock_code: '',
            source: '',
            analysis: '',
            stats: null,
            bars: []
        });
        const clearPositionsConfirmText = ref('');
        const clearPositionsPassword = ref('');
        const clearPositionsPercentage = ref(100);
        // 所有账号一键清仓
        const showClearAllDialog = ref(false);
        const clearAllConfirmText = ref('');
        const clearAllPassword = ref('');
        const clearAllPercentage = ref(100);
        const setClearPasswordForm = ref({
            password: '',
            confirmPassword: ''
        });
        const changeLoginPasswordForm = ref({
            oldPassword: '',
            newPassword: '',
            confirmPassword: ''
        });
        const newUserForm = ref({
            username: '',
            password: '',
            account_id: '',
            account_name: '',
            role: 'user'
        });
        const adminUsersList = ref([]);
        const tradeFactorsLoading = ref(false);
        const tradeFactorsSaving = ref(false);
        const tradeFactorsMixed = ref(false);
        const tradeFactorsMixedFields = ref([]);
        const tradeFactorsAccountCount = ref(0);
        const tradeFactorForm = ref({
            positionFactor: 1.0,
            openCountFactor: 1.0
        });

        // 卖出状态管理（内存中，不持久化）
        const showSellPositionDialog = ref(false);
        const sellPositionStock = ref(null);
        const sellPositionPercentage = ref(100);
        const pendingSellPositions = ref(new Map()); // key: account_id_stock_code, value: {stock_code, percentage}

        // 停止交易状态（拆分为买入/卖出独立控制）
        const buyStopped = ref(false);
        const sellStopped = ref(false);
        const tradingStopped = computed(() => buyStopped.value && sellStopped.value);
        const tradingStatusLoading = ref(false);
        // 买入状态管理（内存中，不持久化）
        const showBuyPositionDialog = ref(false);
        const buyPositionStock = ref(null);
        const buyPositionPercentage = ref(100);
        const pendingBuyPositions = ref(new Map()); // key: account_id_stock_code, value: {stock_code, percentage}

        // 新买入股票对话框状态
        const showNewBuyDialog = ref(false);
        const newBuyStockQuery = ref('');
        const newBuyStockOptions = ref([]);
        const newBuySelectedStock = ref(null);
        const newBuyAmount = ref(100);

        // ===== 报价方式与价格笼子（三个下单弹窗共用）=====
        // 弹窗是模态的，同时只会开一个，所以一套状态够用。
        const orderPriceType = ref('latest');
        const orderLimitPrice = ref(0);
        const orderQuote = ref(null);          // /api/instrument 的返回
        const orderQuoteLoading = ref(false);
        const orderQuoteSide = ref('buy');
        const orderAccountId = ref('');
        const orderTradeMode = ref('');

        const loadOrderQuote = async (code, side) => {
            orderQuoteSide.value = side || 'buy';
            orderQuote.value = null;
            if (!code) return;
            orderQuoteLoading.value = true;
            try {
                // 带上账号和方向：买卖指令类型看的是账户类型，而服务端还会按
                // （人、账号、方向）把上次用过的选择当默认值送回来
                //
                // 汇总视图要的是「不带账号」，但这个值有两个来源，大小写不一样：
                // 账号选择器给的是小写 'all'，而汇总持仓行的 account_id 是后端
                // SQL 里拼出来的大写 'ALL'（app.py 的 "'ALL' as account_id"）。
                // 只挡小写的话，从汇总视图点卖出会把 account_id=ALL 真发出去，
                // 服务端查不到这个账号：账户类型退回 STOCK（信用账户的融资融券
                // 就从选择器里消失了），偏好也匹配不上（记忆功能整个失效）。
                // 两个都是静默降级，界面上看不出来 —— 而汇总正是管理员的默认视图。
                const params = new URLSearchParams();
                const accountId = String(orderAccountId.value || '');
                if (accountId && accountId.toLowerCase() !== 'all') {
                    params.set('account_id', accountId);
                }
                params.set('side', orderQuoteSide.value);
                const resp = await fetch(
                    `/api/instrument/${encodeURIComponent(code)}?${params}`, {
                    headers: { 'Authorization': `Bearer ${accessToken.value}` }
                });
                if (resp.ok) {
                    orderQuote.value = (await resp.json()).instrument;
                    orderTradeMode.value = (orderQuote.value
                        && orderQuote.value.default_trade_mode) || '';
                    orderPriceType.value = (orderQuote.value
                        && orderQuote.value.default_price_type) || 'latest';
                    // 限价默认填最新价，用户改之前至少是个合法值。
                    // 放在报价方式之后：上面那一行会触发 orderPriceRole 的 watcher，
                    // 它会把保护限价归零，先填价的话会被抹掉。
                    await nextTick();
                    if (orderQuote.value && orderQuote.value.last_price
                            && orderPriceRole.value === 'order') {
                        orderLimitPrice.value = orderQuote.value.last_price;
                    }
                }
            } catch (e) {
                console.error('获取报价信息失败', e);
            } finally {
                orderQuoteLoading.value = false;
            }
        };

        const resetOrderPricing = (code, side, accountId) => {
            orderPriceType.value = 'latest';
            orderLimitPrice.value = 0;
            orderTradeMode.value = '';
            orderAccountId.value = accountId || '';
            loadOrderQuote(code, side);
        };

        // 买卖指令类型。普通账户只有「普通买卖」一条，那就不要占一行界面；
        // 信用账户才有担保品 / 融资融券 / 还券还款的区别，而那个区别必须由人来选 ——
        // 推错了就是一笔真实但业务类型不同的单。
        // 名称跟着方向走：卖出弹窗上写「担保品买入」是个会让人下错单的显示。
        const orderTradeModes = computed(() => {
            const field = orderQuoteSide.value === 'sell' ? 'sell_label' : 'buy_label';
            return ((orderQuote.value && orderQuote.value.trade_modes) || [])
                .map(m => Object.assign({}, m, { side_label: m[field] || m.label }));
        });
        const orderTradeModeVisible = computed(() => orderTradeModes.value.length > 1);
        const orderTradeModeHint = computed(() => {
            const mode = orderTradeModes.value.find(m => m.value === orderTradeMode.value);
            return mode ? mode.hint : '';
        });

        const orderPriceTypes = computed(() =>
            (orderQuote.value && orderQuote.value.price_types) || [
                { value: 'latest', label: '最新价', group: '常用', price_role: 'none',
                  needs_price: false, accepts_price: false, hint: '按当前最新成交价报' },
                { value: 'fix', label: '限价', group: '常用', price_role: 'order',
                  needs_price: true, accepts_price: true, hint: '自己指定价格，受价格笼子约束' },
            ]);

        // 按分组切开给 el-option-group 用。二十来个选项排一行 radio 是看不了的，
        // 而且「常用」以外的（盘口档位、分交易所的市价指令）平时不该占视线。
        const orderPriceTypeGroups = computed(() => {
            const order = (orderQuote.value && orderQuote.value.price_type_groups)
                       || ['常用', '一档', '市价', '档位', '盘后'];
            const buckets = new Map();
            orderPriceTypes.value.forEach(t => {
                const g = t.group || '其他';
                if (!buckets.has(g)) buckets.set(g, []);
                buckets.get(g).push(t);
            });
            const known = order.filter(g => buckets.has(g));
            const rest = [...buckets.keys()].filter(g => !order.includes(g));
            return [...known, ...rest].map(g => ({ label: g, items: buckets.get(g) }));
        });

        const orderPriceSpec = computed(() =>
            orderPriceTypes.value.find(t => t.value === orderPriceType.value) || null);

        // price 字段有三种语义，界面得跟着变，否则用户不知道自己填的是什么价：
        //   order   —— 就是委托价，必填，受笼子约束
        //   protect —— 保护限价，选填，留空按涨跌停价保护
        //   none    —— 与本次委托无关，不显示输入框
        const orderPriceRole = computed(() =>
            (orderPriceSpec.value && orderPriceSpec.value.price_role) || 'none');
        const orderNeedsPrice = computed(() => orderPriceRole.value === 'order');
        const orderAcceptsPrice = computed(() => orderPriceRole.value !== 'none');
        const orderPriceLabel = computed(() =>
            orderPriceRole.value === 'protect' ? '保护限价（选填）' : '委托价');
        const orderPriceHint = computed(() => {
            const spec = orderPriceSpec.value;
            if (!spec) return '';
            if (orderPriceRole.value === 'protect') {
                return `${spec.hint || ''}${spec.hint ? '；' : ''}保护限价留空则按涨跌停价保护`;
            }
            return spec.hint || '';
        });

        // 当前方向的价格笼子。取不到就是 null，界面上显示「—」而不是编一个范围。
        const orderCage = computed(() => {
            const cage = orderQuote.value && orderQuote.value.price_cage;
            if (!cage) return null;
            return cage[orderQuoteSide.value] || cage.buy || null;
        });

        const orderCageText = computed(() => {
            const cage = orderCage.value;
            if (!cage) return '';
            if (cage.low === null || cage.high === null) {
                return cage.reason || '价格笼子不可用';
            }
            const label = { auction: '集合竞价', continuous: '连续竞价', closed: '非交易时段' }[cage.session] || cage.session;
            const decimals = (orderQuote.value && orderQuote.value.price_decimals) || 2;
            return `${label} · 有效申报 ${cage.low.toFixed(decimals)} ~ ${cage.high.toFixed(decimals)}`
                 + `（基准 ${Number(cage.base_price).toFixed(decimals)} ±${(cage.band * 100).toFixed(0)}%）`;
        });

        // 限价是否落在笼子里。服务端还会再判一次，这里只是别让用户白填一次。
        // 只管 order 语义的价：市价指令的保护限价不走笼子，交易所自己按对手价成交。
        const orderPriceError = computed(() => {
            if (!orderNeedsPrice.value) return '';
            const price = Number(orderLimitPrice.value);
            if (!price || price <= 0) return '限价委托必须填价格';
            const cage = orderCage.value;
            if (!cage || cage.low === null || cage.high === null) return '';
            const decimals = (orderQuote.value && orderQuote.value.price_decimals) || 2;
            if (price < cage.low) return `低于有效申报下限 ${cage.low.toFixed(decimals)}`;
            if (price > cage.high) return `高于有效申报上限 ${cage.high.toFixed(decimals)}`;
            return '';
        });

        // 换报价方式时给一个合理的默认价：
        //   限价 —— 填最新价，用户至少有个合法起点
        //   保护限价 —— 留 0，即「按涨跌停价保护」。这里不能沿用最新价：
        //               市价单挂着一个最新价的保护限价，行情一动就成不了。
        watch(orderPriceRole, (role) => {
            if (role === 'protect') {
                orderLimitPrice.value = 0;
            } else if (role === 'order' && !Number(orderLimitPrice.value)) {
                orderLimitPrice.value = (orderQuote.value && orderQuote.value.last_price) || 0;
            }
        });

        // 附加到下单请求上的报价参数
        const orderPricingPayload = () => ({
            price_type: orderPriceType.value,
            price: orderAcceptsPrice.value ? Number(orderLimitPrice.value) || 0 : 0,
            trade_mode: orderTradeMode.value || '',
        });

        const orderPriceStep = computed(() =>
            (orderQuote.value && orderQuote.value.price_tick) || 0.01);

        // 品种规则：与后端 bridge/instruments.py 保持一致。
        // 放在前端是为了让数量步进和小数位跟着选中的标的立刻变，不必每次按键打一次接口。
        // 真正的校验仍在服务端 —— 这里只是别让用户填出一个注定被拒的数。
        const instrumentSpec = (code) => {
            const raw = String(code || '').trim().toUpperCase();
            const num = raw.split('.')[0];
            const suffix = raw.includes('.') ? raw.split('.')[1] : '';
            const isSH = suffix === 'SH' || (!suffix && /^(110|111|113|118|132|100|5|6)/.test(num));
            const isBJ = suffix === 'BJ' || (!suffix && /^(4|8|920)/.test(num));
            if ((isSH && /^(110|111|113|118|132|100)/.test(num)) ||
                (!isSH && !isBJ && /^12/.test(num))) {
                return { kind: 'bond', kindName: '可转债', unit: '张',
                         min: 10, step: 10, precision: 3, t0: true };
            }
            if (isSH && /^(688|689)/.test(num)) {
                return { kind: 'star', kindName: '科创板', unit: '股',
                         min: 200, step: 1, precision: 2, t0: false };
            }
            if (isBJ) {
                return { kind: 'bj', kindName: '北交所', unit: '股',
                         min: 100, step: 1, precision: 2, t0: false };
            }
            if ((isSH && /^5/.test(num)) || (!isSH && !isBJ && /^(15|16|18)/.test(num))) {
                return { kind: 'etf', kindName: 'ETF/LOF', unit: '份',
                         min: 100, step: 100, precision: 3, t0: false };
            }
            return { kind: 'stock', kindName: '股票', unit: '股',
                     min: 100, step: 100, precision: 2, t0: false };
        };

        const newBuySpec = computed(() =>
            instrumentSpec(newBuySelectedStock.value && newBuySelectedStock.value.ts_code));

        // 下单方式：'volume' 按数量 / 'cash' 按金额
        const newBuyMode = ref('volume');
        const newBuyCash = ref(10000);
        // 服务端算出来的换算结果。前端不自己算 —— 只有服务端同时握着实时价和品种
        // 规整规则，自己算出来的数会和实际报单量对不上。
        const newBuyQuote = ref(null);
        const newBuyQuoteLoading = ref(false);

        let quoteTimer = null;
        const refreshNewBuyQuote = () => {
            const stock = newBuySelectedStock.value;
            if (!stock || !stock.ts_code) { newBuyQuote.value = null; return; }
            if (quoteTimer) clearTimeout(quoteTimer);
            quoteTimer = setTimeout(async () => {
                const params = newBuyMode.value === 'cash'
                    ? `cash_amount=${newBuyCash.value || 0}`
                    : `volume=${newBuyAmount.value || 0}`;
                newBuyQuoteLoading.value = true;
                try {
                    const resp = await fetch(`/api/instrument/${encodeURIComponent(stock.ts_code)}?${params}`, {
                        headers: { 'Authorization': `Bearer ${accessToken.value}` }
                    });
                    newBuyQuote.value = resp.ok ? (await resp.json()).instrument : null;
                } catch (e) {
                    console.error('获取品种/报价失败', e);
                    newBuyQuote.value = null;
                } finally {
                    newBuyQuoteLoading.value = false;
                }
            }, 300);
        };

        watch([newBuySelectedStock, newBuyMode, newBuyAmount, newBuyCash], () => {
            // 换了标的就把数量重置到该品种的最小申报量，免得留着上一个品种的数
            refreshNewBuyQuote();
        });

        watch(newBuySelectedStock, (stock) => {
            const spec = instrumentSpec(stock && stock.ts_code);
            if (newBuyAmount.value < spec.min) newBuyAmount.value = spec.min;
            loadOrderQuote(stock && stock.ts_code, 'buy');
        });

        // 按金额模式下，实际会报出去的数量
        const newBuyResolvedVolume = computed(() => {
            if (newBuyMode.value !== 'cash') return newBuyAmount.value;
            return (newBuyQuote.value && newBuyQuote.value.volume_for_cash) || 0;
        });

        const newBuyEstimatedCash = computed(() =>
            (newBuyQuote.value && newBuyQuote.value.estimated_cash) || null);

        const newBuyLastPrice = computed(() =>
            (newBuyQuote.value && newBuyQuote.value.last_price) || null);

        const newBuyCashError = computed(() =>
            (newBuyQuote.value && newBuyQuote.value.cash_error) || '');

        // 表格列配置
        const positionColumns = ref([
            { prop: 'is_locked', label: '锁定', width: '50', align: 'center' },
            { prop: 'sell_action', label: '卖出', width: '50', align: 'center' },
            { prop: 'buy_action', label: '买入', width: '50', align: 'center' },
            { prop: 't0_action', label: '做T', width: '50', align: 'center' },
            { prop: 'delete_action', label: '删除', width: '50', align: 'center' },
            { prop: 'instrument_name', label: '名称', minWidth: '100' },
            { prop: 'stock_code', label: '代码', width: '80' },
            { prop: 'mini_chart', label: '走势图', width: '120', align: 'center' },
            { prop: 'can_use_volume', label: '可用', width: '80', align: 'right' },
            { prop: 'volume', label: '持仓', width: '90', align: 'right' },
            { prop: 'avg_price', label: '成本', width: '90', align: 'right' },
            { prop: 'last_price', label: '现价', width: '90', align: 'right' },
            { prop: 'market_value', label: '市值', width: '100', align: 'right' },
            { prop: 'float_profit', label: '盈亏', width: '100', align: 'right' },
            { prop: 'profit_rate', label: '盈亏率', width: '100', align: 'right' },
            { prop: 'current_change', label: '当日幅', width: '100', align: 'right' },
            { prop: 'change_5min', label: '5分幅', width: '100', align: 'right' },
            { prop: 'today_buy_amount', label: '净买入', width: '90', align: 'right' },
            { prop: 'today_sell_amount', label: '净卖出', width: '90', align: 'right' },
            { prop: 'today_net_amount', label: '日差额', width: '90', align: 'right' },
            { prop: 'today_t_profit', label: 'T盈利', width: '90', align: 'right' },
            { prop: 'topic_reason', label: '题材', minWidth: '80' },
            { prop: 'bond_premium', label: '溢价率', width: '90', align: 'right' },
            { prop: 'bond_value', label: '转股价值', width: '96', align: 'right' },
            { prop: 'bond_redeem', label: '强赎', width: '86', align: 'center' }
        ]);

        // 展示模式：只看持仓、隐藏所有买入/卖出/锁定/做T/删除/清仓等操作（按浏览器持久化，互不影响其它客户端）
        // 展示模式是「服务端全局开关」，由 /api/data 返回的 display_mode 同步；仅管理员可切换
        const displayMode = ref(false);
        const displayModeToggling = ref(false);
        const OPERATION_COLUMN_PROPS = ['is_locked', 'sell_action', 'buy_action', 't0_action', 'delete_action'];
        const visiblePositionColumns = computed(() =>
            displayMode.value
                ? positionColumns.value.filter(c => !OPERATION_COLUMN_PROPS.includes(c.prop))
                : positionColumns.value
        );

        // 切换服务端展示模式（仅管理员）：开启后服务端不再下发买卖/锁仓/清仓指令
        const toggleDisplayMode = async (val) => {
            if (currentUser.value?.role !== 'admin') {
                ElementPlus.ElMessage.warning('仅管理员可切换展示模式');
                return;
            }
            const prev = displayMode.value;
            displayMode.value = val;          // 乐观更新
            displayModeToggling.value = true;
            try {
                const response = await fetch('/api/display-mode', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json', 'Authorization': `Bearer ${accessToken.value}` },
                    body: JSON.stringify({ enabled: val })
                });
                const data = await response.json().catch(() => ({}));
                if (response.ok) {
                    displayMode.value = !!data.enabled;
                    ElementPlus.ElMessage.success(data.message || (val ? '展示模式已开启' : '展示模式已关闭'));
                } else {
                    displayMode.value = prev;
                    ElementPlus.ElMessage.error(data.detail || '切换失败');
                }
            } catch (e) {
                displayMode.value = prev;
                ElementPlus.ElMessage.error('切换请求失败');
            } finally {
                displayModeToggling.value = false;
            }
        };

        // 走势图相关
        const marketMinData = ref({});
        const lastCloseData = ref({});   // 昨收价: { ts_code: price }
        const sparklineCharts = {};
        const popoverCharts = {};
        let sparklineRenderTimer = null;

        const renderPopoverChart = (canvasId, code) => {
            nextTick(() => {
                const canvas = document.getElementById(canvasId);
                if (!canvas) return;
                
                const prices = marketMinData.value[code];
                if (!prices || prices.length < 2) return;

                if (popoverCharts[canvasId]) {
                    popoverCharts[canvasId].destroy();
                }

                // 取昨收价作为基准，若无则退化为第一分钟价格（开盘价）
                const basePrice = lastCloseData.value[code] || prices[0] || 1;
                const lastPrice = prices[prices.length - 1];
                const isUp = lastPrice >= basePrice;
                const chartColor = isUp ? '#f56c6c' : '#67c23a';
                const labels = prices.map((_, index) => index.toString());

                // 转换为相对昨收价的涨跌幅 %
                const pctData = prices.map(p => parseFloat(((p - basePrice) / basePrice * 100).toFixed(3)));

                popoverCharts[canvasId] = new Chart(canvas, {
                    type: 'line',
                    data: {
                        labels: labels,
                        datasets: [
                            {
                                data: pctData,
                                borderColor: chartColor,
                                backgroundColor: chartColor + '22',
                                borderWidth: 2,
                                fill: 'origin',
                                pointRadius: 0,
                                pointHoverRadius: 4,
                                tension: 0.1,
                                order: 1
                            }
                        ]
                    },
                    options: {
                        responsive: true,
                        maintainAspectRatio: false,
                        animation: { duration: 300 },
                        interaction: { mode: 'index', intersect: false },
                        plugins: {
                            legend: { display: false },
                            tooltip: { 
                                enabled: true,
                                callbacks: {
                                    title: () => '',
                                    label: function(context) {
                                        const pct = context.parsed.y;
                                        const idx = context.dataIndex;
                                        const price = prices[idx];
                                        const sign = pct >= 0 ? '+' : '';
                                        return [`涨跌幅: ${sign}${pct.toFixed(2)}%  (昨收: ${basePrice.toFixed(2)})`, `价格: ${price.toFixed(2)}`];
                                    }
                                }
                            }
                        },
                        scales: {
                            x: { display: false },
                            y: { 
                                display: true,
                                position: 'right',
                                grid: { color: 'rgba(255,255,255,0.05)' },
                                ticks: {
                                    color: 'rgba(255,255,255,0.5)',
                                    font: { size: 10 },
                                    callback: (v) => (v >= 0 ? '+' : '') + v.toFixed(2) + '%'
                                }
                            }
                        },
                        layout: { padding: { left: 0, right: 0, top: 10, bottom: 10 } }
                    }
                });
            });
        };

        const destroyPopoverChart = (canvasId) => {
            if (popoverCharts[canvasId]) {
                popoverCharts[canvasId].destroy();
                delete popoverCharts[canvasId];
            }
        };

        const renderSparklines = async () => {
            await Vue.nextTick();
            positions.value.forEach(row => {
                try {
                    const code = row.stock_code;
                    const accountId = row.account_id || 'ALL';
                    const rawPrices = marketMinData.value[code];
                    if (!rawPrices || rawPrices.length < 2) return;

                    const canvasId = 'chart-' + accountId + '-' + code.replace(/\./g, '-');
                    const canvas = document.getElementById(canvasId);
                    if (!canvas) return;

                    // 优先用“最新推送的持仓价”作为走势末点，与当日涨跌幅(也用 last_price)保持一致
                    const prices = rawPrices.slice();
                    if (row.last_price != null && Number(row.last_price) > 0) {
                        prices[prices.length - 1] = Number(row.last_price);
                    }

                    const basePrice = lastCloseData.value[code] || prices[0] || 1;
                    const isUp = prices[prices.length - 1] >= basePrice;
                    const chartColor = isUp ? '#f56c6c' : '#67c23a';
                    const labels = prices.map((_, i) => i.toString());

                    // 复用已有图表：canvas 未变则只更新数据，避免每次销毁重建（大幅降低刷新开销）
                    const existing = sparklineCharts[canvasId];
                    if (existing && existing.canvas === canvas) {
                        existing.data.labels = labels;
                        existing.data.datasets[0].data = prices;
                        existing.data.datasets[0].borderColor = chartColor;
                        existing.update('none');
                        return;
                    }
                    if (existing) existing.destroy();

                    sparklineCharts[canvasId] = new Chart(canvas, {
                        type: 'line',
                        data: {
                            labels: labels,
                            datasets: [{
                                data: prices,
                                borderColor: chartColor,
                                borderWidth: 1.5,
                                pointRadius: 0,
                                pointHoverRadius: 0,
                                fill: false,
                                tension: 0.1
                            }]
                        },
                        options: {
                            responsive: true,
                            maintainAspectRatio: false,
                            animation: false,
                            plugins: {
                                legend: { display: false },
                                tooltip: { 
                                    enabled: false
                                }
                            },
                            scales: {
                                x: { display: false },
                                y: { display: false }
                            },
                            layout: { padding: { left: 0, right: 0, top: 2, bottom: 2 } }
                        }
                    });
                } catch (e) {
                    console.error("渲染迷你图异常: ", e);
                }
            });
        };

        const scheduleRenderSparklines = () => {
            if (sparklineRenderTimer) clearTimeout(sparklineRenderTimer);
            sparklineRenderTimer = setTimeout(() => {
                sparklineRenderTimer = null;
                renderSparklines();
            }, 50);
        };

        // 加载保存的列顺序
        const loadSavedColumnOrder = () => {
            const saved = localStorage.getItem('position_column_order');
            const currentVersion = 'v6'; // 列配置版本号，修改列时更新
            const savedVersion = localStorage.getItem('position_column_version');

            // 如果版本变化或没有保存的列顺序，使用默认列顺序并保存
            if (!saved || savedVersion !== currentVersion) {
                localStorage.setItem('position_column_order', JSON.stringify(positionColumns.value.map(c => c.prop)));
                localStorage.setItem('position_column_version', currentVersion);
                // 使用默认列顺序，不需要修改 positionColumns.value
                return;
            }

            if (saved) {
                try {
                    const order = JSON.parse(saved);
                    const newColumns = [];
                    // 按照保存的顺序重新排列列
                    order.forEach(prop => {
                        const col = positionColumns.value.find(c => c.prop === prop);
                        if (col) newColumns.push(col);
                    });
                    // 添加新增加的列（如果之前没保存过）
                    positionColumns.value.forEach(col => {
                        if (!order.includes(col.prop)) newColumns.push(col);
                    });
                    positionColumns.value = newColumns;
                } catch (e) {
                    console.error('加载列顺序失败:', e);
                }
            }
        };

        const initColumnSortable = () => {
            const table = document.querySelector('.position-table .el-table__header-wrapper tr');
            if (!table) return;

            Sortable.create(table, {
                animation: 150,
                delay: 0,
                onEnd: (evt) => {
                    const { oldIndex, newIndex } = evt;
                    if (oldIndex === newIndex) return;

                    // 获取当前列顺序（排除固定列等特殊情况的影响，这里假设都是普通列）
                    const columns = [...positionColumns.value];
                    const currRow = columns.splice(oldIndex, 1)[0];
                    columns.splice(newIndex, 0, currRow);
                    
                    positionColumns.value = columns;
                    
                    // 保存新顺序
                    const order = columns.map(c => c.prop);
                    localStorage.setItem('position_column_order', JSON.stringify(order));
                    
                    // 强制重新渲染表格（Element Plus Table 有时需要这个来同步 DOM）
                    ElementPlus.ElMessage.success('列顺序已保存');
                }
            });
        };

        const getLocalDateStr = (date = new Date()) => {
            const year = date.getFullYear();
            const month = String(date.getMonth() + 1).padStart(2, '0');
            const day = String(date.getDate()).padStart(2, '0');
            return `${year}-${month}-${day}`;
        };
        const selectedDate = ref(getLocalDateStr());
        const isTodaySelected = computed(() => selectedDate.value === getLocalDateStr());
        const positions = ref([]);
        const trades = ref([]);
        const asset = ref({});
        const updateTime = ref('--');
        const lastUpdateTime = ref('');
        const lastUpdateTimestamp = ref(0);
        const secondsSinceUpdate = ref(0);
        const manualRefreshing = ref(false);

        // 图表相关
        const assetChart = ref(null);
        const positionPieChart = ref(null);
        const chartPeriod = ref(24);
        const chartType = ref('asset'); // 'asset' or 'rate'
        const rawChartData = ref([]);
        const dailyChartData = ref([]);
        const isChartLoading = ref(false);

        // UI状态
        const sortProp = ref('market_value');
        const sortOrder = ref('descending');
        const activeTradeTab = ref('today');
        const positionsHeight = ref(400);
        const calendarDays = ref([]);
        const calendarMonth = ref('');
        const calendarDate = ref(new Date());
        const calendarViewType = ref('amount'); // 'amount' or 'rate'
        const rawCalendarData = ref([]);
        const adjustments = ref([]);
        const adjustmentForm = ref({
            amount: 0,
            remark: '',
            date: ''
        });

        // 交易统计
        const todayBuyAmount = ref(0);
        const todaySellAmount = ref(0);
        const todayBuyCount = ref(0);
        const todaySellCount = ref(0);

        // 定时器
        let refreshTimer = null;

        // 登录表单
        const loginForm = ref({
            username: '',
            password: ''
        });

        // ===== 观察者(viewer)体系：独立注册/登录，只看历史买入列表 =====
        const loginMode = ref('operator');            // 'operator' | 'viewer'
        const isViewer = ref(false);                  // 当前登录身份是否为观察者
        const viewerUsername = ref('');
        const viewerForm = ref({ username: '', password: '' });
        const viewerAuthLoading = ref(false);
        const viewerNotifyOn = ref(false);            // 是否开启"新报出"浏览器通知
        let viewerHeartbeatTimer = null;              // 在线时长心跳定时器
        const marketNotifySeen = new Set();           // 已通知过的报出（stock_code|update_time）
        let marketNotifyInitialized = false;          // 首次加载只做基线、不弹通知

        // 计算属性
        const currentAccount = computed(() => {
            if (currentAccountId.value === 'all') {
                return { account_id: 'all', alias: '所有账号' };
            }
            return users.value.find(u => u.account_id === currentAccountId.value) || {};
        });

        // 根据自定义顺序排序后的用户列表
        const sortedUsers = computed(() => {
            if (!users.value.length) return [];
            
            // 获取保存的顺序
            const savedOrder = localStorage.getItem('user_account_order');
            const orderMap = savedOrder ? JSON.parse(savedOrder) : {};
            
            // 分离 "all" 选项和其他用户
            const allOption = users.value.find(u => u.account_id === 'all');
            const otherUsers = users.value.filter(u => u.account_id !== 'all');
            
            // 根据保存的顺序排序其他用户
            const sortedOtherUsers = [...otherUsers].sort((a, b) => {
                const orderA = orderMap[a.account_id] ?? 999999;
                const orderB = orderMap[b.account_id] ?? 999999;
                return orderA - orderB;
            });
            
            // 组合结果：all 选项在最前面，然后是排序后的其他用户
            return allOption ? [allOption, ...sortedOtherUsers] : sortedOtherUsers;
        });

        const sortedPositions = computed(() => {
            let sorted = [...positions.value];
            
            // 过滤：是否隐藏清仓股票
            if (hideClearedPositions.value) {
                sorted = sorted.filter(p => !((p.volume === 0 || p.volume === '0') && (p.can_use_volume === 0 || p.can_use_volume === '0')));
            }

            const prop = sortProp.value;
            const order = sortOrder.value;

            if (!prop || !order) return sorted;

            sorted.sort((a, b) => {
                let valA = a[prop];
                let valB = b[prop];
                
                // 处理空值
                if (valA === undefined || valA === null) valA = -Infinity;
                if (valB === undefined || valB === null) valB = -Infinity;

                let res = 0;
                if (typeof valA === 'string' && typeof valB === 'string') {
                    res = valA.localeCompare(valB);
                } else {
                    res = valA - valB;
                }
                
                return order === 'ascending' ? res : -res;
            });

            return sorted;
        });

        const filteredPositions = computed(() => {
            if (!positionSearch.value) return sortedPositions.value;
            const search = positionSearch.value.toLowerCase();
            return sortedPositions.value.filter(p => 
                (p.instrument_name && p.instrument_name.toLowerCase().includes(search)) ||
                (p.stock_code && p.stock_code.toLowerCase().includes(search))
            );
        });

        const activePositionCount = computed(() => {
            return filteredPositions.value.filter(p => p.volume > 0).length;
        });

        watch(filteredPositions, () => {
            scheduleRenderSparklines();
        });

        const recentTrades = computed(() => {
            return trades.value
                .filter(trade => {
                    if (!trade.traded_time) return false;
                    const tradeDateStr = getLocalDateStr(new Date(trade.traded_time * 1000));
                    return tradeDateStr === selectedDate.value;
                })
                .slice(0, 50)
                .map(trade => {
                    let directionText = getTradeDirection(trade);
                    
                    return {
                        ...trade,
                        time: formatTime(trade.traded_time),
                        direction: directionText,
                        amount: (Number(trade.traded_price) || 0) * (Number(trade.traded_volume) || 0)
                    };
                });
        });

        const buyTrades = computed(() => {
            return trades.value
                .filter(trade => {
                    if (!trade.traded_time) return false;
                    const tradeDateStr = getLocalDateStr(new Date(trade.traded_time * 1000));
                    return tradeDateStr === selectedDate.value && getTradeDirection(trade) === '买入';
                })
                .map(trade => ({
                    ...trade,
                    time: formatTime(trade.traded_time),
                    amount: (Number(trade.traded_price) || 0) * (Number(trade.traded_volume) || 0)
                }));
        });

        const sellTrades = computed(() => {
            return trades.value
                .filter(trade => {
                    if (!trade.traded_time) return false;
                    const tradeDateStr = getLocalDateStr(new Date(trade.traded_time * 1000));
                    return tradeDateStr === selectedDate.value && getTradeDirection(trade) === '卖出';
                })
                .map(trade => ({
                    ...trade,
                    time: formatTime(trade.traded_time),
                    amount: (Number(trade.traded_price) || 0) * (Number(trade.traded_volume) || 0)
                }));
        });

        const availableStrategies = computed(() => {
            const strategies = new Set();
            trades.value.forEach(t => {
                if (t.strategy_name) strategies.add(t.strategy_name);
            });
            return Array.from(strategies).sort();
        });

        const filteredRecentTrades = computed(() => {
            let result = recentTrades.value;
            if (strategyFilter.value) {
                result = result.filter(t => t.strategy_name === strategyFilter.value);
            }
            return result;
        });

        const filteredBuyTrades = computed(() => {
            let result = buyTrades.value;
            if (strategyFilter.value) {
                result = result.filter(t => t.strategy_name === strategyFilter.value);
            }
            return result;
        });

        const filteredSellTrades = computed(() => {
            let result = sellTrades.value;
            if (strategyFilter.value) {
                result = result.filter(t => t.strategy_name === strategyFilter.value);
            }
            return result;
        });

        const totalProfit = ref(0);
        const totalProfitRate = ref(0);
        const monthProfit = ref(0);
        const monthProfitRate = ref(0);

        // 做T配置对话框可选账号列表（排除 "all" 汇总视图）
        const configTargetAccounts = computed(() => {
            return users.value.filter(u => u.account_id !== 'all');
        });

        const systemStatus = computed(() => {
            if (!lastUpdateTimestamp.value) return { text: '未同步', color: '#94a3b8' };
            
            if (secondsSinceUpdate.value < 60) {
                return { text: '在线', color: 'var(--loss-color)' };
            } else if (secondsSinceUpdate.value < 300) {
                return { text: '延迟', color: '#eab308' };
            } else {
                return { text: '离线', color: 'var(--profit-color)' };
            }
        });

        // 定时更新秒数
        let secondsTimer = null;
        onMounted(() => {
            secondsTimer = setInterval(() => {
                if (lastUpdateTimestamp.value) {
                    secondsSinceUpdate.value = Math.floor((Date.now() - lastUpdateTimestamp.value) / 1000);
                }
            }, 1000);
        });
        onUnmounted(() => {
            if (secondsTimer) clearInterval(secondsTimer);
        });

        const todayProfit = ref(0);
        const todayProfitRate = ref(0);

        // 方法
        const getAccountTypeName = (type) => {
            if (currentAccountId.value === 'all') return '汇总';
            const types = {
                1: '股票',
                2: '期货',
                3: '股票期权',
                4: '黄金',
                5: '外汇'
            };
            return types[type] || '普通账户';
        };

        const formatMoney = (amount) => {
            const value = parseFloat(amount || 0);
            const absValue = Math.abs(value);
            if (absValue >= 10000) {
                return (value / 10000).toFixed(2) + '万';
            }
            return value.toFixed(2);
        };

        const formatPrice = (value) => {
            const num = Number(value);
            if (!Number.isFinite(num) || num <= 0) return '--';
            return num.toFixed(2);
        };

        const formatPercent = (value) => {
            if (value === null || value === undefined || value === '') return '--';
            const num = Number(value);
            if (!Number.isFinite(num)) return '--';
            return `${num >= 0 ? '+' : ''}${num.toFixed(2)}%`;
        };

        const formatYi = (value) => {
            if (value === null || value === undefined || value === '') return '--';
            const num = Number(value);
            if (!Number.isFinite(num)) return '--';
            return `${num.toFixed(2)}亿`;
        };

        const formatTime = (timestamp) => {
            if (!timestamp) return '--';
            return new Date(timestamp * 1000).toLocaleTimeString('zh-CN', {
                hour: '2-digit',
                minute: '2-digit'
            });
        };

        const isTodayTrade = (timestamp) => {
            if (!timestamp) return false;
            const tradeDate = new Date(timestamp * 1000);
            const today = new Date();
            return tradeDate.getFullYear() === today.getFullYear() &&
                   tradeDate.getMonth() === today.getMonth() &&
                   tradeDate.getDate() === today.getDate();
        };

        // 判断买卖方向：direction 可能是实际方向或订单状态，同时检查 direction 和 order_type
        const getTradeDirection = (trade) => {
            const dir = Number(trade.direction);
            // direction 如果是方向值（23/24），优先用
            if (dir === 23) return '买入';
            if (dir === 24) return '卖出';
            // direction 可能是订单状态（48~57），此时尝试用 order_type
            const oType = Number(trade.order_type);
            if (oType === 23) return '买入';
            if (oType === 24) return '卖出';
            return '未知';
        };

        const buildTodayTradeStatsMap = (tradeRows, todayStr) => {
            const statsByStock = new Map();
            if (!todayStr) return statsByStock;

            (tradeRows || []).forEach(t => {
                if (!t || !t.traded_time || !t.stock_code) return;
                const tradeDateStr = getLocalDateStr(new Date(t.traded_time * 1000));
                if (tradeDateStr !== todayStr) return;

                let statsByAccount = statsByStock.get(t.stock_code);
                if (!statsByAccount) {
                    statsByAccount = new Map();
                    statsByStock.set(t.stock_code, statsByAccount);
                }

                const accId = t.account_id;
                let stat = statsByAccount.get(accId);
                if (!stat) {
                    stat = { buy_vol: 0, sell_vol: 0, buy_amount: 0, sell_amount: 0 };
                    statsByAccount.set(accId, stat);
                }

                const vol = Number(t.traded_volume) || 0;
                const price = Number(t.traded_price) || 0;
                const currentAmount = price * vol;
                const direction = getTradeDirection(t);

                if (direction === '买入') {
                    stat.buy_vol += vol;
                    stat.buy_amount += currentAmount;
                } else if (direction === '卖出') {
                    stat.sell_vol += vol;
                    stat.sell_amount += currentAmount;
                }
            });

            return statsByStock;
        };

        const summarizeTodayTradeStats = (statsByStock, position) => {
            const statsByAccount = statsByStock.get(position.stock_code);
            const result = { buy_amount: 0, sell_amount: 0, t_profit: 0 };
            if (!statsByAccount) return result;

            const stats = position.account_id === 'ALL'
                ? Array.from(statsByAccount.values())
                : [statsByAccount.get(position.account_id)].filter(Boolean);

            stats.forEach(stat => {
                result.buy_amount += stat.buy_amount;
                result.sell_amount += stat.sell_amount;

                const tVol = Math.min(stat.buy_vol, stat.sell_vol);
                if (tVol > 0 && stat.buy_vol > 0 && stat.sell_vol > 0) {
                    const avgBuy = stat.buy_amount / stat.buy_vol;
                    const avgSell = stat.sell_amount / stat.sell_vol;
                    result.t_profit += (avgSell - avgBuy) * tVol;
                }
            });

            return result;
        };

        const checkAuth = () => {
            const token = localStorage.getItem('access_token');
            if (!token) return;
            accessToken.value = token;
            if (localStorage.getItem('auth_type') === 'viewer') {
                restoreViewerSession();   // 观察者会话
            } else {
                isAuthenticated.value = true;
                fetchCurrentUser();
            }
        };

        // ===== 观察者：会话恢复 / 进入观察者模式 / 注册 / 登录 / 心跳 / 通知 =====
        const restoreViewerSession = async () => {
            try {
                const resp = await fetch('/api/viewer/me', { headers: { 'Authorization': `Bearer ${accessToken.value}` } });
                if (resp.ok) {
                    const data = await resp.json();
                    enterViewerMode(data.username);
                } else {
                    clearAuth();
                }
            } catch (e) { clearAuth(); }
        };

        const enterViewerMode = (username) => {
            isAuthenticated.value = true;
            isViewer.value = true;
            viewerUsername.value = username || '';
            showLoginDialog.value = false;
            currentAccountId.value = '';          // 观察者无交易账号
            marketNotifySeen.clear();
            marketNotifyInitialized = false;      // 登录后首帧只做基线
            viewerNotifyOn.value = ('Notification' in window) && Notification.permission === 'granted' && localStorage.getItem('viewer_notify') === '1';
            loadTradeFlow(false);
            startViewerHeartbeat();
        };

        const viewerRegister = async () => {
            const u = (viewerForm.value.username || '').trim();
            if (!u || !viewerForm.value.password) { ElementPlus.ElMessage.warning('请输入用户名和密码'); return; }
            viewerAuthLoading.value = true;
            try {
                const resp = await fetch('/api/viewer/register', {
                    method: 'POST', headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ username: u, password: viewerForm.value.password })
                });
                const data = await resp.json().catch(() => ({}));
                if (resp.ok) { ElementPlus.ElMessage.success('注册成功，正在登录...'); await viewerLogin(); }
                else ElementPlus.ElMessage.error(data.detail || '注册失败');
            } catch (e) { ElementPlus.ElMessage.error('注册请求出错'); }
            finally { viewerAuthLoading.value = false; }
        };

        const viewerLogin = async () => {
            const u = (viewerForm.value.username || '').trim();
            if (!u || !viewerForm.value.password) { ElementPlus.ElMessage.warning('请输入用户名和密码'); return; }
            viewerAuthLoading.value = true;
            try {
                const formData = new URLSearchParams();
                formData.append('username', u);
                formData.append('password', viewerForm.value.password);
                const resp = await fetch('/api/viewer/token', {
                    method: 'POST', headers: { 'Content-Type': 'application/x-www-form-urlencoded' }, body: formData
                });
                const data = await resp.json().catch(() => ({}));
                if (resp.ok) {
                    accessToken.value = data.access_token;
                    localStorage.setItem('access_token', data.access_token);
                    localStorage.setItem('auth_type', 'viewer');
                    enterViewerMode(data.username);
                    ElementPlus.ElMessage.success('登录成功');
                } else {
                    ElementPlus.ElMessage.error(data.detail || '用户名或密码错误');
                }
            } catch (e) { ElementPlus.ElMessage.error('登录失败'); }
            finally { viewerAuthLoading.value = false; }
        };

        const startViewerHeartbeat = () => {
            stopViewerHeartbeat();
            const beat = () => {
                if (!isViewer.value || !accessToken.value) return;
                fetch('/api/viewer/heartbeat', { method: 'POST', headers: { 'Authorization': `Bearer ${accessToken.value}` } }).catch(() => {});
            };
            beat();
            viewerHeartbeatTimer = setInterval(beat, 30000);
        };
        const stopViewerHeartbeat = () => {
            if (viewerHeartbeatTimer) { clearInterval(viewerHeartbeatTimer); viewerHeartbeatTimer = null; }
        };

        const toggleViewerNotify = async () => {
            if (!('Notification' in window)) { ElementPlus.ElMessage.warning('当前浏览器不支持通知'); return; }
            if (viewerNotifyOn.value) {
                viewerNotifyOn.value = false;
                localStorage.setItem('viewer_notify', '0');
                ElementPlus.ElMessage.info('已关闭新报出通知');
                return;
            }
            let perm = Notification.permission;
            if (perm !== 'granted') { try { perm = await Notification.requestPermission(); } catch (e) {} }
            if (perm === 'granted') {
                viewerNotifyOn.value = true;
                localStorage.setItem('viewer_notify', '1');
                ElementPlus.ElMessage.success('已开启新报出通知');
            } else {
                ElementPlus.ElMessage.warning('通知权限被拒绝，请在浏览器地址栏左侧允许通知后重试');
            }
        };

        // 检测新报出并弹通知（首帧只建立基线，不弹）
        // 新成交提醒。改造前提醒的是「选股信号新报出」，那张表已经整个去掉了；
        // 现在提醒的是账户真的成交了一笔 —— 对盯盘的人来说这才是要立刻知道的事。
        const maybeNotifyNewTrades = (records) => {
            const list = records || [];
            const keyOf = r => `${r.order_sysid || r.stock_code}|${r.traded_time}`;
            if (!marketNotifyInitialized) {
                list.forEach(r => marketNotifySeen.add(keyOf(r)));
                marketNotifyInitialized = true;
                return;
            }
            const fresh = list.filter(r => !marketNotifySeen.has(keyOf(r)));
            fresh.forEach(r => marketNotifySeen.add(keyOf(r)));
            if (fresh.length && viewerNotifyOn.value && ('Notification' in window) && Notification.permission === 'granted') {
                const lines = fresh.slice(0, 5).map(
                    r => `${r.side_label} ${r.stock_name || r.stock_code} ${r.volume}${r.unit}`);
                const body = fresh.length > 5 ? `${lines.join('、')} 等 ${fresh.length} 笔` : lines.join('、');
                try {
                    const n = new Notification(`新成交 ${fresh.length} 笔`, { body, tag: 'trade-new', renotify: true });
                    n.onclick = () => { window.focus(); n.close(); };
                } catch (e) {}
            }
        };

        const fetchCurrentUser = async () => {
            try {
                const response = await fetch('/api/users/me', {
                    headers: { 'Authorization': `Bearer ${accessToken.value}` }
                });

                if (response.ok) {
                    currentUser.value = await response.json();
                    await loadUsers();
                } else {
                    clearAuth();
                }
            } catch (error) {
                console.error('获取用户信息失败:', error);
                clearAuth();
            }
        };

        const handleLogin = async () => {
            try {
                const formData = new URLSearchParams();
                formData.append('username', loginForm.value.username);
                formData.append('password', loginForm.value.password);

                const response = await fetch('/api/token', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
                    body: formData
                });

                if (response.ok) {
                    const data = await response.json();
                    accessToken.value = data.access_token;
                    localStorage.setItem('access_token', data.access_token);
                    localStorage.setItem('auth_type', 'account');
                    isAuthenticated.value = true;
                    showLoginDialog.value = false;
                    await fetchCurrentUser();
                    ElementPlus.ElMessage.success('登录成功');
                } else {
                    ElementPlus.ElMessage.error('用户名或密码错误');
                }
            } catch (error) {
                ElementPlus.ElMessage.error('登录失败');
            }
        };

        const handleLogout = async () => {
            try {
                if (accessToken.value) {
                    await fetch('/api/logout', {
                        method: 'POST',
                        headers: { 'Authorization': `Bearer ${accessToken.value}` }
                    });
                }
            } catch (error) {
                console.error('退出登录失败:', error);
            }
            clearAuth();
            ElementPlus.ElMessage.success('已退出登录');
        };

        const openAliasDialog = () => {
            if (!currentAccountId.value || currentAccountId.value === 'all') return;
            aliasForm.value = {
                account_id: currentAccountId.value,
                alias: currentAccount.value.alias || ''
            };
            showAliasDialog.value = true;
        };

        const submitAliasUpdate = async () => {
            try {
                const response = await fetch('/api/account/alias', {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json',
                        'Authorization': `Bearer ${accessToken.value}`
                    },
                    body: JSON.stringify(aliasForm.value)
                });

                if (response.ok) {
                    ElementPlus.ElMessage.success('账号别名更新成功');
                    showAliasDialog.value = false;
                    await loadUsers(); // 重新加载用户列表以更新别名
                } else {
                    const data = await response.json();
                    ElementPlus.ElMessage.error(data.detail || '更新失败');
                }
            } catch (error) {
                console.error('更新别名出错:', error);
                ElementPlus.ElMessage.error('网络错误');
            }
        };

        // 打开账号排序对话框
        const openUserSortDialog = () => {
            // 获取保存的顺序
            const savedOrder = localStorage.getItem('user_account_order');
            const orderMap = savedOrder ? JSON.parse(savedOrder) : {};
            
            // 准备排序列表（排除 "all" 选项）
            const otherUsers = users.value.filter(u => u.account_id !== 'all');
            userSortList.value = [...otherUsers].sort((a, b) => {
                const orderA = orderMap[a.account_id] ?? 999999;
                const orderB = orderMap[b.account_id] ?? 999999;
                return orderA - orderB;
            });
            
            showUserSortDialog.value = true;
        };

        // 保存账号排序
        const saveUserSort = () => {
            const orderMap = {};
            userSortList.value.forEach((user, index) => {
                orderMap[user.account_id] = index;
            });
            localStorage.setItem('user_account_order', JSON.stringify(orderMap));
            ElementPlus.ElMessage.success('账号顺序已保存');
            showUserSortDialog.value = false;
        };

        // 移动账号位置
        const moveUserOrder = (index, direction) => {
            const newIndex = index + direction;
            if (newIndex < 0 || newIndex >= userSortList.value.length) return;
            
            const temp = userSortList.value[index];
            userSortList.value[index] = userSortList.value[newIndex];
            userSortList.value[newIndex] = temp;
        };

        const clearAuth = () => {
            accessToken.value = null;
            currentUser.value = null;
            isAuthenticated.value = false;
            currentAccountId.value = '';
            // 观察者体系清理
            isViewer.value = false;
            viewerUsername.value = '';
            stopViewerHeartbeat();
            marketNotifySeen.clear();
            marketNotifyInitialized = false;
            localStorage.removeItem('access_token');
            localStorage.removeItem('auth_type');
            positions.value = [];
            trades.value = [];
            asset.value = {};
            tradeFactorForm.value = { positionFactor: 1.0, openCountFactor: 1.0 };
            tradeFactorsMixed.value = false;
            tradeFactorsMixedFields.value = [];
            tradeFactorsAccountCount.value = 0;
        };

        const normalizeUserList = (raw) => {
            return (raw || []).map((u, i) => {
                if (!u) return { account_id: `unknown_${i}`, alias: 'unknown', last_sync: null, online: null, data_time: null, data_delayed: false, trading_stopped: false, buy_stopped: false, sell_stopped: false, is_dormant: false, today_profit_rate: null };
                return {
                    account_id: u.account_id || `unknown_${i}`,
                    alias: u.alias || u.account_id || `unknown_${i}`,
                    last_sync: u.last_sync ?? null,
                    online: typeof u.online === 'boolean' ? u.online : (u.last_sync ? (Date.now()/1000 - u.last_sync <= 60) : null),
                    data_time: u.data_time ?? null,
                    data_delayed: u.data_delayed ?? false,
                    trading_stopped: u.trading_stopped || false,
                    buy_stopped: u.buy_stopped || false,
                    sell_stopped: u.sell_stopped || false,
                    is_dormant: u.is_dormant || false,
                    today_profit_rate: (u.today_profit_rate === null || u.today_profit_rate === undefined) ? null : u.today_profit_rate
                };
            });
        };

        const syncTradingStatusFromUsers = () => {
            if (currentAccountId.value === 'all') {
                const realAccounts = users.value.filter(u => u && u.account_id && u.account_id !== 'all' && !u.account_id.startsWith('ADMIN_'));
                buyStopped.value = realAccounts.some(u => u.buy_stopped);
                sellStopped.value = realAccounts.some(u => u.sell_stopped);
            } else if (currentAccountId.value) {
                const cur = users.value.find(u => u.account_id === currentAccountId.value);
                buyStopped.value = cur ? !!cur.buy_stopped : false;
                sellStopped.value = cur ? !!cur.sell_stopped : false;
            } else {
                buyStopped.value = false;
                sellStopped.value = false;
            }
        };

        const loadCurrentAccountBundle = async () => {
            if (!currentAccountId.value) return;
            loading.value = true;
            try {
                // 全市场行情(分钟线)和记录板都是“与账号无关的全局数据”，
                // 已加载过就不在切换账号时重复拉取（行情有 30s、记录板有 60s 定时刷新兜底），
                // 避免每次切账号都重下载几 MB 行情包导致卡顿
                const marketLoaded = marketMinData.value && Object.keys(marketMinData.value).length > 0;
                const boardLoaded = researchBoardRecords.value && researchBoardRecords.value.length > 0;
                const tradeFlowLoaded = tradeFlowRecords.value && tradeFlowRecords.value.length > 0;
                const tasks = [
                    loadData({ loadAux: false, loadMarket: !marketLoaded }),
                    loadChartData(),
                    loadTradeStats(),
                    loadTradeDates(),
                    loadTradeFactors()
                ];
                if (!boardLoaded) tasks.push(loadResearchBoard(false));
                if (!tradeFlowLoaded) tasks.push(Promise.resolve(loadTradeFlow(true)).catch(() => {}));
                await Promise.all(tasks);
            } finally {
                loading.value = false;
            }
        };

        const loadUsers = async () => {
            try {
                const response = await fetch('/api/users', {
                    headers: { 'Authorization': `Bearer ${accessToken.value}` }
                });

                if (response.ok) {
                    const data = await response.json();
                    // 清洗用户数据，避免 undefined 或缺失字段导致模板崩溃
                    users.value = normalizeUserList(data.users || []);

                    if (users.value.length > 0 && !currentAccountId.value) {
                        if (currentUser.value?.role === 'admin') {
                            currentAccountId.value = 'all';
                        } else {
                            currentAccountId.value = users.value[0].account_id;
                        }
                    }

                    // 如果初始选中 all，同步 buyStopped/sellStopped 状态
                    syncTradingStatusFromUsers();
                    
                    // 如果已经有选中的账号或者是管理员默认的 'all'，则加载数据
                    await loadCurrentAccountBundle();
                }
            } catch (error) {
                console.error('加载用户列表失败:', error);
            }
        };

        // 轻量刷新账号下拉里的涨跌幅（只更新 users 列表，不触发账号数据 bundle）
        const refreshUserRates = async () => {
            if (!isAuthenticated.value) return;
            try {
                const response = await fetch('/api/users', {
                    headers: { 'Authorization': `Bearer ${accessToken.value}` }
                });
                if (response.ok) {
                    const data = await response.json();
                    users.value = normalizeUserList(data.users || []);
                }
            } catch (e) {}
        };

        const handleUserChange = async () => {
            showClearPositionsTag.value = false;
            syncTradingStatusFromUsers();
            await loadCurrentAccountBundle();
        };

        const loadTradeFactors = async () => {
            if (!isAuthenticated.value || !currentAccountId.value) return;
            if (currentAccountId.value === 'all' && currentUser.value?.role !== 'admin') return;

            tradeFactorsLoading.value = true;
            try {
                const response = await fetch(`/api/account/trade-factors?account_id=${encodeURIComponent(currentAccountId.value)}`, {
                    headers: { 'Authorization': `Bearer ${accessToken.value}` }
                });
                const data = await response.json().catch(() => ({}));
                if (!response.ok) {
                    throw new Error(data.detail || '加载开仓参数失败');
                }

                tradeFactorForm.value = {
                    positionFactor: data.position_factor ?? null,
                    openCountFactor: data.open_count_factor ?? null
                };
                tradeFactorsMixed.value = !!data.is_mixed;
                tradeFactorsMixedFields.value = data.mixed_fields || [];
                tradeFactorsAccountCount.value = data.account_count || 0;
            } catch (error) {
                console.error('加载开仓参数失败:', error);
                ElementPlus.ElMessage.error(error.message || '加载开仓参数失败');
            } finally {
                tradeFactorsLoading.value = false;
            }
        };

        const saveTradeFactors = async () => {
            if (!isAuthenticated.value || !currentAccountId.value) return;
            if (tradeFactorForm.value.positionFactor == null || tradeFactorForm.value.openCountFactor == null) {
                ElementPlus.ElMessage.warning('请先填写仓位参数和开仓数量参数');
                return;
            }

            tradeFactorsSaving.value = true;
            try {
                const response = await fetch('/api/account/trade-factors', {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json',
                        'Authorization': `Bearer ${accessToken.value}`
                    },
                    body: JSON.stringify({
                        account_id: currentAccountId.value,
                        position_factor: tradeFactorForm.value.positionFactor,
                        open_count_factor: tradeFactorForm.value.openCountFactor
                    })
                });
                const data = await response.json().catch(() => ({}));
                if (!response.ok) {
                    throw new Error(data.detail || '保存开仓参数失败');
                }

                ElementPlus.ElMessage.success(data.message || '开仓参数已下发');
                await loadTradeFactors();
            } catch (error) {
                console.error('保存开仓参数失败:', error);
                ElementPlus.ElMessage.error(error.message || '保存开仓参数失败');
            } finally {
                tradeFactorsSaving.value = false;
            }
        };

        const loadMarketMinData = async () => {
            if (!isAuthenticated.value) return;
            try {
                const response = await fetch('/api/market-data/rt-min', {
                    headers: { 'Authorization': `Bearer ${accessToken.value}` }
                });
                if (response.ok) {
                    const data = await response.json();
                    // 兼容新格式 {prices, last_close} 和旧格式（直接是价格字典）
                    if (data.prices !== undefined) {
                        marketMinData.value = data.prices || {};
                        lastCloseData.value = data.last_close || {};
                    } else {
                        marketMinData.value = data;
                    }
                }
            } catch (error) {
                console.error('Failed to load market min data:', error);
            }
        };

        const loadData = async (options = {}) => {
            if (!isAuthenticated.value || !currentAccountId.value) return;

            const { loadAux = true, loadMarket = true } = options;
            const reqAccount = currentAccountId.value;   // 本次请求对应的账号

            try {
                // 并发加载行情和账户数据
                const marketPromise = loadMarket ? loadMarketMinData() : Promise.resolve();
                const url = `/api/data?account_id=${encodeURIComponent(reqAccount)}`;
                const dataPromise = fetch(url, {
                    headers: { 'Authorization': `Bearer ${accessToken.value}` }
                });
                const [, response] = await Promise.all([marketPromise, dataPromise]);

                // 过期响应丢弃：请求期间若已切换账号，旧账号的响应不能覆盖当前账号数据
                if (reqAccount !== currentAccountId.value) return;

                if (response.ok) {
                    const data = await response.json();

                    // 平滑更新数据
                    updateTime.value = data.update_time;
                    asset.value = data.asset || {};
                    trades.value = data.trades || [];

                    // 计算当天的 T0 统计数据
                    let todayStr = '';
                    try {
                        todayStr = getLocalDateStr(new Date());
                    } catch(e) {}

                    // 持仓数据带更新标记
                    const newPositions = data.positions || [];
                    const todayTradeStatsMap = buildTodayTradeStatsMap(trades.value, todayStr);
                    newPositions.forEach(pos => {
                        const todayStats = summarizeTodayTradeStats(todayTradeStatsMap, pos);
                        pos.today_buy_amount = todayStats.buy_amount;
                        pos.today_sell_amount = todayStats.sell_amount;
                        pos.today_net_amount = todayStats.buy_amount - todayStats.sell_amount;
                        pos.today_t_profit = todayStats.t_profit;
                    });
                    positions.value = newPositions;

                    // 渲染走势小图
                    scheduleRenderSparklines();

                    // 更新盈亏数据
                    totalProfit.value = data.total_profit || 0;
                    totalProfitRate.value = data.total_profit_rate || 0;
                    monthProfit.value = data.month_profit || 0;
                    monthProfitRate.value = data.month_profit_rate || 0;
                    todayProfit.value = data.today_profit || 0;
                    todayProfitRate.value = data.today_profit_rate || 0;
                    // 同步服务端展示模式（管理员在任一端切换后，所有端随刷新生效）
                    if (data.display_mode !== undefined && !displayModeToggling.value) {
                        displayMode.value = !!data.display_mode;
                    }

                    // 显示刷新提示
                    showRefreshIndicator.value = true;
                    lastUpdateTime.value = new Date().toLocaleTimeString();
                    lastUpdateTimestamp.value = Date.now();
                    secondsSinceUpdate.value = 0;
                    setTimeout(() => {
                        showRefreshIndicator.value = false;
                    }, 3000);
                    
                    // 更新饼图
                    updatePieChart();
                    
                    if (loadAux) {
                        // 加载交易统计和日期
                        await Promise.all([
                            loadTradeStats(),
                            loadTradeDates()
                        ]);
                    }
                }
            } catch (error) {
                console.error('加载数据失败:', error);
            }
        };

        const loadChartData = async () => {
            if (!isAuthenticated.value || !currentAccountId.value) return;

            isChartLoading.value = true;
            const reqAccount = currentAccountId.value;   // 本次请求对应的账号
            try {
                // 使用 chartPeriod.value 作为请求的小时数
                const hours = chartPeriod.value;
                const url = `/api/asset-history?account_id=${encodeURIComponent(reqAccount)}&hours=${hours}`;
                const response = await fetch(url, {
                    headers: { 'Authorization': `Bearer ${accessToken.value}` }
                });

                // 过期响应丢弃：请求期间若已切换账号，旧账号的曲线不能覆盖当前账号
                if (reqAccount !== currentAccountId.value) return;

                if (response.ok) {
                    const data = await response.json();
                    console.log('Chart data received:', data);
                    rawChartData.value = data.history || [];
                    dailyChartData.value = data.daily_history || [];
                    
                    // 等待 DOM 更新并确保 loading 结束
                    await nextTick();
                    updateChart();
                }
            } catch (error) {
                console.error('加载图表数据失败:', error);
            } finally {
                isChartLoading.value = false;
                // 再次确保在 loading 结束后重绘一次
                setTimeout(() => {
                    updateChart();
                }, 100);
            }
        };

        // 手动刷新：一键同时刷新行情/资产曲线/记录板（不重置分页）
        // 15s 超时兜底 + allSettled，避免任一接口卡住导致按钮一直转
        const manualRefresh = async () => {
            if (!isAuthenticated.value || manualRefreshing.value) return;
            manualRefreshing.value = true;
            let timedOut = false;
            let timer = null;
            try {
                const tasks = [];
                if (currentAccountId.value) {
                    tasks.push(Promise.resolve(loadData()).catch(() => {}));
                    tasks.push(Promise.resolve(loadChartData()).catch(() => {}));
                }
                tasks.push(Promise.resolve(loadResearchBoard(false, true)).catch(() => {}));
                const timeout = new Promise(resolve => {
                    timer = setTimeout(() => { timedOut = true; resolve(); }, 15000);
                });
                await Promise.race([Promise.allSettled(tasks), timeout]);
                if (timer) clearTimeout(timer);
                if (timedOut) {
                    ElementPlus.ElMessage.warning('刷新超时，请稍后重试');
                } else {
                    ElementPlus.ElMessage.success('已刷新');
                }
            } catch (e) {
                if (timer) clearTimeout(timer);
                ElementPlus.ElMessage.error('刷新失败');
            } finally {
                manualRefreshing.value = false;
            }
        };

        const handleChartTypeChange = () => {
            // 只要有任何一套数据，就可以直接重绘，不需要重新请求
            if (rawChartData.value.length > 0 || dailyChartData.value.length > 0) {
                updateChart();
            } else {
                loadChartData();
            }
        };

        const loadTradeDates = async () => {
            if (!isAuthenticated.value || !currentAccountId.value) return;

            const reqAccount = currentAccountId.value;   // 本次请求对应的账号
            try {
                const url = `/api/trade-dates?account_id=${encodeURIComponent(reqAccount)}&days=365`;
                const response = await fetch(url, {
                    headers: { 'Authorization': `Bearer ${accessToken.value}` }
                });

                // 过期响应丢弃：请求期间若已切换账号，旧账号数据不能覆盖当前账号
                if (reqAccount !== currentAccountId.value) return;

                if (response.ok) {
                    const data = await response.json();
                    const dates = data.result || data.dates || [];
                    rawCalendarData.value = dates; // 保存原始数据以支持本地翻页
                    updateCalendar(dates); 
                }
            } catch (error) {
                console.error('加载交易日期失败:', error);
            }
        };

        const loadTradeStats = async () => {
            if (!isAuthenticated.value || !currentAccountId.value) return;

            const reqAccount = currentAccountId.value;   // 本次请求对应的账号
            try {
                let url = `/api/trade-stats?account_id=${encodeURIComponent(reqAccount)}`;
                if (selectedDate.value) {
                    url += `&date=${selectedDate.value}`;
                }
                const response = await fetch(url, {
                    headers: { 'Authorization': `Bearer ${accessToken.value}` }
                });

                // 过期响应丢弃：请求期间若已切换账号，旧账号统计不能覆盖当前账号
                if (reqAccount !== currentAccountId.value) return;

                if (response.ok) {
                    const stats = await response.json();
                    todayBuyAmount.value = stats.buy_amount || 0;
                    todaySellAmount.value = stats.sell_amount || 0;
                    todayBuyCount.value = stats.buy_count || 0;
                    todaySellCount.value = stats.sell_count || 0;
                }
            } catch (error) {
                console.error('加载交易统计失败:', error);
            }
        };

        const updatePieChart = () => {
            const canvas = document.querySelector('#positionPieChart');
            if (!canvas || positions.value.length === 0) return;

            const ctx = canvas.getContext('2d');

            // 准备数据：市值前5名，其余归为“其他”
            const sorted = [...positions.value].sort((a, b) => b.market_value - a.market_value);
            const top5 = sorted.slice(0, 5);
            const others = sorted.slice(5);

            const labels = top5.map(p => p.instrument_name);
            const data = top5.map(p => p.market_value);

            if (others.length > 0) {
                labels.push('其他');
                data.push(others.reduce((sum, p) => sum + p.market_value, 0));
            }

            // 如果有可用资金，也加入饼图
            if (asset.value.cash > 0) {
                labels.push('可用资金');
                data.push(asset.value.cash);
            }

            const colors = [
                '#00f2ff', '#a855f7', '#eab308', '#ff4d4f', '#00ff88',
                '#3b82f6', '#6366f1', '#f43f5e', '#10b981', '#f59e0b'
            ];

            // 复用已有图表：canvas 未变只更新数据，避免销毁重建
            const existingPie = positionPieChart.value;
            if (existingPie && existingPie.canvas === canvas) {
                existingPie.data.labels = labels;
                existingPie.data.datasets[0].data = data;
                existingPie.data.datasets[0].backgroundColor = colors.slice(0, labels.length);
                existingPie.update('none');
                return;
            }
            if (existingPie) existingPie.destroy();

            const newPieChart = new Chart(ctx, {
                type: 'doughnut',
                data: {
                    labels: labels,
                    datasets: [{
                        data: data,
                        backgroundColor: colors.slice(0, labels.length),
                        borderWidth: 0,
                        hoverOffset: 10
                    }]
                },
                options: {
                    responsive: true,
                    maintainAspectRatio: false,
                    cutout: '70%',
                    plugins: {
                        legend: {
                            position: 'right',
                            labels: {
                                color: '#94a3b8',
                                font: {
                                    size: 10,
                                    family: 'Share Tech Mono'
                                },
                                padding: 10,
                                boxWidth: 10
                            }
                        },
                        tooltip: {
                            backgroundColor: 'rgba(11, 14, 20, 0.9)',
                            titleColor: '#94a3b8',
                            bodyColor: '#e2e8f0',
                            borderColor: 'rgba(0, 242, 255, 0.2)',
                            borderWidth: 1,
                            padding: 12,
                            displayColors: true,
                            callbacks: {
                                label: function(context) {
                                    const value = context.raw;
                                    const total = context.dataset.data.reduce((a, b) => a + b, 0);
                                    const percentage = ((value / total) * 100).toFixed(1) + '%';
                                    return ` ${context.label}: ${formatMoney(value)} (${percentage})`;
                                }
                            }
                        }
                    }
                }
            });
            positionPieChart.value = markRaw(newPieChart);
        };

        const updateChart = () => {
            const canvas = document.querySelector('#assetChart');
            if (!canvas) {
                console.warn('Canvas #assetChart not found');
                return;
            }

            const isRate = chartType.value === 'rate';
            const history = isRate ? dailyChartData.value : rawChartData.value;

            if (history.length === 0) {
                return;
            }

            const ctx = canvas.getContext('2d');
            const chartLabel = isRate ? '累计收益率' : '总资产';
            
            // 收益率显示日期(天)，资产显示时间(分时)
            const labels = history.map(h => isRate ? h.date : (h.record_time ? h.record_time.substring(5, 16) : '--:--'));
            const chartData = history.map(h => isRate ? (h.cumulative_rate || 0) : h.total_asset);
            const chartColor = isRate ? '#a855f7' : '#00f2ff'; // 收益率用紫色，资产用青色

            // 过滤异常数据，分两步：
            // 第一步：基于相邻差值去掉负向大幅异常（10σ），正向跳变保留作为新基准
            // 第二步：剔除孤立单点异常（仅处理极明显的"一根针"式毛刺）
            const applyOutlierFilter = (lbls, vals) => {
                // 数据点太少时不过滤，避免误伤正常波动
                if (vals.length < 6) return { filteredLabels: lbls, filteredValues: vals };

                // === 第一步：基于相邻差值过滤负向异常 ===
                const diffs = vals.slice(1).map((v, i) => v - vals[i]);
                const mean = diffs.reduce((a, b) => a + b, 0) / diffs.length;
                const stdDev = Math.sqrt(diffs.reduce((a, b) => a + Math.pow(b - mean, 2), 0) / diffs.length);

                if (stdDev === 0) return { filteredLabels: lbls, filteredValues: vals };

                const threshold = 10 * stdDev;
                let filteredLabels = [lbls[0]];
                let filteredValues = [vals[0]];

                for (let i = 1; i < vals.length; i++) {
                    const prevKept = filteredValues[filteredValues.length - 1];
                    const diff = vals[i] - prevKept;
                    if (diff >= -threshold) {
                        filteredLabels.push(lbls[i]);
                        filteredValues.push(vals[i]);
                    }
                }

                if (filteredValues.length < 6) return { filteredLabels: lbls, filteredValues: vals };

                // === 第二步：剔除孤立单点异常（放宽判定，仅处理极明显的"一根针"式毛刺）===
                const allDiffs = filteredValues.slice(1).map((v, i) => Math.abs(v - filteredValues[i]));
                allDiffs.sort((a, b) => a - b);
                const medianDiff = allDiffs[Math.floor(allDiffs.length / 2)];
                // 大幅提高孤立判定倍率，避免假期前后正常的几千块资产跳变被误判为"尖刺"
                const isolatedMultiplier = 25;

                const resultLabels = [...filteredLabels];
                const resultValues = [...filteredValues];

                // 处理内部孤立点（前后邻居非常接近，中间点突兀地高出25倍中位波动）
                for (let i = 1; i < resultValues.length - 1; i++) {
                    const prev = resultValues[i - 1];
                    const curr = resultValues[i];
                    const next = resultValues[i + 1];
                    const diffPrev = Math.abs(curr - prev);
                    const diffNext = Math.abs(curr - next);
                    const neighborGap = Math.abs(next - prev);

                    const isIsolatedSpike =
                        diffPrev > medianDiff * isolatedMultiplier &&
                        diffNext > medianDiff * isolatedMultiplier &&
                        neighborGap < medianDiff * 8;

                    if (isIsolatedSpike) {
                        resultValues[i] = (prev + next) / 2;
                    }
                }

                // 处理最后一个点的端点异常（仅在中位波动极小且末尾跳变极为夸张时触发）
                if (resultValues.length >= 4) {
                    const lastIdx = resultValues.length - 1;
                    const prev = resultValues[lastIdx - 1];
                    const prev2 = resultValues[lastIdx - 2];
                    const last = resultValues[lastIdx];
                    const diffLast = Math.abs(last - prev);
                    const diffPrev = Math.abs(prev - prev2);

                    if (diffLast > medianDiff * isolatedMultiplier && diffPrev < medianDiff * 8) {
                        resultValues[lastIdx] = prev;
                    }
                }

                return { filteredLabels: resultLabels, filteredValues: resultValues };
            };

            const { filteredLabels, filteredValues } = applyOutlierFilter(labels, chartData);

            // 复用已有图表：canvas 未变只更新数据，避免销毁重建（资产曲线点很多，重建尤其贵）
            const existingAsset = assetChart.value;
            if (existingAsset && existingAsset.canvas === canvas) {
                existingAsset.data.labels = filteredLabels;
                existingAsset.data.datasets[0].label = chartLabel;
                existingAsset.data.datasets[0].data = filteredValues;
                existingAsset.data.datasets[0].borderColor = chartColor;
                existingAsset.data.datasets[0].backgroundColor = `${chartColor}22`;
                existingAsset.data.datasets[0].pointRadius = isRate ? 4 : (filteredValues.length < 50 ? 2 : 0);
                existingAsset.update('none');
                return;
            }
            if (existingAsset) existingAsset.destroy();

            try {
                const newChart = new Chart(ctx, {
                    type: 'line',
                    data: {
                        labels: filteredLabels,
                        datasets: [{
                            label: chartLabel,
                            data: filteredValues,
                            borderColor: chartColor,
                            backgroundColor: `${chartColor}22`,
                            fill: true,
                            tension: 0.4,
                            borderWidth: 2,
                            pointRadius: isRate ? 4 : (filteredValues.length < 50 ? 2 : 0), // 数据点少时显示点
                            pointHoverRadius: 6
                        }]
                    },
                    options: {
                        responsive: true,
                        maintainAspectRatio: false,
                        animation: {
                            duration: 0 // 禁用动画以排查是否是动画导致不显示
                        },
                        plugins: {
                            legend: { display: false },
                            tooltip: {
                                mode: 'index',
                                intersect: false,
                                callbacks: {
                                    label: function(context) {
                                        let label = context.dataset.label || '';
                                        if (label) label += ': ';
                                        if (isRate) {
                                            label += context.parsed.y.toFixed(2) + '%';
                                        } else {
                                            label += '¥' + context.parsed.y.toLocaleString();
                                        }
                                        return label;
                                    }
                                }
                            }
                        },
                        scales: {
                            x: {
                                grid: { color: 'rgba(255, 255, 255, 0.05)' },
                                ticks: {
                                    color: '#888',
                                    maxRotation: 0,
                                    autoSkip: true,
                                    maxTicksLimit: 10
                                }
                            },
                            y: {
                                grid: { color: 'rgba(255, 255, 255, 0.05)' },
                                ticks: {
                                    color: '#888',
                                    callback: function(value) {
                                        if (isRate) return value.toFixed(1) + '%';
                                        if (value >= 10000) return (value / 10000).toFixed(1) + '万';
                                        return value;
                                    }
                                }
                            }
                        }
                    }
                });
                
                // 使用 markRaw 避免 Vue 3 的响应式代理导致 Chart.js 实例失效
                assetChart.value = markRaw(newChart);
                console.log('Chart created successfully');
            } catch (err) {
                console.error('Error creating chart:', err);
            }
        };

        const selectDate = (dayOrStr) => {
            if (typeof dayOrStr === 'string') {
                selectedDate.value = dayOrStr;
            } else if (dayOrStr && dayOrStr.dateStr) {
                selectedDate.value = dayOrStr.dateStr;
            }
            // 选定日期后立即更新交易统计
            loadTradeStats();
        };

        const updateCalendar = (dates) => {
            const today = new Date();
            const year = calendarDate.value.getFullYear();
            const month = calendarDate.value.getMonth();

            calendarMonth.value = `${year}年${month + 1}月`;

            const firstDay = new Date(year, month, 1);
            const startDate = new Date(firstDay);
            // 将第一列设置为周一：(getDay() + 6) % 7 得到距离上周一的天数
            const dayOffset = (firstDay.getDay() + 6) % 7;
            startDate.setDate(startDate.getDate() - dayOffset);

            // 将 dates 转换为 Map 方便查找
            const statsMap = new Map();
            if (Array.isArray(dates)) {
                dates.forEach(item => {
                    if (typeof item === 'string') {
                        statsMap.set(item, { hasTrade: true });
                    } else if (item && item.date) {
                        statsMap.set(item.date, {
                            hasTrade: item.has_trade,
                            profit: item.profit,
                            profitRate: item.profit_rate,
                            positionRatio: item.position_ratio
                        });
                    }
                });
            }

            const days = [];
            for (let i = 0; i < 42; i++) {
                // 使用毫秒计算确保日期正确递增，避免跨月时的日期错误
                const date = new Date(startDate.getTime() + i * 24 * 60 * 60 * 1000);
                const dateStr = formatDateKey(date);
                const stats = statsMap.get(dateStr) || {};
                const isToday = date.toDateString() === today.toDateString();

                days.push({
                    key: i,
                    day: date.getDate(),
                    dateStr: dateStr,
                    isToday: isToday,
                    isCurrentMonth: date.getMonth() === month,
                    hasTrade: stats.hasTrade || false,
                    profit: (isToday && (stats.profit === null || stats.profit === undefined)) ? todayProfit.value : (stats.profit || 0),
                    profitRate: (isToday && (stats.profitRate === null || stats.profitRate === undefined || stats.profitRate === 0)) ? todayProfitRate.value : (stats.profitRate || 0),
                    positionRatio: stats.positionRatio,
                    totalAsset: stats.totalAsset
                });
            }

            calendarDays.value = days;
        };

        const prevMonth = () => {
            const date = new Date(calendarDate.value);
            date.setMonth(date.getMonth() - 1);
            calendarDate.value = date;
            updateCalendar(rawCalendarData.value);
        };

        const nextMonth = () => {
            const date = new Date(calendarDate.value);
            date.setMonth(date.getMonth() + 1);
            calendarDate.value = date;
            updateCalendar(rawCalendarData.value);
        };

        const handleSortChange = ({ prop, order }) => {
            sortProp.value = prop;
            sortOrder.value = order;
        };

        const loadAdjustments = async () => {
            if (!isAuthenticated.value || !currentAccountId.value) return;
            try {
                const response = await fetch(`/api/capital-adjusts?account_id=${encodeURIComponent(currentAccountId.value)}`, {
                    headers: { 'Authorization': `Bearer ${accessToken.value}` }
                });
                if (response.ok) {
                    const data = await response.json();
                    adjustments.value = data.adjusts || [];
                }
            } catch (error) {
                console.error('加载资金调整记录失败:', error);
            }
        };

        const submitAdjustment = async () => {
            if (adjustmentForm.value.amount === 0) {
                ElementPlus.ElMessage.warning('请输入调整金额');
                return;
            }
            
            adjusting.value = true;
            try {
                const response = await fetch('/api/capital-adjust', {
                    method: 'POST',
                    headers: { 
                        'Content-Type': 'application/json',
                        'Authorization': `Bearer ${accessToken.value}` 
                    },
                    body: JSON.stringify({
                        account_id: currentAccountId.value,
                        amount: adjustmentForm.value.amount,
                        remark: adjustmentForm.value.remark,
                        adjust_time: adjustmentForm.value.date || null
                    })
                });

                if (response.ok) {
                    ElementPlus.ElMessage.success('添加成功');
                    adjustmentForm.value.amount = 0;
                    adjustmentForm.value.remark = '';
                    adjustmentForm.value.date = '';
                    await loadAdjustments();
                    await loadData(); // 重新加载数据以更新累计盈亏
                } else {
                    ElementPlus.ElMessage.error('添加失败');
                }
            } catch (error) {
                ElementPlus.ElMessage.error('请求出错');
            } finally {
                adjusting.value = false;
            }
        };

        const deleteAdjustment = async (id) => {
            try {
                const response = await fetch(`/api/capital-adjust/${id}`, {
                    method: 'DELETE',
                    headers: { 'Authorization': `Bearer ${accessToken.value}` }
                });

                if (response.ok) {
                    ElementPlus.ElMessage.success('删除成功');
                    await loadAdjustments();
                    await loadData(); // 重新加载数据以更新累计盈亏
                } else {
                    ElementPlus.ElMessage.error('删除失败');
                }
            } catch (error) {
                ElementPlus.ElMessage.error('请求出错');
            }
        };

        const submitClearAll = async () => {
            if (clearAllConfirmText.value !== '全部清仓') return;
            if (!clearAllPassword.value) {
                ElementPlus.ElMessage.warning('请输入清仓密码');
                return;
            }
            try {
                const response = await fetch('/api/admin/clear-all-positions', {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json',
                        'Authorization': `Bearer ${accessToken.value}`
                    },
                    body: JSON.stringify({
                        account_id: currentUser.value?.account_id || '',
                        command_type: 'clear_positions',
                        command_data: JSON.stringify({ percentage: clearAllPercentage.value }),
                        password: clearAllPassword.value
                    })
                });
                const result = await response.json();
                if (response.ok && result.status === 'success') {
                    ElementPlus.ElMessage.success(result.message);
                    showClearAllDialog.value = false;
                    clearAllConfirmText.value = '';
                    clearAllPassword.value = '';
                } else {
                    ElementPlus.ElMessage.error(result.message || '指令下发失败');
                }
            } catch (error) {
                ElementPlus.ElMessage.error('请求出错');
            }
        };

        const submitClearPositions = async () => {
            if (clearPositionsConfirmText.value !== '一键清仓') return;
            if (!clearPositionsPassword.value) {
                ElementPlus.ElMessage.warning('请输入清仓密码');
                return;
            }
            
            try {
                const response = await fetch('/api/account/clear-positions', {
                    method: 'POST',
                    headers: { 
                        'Content-Type': 'application/json',
                        'Authorization': `Bearer ${accessToken.value}` 
                    },
                    body: JSON.stringify({
                        account_id: currentAccountId.value,
                        command_type: 'clear_positions',
                        command_data: JSON.stringify({ percentage: clearPositionsPercentage.value }),
                        password: clearPositionsPassword.value
                    })
                });

                const result = await response.json();
                if (response.ok) {
                    if (result.status === 'success') {
                        ElementPlus.ElMessage.success(result.message);
                        showClearPositionsDialog.value = false;
                        clearPositionsConfirmText.value = '';
                        clearPositionsPassword.value = '';
                    } else {
                        ElementPlus.ElMessage.warning(result.message);
                    }
                } else {
                    ElementPlus.ElMessage.error(result.message || '指令下发失败');
                }
            } catch (error) {
                ElementPlus.ElMessage.error('请求出错');
            }
        };

        // 通用下发停止/恢复指令
        const _sendTradingCmd = async (action, successMsg, failMsg) => {
            tradingStatusLoading.value = true;
            try {
                const targetAccounts = currentAccountId.value === 'all'
                    ? users.value.filter(u => u && u.account_id && u.account_id !== 'all' && !u.account_id.startsWith('ADMIN_'))
                    : [{ account_id: currentAccountId.value }];

                let successCount = 0;
                let failCount = 0;
                for (const acct of targetAccounts) {
                    try {
                        const response = await fetch('/api/account/stop-trading', {
                            method: 'POST',
                            headers: { 
                                'Content-Type': 'application/json',
                                'Authorization': `Bearer ${accessToken.value}` 
                            },
                            body: JSON.stringify({
                                account_id: acct.account_id,
                                command_type: 'stop_trading',
                                command_data: action,
                                password: ''
                            })
                        });
                        const result = await response.json();
                        if (response.ok && result.status === 'success') {
                            successCount++;
                        } else {
                            failCount++;
                        }
                    } catch (e) {
                        failCount++;
                    }
                }
                if (currentAccountId.value === 'all') {
                    ElementPlus.ElMessage.success(`${successMsg}: ${successCount} 个成功${failCount > 0 ? ', ' + failCount + ' 个失败' : ''}`);
                } else {
                    ElementPlus.ElMessage.success(successCount > 0 ? `${successMsg}已下发` : `${failMsg}下发失败`);
                }
            } catch (error) {
                ElementPlus.ElMessage.error('请求出错');
            } finally {
                tradingStatusLoading.value = false;
            }
        };

        // 停止买入
        const submitStopBuying = async () => {
            await _sendTradingCmd('stop_buy', '停止买入指令', '停止买入指令');
            buyStopped.value = true;
        };

        // 恢复买入
        const submitResumeBuying = async () => {
            await _sendTradingCmd('resume_buy', '恢复买入指令', '恢复买入指令');
            buyStopped.value = false;
        };

        // 停止卖出
        const submitStopSelling = async () => {
            await _sendTradingCmd('stop_sell', '停止卖出指令', '停止卖出指令');
            sellStopped.value = true;
        };

        // 恢复卖出
        const submitResumeSelling = async () => {
            await _sendTradingCmd('resume_sell', '恢复卖出指令', '恢复卖出指令');
            sellStopped.value = false;
        };

        // 重新获取所有K线数据
        const refreshAllKline = async () => {
            try {
                const response = await fetch('/api/admin/refresh-kline', {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json',
                        'Authorization': `Bearer ${accessToken.value}`
                    }
                });
                const result = await response.json();
                if (response.ok && result.status === 'success') {
                    ElementPlus.ElMessage.success(result.message);
                    // 刷新页面上的走势图
                    await loadMarketMinData();
                    await renderSparklines();
                } else {
                    ElementPlus.ElMessage.error(result.detail || '重新获取K线数据失败');
                }
            } catch (error) {
                console.error('重新获取K线数据出错:', error);
                ElementPlus.ElMessage.error('网络错误，请稍后重试');
            }
        };

        // 做T开关: 点击切换单个股票的做T启用/暂停
        // 互斥: 启用做T时, 如该股本来是锁定的, 服务端也一并解锁
        const toggleT0 = async (row) => {
            if (!isAuthenticated.value) return;
            const stockCode = row.stock_code;
            const currentlyEnabled = !!row.t0_enabled;
            const action = currentlyEnabled ? 'disable' : 'enable';
            const wasLocked = !!row.is_locked;

            // Optimistic update
            row.t0_enabled = action === 'enable' ? 1 : 0;
            if (action === 'enable' && wasLocked) row.is_locked = 0;

            try {
                const response = await fetch('/api/position/t0', {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json',
                        'Authorization': `Bearer ${accessToken.value}`
                    },
                    body: JSON.stringify({
                        account_id: currentAccountId.value,
                        stock_code: stockCode,
                        action: action
                    })
                });
                const result = await response.json();
                if (response.ok && result.status === 'success') {
                    if (action === 'enable') {
                        // 互斥: 真正请求服务端解锁
                        if (wasLocked) {
                            try {
                                await fetch('/api/position/lock', {
                                    method: 'POST',
                                    headers: {
                                        'Content-Type': 'application/json',
                                        'Authorization': `Bearer ${accessToken.value}`
                                    },
                                    body: JSON.stringify({
                                        account_id: currentAccountId.value,
                                        stock_code: stockCode,
                                        is_locked: false
                                    })
                                });
                            } catch (e) {
                                console.warn('同步解锁失败:', e);
                            }
                        }
                        ElementPlus.ElMessage.success(`${stockCode} 做T已启用` + (wasLocked ? '（已自动解除锁定）' : ''));
                    } else {
                        ElementPlus.ElMessage.success(`${stockCode} 做T已暂停`);
                    }
                } else {
                    // 回滚
                    row.t0_enabled = currentlyEnabled ? 1 : 0;
                    if (action === 'enable' && wasLocked) row.is_locked = 1;
                    ElementPlus.ElMessage.error(result.message || '操作失败');
                }
            } catch (error) {
                // 回滚
                row.t0_enabled = currentlyEnabled ? 1 : 0;
                if (action === 'enable' && wasLocked) row.is_locked = 1;
                ElementPlus.ElMessage.error('请求出错: ' + error.message);
            }
        };

        // 判断某行是否已启用做T
        const isPendingT0 = (row) => {
            return !!row.t0_enabled;
        };

        // 删除单条持仓记录
        const deletePosition = async (row) => {
            if (!isAuthenticated.value) return;
            const stockCode = row.stock_code;
            const accountId = currentAccountId.value;

            if (accountId === 'all') {
                ElementPlus.ElMessage.warning('汇总视图不支持删除持仓，请切换到具体账号操作');
                return;
            }

            try {
                await ElementPlus.ElMessageBox.confirm(
                    `确定删除 ${row.instrument_name || stockCode} (${stockCode}) 的持仓记录吗？\n此操作仅从面板移除，不影响 QMT 实际持仓。`,
                    '确认删除',
                    { confirmButtonText: '删除', cancelButtonText: '取消', type: 'warning' }
                );
            } catch {
                return; // 用户取消
            }

            try {
                const url = `/api/position/${encodeURIComponent(stockCode)}?account_id=${encodeURIComponent(accountId)}`;
                const response = await fetch(url, {
                    method: 'DELETE',
                    headers: { 'Authorization': `Bearer ${accessToken.value}` }
                });
                const result = await response.json();
                if (response.ok) {
                    positions.value = positions.value.filter(p => p.stock_code !== stockCode);
                    ElementPlus.ElMessage.success(result.message || '已删除');
                } else {
                    ElementPlus.ElMessage.error(result.detail || '删除失败');
                }
            } catch (error) {
                ElementPlus.ElMessage.error('请求出错: ' + error.message);
            }
        };

        const submitSetClearPassword = async () => {
             if (!setClearPasswordForm.value.password) {
                 ElementPlus.ElMessage.warning('请输入新密码');
                 return;
             }
             if (setClearPasswordForm.value.password !== setClearPasswordForm.value.confirmPassword) {
                 ElementPlus.ElMessage.warning('两次输入的密码不一致');
                 return;
             }
             if (!currentAccountId.value || currentAccountId.value === 'all') {
                 ElementPlus.ElMessage.warning('请先选择一个具体的账号');
                 return;
             }
 
             try {
                 const response = await fetch('/api/account/clear-password', {
                     method: 'POST',
                     headers: { 
                         'Content-Type': 'application/json',
                         'Authorization': `Bearer ${accessToken.value}` 
                     },
                     body: JSON.stringify({
                         account_id: currentAccountId.value,
                         password: setClearPasswordForm.value.password
                     })
                 });

                if (response.ok) {
                    ElementPlus.ElMessage.success('清仓密码设置成功');
                    showSetClearPasswordDialog.value = false;
                    setClearPasswordForm.value.password = '';
                    setClearPasswordForm.value.confirmPassword = '';
                } else {
                    const result = await response.json();
                    ElementPlus.ElMessage.error(result.detail || '设置失败');
                }
            } catch (error) {
                ElementPlus.ElMessage.error('请求出错');
            }
        };

        const submitChangeLoginPassword = async () => {
            if (!changeLoginPasswordForm.value.oldPassword || !changeLoginPasswordForm.value.newPassword) {
                ElementPlus.ElMessage.warning('请输入完整信息');
                return;
            }
            if (changeLoginPasswordForm.value.newPassword !== changeLoginPasswordForm.value.confirmPassword) {
                ElementPlus.ElMessage.warning('两次输入的新密码不一致');
                return;
            }

            try {
                const response = await fetch('/api/account/change-password', {
                    method: 'POST',
                    headers: { 
                        'Content-Type': 'application/json',
                        'Authorization': `Bearer ${accessToken.value}` 
                    },
                    body: JSON.stringify({
                        old_password: changeLoginPasswordForm.value.oldPassword,
                        new_password: changeLoginPasswordForm.value.newPassword
                    })
                });

                if (response.ok) {
                    ElementPlus.ElMessage.success('登录密码修改成功，请重新登录');
                    showUserManagementDialog.value = false;
                    handleLogout();
                } else {
                    const result = await response.json();
                    ElementPlus.ElMessage.error(result.detail || '修改失败');
                }
            } catch (error) {
                ElementPlus.ElMessage.error('请求出错');
            }
        };

        const toggleLock = async (row) => {
            if (!isAuthenticated.value) return;
            
            const isAdmin = currentUser.value?.role === 'admin';
            const isAllView = currentAccountId.value === 'all';
            
            // 检查权限：
            // 1. 汇总视图只有管理员可以操作
            // 2. 普通用户只能操作自己的账号
            if (isAllView && !isAdmin) {
                ElementPlus.ElMessage.warning('只有管理员可以在汇总视图锁定/解锁持仓');
                return;
            }
            
            const newStatus = !row.is_locked;
            const wasT0 = !!row.t0_enabled;

            try {
                // Optimistic update
                row.is_locked = newStatus;
                if (newStatus && wasT0) row.t0_enabled = 0;

                const response = await fetch('/api/position/lock', {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json',
                        'Authorization': `Bearer ${accessToken.value}`
                    },
                    body: JSON.stringify({
                        account_id: currentAccountId.value,
                        stock_code: row.stock_code,
                        is_locked: newStatus
                    })
                });

                if (!response.ok) {
                    const errorData = await response.json();
                    throw new Error(errorData.detail || 'Lock update failed');
                }

                const result = await response.json();

                // 互斥: 锁定时真正请求服务端暂停做T
                if (newStatus && wasT0) {
                    try {
                        await fetch('/api/position/t0', {
                            method: 'POST',
                            headers: {
                                'Content-Type': 'application/json',
                                'Authorization': `Bearer ${accessToken.value}`
                            },
                            body: JSON.stringify({
                                account_id: currentAccountId.value,
                                stock_code: row.stock_code,
                                action: 'disable'
                            })
                        });
                    } catch (e) {
                        console.warn('同步暂停做T失败:', e);
                    }
                }

                // Update successful
                const mutexNote = (newStatus && wasT0) ? '（已自动暂停做T）' : '';
                if (isAllView && result.affected_accounts !== undefined) {
                    ElementPlus.ElMessage.success(
                        newStatus
                            ? `已为 ${result.affected_accounts} 个账号锁定 ${row.instrument_name || row.stock_code}${mutexNote}`
                            : `已为 ${result.affected_accounts} 个账号解锁 ${row.instrument_name || row.stock_code}`
                    );
                } else {
                    ElementPlus.ElMessage.success((newStatus ? '已锁定持仓' : '已解锁持仓') + mutexNote);
                }

            } catch (error) {
                console.error('Lock toggle failed:', error);
                // Revert change
                row.is_locked = !newStatus;
                if (newStatus && wasT0) row.t0_enabled = 1;
                ElementPlus.ElMessage.error(error.message || '操作失败，请重试');
            }
        };

        // 打开卖出对话框
        const openSellPositionDialog = (row) => {
            resetOrderPricing(row && row.stock_code, 'sell',
                              (row && row.account_id) || currentAccountId.value);
            if (!isAuthenticated.value) return;
            sellPositionStock.value = row;
            sellPositionPercentage.value = 100;
            showSellPositionDialog.value = true;
        };

        // 打开买入对话框
        const openBuyPositionDialog = (row) => {
            resetOrderPricing(row && row.stock_code, 'buy',
                              (row && row.account_id) || currentAccountId.value);
            if (!isAuthenticated.value) return;
            buyPositionStock.value = row;
            buyPositionPercentage.value = 100;
            showBuyPositionDialog.value = true;
        };

        // 撤单。委托号是 order_id，服务端据此调 cancel_order_stock。
        const cancelPendingOrder = async (row) => {
            if (!row || !row.cancelable) return;
            const key = `${row.account_id}|${row.order_id}`;
            if (cancellingOrders.value.has(key)) return;
            try {
                await ElementPlus.ElMessageBox.confirm(
                    `撤销 ${row.side_label} ${row.stock_name || row.stock_code} `
                    + `${row.left_volume}${row.unit}（委托号 ${row.order_sysid || row.order_id}）？`,
                    '撤单确认', { type: 'warning', confirmButtonText: '撤单', cancelButtonText: '取消' });
            } catch (e) {
                return;   // 用户点了取消
            }
            cancellingOrders.value = new Set(cancellingOrders.value).add(key);
            try {
                const response = await fetch('/api/orders/cancel', {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json',
                        'Authorization': `Bearer ${accessToken.value}`
                    },
                    body: JSON.stringify({
                        account_id: row.account_id,
                        order_id: String(row.order_id)
                    })
                });
                const result = await response.json().catch(() => ({}));
                if (response.ok) {
                    ElementPlus.ElMessage.success(result.message || '撤单已提交');
                    loadTradeFlow(true);
                } else {
                    ElementPlus.ElMessage.error(result.detail || '撤单失败');
                }
            } catch (e) {
                ElementPlus.ElMessage.error('撤单请求出错');
            } finally {
                const next = new Set(cancellingOrders.value);
                next.delete(key);
                cancellingOrders.value = next;
            }
        };

        const isCancelling = (row) =>
            cancellingOrders.value.has(`${row.account_id}|${row.order_id}`);

        // 下单结果提示：单笔带委托号，批量带成功/失败笔数。
        // 改造前接口只回「已下发，等客户端执行」，成没成要等下一次同步才知道。
        const formatOrderResult = (result) => {
            if (!result) return '已提交';
            if (result.order_sys_id) {
                return `${result.message || '已报单'}（委托号 ${result.order_sys_id}）`;
            }
            if (result.succeeded !== undefined) {
                return result.message || `成功 ${result.succeeded} 笔，失败 ${result.failed} 笔`;
            }
            return result.message || '已提交';
        };

        // 打开新买入股票对话框
        const openNewBuyDialog = () => {
            if (!isAuthenticated.value) return;
            newBuyStockQuery.value = '';
            newBuyStockOptions.value = [];
            newBuySelectedStock.value = null;
            newBuyAmount.value = 100;
            resetOrderPricing(null, 'buy', currentAccountId.value);
            newBuyMode.value = 'volume';
            newBuyCash.value = 10000;
            newBuyQuote.value = null;
            showNewBuyDialog.value = true;
        };

        // 远程搜索股票/ETF：250ms 防抖 + 至少2字符，减少按键请求数
        let stockSearchTimer = null;
        let stockSearchSeq = 0;   // 丢弃过期响应：只采用最后一次请求的结果
        const searchStocksRemote = (query) => {
            const q = (query || '').trim();
            if (stockSearchTimer) clearTimeout(stockSearchTimer);
            if (q.length < 2) {
                newBuyStockOptions.value = [];
                return;
            }
            stockSearchTimer = setTimeout(async () => {
                const seq = ++stockSearchSeq;
                try {
                    const response = await fetch(`/api/stocks/search?q=${encodeURIComponent(q)}`, {
                        headers: { 'Authorization': `Bearer ${accessToken.value}` }
                    });
                    if (response.ok && seq === stockSearchSeq) {
                        newBuyStockOptions.value = await response.json();
                    }
                } catch (e) {
                    console.error('搜索股票失败', e);
                }
            }, 250);
        };

        // 确认新买入股票
        const confirmNewBuy = async (force = false) => {
            if (!newBuySelectedStock.value) {
                ElementPlus.ElMessage.warning('请先选择股票');
                return;
            }
            if (orderPriceError.value) {
                ElementPlus.ElMessage.warning(orderPriceError.value);
                return;
            }
            const spec = newBuySpec.value;
            if (newBuyMode.value === 'cash') {
                if (!newBuyCash.value || newBuyCash.value <= 0) {
                    ElementPlus.ElMessage.warning('请填写买入金额');
                    return;
                }
                if (newBuyCashError.value) {
                    ElementPlus.ElMessage.warning(newBuyCashError.value);
                    return;
                }
            } else {
                // 按品种校验：转债 10 张起步进 10，科创板 200 股起按 1 递增。
                // 改造前这里写死「100 的整数倍」，转债根本下不了单。
                const amount = newBuyAmount.value;
                if (!amount || amount < spec.min || (amount - spec.min) % spec.step !== 0) {
                    ElementPlus.ElMessage.warning(
                        `${spec.kindName}最少 ${spec.min}${spec.unit}，之后按 ${spec.step}${spec.unit} 递增`);
                    return;
                }
            }
            try {
                const payload = {
                    account_id: currentAccountId.value,
                    stock_code: newBuySelectedStock.value.ts_code,
                    stock_name: newBuySelectedStock.value.name,
                    ...orderPricingPayload()
                };
                // 按金额下单交给服务端换算：它有实时价，也有品种规整规则
                if (newBuyMode.value === 'cash') {
                    payload.cash_amount = newBuyCash.value;
                } else {
                    payload.amount = newBuyAmount.value;
                }
                if (force === true) {
                    payload.force = true;
                }
                const response = await fetch('/api/position/buy_new', {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json',
                        'Authorization': `Bearer ${accessToken.value}`
                    },
                    body: JSON.stringify(payload)
                });
                if (response.ok) {
                    const result = await response.json();
                    ElementPlus.ElMessage.success(formatOrderResult(result));
                    showNewBuyDialog.value = false;
                } else {
                    const error = await response.json();
                    ElementPlus.ElMessage.error(error.detail || '设置失败');
                }
            } catch (e) {
                console.error('新买入股票失败', e);
                ElementPlus.ElMessage.error('请求出错');
            }
        };

        // 计算买入数量（展示用）
        // 加仓弹窗的预计金额：数量 × 现价，现价直接用持仓行上的
        const calcBuyCash = (row, percentage) => {
            const volume = calcBuyAmount(row, percentage);
            const price = row && (row.last_price || row.avg_price);
            if (!volume || !price) return null;
            return volume * price;
        };

        const formatCash = (value) => {
            if (value === null || value === undefined) return '—';
            const n = Number(value);
            if (!isFinite(n)) return '—';
            return n >= 10000 ? (n / 10000).toFixed(2) + ' 万元'
                              : n.toFixed(2) + ' 元';
        };

        const calcBuyAmount = (row, percentage) => {
            if (!row || !row.volume) return 0;
            const spec = instrumentSpec(row.stock_code);
            const raw = Math.floor(row.volume * percentage / 100);
            if (raw < spec.min) return 0;
            return spec.min + Math.floor((raw - spec.min) / spec.step) * spec.step;
        };

        // ===== 买卖流水（账户真实成交，买+卖）=====
        // 取代原来的「历史买入列表」—— 那个展示的是 stock_market_data 的选股信号，
        // 表里没有买卖方向。这里的数据来自 trades 表，由轮询和实时回报填充。
        const loadTradeFlow = async (silent = false) => {
            if (!isAuthenticated.value && !isViewer.value) return;
            if (!silent) tradeFlowLoading.value = true;
            try {
                const params = new URLSearchParams({
                    account_id: currentAccountId.value || 'all',
                    days: String(tradeFlowDays.value),
                    side: tradeFlowSide.value,
                    q: tradeFlowQuery.value || ''
                });
                const response = await fetch(`/api/trade-flow?${params}`, {
                    headers: { 'Authorization': `Bearer ${accessToken.value}` }
                });
                if (response.ok) {
                    const data = await response.json();
                    const records = data.records || [];
                    maybeNotifyNewTrades(records);   // 有新成交时弹浏览器通知
                    tradeFlowRecords.value = records;
                    tradeFlowPending.value = data.pending || [];
                    tradeFlowSummary.value = data.summary || {};
                }
            } catch (e) {
                console.error('加载买卖流水失败:', e);
            } finally {
                if (!silent) tradeFlowLoading.value = false;
            }
        };

        watch([tradeFlowSide, tradeFlowDays, tradeFlowQuery, currentAccountId], () => loadTradeFlow(true));

        // 取消卖出
        const cancelSellPosition = async (row) => {
            try {
                const response = await fetch('/api/position/sell/cancel', {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json',
                        'Authorization': `Bearer ${accessToken.value}`
                    },
                    body: JSON.stringify({
                        account_id: currentAccountId.value,
                        stock_code: row.stock_code,
                        percentage: 0
                    })
                });

                if (response.ok) {
                    const result = await response.json();
                    // 汇总视图下需要清除所有相关账号的卖出状态
                    if (currentAccountId.value === 'all') {
                        // 清除所有该股票的卖出状态
                        for (const [key, value] of pendingSellPositions.value.entries()) {
                            if (value.stock_code === row.stock_code) {
                                pendingSellPositions.value.delete(key);
                            }
                        }
                    } else {
                        const key = `${currentAccountId.value}_${row.stock_code}`;
                        pendingSellPositions.value.delete(key);
                    }
                    ElementPlus.ElMessage.success(result.message);
                } else {
                    const error = await response.json();
                    ElementPlus.ElMessage.error(error.detail || '取消失败');
                }
            } catch (error) {
                console.error('Cancel sell failed:', error);
                ElementPlus.ElMessage.error('请求出错');
            }
        };

        // 取消买入
        const cancelBuyPosition = async (row) => {
            try {
                const response = await fetch('/api/position/buy/cancel', {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json',
                        'Authorization': `Bearer ${accessToken.value}`
                    },
                    body: JSON.stringify({
                        account_id: currentAccountId.value,
                        stock_code: row.stock_code,
                        percentage: 0
                    })
                });

                if (response.ok) {
                    const result = await response.json();
                    if (currentAccountId.value === 'all') {
                        for (const [key, value] of pendingBuyPositions.value.entries()) {
                            if (value.stock_code === row.stock_code) {
                                pendingBuyPositions.value.delete(key);
                            }
                        }
                    } else {
                        const key = `${currentAccountId.value}_${row.stock_code}`;
                        pendingBuyPositions.value.delete(key);
                    }
                    ElementPlus.ElMessage.success(result.message);
                } else {
                    const error = await response.json();
                    ElementPlus.ElMessage.error(error.detail || '取消失败');
                }
            } catch (error) {
                console.error('Cancel buy failed:', error);
                ElementPlus.ElMessage.error('请求出错');
            }
        };

        // 确认卖出
        const confirmSellPosition = async () => {
            if (!sellPositionStock.value) return;

            const row = sellPositionStock.value;

            try {
                const response = await fetch('/api/position/sell', {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json',
                        'Authorization': `Bearer ${accessToken.value}`
                    },
                    body: JSON.stringify({
                        account_id: currentAccountId.value,
                        stock_code: row.stock_code,
                        percentage: sellPositionPercentage.value,
                        ...orderPricingPayload()
                    })
                });

                if (response.ok) {
                    const result = await response.json();
                    // 汇总视图下，为所有相关账号添加卖出状态
                    if (currentAccountId.value === 'all' && result.affected_accounts) {
                        // 使用一个特殊的key来标识汇总视图的卖出状态
                        const key = `all_${row.stock_code}`;
                        pendingSellPositions.value.set(key, {
                            stock_code: row.stock_code,
                            percentage: sellPositionPercentage.value,
                            account_id: 'all'
                        });
                    } else {
                        const key = `${currentAccountId.value}_${row.stock_code}`;
                        pendingSellPositions.value.set(key, {
                            stock_code: row.stock_code,
                            percentage: sellPositionPercentage.value,
                            account_id: currentAccountId.value
                        });
                    }
                    ElementPlus.ElMessage.success(formatOrderResult(result));
                    showSellPositionDialog.value = false;
                    sellPositionStock.value = null;
                } else {
                    const error = await response.json();
                    ElementPlus.ElMessage.error(error.detail || '设置失败');
                }
            } catch (error) {
                console.error('Set sell command failed:', error);
                ElementPlus.ElMessage.error('请求出错');
            }
        };

        // 确认买入
        const confirmBuyPosition = async () => {
            if (!buyPositionStock.value) return;

            const row = buyPositionStock.value;

            try {
                const response = await fetch('/api/position/buy', {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json',
                        'Authorization': `Bearer ${accessToken.value}`
                    },
                    body: JSON.stringify({
                        account_id: currentAccountId.value,
                        stock_code: row.stock_code,
                        percentage: buyPositionPercentage.value,
                        ...orderPricingPayload()
                    })
                });

                if (response.ok) {
                    const result = await response.json();
                    if (currentAccountId.value === 'all' && result.affected_accounts) {
                        const key = `all_${row.stock_code}`;
                        pendingBuyPositions.value.set(key, {
                            stock_code: row.stock_code,
                            percentage: buyPositionPercentage.value,
                            account_id: 'all'
                        });
                    } else {
                        const key = `${currentAccountId.value}_${row.stock_code}`;
                        pendingBuyPositions.value.set(key, {
                            stock_code: row.stock_code,
                            percentage: buyPositionPercentage.value,
                            account_id: currentAccountId.value
                        });
                    }
                    ElementPlus.ElMessage.success(result.message);
                    showBuyPositionDialog.value = false;
                    buyPositionStock.value = null;
                } else {
                    const error = await response.json();
                    ElementPlus.ElMessage.error(error.detail || '设置失败');
                }
            } catch (error) {
                console.error('Set buy command failed:', error);
                ElementPlus.ElMessage.error('请求出错');
            }
        };

        // 检查是否处于待卖出状态
        const isPendingSell = (row) => {
            // 汇总视图下，检查是否有任何账号设置了该股票的卖出
            if (currentAccountId.value === 'all') {
                for (const [key, value] of pendingSellPositions.value.entries()) {
                    if (value.stock_code === row.stock_code) {
                        return true;
                    }
                }
                return false;
            }
            const key = `${currentAccountId.value}_${row.stock_code}`;
            return pendingSellPositions.value.has(key);
        };

        // 获取待卖出比例
        const getPendingSellPercentage = (row) => {
            // 汇总视图下，返回第一个找到的卖出比例
            if (currentAccountId.value === 'all') {
                for (const [key, value] of pendingSellPositions.value.entries()) {
                    if (value.stock_code === row.stock_code) {
                        return value.percentage;
                    }
                }
                return 0;
            }
            const key = `${currentAccountId.value}_${row.stock_code}`;
            const data = pendingSellPositions.value.get(key);
            return data ? data.percentage : 0;
        };

        // 检查是否处于待买入状态
        const isPendingBuy = (row) => {
            if (currentAccountId.value === 'all') {
                for (const [key, value] of pendingBuyPositions.value.entries()) {
                    if (value.stock_code === row.stock_code) {
                        return true;
                    }
                }
                return false;
            }
            const key = `${currentAccountId.value}_${row.stock_code}`;
            return pendingBuyPositions.value.has(key);
        };

        // 获取待买入比例
        const getPendingBuyPercentage = (row) => {
            if (currentAccountId.value === 'all') {
                for (const [key, value] of pendingBuyPositions.value.entries()) {
                    if (value.stock_code === row.stock_code) {
                        return value.percentage;
                    }
                }
                return 0;
            }
            const key = `${currentAccountId.value}_${row.stock_code}`;
            const data = pendingBuyPositions.value.get(key);
            return data ? data.percentage : 0;
        };

        const loadAdminUsers = async () => {
            if (currentUser.value?.role !== 'admin') return;
            try {
                const response = await fetch('/api/admin/users', {
                    headers: { 'Authorization': `Bearer ${accessToken.value}` }
                });
                if (response.ok) {
                    const data = await response.json();
                    adminUsersList.value = data.users || [];
                }
            } catch (error) {
                console.error('加载用户列表失败:', error);
            }
        };

        // ===== 用户登录统计（管理员）=====
        const userStats = ref({ account_users: [], viewers: [] });
        const userStatsLoading = ref(false);
        const mergedAccountStats = computed(() => {
            const byId = {};
            (users.value || []).forEach(u => { if (u && u.account_id) byId[u.account_id] = u; });
            return (userStats.value.account_users || []).map(a => {
                const u = byId[a.account_id] || {};
                return { ...a, online: u.online, last_sync: u.last_sync || u.data_time || '' };
            });
        });
        const formatOnlineDuration = (sec) => {
            sec = Number(sec) || 0;
            if (sec < 60) return sec + '秒';
            const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60);
            return h > 0 ? `${h}小时${m}分` : `${m}分`;
        };
        const loadUserStats = async () => {
            if (currentUser.value?.role !== 'admin') return;
            userStatsLoading.value = true;
            try {
                const resp = await fetch('/api/admin/user-stats', { headers: { 'Authorization': `Bearer ${accessToken.value}` } });
                if (resp.ok) userStats.value = await resp.json();
            } catch (e) { console.error('加载用户统计失败:', e); }
            finally { userStatsLoading.value = false; }
        };

        const loadLLMConfig = async () => {
            if (currentUser.value?.role !== 'admin') return;
            llmConfigLoading.value = true;
            try {
                const response = await fetch('/api/llm-config', {
                    headers: { 'Authorization': `Bearer ${accessToken.value}` }
                });
                if (response.ok) {
                    const data = await response.json();
                    llmConfigForm.value = data.config || { api_url: '', api_key: '', model: '' };
                }
            } catch (error) {
                ElementPlus.ElMessage.error('加载大模型配置失败');
            } finally {
                llmConfigLoading.value = false;
            }
        };

        const saveLLMConfig = async () => {
            llmConfigSaving.value = true;
            try {
                const response = await fetch('/api/llm-config', {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json',
                        'Authorization': `Bearer ${accessToken.value}`
                    },
                    body: JSON.stringify(llmConfigForm.value)
                });
                const data = await response.json().catch(() => ({}));
                if (response.ok) {
                    ElementPlus.ElMessage.success(data.message || '大模型配置已保存');
                    await loadLLMConfig();
                } else {
                    ElementPlus.ElMessage.error(data.detail || '保存失败');
                }
            } catch (error) {
                ElementPlus.ElMessage.error('保存大模型配置失败');
            } finally {
                llmConfigSaving.value = false;
            }
        };

        const loadResearchBoard = async (resetPage = true, refreshChange = false) => {
            if (!isAuthenticated.value) return;
            if (resetPage) researchBoardLoading.value = true;
            try {
                // 点「刷新」时顺带让后端立即拉取最新涨跌幅（不必等定时任务）
                if (refreshChange) {
                    try {
                        await fetch('/api/research-board/refresh-change', {
                            method: 'POST',
                            headers: { 'Authorization': `Bearer ${accessToken.value}` }
                        });
                    } catch (e) { /* 涨跌幅刷新失败不阻塞列表加载 */ }
                }
                const response = await fetch('/api/research-board', {
                    headers: { 'Authorization': `Bearer ${accessToken.value}` }
                });
                if (response.ok) {
                    const data = await response.json();
                    researchBoardRecords.value = data.records || [];
                    if (resetPage) researchBoardPage.value = 1;
                }
            } catch (error) {
                console.error('加载记录板失败:', error);
            } finally {
                researchBoardLoading.value = false;
            }
        };

        const openResearchBoardLargeDialog = async () => {
            showResearchBoardLargeDialog.value = true;
            await loadResearchBoard();
        };

        const loadTScan = async () => {
            if (!isAuthenticated.value || !currentAccountId.value) return;
            tScanLoading.value = true;
            try {
                const response = await fetch(`/api/t0-scan?account_id=${encodeURIComponent(currentAccountId.value)}`, {
                    headers: { 'Authorization': `Bearer ${accessToken.value}` }
                });
                const data = await response.json().catch(() => ({}));
                if (response.ok) {
                    tScanRows.value = data.rows || [];
                    tScanSummary.value = data.summary || {};
                    tScanTradeDate.value = data.trade_date || '';
                    tScanScanTime.value = data.scan_time || '';
                } else {
                    ElementPlus.ElMessage.error(data.detail || 'T扫描失败');
                }
            } catch (error) {
                console.error('T扫描失败:', error);
                ElementPlus.ElMessage.error('T扫描请求出错');
            } finally {
                tScanLoading.value = false;
            }
        };

        const openTScanDialog = async () => {
            showTScanDialog.value = true;
            await loadTScan();
        };

        const processTScanCommandQueue = async () => {
            if (tScanQueueRunning.value) return;
            tScanQueueRunning.value = true;
            try {
                while (tScanQueueCursor < tScanCommandQueue.value.length) {
                    const command = tScanCommandQueue.value[tScanQueueCursor];
                    tScanQueueCursor += 1;
                    if (!command || !command.key) continue;
                    clearTScanRowState(tScanQueuedRows, command.key);
                    if (tScanHandledRows.value[command.key]) continue;

                    tScanProcessingRows.value = {
                        ...tScanProcessingRows.value,
                        [command.key]: {
                            side: command.side,
                            started_at: Date.now()
                        }
                    };

                    try {
                        const response = await fetch(command.side === 'buy' ? '/api/position/buy_new' : '/api/position/sell_amount', {
                            method: 'POST',
                            headers: {
                                'Content-Type': 'application/json',
                                'Authorization': `Bearer ${accessToken.value}`
                            },
                            body: JSON.stringify(command.payload)
                        });
                        const data = await response.json().catch(() => ({}));
                        if (response.ok) {
                            markTScanRowHandled(command.row, command.side);
                            ElementPlus.ElMessage.success(data.message || '指令已下发');
                        } else {
                            ElementPlus.ElMessage.error(data.detail || '指令下发失败');
                        }
                    } catch (error) {
                        console.error('T扫描指令下发失败:', error);
                        ElementPlus.ElMessage.error('指令下发请求出错');
                    } finally {
                        clearTScanRowState(tScanProcessingRows, command.key);
                    }
                }
            } finally {
                tScanCommandQueue.value = [];
                tScanQueueCursor = 0;
                tScanQueueRunning.value = false;
            }
        };

        const submitTScanTrade = async (row, side) => {
            if (!row || !row.account_id || !row.stock_code) return;
            if (displayMode.value) {
                ElementPlus.ElMessage.warning('展示模式下不能下发交易指令');
                return;
            }
            const amount = Number(row.suggested_volume) || 0;
            if (amount <= 0 || amount % 100 !== 0) {
                ElementPlus.ElMessage.warning('下发股数必须是100的正整数倍');
                return;
            }
            if (side === 'buy' && row.action !== '买入') {
                ElementPlus.ElMessage.warning('当前记录不是买入建议');
                return;
            }
            if (side === 'sell' && row.action !== '卖出') {
                ElementPlus.ElMessage.warning('当前记录不是卖出建议');
                return;
            }

            const key = getTScanRowKey(row);
            if (tScanHandledRows.value[key] || tScanQueuedRows.value[key] || tScanProcessingRows.value[key]) {
                return;
            }
            const payload = {
                account_id: row.account_id,
                stock_code: row.stock_code,
                stock_name: row.stock_name || '',
                amount: row.suggested_volume
            };

            tScanQueuedRows.value = {
                ...tScanQueuedRows.value,
                [key]: {
                    side,
                    queued_at: Date.now()
                }
            };
            tScanCommandQueue.value.push({
                key,
                side,
                row: { ...row },
                payload
            });
            processTScanCommandQueue();
        };

        const parseResearchBoard = async () => {
            if (!researchBoardInput.value.trim()) {
                ElementPlus.ElMessage.warning('请输入要解析的记录');
                return;
            }
            if (researchBoardPollTimer) {
                clearInterval(researchBoardPollTimer);
                researchBoardPollTimer = null;
            }
            researchBoardParsing.value = true;
            researchBoardTaskMessage.value = '解析任务已提交，等待大模型和行情数据返回...';
            try {
                const response = await fetch('/api/research-board/parse', {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json',
                        'Authorization': `Bearer ${accessToken.value}`
                    },
                    body: JSON.stringify({ content: researchBoardInput.value })
                });
                const data = await response.json().catch(() => ({}));
                if (response.ok) {
                    ElementPlus.ElMessage.info(data.message || '记录板解析已提交');
                    researchBoardInput.value = '';
                    pollResearchBoardTask(data.task_id);
                } else {
                    ElementPlus.ElMessage.error(data.detail || '解析失败');
                    researchBoardTaskMessage.value = '';
                    researchBoardParsing.value = false;
                }
            } catch (error) {
                ElementPlus.ElMessage.error('解析请求失败');
                researchBoardTaskMessage.value = '';
                researchBoardParsing.value = false;
            }
        };

        const pollResearchBoardTask = (taskId) => {
            if (!taskId) {
                researchBoardTaskMessage.value = '';
                researchBoardParsing.value = false;
                return;
            }
            const poll = async () => {
                try {
                    const response = await fetch(`/api/research-board/tasks/${taskId}`, {
                        headers: { 'Authorization': `Bearer ${accessToken.value}` }
                    });
                    const data = await response.json().catch(() => ({}));
                    if (!response.ok) {
                        throw new Error(data.detail || '任务查询失败');
                    }
                    if (data.status === 'completed') {
                        clearInterval(researchBoardPollTimer);
                        researchBoardPollTimer = null;
                        researchBoardTaskMessage.value = `解析完成：新增 ${data.added || 0} 条，合并 ${data.merged || 0} 条`;
                        researchBoardParsing.value = false;
                        await loadResearchBoard();
                        ElementPlus.ElMessage.success(researchBoardTaskMessage.value);
                        setTimeout(() => {
                            if (!researchBoardParsing.value) researchBoardTaskMessage.value = '';
                        }, 3000);
                    } else if (data.status === 'failed') {
                        clearInterval(researchBoardPollTimer);
                        researchBoardPollTimer = null;
                        researchBoardTaskMessage.value = '';
                        researchBoardParsing.value = false;
                        ElementPlus.ElMessage.error(data.error || '解析失败');
                    } else {
                        researchBoardTaskMessage.value = '解析中，完成后会自动刷新表格...';
                    }
                } catch (error) {
                    clearInterval(researchBoardPollTimer);
                    researchBoardPollTimer = null;
                    researchBoardTaskMessage.value = '';
                    researchBoardParsing.value = false;
                    ElementPlus.ElMessage.error(error.message || '任务查询失败');
                }
            };
            poll();
            researchBoardPollTimer = setInterval(poll, 1500);
        };

        const openResearchBoardEditDialog = (row) => {
            researchBoardEditForm.value = {
                id: row.id,
                stock_name: row.stock_name || '',
                stock_code: row.stock_code || '',
                logic: row.logic || '',
                target_market_value_yi: row.target_market_value_yi,
                industry: row.industry || '',
                concept: row.concept || '',
                limit_up_reason: row.limit_up_reason || '',
                topic: row.topic || ''
            };
            showResearchBoardEditDialog.value = true;
        };

        const saveResearchBoardEdit = async () => {
            if (!researchBoardEditForm.value.id) return;
            researchBoardEditing.value = true;
            try {
                const response = await fetch(`/api/research-board/${researchBoardEditForm.value.id}`, {
                    method: 'PUT',
                    headers: {
                        'Content-Type': 'application/json',
                        'Authorization': `Bearer ${accessToken.value}`
                    },
                    body: JSON.stringify(researchBoardEditForm.value)
                });
                const data = await response.json().catch(() => ({}));
                if (response.ok) {
                    ElementPlus.ElMessage.success(data.message || '记录已更新');
                    showResearchBoardEditDialog.value = false;
                    await loadResearchBoard();
                } else {
                    ElementPlus.ElMessage.error(data.detail || '更新失败');
                }
            } catch (error) {
                ElementPlus.ElMessage.error('更新请求失败');
            } finally {
                researchBoardEditing.value = false;
            }
        };

        const deleteResearchBoardRecord = async (row) => {
            try {
                const response = await fetch(`/api/research-board/${row.id}`, {
                    method: 'DELETE',
                    headers: { 'Authorization': `Bearer ${accessToken.value}` }
                });
                if (response.ok) {
                    ElementPlus.ElMessage.success('记录已删除');
                    await loadResearchBoard();
                } else {
                    const data = await response.json().catch(() => ({}));
                    ElementPlus.ElMessage.error(data.detail || '删除失败');
                }
            } catch (error) {
                ElementPlus.ElMessage.error('删除请求失败');
            }
        };

        // 一键修复：用 bak_basic 重新整理所有记录的名称/代码/市值/题材
        const repairResearchBoard = async () => {
            if (researchBoardRepairing.value) return;
            if (researchBoardPollTimer) { clearInterval(researchBoardPollTimer); researchBoardPollTimer = null; }
            researchBoardRepairing.value = true;
            researchBoardTaskMessage.value = '一键修复已提交，正在重新整理记录...';
            try {
                const response = await fetch('/api/research-board/repair', {
                    method: 'POST',
                    headers: { 'Authorization': `Bearer ${accessToken.value}` }
                });
                const data = await response.json().catch(() => ({}));
                if (!response.ok) {
                    ElementPlus.ElMessage.error(data.detail || '一键修复提交失败');
                    researchBoardTaskMessage.value = '';
                    researchBoardRepairing.value = false;
                    return;
                }
                const taskId = data.task_id;
                const poll = async () => {
                    try {
                        const r = await fetch(`/api/research-board/tasks/${taskId}`, {
                            headers: { 'Authorization': `Bearer ${accessToken.value}` }
                        });
                        const t = await r.json().catch(() => ({}));
                        if (t.status === 'completed') {
                            clearInterval(researchBoardPollTimer); researchBoardPollTimer = null;
                            researchBoardRepairing.value = false;
                            researchBoardTaskMessage.value = `修复完成：共 ${t.total || 0} 条，修正 ${t.fixed || 0} 条`;
                            await loadResearchBoard();
                            ElementPlus.ElMessage.success(researchBoardTaskMessage.value);
                            setTimeout(() => {
                                if (!researchBoardRepairing.value && !researchBoardParsing.value) researchBoardTaskMessage.value = '';
                            }, 4000);
                        } else if (t.status === 'failed') {
                            clearInterval(researchBoardPollTimer); researchBoardPollTimer = null;
                            researchBoardRepairing.value = false;
                            researchBoardTaskMessage.value = '';
                            ElementPlus.ElMessage.error(t.error || '一键修复失败');
                        } else {
                            researchBoardTaskMessage.value = `修复中... ${t.processed || 0}/${t.total || 0}，已修正 ${t.fixed || 0} 条`;
                        }
                    } catch (e) {
                        clearInterval(researchBoardPollTimer); researchBoardPollTimer = null;
                        researchBoardRepairing.value = false;
                        researchBoardTaskMessage.value = '';
                    }
                };
                poll();
                researchBoardPollTimer = setInterval(poll, 1500);
            } catch (error) {
                ElementPlus.ElMessage.error('一键修复请求失败');
                researchBoardTaskMessage.value = '';
                researchBoardRepairing.value = false;
            }
        };

        // 归档复核：查看每次输入原文 + 大模型返回 + 解析结果
        const loadResearchBoardInputs = async () => {
            researchBoardInputsLoading.value = true;
            try {
                const r = await fetch('/api/research-board/inputs?limit=100', {
                    headers: { 'Authorization': `Bearer ${accessToken.value}` }
                });
                const d = await r.json().catch(() => ({}));
                if (r.ok) {
                    researchBoardInputs.value = d.inputs || [];
                } else {
                    ElementPlus.ElMessage.error(d.detail || '加载归档失败');
                }
            } catch (e) {
                ElementPlus.ElMessage.error('加载归档失败');
            } finally {
                researchBoardInputsLoading.value = false;
            }
        };
        const openResearchBoardInputsDialog = () => {
            showResearchBoardInputsDialog.value = true;
            loadResearchBoardInputs();
        };
        const formatJson = (s) => {
            if (!s) return '--';
            try { return JSON.stringify(JSON.parse(s), null, 2); } catch (e) { return s; }
        };

        // 触发 K 线 AI 分析：打开弹窗并请求后端
        const analyzeResearchBoardKline = async (row) => {
            if (!row || !row.id) return;
            if (!row.stock_code) {
                ElementPlus.ElMessage.warning('该记录缺少股票代码，无法分析');
                return;
            }
            klineAnalysisData.value = {
                stock_name: row.stock_name || '',
                stock_code: row.stock_code || '',
                source: '',
                analysis: '',
                stats: null,
                bars: []
            };
            showKlineAnalysisDialog.value = true;
            klineAnalysisLoading.value = true;
            try {
                const response = await fetch(`/api/research-board/${row.id}/kline-analysis`, {
                    method: 'POST',
                    headers: { 'Authorization': `Bearer ${accessToken.value}` }
                });
                const data = await response.json().catch(() => ({}));
                if (!response.ok) {
                    ElementPlus.ElMessage.error(data.detail || 'K线分析失败');
                    klineAnalysisData.value.analysis = data.detail || '请求失败';
                    return;
                }
                klineAnalysisData.value = {
                    stock_name: data.stock_name || row.stock_name || '',
                    stock_code: data.stock_code || row.stock_code || '',
                    source: data.source || '',
                    analysis: data.analysis || '',
                    stats: data.stats || null,
                    bars: Array.isArray(data.bars) ? data.bars : []
                };
                if (data.source === 'llm') {
                    ElementPlus.ElMessage.success('K线分析已生成');
                } else if (data.source === 'fallback') {
                    ElementPlus.ElMessage.info('未配置大模型或调用失败，已使用规则摘要');
                }
            } catch (error) {
                ElementPlus.ElMessage.error('K线分析请求失败');
                klineAnalysisData.value.analysis = error.message || '请求失败';
            } finally {
                klineAnalysisLoading.value = false;
            }
        };

        // 打开策略配置对话框（自动选择第一个可用账号）
        const openConfigDialog = () => {
            showConfigDialog.value = true;
            // 等 el-dialog / el-select 渲染完成后设置默认账号并加载
            nextTick(() => {
                if (!configAccountId.value && configTargetAccounts.value.length > 0) {
                    configAccountId.value = configTargetAccounts.value[0].account_id;
                }
                loadConfig();
            });
        };

        // 加载指定账号的 strategy.ini 配置
        const loadConfig = async () => {
            if (!configAccountId.value) {
                configContent.value = '# 请先在上方选择一个目标账号';
                return;
            }
            configLoading.value = true;
            configContent.value = '# 加载中...';
            try {
                const resp = await fetch(`/api/config/strategy?account_id=${encodeURIComponent(configAccountId.value)}`, {
                    headers: { 'Authorization': `Bearer ${accessToken.value}` }
                });
                if (resp.ok) {
                    const data = await resp.json();
                    configContent.value = data.content || '# 暂无配置内容\n# 保存后将在此处显示';
                    configLoadTime.value = new Date().toLocaleString();
                    if (data.note) {
                        ElementPlus.ElMessage.info(data.note);
                    }
                } else if (resp.status === 403) {
                    configContent.value = '# 错误: 无权限访问策略配置，仅管理员可操作';
                    ElementPlus.ElMessage.error('仅管理员可管理远程策略配置');
                } else {
                    const err = await resp.json().catch(() => ({}));
                    configContent.value = `# 请求失败 (${resp.status}): ${err.detail || '未知错误'}`;
                    ElementPlus.ElMessage.error(`加载配置失败: ${err.detail || resp.statusText}`);
                }
            } catch (e) {
                configContent.value = '# 网络错误: ' + e.message;
                ElementPlus.ElMessage.error('加载配置失败: ' + e.message);
            } finally {
                configLoading.value = false;
            }
        };

        // 刷新 (重新拉取)
        const refreshConfig = () => { loadConfig(); };

        // 保存并下发 strategy.ini
        const saveConfig = async () => {
            if (!configAccountId.value) {
                ElementPlus.ElMessage.warning('请先选择目标账号');
                return;
            }
            configSaving.value = true;
            try {
                const resp = await fetch('/api/config/strategy', {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json',
                        'Authorization': `Bearer ${accessToken.value}`
                    },
                    body: JSON.stringify({
                        account_id: configAccountId.value,
                        content: configContent.value
                    })
                });
                if (resp.ok) {
                    const data = await resp.json();
                    if (data.status === 'success') {
                        ElementPlus.ElMessage.success(data.message || '配置已下发');
                        configLoadTime.value = new Date().toLocaleString();
                    } else if (data.status === 'skipped') {
                        ElementPlus.ElMessage.info(data.message || '内容未变更');
                    } else {
                        ElementPlus.ElMessage.error(data.message || '下发失败');
                    }
                } else {
                    const err = await resp.json();
                    ElementPlus.ElMessage.error(err.detail || '请求失败');
                }
            } catch (e) {
                ElementPlus.ElMessage.error('保存失败: ' + e.message);
            } finally {
                configSaving.value = false;
            }
        };

        const submitCreateUser = async () => {
            if (!newUserForm.value.username || !newUserForm.value.password || !newUserForm.value.account_id) {
                ElementPlus.ElMessage.warning('请填写必填项');
                return;
            }

            try {
                const response = await fetch('/api/admin/create-user', {
                    method: 'POST',
                    headers: { 
                        'Content-Type': 'application/json',
                        'Authorization': `Bearer ${accessToken.value}` 
                    },
                    body: JSON.stringify(newUserForm.value)
                });

                if (response.ok) {
                    const result = await response.json();
                    ElementPlus.ElMessage.success(result.message || '用户创建成功');
                    newUserForm.value = {
                        username: '',
                        password: '',
                        account_id: '',
                        account_name: '',
                        role: 'user'
                    };
                    await loadAdminUsers();
                } else {
                    const result = await response.json();
                    ElementPlus.ElMessage.error(result.detail || '创建失败');
                }
            } catch (error) {
                ElementPlus.ElMessage.error('请求出错');
            }
        };

        // 删除用户
        // 删除账号及其所有相关数据
        const deleteUser = async (user) => {
            if (user.username === currentUser.value?.username || user.account_id === currentUser.value?.account_id) {
                ElementPlus.ElMessage.warning('不能删除当前登录用户');
                return;
            }
            if (!user.account_id || user.account_id === 'all') {
                ElementPlus.ElMessage.warning('不能删除汇总账号');
                return;
            }

            try {
                await ElementPlus.ElMessageBox.confirm(
                    `确定要永久删除账号 ${user.account_id} 及其所有相关数据吗？此操作不可恢复。`,
                    '永久删除账号',
                    {
                        confirmButtonText: '永久删除',
                        cancelButtonText: '取消',
                        type: 'warning',
                        confirmButtonClass: 'el-button--danger'
                    }
                );

                const response = await fetch('/api/admin/delete-account', {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json',
                        'Authorization': `Bearer ${accessToken.value}`
                    },
                    body: JSON.stringify({ account_id: user.account_id })
                });

                if (response.ok) {
                    const result = await response.json();
                    ElementPlus.ElMessage.success(result.message || '账号已删除');
                    if (currentAccountId.value === user.account_id) {
                        currentAccountId.value = currentUser.value?.role === 'admin' ? 'all' : '';
                    }
                    await loadAdminUsers();
                    await loadUsers();
                } else {
                    const result = await response.json();
                    ElementPlus.ElMessage.error(result.detail || '删除失败');
                }
            } catch (error) {
                if (error !== 'cancel') {
                    ElementPlus.ElMessage.error('请求出错');
                }
            }
        };
        const toggleDormant = async (user) => {
            const action = user.is_dormant ? '激活' : '设为休眠';
            const tip = user.is_dormant
                ? `激活后，账户 ${user.account_id} 将重新纳入汇总统计。`
                : `休眠后，账户 ${user.account_id} 将被排除在所有汇总统计之外（持仓、盈亏、资产等）。`;
            try {
                await ElementPlus.ElMessageBox.confirm(tip, `确认${action}`, {
                    confirmButtonText: '确定',
                    cancelButtonText: '取消',
                    type: 'warning'
                });
                const response = await fetch('/api/admin/toggle-dormant', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json', 'Authorization': `Bearer ${accessToken.value}` },
                    body: JSON.stringify({ account_id: user.account_id })
                });
                if (response.ok) {
                    const result = await response.json();
                    ElementPlus.ElMessage.success(result.message || `操作成功`);
                    await loadAdminUsers();
                    await fetchUsers(); // 同步刷新侧边栏用户列表
                } else {
                    const result = await response.json();
                    ElementPlus.ElMessage.error(result.detail || '操作失败');
                }
            } catch (error) {
                if (error !== 'cancel') ElementPlus.ElMessage.error('请求出错');
            }
        };

        const recalculateTodayProfit = async () => {
            try {
                await ElementPlus.ElMessageBox.confirm(
                    '将自动检测今日新加入的账户，为其补录资金调整条目（避免初始资金虚增当日盈亏）。\n已处理过的账户不会重复操作。',
                    '重算今日盈亏',
                    { confirmButtonText: '确定执行', cancelButtonText: '取消', type: 'info' }
                );
                const response = await fetch('/api/admin/recalculate-today', {
                    method: 'POST',
                    headers: { 'Authorization': `Bearer ${accessToken.value}` }
                });
                const result = await response.json();
                if (response.ok) {
                    let detail = result.message || '重算完成';
                    if (result.adjusted_accounts && result.adjusted_accounts.length > 0) {
                        detail += '\n补录账户：' + result.adjusted_accounts.map(a => `${a.account_id}(+${a.amount.toFixed(2)})`).join('、');
                    }
                    ElementPlus.ElMessage({ message: detail, type: 'success', duration: 5000 });
                    await loadData(); // 刷新今日盈亏显示
                } else {
                    ElementPlus.ElMessage.error(result.detail || '重算失败');
                }
            } catch (error) {
                if (error !== 'cancel') ElementPlus.ElMessage.error('请求出错');
            }
        };

        // 用修正后的公式重算 daily_profits 历史每日收益率（修复大额出入金导致的收益率异常）
        const recalcRatesLoading = ref(false);
        const recalcDailyRates = async () => {
            try {
                await ElementPlus.ElMessageBox.confirm(
                    '将用修正后的公式（出金不缩小当日本金基数）重算所有历史每日收益率，修复大额提现/入金导致的收益率异常。\n会同时刷新累计收益率与本月收益率。',
                    '重算历史收益率',
                    { confirmButtonText: '确定执行', cancelButtonText: '取消', type: 'warning' }
                );
                recalcRatesLoading.value = true;
                const response = await fetch('/api/admin/recalc-daily-rates', {
                    method: 'POST',
                    headers: { 'Authorization': `Bearer ${accessToken.value}` }
                });
                const result = await response.json();
                if (response.ok) {
                    ElementPlus.ElMessage({ message: result.message || '重算完成', type: 'success', duration: 5000 });
                    await loadData();
                } else {
                    ElementPlus.ElMessage.error(result.detail || '重算失败');
                }
            } catch (error) {
                if (error !== 'cancel') ElementPlus.ElMessage.error('请求出错');
            } finally {
                recalcRatesLoading.value = false;
            }
        };

        const goToStockDetail = (stockCode) => {
            if (!stockCode) return;
            // 跳转雪球K线：https://xueqiu.com/S/SH688825（交易所前缀大写 + 6位代码）
            const s = String(stockCode).trim();
            const code = s.substring(0, 6);
            let ex;
            if (s.includes('.')) {
                ex = s.split('.')[1].toUpperCase();               // SH / SZ / BJ
            } else {
                const h = code[0];
                ex = (h === '6' || h === '9') ? 'SH' : (h === '4' || h === '8') ? 'BJ' : 'SZ';
            }
            window.open(`https://xueqiu.com/S/${ex}${code}`, '_blank');
        };

        const formatDateKey = (date) => {
            const year = date.getFullYear();
            const month = String(date.getMonth() + 1).padStart(2, '0');
            const day = String(date.getDate()).padStart(2, '0');
            return `${year}-${month}-${day}`;
        };

        // 是否在交易时段（周一~周五 9:30-15:00）
        const isTradingHoursNow = () => {
            const d = new Date();
            if (d.getDay() === 0 || d.getDay() === 6) return false;
            const hm = d.getHours() * 100 + d.getMinutes();
            return hm >= 930 && hm <= 1500;
        };

        // 启动定时刷新
        const startRefreshTimer = () => {
            if (refreshTimer) clearInterval(refreshTimer);

            refreshTimer = setInterval(() => {
                // 后台也持续刷新（不再判断 document.hidden）；观察者无账号，不拉账户数据
                if (isAuthenticated.value && !isViewer.value) {
                    refreshUserRates();
                    if (currentAccountId.value) {
                        loadData();
                        loadChartData();
                    }
                }
            }, 30000);

            // 记录板：仅交易时段每60秒静默刷新（观察者不可见，跳过）
            if (researchBoardAutoTimer) clearInterval(researchBoardAutoTimer);
            researchBoardAutoTimer = setInterval(() => {
                if (isAuthenticated.value && !isViewer.value && isTradingHoursNow()) {
                    loadResearchBoard(false);
                }
            }, 60000);

            // 买卖流水：登录后每5秒静默拉取（观察者据此触发新成交通知）
            if (tradeFlowTimer) clearInterval(tradeFlowTimer);
            tradeFlowTimer = setInterval(() => {
                if (isAuthenticated.value && (isViewer.value || isTradingHoursNow())) {
                    loadTradeFlow(true);
                }
            }, 5000);
        };

        // 页面切回前台时立即刷新一次
        const onVisibilityChange = () => {
            if (document.hidden || !isAuthenticated.value) return;
            if (isViewer.value) { loadTradeFlow(true); return; }   // 观察者只看买卖流水
            if (currentAccountId.value) {
                loadData();
                loadChartData();
            }
            loadResearchBoard(false);
            if (isTradingHoursNow()) loadTradeFlow(true);
        };

        // 生命周期
        onMounted(() => {
            checkAuth();
            startRefreshTimer();
            initCalendar();
            loadSavedColumnOrder();
            document.addEventListener('visibilitychange', onVisibilityChange);
            // 给 DOM 一点时间渲染
            setTimeout(() => {
                initColumnSortable();
            }, 500);
        });

        onUnmounted(() => {
            if (refreshTimer) clearInterval(refreshTimer);
            if (researchBoardPollTimer) clearInterval(researchBoardPollTimer);
            if (researchBoardAutoTimer) clearInterval(researchBoardAutoTimer);
            if (tradeFlowTimer) clearInterval(tradeFlowTimer);
            if (sparklineRenderTimer) clearTimeout(sparklineRenderTimer);
            document.removeEventListener('visibilitychange', onVisibilityChange);
        });

        const initCalendar = () => {
            updateCalendar([]);
        };

        return {
            // 状态
            loading,
            showLoginDialog,
            showRefreshIndicator,
            showAdjustmentDialog,
            showAliasDialog,
            showConfigDialog,
            configAccountId,
            configContent,
            configLoadTime,
            configLoading,
            configSaving,
            llmConfigForm,
            llmConfigLoading,
            llmConfigSaving,
            researchBoardInput,
            researchBoardRecords,
            researchBoardPagedRecords,
            researchBoardPage,
            researchBoardPageSize,
            researchBoardSortedRecords,
            onResearchBoardSortChange,
            researchBoardLoading,
            researchBoardParsing,
            researchBoardRepairing,
            showResearchBoardInputsDialog,
            researchBoardInputs,
            researchBoardInputsLoading,
            researchBoardTaskMessage,
            showResearchBoardLargeDialog,
            showResearchBoardEditDialog,
            researchBoardEditing,
            showTScanDialog,
            tScanRows,
            tScanVisibleRows,
            tScanSummary,
            tScanTradeDate,
            tScanScanTime,
            tScanLoading,
            tScanOnlyCandidates,
            tScanHandledRows,
            researchBoardEditForm,
            showKlineAnalysisDialog,
            klineAnalysisLoading,
            klineAnalysisData,
            showUserManagementDialog,
            activeUserTab,
            showClearPositionsTag,
            clearPositionsConfirmText,
            clearPositionsPassword,
            clearPositionsPercentage,
            setClearPasswordForm,
            changeLoginPasswordForm,
            newUserForm,
            adminUsersList,
            tradeFactorsLoading,
            tradeFactorsSaving,
            tradeFactorsMixed,
            tradeFactorsMixedFields,
            tradeFactorsAccountCount,
            tradeFactorForm,
            showSellPositionDialog,
            sellPositionStock,
            sellPositionPercentage,
            pendingSellPositions,
            buyStopped,
            sellStopped,
            tradingStopped,
            tradingStatusLoading,
            adjusting,
            isAuthenticated,
            currentUser,
            currentAccount,
            loginForm,
            adjustmentForm,
            aliasForm,
            showUserSortDialog,
            userSortList,

            // 数据
            users,
            currentAccountId,
            positions,
            trades,
            asset,
            updateTime,
            lastUpdateTime,
            secondsSinceUpdate,
            manualRefreshing,
            manualRefresh,
            adjustments,

            // UI
            selectedDate,
            isTodaySelected,
            positionSearch,
            strategyFilter,
            hideClearedPositions,
            filteredPositions,
            activePositionCount,
            availableStrategies,
            filteredRecentTrades,
            filteredBuyTrades,
            filteredSellTrades,
            sortProp,
            sortOrder,
            activeTradeTab,
            positionsHeight,
            chartPeriod,
            chartType,
            isChartLoading,
            rawChartData,
            calendarDays,
            calendarMonth,
            calendarViewType,

            // 统计
            totalProfit,
            totalProfitRate,
            monthProfit,
            monthProfitRate,
            todayProfit,
            todayProfitRate,
            todayBuyAmount,
            todaySellAmount,
            todayBuyCount,
            todaySellCount,
            configTargetAccounts,
            systemStatus,
            positionColumns,
            displayMode,
            displayModeToggling,
            visiblePositionColumns,
            toggleDisplayMode,
            containerMaxWidth,

            // 计算属性
            sortedPositions,
            sortedUsers,
            recentTrades,
            buyTrades,
            sellTrades,

            // 方法
            getAccountTypeName,
            formatMoney,
            formatPrice,
            formatPercent,
            formatYi,
            handleLogin,
            loginMode,
            isViewer,
            viewerUsername,
            viewerForm,
            viewerAuthLoading,
            viewerNotifyOn,
            viewerRegister,
            viewerLogin,
            toggleViewerNotify,
            handleLogout,
            openAliasDialog,
            submitAliasUpdate,
            openUserSortDialog,
            saveUserSort,
            moveUserOrder,
            handleUserChange,
            loadChartData,
            handleChartTypeChange,
            updatePieChart,
            selectDate,
            prevMonth,
            nextMonth,
            handleSortChange,
            goToStockDetail,
            loadAdjustments,
            submitAdjustment,
            deleteAdjustment,
            submitClearPositions,
            submitClearAll,
            showClearAllDialog,
            clearAllConfirmText,
            clearAllPassword,
            clearAllPercentage,
            submitStopBuying,
            submitResumeBuying,
            submitStopSelling,
            submitResumeSelling,
            refreshAllKline,
            toggleT0,
            isPendingT0,
            deletePosition,
            submitSetClearPassword,
            submitChangeLoginPassword,
            submitCreateUser,
            deleteUser,
            toggleDormant,
            recalculateTodayProfit,
            recalcDailyRates,
            recalcRatesLoading,
            loadAdminUsers,
            userStats,
            userStatsLoading,
            mergedAccountStats,
            formatOnlineDuration,
            loadUserStats,
            loadTradeFactors,
            saveTradeFactors,
            loadLLMConfig,
            saveLLMConfig,
            loadResearchBoard,
            openResearchBoardLargeDialog,
            loadTScan,
            openTScanDialog,
            submitTScanTrade,
            parseResearchBoard,
            openResearchBoardEditDialog,
            saveResearchBoardEdit,
            deleteResearchBoardRecord,
            analyzeResearchBoardKline,
            tradeFlowRecords,
            tradeFlowPending,
            cancelPendingOrder,
            isCancelling,
            orderPriceType,
            orderLimitPrice,
            orderQuote,
            orderQuoteLoading,
            orderTradeMode,
            orderTradeModes,
            orderTradeModeVisible,
            orderTradeModeHint,
            orderPriceTypes,
            orderPriceTypeGroups,
            orderPriceSpec,
            orderPriceRole,
            orderNeedsPrice,
            orderAcceptsPrice,
            orderPriceLabel,
            orderPriceHint,
            orderCage,
            orderCageText,
            orderPriceError,
            orderPriceStep,
            tradeFlowSummary,
            tradeFlowLoading,
            tradeFlowSide,
            tradeFlowDays,
            tradeFlowQuery,
            loadTradeFlow,

            repairResearchBoard,
            openResearchBoardInputsDialog,
            loadResearchBoardInputs,
            formatJson,
            openConfigDialog,
            loadConfig,
            refreshConfig,
            saveConfig,
            loadSavedColumnOrder,
            initColumnSortable,
            toggleLock,
            openSellPositionDialog,
            renderPopoverChart,
            destroyPopoverChart,
            cancelSellPosition,
            confirmSellPosition,
            isPendingSell,
            getPendingSellPercentage,
            isPendingBuy,
            getPendingBuyPercentage,
            showBuyPositionDialog,
            buyPositionStock,
            buyPositionPercentage,
            openBuyPositionDialog,
            cancelBuyPosition,
            confirmBuyPosition,
            calcBuyAmount,
            showNewBuyDialog,
            newBuyStockQuery,
            newBuyStockOptions,
            newBuySelectedStock,
            newBuyAmount,
            newBuySpec,
            newBuyMode,
            newBuyCash,
            newBuyQuote,
            newBuyQuoteLoading,
            newBuyResolvedVolume,
            newBuyEstimatedCash,
            newBuyLastPrice,
            newBuyCashError,
            calcBuyCash,
            formatCash,
            instrumentSpec,
            formatOrderResult,
            openNewBuyDialog,
            searchStocksRemote,
            confirmNewBuy
        };
    }
});

// 注册 Element Plus 图标
for (const [key, component] of Object.entries(ElementPlusIconsVue)) {
    app.component(key, component);
}

// 使用 Element Plus
app.use(ElementPlus, {
    locale: ElementPlusLocaleZhCn
});

// 挂载应用
app.mount('#app');
