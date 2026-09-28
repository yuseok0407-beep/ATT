'use strict';
// auto2 대시보드. 서버(/api/*)가 계산한 값을 그리기만 한다 — 전략 조건이나 R 복원을 여기서 다시
// 판정하지 않는다(대시보드·텔레그램·실거래 봇이 같은 정의를 쓰게 하려는 것).

// ---------------------------------------------------------------- 형식
function fmtUsd(n) {
  if (n === null || n === undefined || Number.isNaN(Number(n))) return '–';
  return '$' + Number(n).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}
function fmtSignedUsd(n) {
  if (n === null || n === undefined) return '–';
  return (n >= 0 ? '+' : '-') + fmtUsd(Math.abs(n));
}
// 가격대가 종목마다 크게 다르다(BTC 수만 달러 vs PEPE 0.01달러 미만) — 자릿수를 고정하면 저가 종목의
// 손절/익절가가 진입가와 구분이 안 된다. 텔레그램(_fmt_price)과 같은 기준.
function priceDigits(n) {
  const abs = Math.abs(n);
  if (abs >= 1000) return 1;
  if (abs >= 100) return 2;
  if (abs >= 1) return 4;
  if (abs >= 0.01) return 6;
  return 8;
}
function fmtPrice(n) {
  if (n === null || n === undefined || n === '') return '–';
  n = Number(n);
  const d = priceDigits(n);
  return n.toLocaleString('en-US', { minimumFractionDigits: d, maximumFractionDigits: d });
}
function fmtPct(n, digits) {
  if (n === null || n === undefined) return '–';
  return (n * 100).toFixed(digits === undefined ? 1 : digits) + '%';
}
function fmtSignedPct(n, digits) {
  if (n === null || n === undefined) return '–';
  return (n >= 0 ? '+' : '') + fmtPct(n, digits);
}
function fmtR(v) {
  return v === null || v === undefined ? '–' : (v >= 0 ? '+' : '') + Number(v).toFixed(2) + 'R';
}
function fmtQty(n) {
  if (n === null || n === undefined) return '–';
  return Number(n).toLocaleString('en-US', { maximumFractionDigits: 4 });
}
function fmtCompact(n) {
  if (n === null || n === undefined) return '–';
  return Intl.NumberFormat('en-US', { notation: 'compact', maximumFractionDigits: 1 }).format(n);
}
function formatUptime(seconds) {
  const d = Math.floor(seconds / 86400), h = Math.floor((seconds % 86400) / 3600), m = Math.floor((seconds % 3600) / 60);
  if (d > 0) return `${d}일 ${h}시간`;
  return h > 0 ? `${h}시간 ${m}분` : `${m}분`;
}
function formatAge(seconds) {
  if (seconds < 60) return `${Math.floor(seconds)}초 전`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}분 전`;
  return `${Math.floor(seconds / 3600)}시간 전`;
}
function fmtDateTime(iso) {
  if (!iso) return '–';
  const d = new Date(iso);
  const p = v => String(v).padStart(2, '0');
  return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}
function fmtShortDate(iso) {
  return new Date(iso).toLocaleString('ko-KR', { dateStyle: 'short', timeStyle: 'short' });
}
function shortSymbol(s) { return String(s).split('/')[0]; }
function signClass(v) { return v === null || v === undefined ? '' : (v >= 0 ? 'up' : 'down'); }
// 저널의 사유 문자열에는 거래소 오류 원문(JSON, 꺾쇠 포함)이 들어온다 — 그대로 innerHTML에 넣지 않는다.
function esc(s) {
  return String(s === null || s === undefined ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}
function $(id) { return document.getElementById(id); }
function setText(id, text, cls) {
  const el = $(id);
  el.textContent = text;
  if (cls !== undefined) el.className = el.className.replace(/\b(up|down|warn)\b/g, '').trim() + (cls ? ' ' + cls : '');
}
function cssVar(name) { return getComputedStyle(document.documentElement).getPropertyValue(name).trim(); }

// 브라우저 저장소는 사생활 보호 모드 등에서 예외를 던질 수 있다 — 편의 기능이라 실패해도 무시한다.
function store(key, value) {
  try {
    if (value === undefined) return localStorage.getItem(key);
    localStorage.setItem(key, value);
  } catch (e) { return null; }
  return null;
}

const CONFIG_LABELS = {
  timeframe: '타임프레임', adx_threshold: 'ADX 임계값', sma_period: 'SMA 기간',
  regime_sma_period: '레짐 SMA (숏 차단)', min_atr_to_stop_ratio: '최소 변동성 (ATR/손절폭)',
  max_entry_price_drift_r: '진입 괴리 한도', direction_filter: '방향 확인',
  stop_loss_pct: '손절폭', take_profit_rr: '손익비', take_profit_order_type: '익절 주문 방식',
  leverage: '레버리지', risk_per_trade: '거래당 리스크',
  max_concurrent_positions: '최대 동시 포지션', max_daily_loss_pct: '일일 손실 한도',
  max_consecutive_losses: '연속 손실 한도', consecutive_loss_cooldown_hours: '연속손실 쿨다운',
  symbols: '감시 종목', logic_revision: '규칙 코드 개정',
};
const REASON_LABELS = {
  stop_loss: '손절', take_profit: '익절', manual: '수동청산', breakeven_stop: '손익분기청산', unknown: '기타',
};
// 저널 이벤트 → 화면 이름과 색. 목록에 없는 이벤트는 원래 코드를 그대로 보여준다.
const EVENT_INFO = {
  entered: ['진입', 'ev-entered'], closed: ['청산', 'ev-closed'],
  no_signal: ['신호 없음', 'ev-muted'], holding_position: ['보유 중', 'ev-muted'],
  skipped_max_positions: ['동시보유 한도', 'ev-warn'], circuit_breaker_blocked: ['서킷브레이커', 'ev-bad'],
  rejected_unsafe_stop: ['손절 위험 거부', 'ev-bad'], rejected_zero_quantity: ['수량 0 거부', 'ev-bad'],
  rejected_exchange_error: ['거래소 거부', 'ev-bad'], unprotected_position: ['손절 없음', 'ev-bad'],
  position_protected: ['보호 복구', 'ev-entered'], config_changed: ['설정 변경', 'ev-muted'],
  consecutive_loss_reset: ['연속손실 리셋', 'ev-muted'], untracked_position: ['외부 포지션', 'ev-warn'],
};
const TF_MS = { '15m': 9e5, '1h': 36e5, '4h': 144e5, '1d': 864e5 };

// ---------------------------------------------------------------- 상태
// 새로고침/새로 열 때는 항상 DEMO로 시작한다 — env는 저장하지 않는다(의도적). 실계좌 화면을
// 마지막으로 보고 있었더라도 다시 열면 데모부터 보여줘서, 계좌를 착각한 채 긴급청산 등을
// 누르는 사고를 막는다.
const state = {
  env: 'demo',
  status: null, perf: null, cond: null, tickers: {}, equity: null,
  symbols: [],
  selected: store('auto2.chartSymbol'),
  tf: store('auto2.chartTf') || null,
  candles: [], chartReq: 0, hover: null,
  curveMode: 'r', histFilter: 'all',
  mview: 'home',
};

async function api(path, options) {
  const sep = path.includes('?') ? '&' : '?';
  const res = await fetch(path + sep + 'env=' + state.env, options);
  if (res.status === 401) { window.location.href = '/login'; throw new Error('로그인이 필요합니다.'); }
  let data = null;
  try { data = await res.json(); } catch (e) { /* JSON이 아닌 응답 */ }
  return { ok: res.ok, status: res.status, data: data || {} };
}

// ---------------------------------------------------------------- 계좌 전환
function showStatusUnavailable(message) {
  // 계좌 전환 직후나 조회 실패 시 "이전 계좌의 숫자"가 화면에 남아 있으면 지금 보는 계좌의 것으로
  // 오인한다(실전에서 겪음 — LIVE로 바꿨는데 연결이 실패하자 DEMO 숫자가 LIVE 배너 아래 그대로
  // 남아 있었다, 2026-08-22). 실패/전환 시점엔 계좌 숫자를 전부 지운다.
  $('last-updated').textContent = message;
  ['kpi-equity', 'kpi-today', 'kpi-upnl', 'kpi-positions'].forEach(id => setText(id, '–', ''));
  ['kpi-equity-sub', 'kpi-today-sub', 'kpi-upnl-sub', 'kpi-positions-sub'].forEach(id => { $(id).innerHTML = '&nbsp;'; });
  state.status = null;
  renderPositions();
  renderWatch();
  renderUnprotectedBanner([]);
  $('issue-chips').innerHTML = '';
  $('issue-note').textContent = '';
  $('filter-chips').innerHTML = '';
  $('entries-body').innerHTML = '';
  $('entries-cards').innerHTML = '';
  $('entries-empty').classList.add('show');
}

function switchEnv(env) {
  if (env === state.env) return;
  state.env = env;
  document.querySelectorAll('#env-toggle button').forEach(b => b.classList.toggle('active', b.dataset.env === env));
  document.body.classList.toggle('env-live', env === 'live');
  const tag = $('bot-env-tag');
  tag.textContent = env.toUpperCase();
  tag.className = 'tag' + (env === 'live' ? ' live' : '');
  // 계좌가 바뀌면 이전 계좌의 과거 기록을 "새로 발생한 이벤트"로 오인해 알림을 쏘지 않도록 리셋
  seenEntryKeys.clear();
  notifyInitialized = false;
  state.perf = null; state.cond = null; state.tickers = {}; state.equity = null;
  setText('kpi-month', '–', ''); setText('kpi-netr', '–', '');
  showStatusUnavailable('불러오는 중');
  refreshAll();
  if (env === 'live') fetchPublicIp();  // 바이낸스 IP 화이트리스트와 비교할 수 있게 전환 때마다 갱신
}

async function fetchPublicIp() {
  const el = $('public-ip');
  el.textContent = '확인 중';
  try {
    const res = await fetch('/api/public-ip');
    const data = await res.json();
    el.textContent = data.ip || '확인 불가';
  } catch (e) {
    el.textContent = '확인 불가';
  }
}

// ---------------------------------------------------------------- 탭 / 휴대폰 화면
function setTab(tab) {
  document.querySelectorAll('#dock-tabs button').forEach(b => b.classList.toggle('active', b.dataset.tab === tab));
  document.querySelectorAll('.pane').forEach(p => p.classList.toggle('active', p.dataset.pane === tab));
  store('auto2.tab', tab);
  if (tab === 'performance') drawCurve();
}

function setMobileView(view) {
  state.mview = view;
  document.querySelectorAll('#mnav button').forEach(b => b.classList.toggle('active', b.dataset.m === view));
  document.querySelectorAll('[data-m]').forEach(el => {
    if (el.closest('#mnav')) return;
    el.classList.toggle('m-on', el.dataset.m === view);
  });
  window.scrollTo(0, 0);
  // 숨겨져 있던 캔버스는 크기가 0이라 보이게 된 뒤에 다시 그려야 한다
  requestAnimationFrame(() => { drawChart(); drawCurve(); });
}

// ---------------------------------------------------------------- 주기 갱신
// 탭이 가려져 있으면(휴대폰 화면 꺼짐 등) 거래소를 계속 때리지 않는다. 다시 보이면 즉시 한 번.
const jobs = [];
function every(fn, ms) { jobs.push(fn); setInterval(() => { if (!document.hidden) fn(); }, ms); }
document.addEventListener('visibilitychange', () => { if (!document.hidden) jobs.forEach(fn => fn()); });

function refreshAll() {
  // 저장된 종목이 이 계좌에 없으면 상태 응답이 온 뒤 selectSymbol이 다른 종목으로 바꾼다
  if (state.selected) { state.candles = []; loadChart(); }
  refreshStatus();
  refreshBotStatus();
  fetchTickers();
  fetchPerformance();
  fetchConditions();
  fetchEquity();
}

// ---------------------------------------------------------------- /api/status
async function refreshStatus() {
  const env = state.env;
  let r;
  try {
    r = await api('/api/status');
  } catch (e) {
    if (env === state.env) showStatusUnavailable('연결 실패 — 재시도 중');
    return;
  }
  if (env !== state.env) return;  // 그 사이 다른 계좌로 전환됨
  if (!r.ok) {
    showStatusUnavailable((r.data.message || '거래소 응답 실패') + ' — 재시도 중');
    return;
  }
  // 빠르게 DEMO<->LIVE를 오가면 요청이 겹칠 수 있다 — 지금 보는 계좌가 아닌 응답은 절대 그리지 않는다.
  if (r.data.env && r.data.env !== state.env) return;

  const data = r.data;
  state.status = data;
  state.symbols = Object.keys(data.symbols || {});
  if (!state.selected || !state.symbols.includes(state.selected)) {
    const open = state.symbols.find(s => data.symbols[s].has_position);
    selectSymbol(open || state.symbols[0], true);
  }

  $('last-updated').textContent = new Date().toLocaleTimeString('en-GB', { hour12: false });
  renderAccount(data);
  renderRisk(data);
  renderConfig(data.config || {});
  renderUnprotectedBanner(data.unprotected_symbols || []);
  renderIssues(data.recent_issues || {});
  renderFilterStats(data.filter_stats || {}, data.filter_events || []);
  renderPositions();
  renderWatch();
  renderEntries();
  collectNotifications(data.recent_entries || []);
  if (state.candles.length) drawChart();  // 진입/청산 표시와 손절/익절선 갱신
}

function renderAccount(data) {
  setText('kpi-equity', fmtUsd(data.margin_equity));
  $('kpi-equity-sub').textContent = 'USDT 선물 지갑 · ' + state.env.toUpperCase();

  const pct = data.daily_pnl_pct || 0;
  setText('kpi-today', fmtSignedPct(pct, 2), signClass(pct));
  // daily_pnl_pct는 오늘 시작 자산 대비(서킷브레이커와 같은 기준)라 금액은 거기서 되짚는다.
  const start = data.margin_equity / (1 + pct);
  const limit = data.risk_limits ? data.risk_limits.max_daily_loss_pct : null;
  $('kpi-today-sub').textContent = `${fmtSignedUsd(data.margin_equity - start)} · 한도 -${fmtPct(limit, 1)}`;

  const open = Object.values(data.symbols || {}).filter(s => s.has_position && s.position);
  const upnl = open.reduce((a, s) => a + (Number(s.position.unrealizedPnl) || 0), 0);
  setText('kpi-upnl', open.length ? fmtSignedUsd(upnl) : '$0.00', open.length ? signClass(upnl) : '');
  $('kpi-upnl-sub').textContent = open.length ? `포지션 ${open.length}개 평가손익` : '보유 포지션 없음';

  setText('kpi-positions', `${data.open_position_count} / ${data.max_concurrent}`);
  const bad = (data.unprotected_symbols || []).length;
  $('kpi-positions-sub').innerHTML = bad
    ? `<span class="down">손절 없음 ${bad}건</span>`
    : (open.length ? '손절 주문 모두 확인됨' : '대기 중');
  $('tab-pos-count').textContent = open.length;
}

function renderSteps(id, count, limit, colorFor) {
  const n = Math.max(1, limit || 1);
  $(id).innerHTML = Array.from({ length: n }, (_, i) =>
    `<span class="${i < count ? 'on ' + colorFor(count, n) : ''}"></span>`).join('');
}

function renderRisk(data) {
  const rl = data.risk_limits;
  if (!rl) return;
  const pnlPct = data.daily_pnl_pct || 0;
  const limitPct = rl.max_daily_loss_pct;
  const used = limitPct ? Math.min(1, Math.abs(Math.min(0, pnlPct)) / limitPct) : 0;
  const gauge = $('daily-pnl-gauge');
  gauge.style.width = (used * 100) + '%';
  gauge.className = 'fill' + (used > 0.8 ? ' down' : used > 0.5 ? ' warn' : '');
  setText('daily-pnl-text', fmtSignedPct(pnlPct, 2), signClass(pnlPct));
  $('daily-loss-limit-text').textContent = `-${fmtPct(limitPct)}에서 신규 진입 중단`;

  const streak = rl.consecutive_losses || 0;
  const streakLimit = rl.max_consecutive_losses;
  setText('streak-text', `${streak} / ${streakLimit}`,
    streak >= streakLimit ? 'down' : streak >= streakLimit - 1 && streak > 0 ? 'warn' : '');
  $('reset-streak-btn').hidden = streak === 0;
  renderSteps('streak-steps', streak, streakLimit,
    (c, n) => (c >= n ? 'down' : c >= n - 1 ? 'warn' : ''));

  setText('slots-text', `${data.open_position_count} / ${data.max_concurrent}`);
  renderSteps('slot-steps', data.open_position_count, data.max_concurrent, () => '');
}

// 전략 설정은 futures_rule_bot.current_strategy_config() 한 곳에서 온다 — 저널 스냅샷과 같은 값.
function configValue(key, v) {
  if (v === null || v === undefined) return '–';
  if (key === 'symbols') return v.map(shortSymbol).join(', ');
  if (['stop_loss_pct', 'risk_per_trade', 'max_daily_loss_pct'].includes(key)) return fmtPct(v, 2);
  if ((key === 'regime_sma_period' || key === 'min_atr_to_stop_ratio') && !v) return '꺼짐';
  if (key === 'max_entry_price_drift_r') return v + 'R';
  if (key === 'leverage') return v + 'x';
  if (key === 'consecutive_loss_cooldown_hours') return v ? v + '시간' : '없음 (수동 리셋)';
  if (key === 'take_profit_order_type') return v === 'limit' ? '지정가 (메이커)' : '조건부 시장가';
  return String(v);
}
function renderConfig(config) {
  $('config-grid').innerHTML = Object.entries(config).map(([k, v]) =>
    `<div class="kv"><div class="k">${esc(CONFIG_LABELS[k] || k)}</div><div class="v">${esc(configValue(k, v))}</div></div>`
  ).join('');
}

// 손절 주문이 없는 포지션은 화면 맨 위에서 경고한다 — 10배 레버리지에서 일어날 수 있는 가장
// 비싼 실패인데 예전엔 카드 안의 "-" 하나로만 표시됐다(2026-09-09).
function renderUnprotectedBanner(symbols) {
  const banner = $('unprotected-banner');
  if (!symbols.length) { banner.className = 'banner alert-banner'; banner.innerHTML = ''; return; }
  banner.className = 'banner alert-banner show';
  banner.innerHTML = `손절 주문이 없는 포지션: ${symbols.map(s => esc(shortSymbol(s))).join(', ')}
    <div class="banner-sub">레버리지 포지션이 손절 없이 열려 있습니다. 포지션 탭에서 즉시 청산하거나
    거래소에서 손절 주문을 직접 확인하세요.</div>`;
}

// 거래소 거부는 저널엔 남지만 알림에선 노이즈로 빠지고 "최근 내역" 30줄에서도 금방 밀려나서, 몇 달치가
// 조용히 쌓이기만 했다(TSLA -2027 141건 등). 코드별 건수로 묶어서 드러낸다.
function renderIssues(issues) {
  const rejections = issues.rejections || [];
  const hours = issues.since_hours || 24;
  if (!rejections.length) {
    $('issue-chips').innerHTML = `<span class="chip">최근 ${hours}시간 거래소 거부 없음</span>`;
    $('issue-note').textContent = '';
    return;
  }
  $('issue-chips').innerHTML = `<span class="chip">최근 ${hours}시간</span>` + rejections.map(r =>
    `<span class="chip bad">${esc(r.code)} × ${r.count} · ${esc(r.symbols.join(', '))}</span>`).join('');
  $('issue-note').innerHTML = rejections.filter(r => r.hint).map(r => `<b>${esc(r.code)}</b> ${esc(r.hint)}`).join('<br>');
}

// 저널에 안 남는 진입 차단 사유(레짐숏/저변동/같은봉/가격이탈)의 일별 집계 — 필터가 실제로 몇 번
// 일하는지 확인할 유일한 창구다.
function renderFilterStats(stats, events) {
  const days = Object.keys(stats).sort().reverse();  // 날짜 키 순서를 서버(Flask 정렬)에 맡기지 않는다
  if (!days.length) { $('filter-chips').innerHTML = '<span class="chip">아직 집계된 차단이 없습니다</span>'; return; }
  $('filter-chips').innerHTML = '<span class="chip">필터가 막은 신호</span>' + days.map(day => {
    const counts = stats[day];
    const parts = events.filter(e => counts[e.key]).map(e => `${esc(e.label)} <b>${counts[e.key]}</b>`);
    return `<span class="chip">${day.slice(5)} · ${parts.length ? parts.join(' · ') : '차단 없음'}</span>`;
  }).join('');
}

// ---------------------------------------------------------------- 포지션
// 진입가/손절가/현재가로 R배수를 계산한다. 손절가까지가 정확히 -1R이라 손익($)과 달리 사이징과
// 무관하게 "계획 대비 지금 어디쯤인지"를 바로 읽을 수 있다.
function rMultiples(p, s) {
  const entry = p.entryPrice, stop = s.stop_loss_price;
  if (!entry || !stop) return null;
  const risk = Math.abs(entry - stop);
  if (!(risk > 0)) return null;
  const dir = p.side === 'long' ? 1 : -1;
  const now = dir * (p.markPrice - entry) / risk;
  const target = s.take_profit_price ? dir * (s.take_profit_price - entry) / risk : 2;
  const peak = s.excursion && typeof s.excursion.max_favorable_r === 'number' ? s.excursion.max_favorable_r : null;
  return { now, target, peak };
}

// 손절 -1R부터 익절까지의 막대 위에 지금 위치와 보유 중 최고점(주황)을 같이 찍는다 — 최고점 표식이
// 현재 위치보다 한참 오른쪽이면 지금 되돌림 중이라는 뜻.
function renderRBar(r) {
  if (!r) return '<span class="muted">–</span>';
  const lo = -1.15, hi = Math.max(r.target, r.peak || 0, r.now) + 0.15;
  const pos = v => Math.max(0, Math.min(100, ((v - lo) / (hi - lo)) * 100));
  const zero = pos(0), cur = pos(r.now);
  const peak = r.peak === null ? '' : `<div class="rbar-mark peak" style="left:${pos(r.peak)}%" title="보유 중 최고 ${fmtR(r.peak)}"></div>`;
  return `<div class="rbar">
      <div class="rbar-fill ${r.now < 0 ? 'neg' : ''}" style="left:${Math.min(zero, cur)}%;width:${Math.abs(cur - zero)}%"></div>
      <div class="rbar-mark entry" style="left:${zero}%"></div>${peak}
    </div>
    <div class="rbar-legend"><span>-1R</span>
      <span class="${signClass(r.now)}">${fmtR(r.now)}${r.peak === null ? '' : ' · 최고 ' + fmtR(r.peak)}</span>
      <span>${fmtR(r.target)}</span></div>`;
}

function openPositions() {
  const symbols = (state.status && state.status.symbols) || {};
  return Object.entries(symbols).filter(([, s]) => s.has_position && s.position);
}

function renderPositions() {
  const rows = openPositions();
  $('pos-empty').classList.toggle('show', rows.length === 0);
  $('pos-table').style.display = rows.length ? '' : 'none';

  $('pos-body').innerHTML = rows.map(([symbol, s]) => {
    const p = s.position;
    const sideCls = p.side === 'long' ? 'up' : 'down';
    const roe = typeof p.percentage === 'number' ? ` <span class="${signClass(p.percentage)}">(${fmtSignedPct(p.percentage / 100, 2)})</span>` : '';
    return `<tr class="${s.unprotected ? 'bad' : ''}">
      <td><span class="sym-cell" data-sym="${esc(symbol)}">${esc(shortSymbol(symbol))}</span>
        <span class="sym-sub ${sideCls}">${p.side === 'long' ? '롱' : '숏'} ${s.leverage || ''}x${s.unprotected ? ' · <b class="down">손절 없음</b>' : ''}</span></td>
      <td class="r">${fmtQty(Math.abs(p.contracts))}<span class="sym-sub muted">${fmtUsd(Math.abs(p.notional))}</span></td>
      <td class="r">${fmtPrice(p.entryPrice)}</td>
      <td class="r">${fmtPrice(p.markPrice)}</td>
      <td class="r warn">${fmtPrice(p.liquidationPrice)}</td>
      <td class="r down">${fmtPrice(s.stop_loss_price)}</td>
      <td class="r up">${fmtPrice(s.take_profit_price)}</td>
      <td class="r ${signClass(p.unrealizedPnl)}">${fmtSignedUsd(p.unrealizedPnl)}${roe}</td>
      <td style="min-width:160px">${renderRBar(rMultiples(p, s))}</td>
      <td class="r"><button class="btn btn-down btn-sm" id="close-btn-${esc(shortSymbol(symbol))}"
        onclick="closePosition('${esc(symbol)}')">시장가 청산</button></td>
    </tr>`;
  }).join('');

  $('pos-cards').innerHTML = rows.map(([symbol, s]) => {
    const p = s.position;
    return `<div class="pos-card ${s.unprotected ? 'bad' : ''}">
      <div class="pos-card-head">
        <div><span class="sym-cell" data-sym="${esc(symbol)}">${esc(shortSymbol(symbol))}</span>
          <span class="sym-sub ${p.side === 'long' ? 'up' : 'down'}">${p.side === 'long' ? '롱' : '숏'} ${s.leverage || ''}x${s.unprotected ? ' · <b class="down">손절 없음</b>' : ''}</span></div>
        <div class="pos-pnl"><div class="v num ${signClass(p.unrealizedPnl)}">${fmtSignedUsd(p.unrealizedPnl)}</div>
          <div class="small ${signClass(p.percentage)}">${typeof p.percentage === 'number' ? fmtSignedPct(p.percentage / 100, 2) : ''}</div></div>
      </div>
      <div class="pos-grid">
        <div><div class="k">진입가</div><div class="v">${fmtPrice(p.entryPrice)}</div></div>
        <div><div class="k">현재가</div><div class="v">${fmtPrice(p.markPrice)}</div></div>
        <div><div class="k">청산가</div><div class="v warn">${fmtPrice(p.liquidationPrice)}</div></div>
        <div><div class="k">손절</div><div class="v down">${fmtPrice(s.stop_loss_price)}</div></div>
        <div><div class="k">익절</div><div class="v up">${fmtPrice(s.take_profit_price)}</div></div>
        <div><div class="k">수량</div><div class="v">${fmtQty(Math.abs(p.contracts))}</div></div>
      </div>
      ${renderRBar(rMultiples(p, s))}
      <button class="btn btn-down btn-block" style="margin-top:10px" id="close-card-${esc(shortSymbol(symbol))}"
        onclick="closePosition('${esc(symbol)}')">시장가 청산</button>
    </div>`;
  }).join('');
}

// ---------------------------------------------------------------- 종목 목록
function renderWatch() {
  const status = state.status;
  const condBy = {};
  ((state.cond && state.cond.symbols) || []).forEach(r => { condBy[r.symbol] = r; });
  const list = state.symbols.length ? state.symbols : Object.keys(condBy);
  $('watch-meta').textContent = list.length ? `${list.length}종목` : '';

  $('watch-list').innerHTML = list.map(symbol => {
    const s = status && status.symbols ? status.symbols[symbol] : null;
    const t = state.tickers[symbol] || {};
    const c = condBy[symbol];
    const last = t.last !== undefined && t.last !== null ? t.last : (c ? c.close : null);
    let mark = '';
    if (s && s.has_position && s.position) {
      mark = s.unprotected ? '<span class="side-mark bad">무보호</span>'
        : `<span class="side-mark ${s.position.side}">${s.position.side === 'long' ? 'L' : 'S'}</span>`;
    }
    let prox = '<span class="watch-prox">–</span>';
    if (c && c.ready) prox = '<span class="watch-prox ready">신호</span>';
    else if (c && c.regime_blocks_short) prox = '<span class="watch-prox" title="상승 레짐이라 숏 신호를 막는 중">차단</span>';
    else if (c && c.candidate_side) prox = `<span class="watch-prox">${Math.round(c.proximity * 100)}%</span>`;
    const chg = t.change_pct;
    return `<div class="watch-row ${symbol === state.selected ? 'sel' : ''}" data-sym="${esc(symbol)}">
      <span class="watch-sym"><span class="name">${esc(shortSymbol(symbol))}</span>${mark}</span>
      <span class="num">${fmtPrice(last)}</span>
      <span class="num small ${signClass(chg)}">${chg === null || chg === undefined ? '–' : (chg >= 0 ? '+' : '') + chg.toFixed(2) + '%'}</span>
      ${prox}
    </div>`;
  }).join('');
}

async function fetchTickers() {
  const env = state.env;
  let r;
  try { r = await api('/api/tickers'); } catch (e) { return; }
  if (!r.ok || env !== state.env || r.data.env !== state.env) return;
  state.tickers = r.data.tickers || {};
  renderWatch();
  // 진행 중인 마지막 봉을 현재가로 이어 그린다 — 거래소 차트처럼 차트가 멈춰 보이지 않게.
  const t = state.tickers[state.selected];
  if (t && typeof t.last === 'number' && state.candles.length) {
    const last = state.candles[state.candles.length - 1];
    last.c = t.last; last.h = Math.max(last.h, t.last); last.l = Math.min(last.l, t.last);
    drawChart();
  }
  renderChartHeader();
}

// ---------------------------------------------------------------- 차트
function selectSymbol(symbol, silent) {
  if (!symbol) return;
  state.selected = symbol;
  store('auto2.chartSymbol', symbol);
  state.candles = [];
  state.hover = null;
  renderWatch();
  renderChartHeader();
  loadChart();
  if (!silent && window.matchMedia('(max-width: 767px)').matches) setMobileView('chart');
}

function renderTfButtons(timeframes, strategyTf) {
  const seg = $('tf-seg');
  const list = timeframes.includes(strategyTf) ? timeframes : [strategyTf].concat(timeframes);
  seg.innerHTML = list.map(tf =>
    `<button data-tf="${tf}" class="${tf === state.tf ? 'active' : ''}" title="${tf === strategyTf ? '전략이 신호를 계산하는 주기' : ''}">${tf}</button>`
  ).join('');
}

function renderChartHeader() {
  const sym = state.selected;
  $('chart-symbol').textContent = sym ? shortSymbol(sym) + ' / USDT' : '–';
  const t = state.tickers[sym] || {};
  const last = typeof t.last === 'number' ? t.last
    : (state.candles.length ? state.candles[state.candles.length - 1].c : null);
  setText('chart-last', fmtPrice(last), signClass(t.change_pct));
  $('chart-change').className = 'num small ' + signClass(t.change_pct);
  // 휴대폰에서는 등락률만 — 고가/저가/거래대금까지 넣으면 제목이 세 줄로 넘친다
  $('chart-change').innerHTML = typeof t.change_pct === 'number'
    ? `${t.change_pct >= 0 ? '+' : ''}${t.change_pct.toFixed(2)}%<span class="hide-sm muted">&nbsp; 24h 고 ${fmtPrice(t.high)} · 저 ${fmtPrice(t.low)} · 거래대금 ${fmtCompact(t.quote_volume)}</span>`
    : '';
}

async function loadChart() {
  const sym = state.selected;
  if (!sym) return;
  const req = ++state.chartReq;
  const env = state.env;
  if (!state.candles.length) $('chart-msg').textContent = '불러오는 중';
  let r;
  try {
    r = await api('/api/chart/' + encodeURIComponent(sym) + (state.tf ? '?tf=' + state.tf : ''));
  } catch (e) {
    if (req === state.chartReq) $('chart-msg').textContent = '차트를 불러오지 못했습니다';
    return;
  }
  if (req !== state.chartReq || env !== state.env) return;
  if (!r.ok) {
    // 선택한 주기를 서버가 모르면(설정 변경 등) 전략 기본 주기로 되돌린다
    if (r.status === 400 && state.tf) { state.tf = null; store('auto2.chartTf', ''); loadChart(); return; }
    state.candles = [];
    drawChart();
    $('chart-msg').textContent = r.data.message || '차트를 불러오지 못했습니다';
    return;
  }
  const data = r.data;
  if (!state.tf) state.tf = data.timeframe;
  state.strategyTf = data.strategy_timeframe;
  renderTfButtons(data.timeframes || [], data.strategy_timeframe);
  state.candles = data.candles || [];
  $('chart-msg').textContent = state.candles.length ? '' : '캔들 데이터가 없습니다';
  renderChartHeader();
  drawChart();
}

function smaSeries(candles, period) {
  const out = new Array(candles.length).fill(null);
  let sum = 0;
  for (let i = 0; i < candles.length; i++) {
    sum += candles[i].c;
    if (i >= period) sum -= candles[i - period].c;
    if (i >= period - 1) out[i] = sum / period;
  }
  return out;
}

function sizeCanvas(canvas) {
  const rect = canvas.getBoundingClientRect();
  const dpr = window.devicePixelRatio || 1;
  const w = Math.max(1, Math.round(rect.width * dpr)), h = Math.max(1, Math.round(rect.height * dpr));
  if (canvas.width !== w || canvas.height !== h) { canvas.width = w; canvas.height = h; }
  const ctx = canvas.getContext('2d');
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, rect.width, rect.height);
  return { ctx, W: rect.width, H: rect.height };
}

function axisLabel(ctx, text, x, y, bg, fg, w) {
  ctx.font = '11px ' + cssVar('--font');
  const tw = w || ctx.measureText(text).width + 10;
  ctx.fillStyle = bg;
  ctx.fillRect(x, y - 9, tw, 18);
  ctx.fillStyle = fg;
  ctx.textAlign = 'left';
  ctx.textBaseline = 'middle';
  ctx.fillText(text, x + 5, y + 0.5);
}

function timeLabel(t, tf, withDate) {
  const d = new Date(t);
  const p = v => String(v).padStart(2, '0');
  if (tf === '1d' || withDate) return `${p(d.getMonth() + 1)}/${p(d.getDate())}`;
  return `${p(d.getHours())}:${p(d.getMinutes())}`;
}

// 캔들 + 거래량 + SMA + 진입/손절/익절선 + 저널의 진입·청산 표시 + 십자선.
let chartGeom = null;
function drawChart() {
  const canvas = $('chart-canvas');
  const { ctx, W, H } = sizeCanvas(canvas);
  chartGeom = null;
  const all = state.candles;
  if (!all.length || W < 50 || H < 50) return;

  const C = {
    up: cssVar('--up'), down: cssVar('--down'), warn: cssVar('--warn'), muted: cssVar('--muted'),
    line: cssVar('--line'), text2: cssVar('--text-2'), surface3: cssVar('--surface-3'), accent: cssVar('--accent'),
  };
  const axisW = 70, timeH = 20;
  const plotW = W - axisW, plotH = H - timeH;
  const maxBars = Math.max(30, Math.floor(plotW / 6));
  const candles = all.slice(-maxBars);
  const offset = all.length - candles.length;
  const n = candles.length;
  const step = plotW / n;
  const volH = plotH * 0.16;
  const priceH = plotH - volH - 6;

  // 이 종목의 포지션이 있으면 진입/손절/익절선을 같이 그린다
  const s = state.status && state.status.symbols ? state.status.symbols[state.selected] : null;
  const refs = [];
  if (s && s.has_position && s.position) {
    refs.push({ price: s.position.entryPrice, color: C.text2, label: '진입' });
    if (s.stop_loss_price) refs.push({ price: s.stop_loss_price, color: C.down, label: '손절' });
    if (s.take_profit_price) refs.push({ price: s.take_profit_price, color: C.up, label: '익절' });
  }

  let lo = Math.min(...candles.map(c => c.l)), hi = Math.max(...candles.map(c => c.h));
  refs.forEach(r => { lo = Math.min(lo, r.price); hi = Math.max(hi, r.price); });
  const pad = (hi - lo) * 0.06 || hi * 0.01 || 1;
  lo -= pad; hi += pad;
  const y = v => 4 + (hi - v) / (hi - lo) * (priceH - 4);
  const x = i => i * step + step / 2;
  const maxVol = Math.max(...candles.map(c => c.v || 0)) || 1;

  // 가격 격자
  ctx.font = '11px ' + cssVar('--font');
  ctx.textBaseline = 'middle';
  ctx.textAlign = 'left';
  const ticks = 5;
  for (let i = 0; i <= ticks; i++) {
    const v = lo + (hi - lo) * (i / ticks);
    const yy = Math.round(y(v)) + 0.5;
    ctx.strokeStyle = C.line; ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(0, yy); ctx.lineTo(plotW, yy); ctx.stroke();
    ctx.fillStyle = C.muted;
    ctx.fillText(fmtPrice(v), plotW + 6, yy);
  }

  // 시간 축
  const tf = state.tf || '1h';
  const labelEvery = Math.max(1, Math.ceil(80 / step));
  ctx.textAlign = 'center';
  ctx.fillStyle = C.muted;
  for (let i = n - 1; i >= 0; i -= labelEvery) {
    const prev = candles[i - labelEvery];
    const newDay = prev && new Date(prev.t).getDate() !== new Date(candles[i].t).getDate();
    ctx.fillText(timeLabel(candles[i].t, tf, newDay), x(i), plotH + timeH / 2 + 1);
  }

  // 거래량
  candles.forEach((c, i) => {
    const h = (c.v || 0) / maxVol * volH;
    ctx.fillStyle = c.c >= c.o ? C.up : C.down;
    ctx.globalAlpha = 0.28;
    ctx.fillRect(x(i) - Math.max(1, step * 0.35), plotH - h, Math.max(1, step * 0.7), h);
  });
  ctx.globalAlpha = 1;

  // 캔들
  const bodyW = Math.max(1, Math.min(12, step * 0.68));
  candles.forEach((c, i) => {
    const color = c.c >= c.o ? C.up : C.down;
    const cx = Math.round(x(i)) + 0.5;
    ctx.strokeStyle = color; ctx.fillStyle = color; ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(cx, y(c.h)); ctx.lineTo(cx, y(c.l)); ctx.stroke();
    const top = y(Math.max(c.o, c.c)), bot = y(Math.min(c.o, c.c));
    ctx.fillRect(cx - bodyW / 2, top, bodyW, Math.max(1, bot - top));
  });

  // 전략 SMA (신호 기준선) — 전략 주기로 볼 때만 의미가 같다
  const smaPeriod = state.status && state.status.config ? state.status.config.sma_period : null;
  if (smaPeriod && all.length > smaPeriod) {
    const sma = smaSeries(all, smaPeriod).slice(offset);
    ctx.strokeStyle = C.accent; ctx.lineWidth = 1.3;
    ctx.beginPath();
    let started = false;
    sma.forEach((v, i) => {
      if (v === null) return;
      if (!started) { ctx.moveTo(x(i), y(v)); started = true; } else ctx.lineTo(x(i), y(v));
    });
    ctx.stroke();
  }

  // 진입/손절/익절선
  refs.forEach(r => {
    const yy = Math.round(y(r.price)) + 0.5;
    ctx.strokeStyle = r.color; ctx.setLineDash([5, 4]); ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(0, yy); ctx.lineTo(plotW, yy); ctx.stroke();
    ctx.setLineDash([]);
    axisLabel(ctx, fmtPrice(r.price), plotW + 1, yy, r.color, '#0c0e12', axisW - 1);
    ctx.fillStyle = r.color; ctx.textAlign = 'left'; ctx.textBaseline = 'bottom';
    ctx.fillText(r.label, 6, yy - 3);
    ctx.textBaseline = 'middle';
  });

  // 저널의 진입·청산 시점 표시(최근 30건 안에 있는 것만)
  const tfMs = TF_MS[tf] || 36e5;
  const events = ((state.status && state.status.recent_entries) || [])
    .filter(e => e.symbol === state.selected && (e.event === 'entered' || e.event === 'closed'));
  events.forEach(e => {
    const ts = new Date(e.timestamp).getTime();
    const idx = candles.findIndex(c => ts >= c.t && ts < c.t + tfMs);
    if (idx < 0) return;
    const c = candles[idx];
    const cx = x(idx);
    if (e.event === 'entered') {
      const long = e.signal === 'LONG';
      const yy = long ? y(c.l) + 10 : y(c.h) - 10;
      ctx.fillStyle = long ? C.up : C.down;
      ctx.beginPath();
      if (long) { ctx.moveTo(cx, yy - 5); ctx.lineTo(cx - 5, yy + 4); ctx.lineTo(cx + 5, yy + 4); }
      else { ctx.moveTo(cx, yy + 5); ctx.lineTo(cx - 5, yy - 4); ctx.lineTo(cx + 5, yy - 4); }
      ctx.fill();
    } else if (e.exit_price) {
      ctx.strokeStyle = (e.realized_pnl || 0) >= 0 ? C.up : C.down; ctx.lineWidth = 1.5;
      ctx.beginPath(); ctx.arc(cx, y(e.exit_price), 4, 0, Math.PI * 2); ctx.stroke();
    }
  });

  // 현재가선
  const lastC = candles[n - 1];
  const ly = Math.round(y(lastC.c)) + 0.5;
  const lastColor = lastC.c >= lastC.o ? C.up : C.down;
  ctx.strokeStyle = lastColor; ctx.setLineDash([1, 3]); ctx.lineWidth = 1;
  ctx.beginPath(); ctx.moveTo(0, ly); ctx.lineTo(plotW, ly); ctx.stroke();
  ctx.setLineDash([]);
  axisLabel(ctx, fmtPrice(lastC.c), plotW + 1, ly, lastColor, '#fff', axisW - 1);

  chartGeom = { candles, step, plotW, plotH, priceH, lo, hi, y, x, axisW, timeH, tf };

  // 십자선
  if (state.hover) {
    const { i, my } = state.hover;
    if (i >= 0 && i < n) {
      const cx = Math.round(x(i)) + 0.5;
      ctx.strokeStyle = C.muted; ctx.setLineDash([3, 3]); ctx.lineWidth = 1;
      ctx.beginPath(); ctx.moveTo(cx, 0); ctx.lineTo(cx, plotH); ctx.stroke();
      if (my !== null && my >= 0 && my <= priceH) {
        ctx.beginPath(); ctx.moveTo(0, my + 0.5); ctx.lineTo(plotW, my + 0.5); ctx.stroke();
        const price = hi - (my - 4) / (priceH - 4) * (hi - lo);
        ctx.setLineDash([]);
        axisLabel(ctx, fmtPrice(price), plotW + 1, my, C.surface3, C.text2, axisW - 1);
      }
      ctx.setLineDash([]);
      const d = new Date(candles[i].t);
      const p = v => String(v).padStart(2, '0');
      const label = `${p(d.getMonth() + 1)}/${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
      ctx.font = '11px ' + cssVar('--font');
      const tw = ctx.measureText(label).width + 10;
      const lx = Math.min(Math.max(0, cx - tw / 2), plotW - tw);
      ctx.fillStyle = C.surface3; ctx.fillRect(lx, plotH + 1, tw, timeH - 2);
      ctx.fillStyle = C.text2; ctx.textAlign = 'left'; ctx.textBaseline = 'middle';
      ctx.fillText(label, lx + 5, plotH + timeH / 2);
    }
  }

  renderOhlc(state.hover && state.hover.i < n ? candles[state.hover.i] : lastC, smaPeriod);
  renderChartLegend(refs, smaPeriod);
}

function renderOhlc(c, smaPeriod) {
  if (!c) { $('chart-ohlc').innerHTML = '&nbsp;'; return; }
  const chg = c.o ? (c.c - c.o) / c.o : 0;
  const cls = c.c >= c.o ? 'up' : 'down';
  $('chart-ohlc').innerHTML =
    `시 <b class="${cls}">${fmtPrice(c.o)}</b>고 <b class="${cls}">${fmtPrice(c.h)}</b>` +
    `저 <b class="${cls}">${fmtPrice(c.l)}</b>종 <b class="${cls}">${fmtPrice(c.c)}</b>` +
    `<b class="${cls}">${fmtSignedPct(chg, 2)}</b>거래량 <b>${fmtCompact(c.v)}</b>`;
}

function renderChartLegend(refs, smaPeriod) {
  const items = [];
  if (smaPeriod) items.push(`<span><i style="background:var(--accent)"></i>SMA${smaPeriod}${state.tf !== state.strategyTf ? ' (전략은 ' + (state.strategyTf || '') + ' 기준)' : ''}</span>`);
  refs.forEach(r => items.push(`<span><i style="background:${r.color}"></i>${r.label} ${fmtPrice(r.price)}</span>`));
  items.push('<span>▲▼ 봇 진입 · ○ 청산</span>');
  if (state.tf === state.strategyTf) items.push('<span>마지막 봉은 진행 중 (신호는 마감 봉만 봄)</span>');
  $('chart-legend').innerHTML = items.join('');
}

function chartPointer(clientX, clientY) {
  if (!chartGeom) return;
  const rect = $('chart-canvas').getBoundingClientRect();
  const mx = clientX - rect.left, my = clientY - rect.top;
  if (mx < 0 || mx > chartGeom.plotW || my < 0 || my > chartGeom.plotH) { state.hover = null; drawChart(); return; }
  state.hover = { i: Math.min(chartGeom.candles.length - 1, Math.max(0, Math.floor(mx / chartGeom.step))), my };
  drawChart();
}

// ---------------------------------------------------------------- 진입 조건
// state: 'ok'(충족) | 'no'(미충족 게이트) | ''(중립 — 통과/실패가 아니라 '거리'인 항목).
// SMA까지의 거리를 미충족처럼 칠하면 모든 줄이 경고로 보여서 오히려 안 읽힌다.
function condMetric(label, ratio, valueText, st) {
  const pct = Math.max(0, Math.min(1, ratio)) * 100;
  return `<div class="cond-metric"><div class="k"><span>${label}</span><span class="v ${st}">${valueText}</span></div>
    <div class="mini-track"><div class="mini-fill ${st}" style="width:${pct}%"></div></div></div>`;
}

function renderConditions(data) {
  $('cond-meta').textContent = `${data.timeframe} · ADX ≥ ${data.adx_threshold} · SMA${data.sma_period} 돌파 · 레짐 SMA${data.regime_sma_period} 위에서 숏 차단`;
  if (!data.symbols || !data.symbols.length) {
    $('cond-list').innerHTML = '<div class="cond-row"><span class="muted">표시할 종목이 없습니다.</span></div>';
    return;
  }
  const rows = data.symbols.slice().sort((a, b) => (b.ready - a.ready) || ((b.proximity || 0) - (a.proximity || 0)));
  $('cond-list').innerHTML = rows.map(r => {
    const name = esc(shortSymbol(r.symbol));
    if (r.candidate_side === null) {
      return `<div class="cond-row blocked"><div><div class="cond-name" data-sym="${esc(r.symbol)}">${name}</div></div>
        <div class="cond-bars"></div><div class="cond-score"><div class="pct">–</div></div>
        <div class="cond-blockers">${esc(r.blockers.join(' · ') || '계산 불가')}</div></div>`;
    }
    const isLong = r.candidate_side === 'LONG';
    const sideText = r.ready ? `지금 ${r.signal === 'LONG' ? '롱' : '숏'} 신호` : `${isLong ? '롱' : '숏'} 대기`;
    const crossRatio = 1 - Math.min(1, Math.abs(r.distance_pct) / data.cross_near_pct);
    const adxRatio = r.adx_threshold ? r.adx / r.adx_threshold : 1;
    // 방향 확인 지표는 설정에 따라 RSI일 수도 +DI/-DI일 수도 있다 — 문구는 서버가 만들어 보낸다.
    const dirRatio = typeof r.direction_score === 'number' ? r.direction_score : 0;
    const dirName = r.direction_filter === 'di' ? 'DI' : (r.direction_filter === 'none' ? '방향' : 'RSI');
    const cls = 'cond-row' + (r.ready ? ' ready' : r.regime_blocks_short ? ' blocked' : '');
    return `<div class="${cls}">
      <div><div class="cond-name" data-sym="${esc(r.symbol)}">${name}</div>
        <div class="cond-side ${isLong ? 'long' : 'short'}">${sideText}</div></div>
      <div class="cond-bars">
        ${condMetric('SMA' + r.sma_period + '까지', crossRatio, r.distance_pct.toFixed(2) + '%', r.ready ? 'ok' : '')}
        ${condMetric('ADX', adxRatio, r.adx.toFixed(1) + ' / ' + r.adx_threshold, r.adx_ok ? 'ok' : 'no')}
        ${condMetric(dirName, dirRatio, esc(r.direction_label || '–'), r.direction_ok ? 'ok' : 'no')}
      </div>
      <div class="cond-score"><div class="pct">${Math.round(r.proximity * 100)}%</div>
        <div class="cap">${r.regime_blocks_short ? '숏 차단' : '근접도'}</div></div>
      <div class="cond-blockers">${esc(r.blockers.join(' · ') || '조건 충족')}</div>
    </div>`;
  }).join('');

  const parts = ['막대가 길수록 조건에 가깝습니다. 근접도는 세 조건의 곱이라 하나만 멀어도 낮게 나옵니다.',
    '진입 신호는 SMA를 "막 돌파하는 순간"만 잡으므로, 거리가 0%에 가까워도 실제 돌파 전까지는 대기입니다.'];
  if (data.errors && data.errors.length) parts.push('조회 실패: ' + data.errors.map(e => esc(shortSymbol(e.symbol))).join(', '));
  $('cond-note').innerHTML = parts.join('<br>');
}

async function fetchConditions() {
  const env = state.env;
  let r;
  try { r = await api('/api/conditions'); } catch (e) { return; }
  // 응답을 기다리는 사이 DEMO/LIVE를 바꿨으면 낡은 결과를 그리지 않는다
  if (!r.ok || env !== state.env || r.data.env !== state.env) return;
  state.cond = r.data;
  renderConditions(r.data);
  renderWatch();
}

// ---------------------------------------------------------------- 내역
function entryMatchesFilter(e) {
  if (state.histFilter === 'trades') return e.event === 'entered' || e.event === 'closed';
  if (state.histFilter === 'issues') {
    const ev = e.event || '';
    return ev.startsWith('rejected') || ev.startsWith('skipped') || ev === 'circuit_breaker_blocked' || ev === 'unprotected_position';
  }
  return true;
}

function renderEntries() {
  const entries = ((state.status && state.status.recent_entries) || []).filter(entryMatchesFilter);
  $('entries-empty').classList.toggle('show', entries.length === 0);
  $('entries-body').innerHTML = entries.map(e => {
    const ev = e.event || 'no_signal';
    const [label, cls] = EVENT_INFO[ev] || [ev, 'ev-muted'];
    const closed = ev === 'closed';
    const reason = closed ? (REASON_LABELS[e.reason] || e.reason || '') : (e.reason || '');
    return `<tr>
      <td class="muted">${fmtDateTime(e.timestamp)}</td>
      <td>${e.symbol ? `<span class="sym-cell" data-sym="${esc(e.symbol)}">${esc(shortSymbol(e.symbol))}</span>` : '–'}</td>
      <td><span class="ev ${cls}" title="${esc(ev)}">${esc(label)}</span></td>
      <td class="${e.signal === 'LONG' ? 'up' : e.signal === 'SHORT' ? 'down' : ''}">${esc(e.signal || '–')}</td>
      <td class="r">${fmtPrice(e.entry_price)}</td>
      <td class="r">${closed ? fmtPrice(e.exit_price) : '–'}</td>
      <td class="r">${fmtPrice(e.stop_loss_price)}</td>
      <td class="r">${fmtPrice(e.take_profit_price)}</td>
      <td class="r ${closed ? signClass(e.realized_pnl) : ''}">${closed ? fmtSignedUsd(e.realized_pnl) : '–'}</td>
      <td class="wrap">${esc(reason)}</td>
    </tr>`;
  }).join('');

  // 휴대폰: 표는 옆으로 잘리므로 한 건을 두세 줄짜리 항목으로
  $('entries-cards').innerHTML = entries.map(e => {
    const ev = e.event || 'no_signal';
    const [label, cls] = EVENT_INFO[ev] || [ev, 'ev-muted'];
    const closed = ev === 'closed';
    const reason = closed ? (REASON_LABELS[e.reason] || e.reason || '') : (e.reason || '');
    const prices = [];
    if (e.entry_price) prices.push(`진입 ${fmtPrice(e.entry_price)}`);
    if (closed && e.exit_price) prices.push(`청산 ${fmtPrice(e.exit_price)}`);
    if (!closed && e.stop_loss_price) prices.push(`손절 ${fmtPrice(e.stop_loss_price)}`);
    if (!closed && e.take_profit_price) prices.push(`익절 ${fmtPrice(e.take_profit_price)}`);
    return `<div class="hist-item">
      <div class="hist-line">
        <span><span class="ev ${cls}">${esc(label)}</span>
          ${e.symbol ? ` <b class="sym-cell" data-sym="${esc(e.symbol)}">${esc(shortSymbol(e.symbol))}</b>` : ''}
          ${e.signal ? ` <span class="${e.signal === 'LONG' ? 'up' : 'down'} small">${esc(e.signal)}</span>` : ''}</span>
        ${closed ? `<span class="num ${signClass(e.realized_pnl)}">${fmtSignedUsd(e.realized_pnl)}</span>` : ''}
      </div>
      ${prices.length ? `<div class="hist-sub num">${prices.join(' · ')}</div>` : ''}
      ${reason ? `<div class="hist-sub">${esc(reason)}</div>` : ''}
      <div class="hist-sub">${fmtDateTime(e.timestamp)}</div>
    </div>`;
  }).join('');
}

// ---------------------------------------------------------------- 성과
async function fetchPerformance() {
  const env = state.env;
  let r;
  try { r = await api('/api/performance'); } catch (e) { return; }
  if (!r.ok || env !== state.env) return;
  state.perf = r.data;
  renderPerformance(r.data);
}

function stat(label, value, cls) {
  return `<div class="stat"><div class="k">${label}</div><div class="v ${cls || ''}">${value}</div></div>`;
}

function renderPerformance(data) {
  const r = data.r || {};
  // 순R(수수료 차감)을 앞에 둔다. 총R은 가격만으로 잰 값이라 수수료가 빠져 있고, 이 전략은 왕복
  // 수수료가 건당 기대값과 같은 크기라 총R만 보면 부호가 반대인 숫자를 믿게 된다(2026-09-22).
  $('perf-r-grid').innerHTML =
    stat('순 R (수수료 차감)', fmtR(r.total_net_r), signClass(r.total_net_r)) +
    stat('거래당 기대값 (순)', fmtR(r.avg_net_r), signClass(r.avg_net_r)) +
    stat('최대 낙폭 (순 R)', fmtR(r.max_drawdown_net_r), 'down') +
    stat('최대 낙폭 (실현)', fmtSignedUsd(r.max_drawdown_usd), 'down');
  $('perf-usd-grid').innerHTML =
    stat('총 거래 수', data.num_trades ?? '–') +
    stat('승률', fmtPct(data.win_rate)) +
    stat('총 실현손익', fmtSignedUsd(data.total_realized_pnl), signClass(data.total_realized_pnl)) +
    stat('평균 손익/거래', fmtSignedUsd(data.avg_realized_pnl), signClass(data.avg_realized_pnl));

  setText('kpi-netr', fmtR(r.total_net_r), signClass(r.total_net_r));
  $('kpi-netr-sub').textContent = r.num_trades ? `${r.num_trades}건 · 건당 ${fmtR(r.avg_net_r)} · 수수료 차감` : '수수료 차감';

  // 달러 줄과 모수가 다를 수 있다 — 진입가가 청산과 안 맞아 R을 못 믿는 거래는 R 통계에서만 빠진다.
  const excluded = (r.trades_total || 0) - (r.num_trades || 0);
  const parts = [`R 기준 ${r.num_trades || 0}건`];
  if (excluded > 0) parts.push(`${excluded}건은 진입가가 청산과 맞지 않아 제외 (달러 통계에는 포함)`);
  parts.push(`수수료 전 ${fmtR(r.total_r)}`);
  if (r.net_r_estimated_trades) {
    parts.push(`${r.net_r_estimated_trades}건은 실측 수수료가 없어 편도 ${((r.fee_pct_per_side || 0) * 100).toFixed(3)}%로 추정`);
  }
  $('perf-r-coverage').textContent = parts.join(' · ');

  renderConfigNote(r);
  renderMfe(r.mfe || {});
  renderSides(r.by_side || {});
  renderSymbols(data);
  renderVersions(data.versions || {});
  $('perf-empty').classList.toggle('show', !data.num_trades);
  drawCurve();
}

function renderConfigNote(r) {
  const since = r.since_config_change;
  if (!since) {
    // 설정 스냅샷은 봇이 시작할 때 기록된다 — 없으면 이 곡선이 어떤 규칙으로 나온 건지 알 수 없다.
    $('perf-config-note').textContent = '설정 기록이 아직 없습니다 — 봇을 다시 시작하면 지금 설정이 저널에 남고, 그 이후 구간이 여기 표시됩니다.';
    return;
  }
  const changed = Object.entries(since.changes || {})
    .map(([k, v]) => `${CONFIG_LABELS[k] || k} ${Array.isArray(v.from) ? '변경' : `${v.from ?? '–'}→${v.to}`}`).join(', ');
  const head = changed ? `마지막 설정 변경 (${fmtShortDate(since.timestamp)}): ${changed}` : `설정 기록 (${fmtShortDate(since.timestamp)})`;
  $('perf-config-note').innerHTML = esc(head) + (since.trades
    ? ` · 이후 <b>${since.trades}건, ${since.total_r === null ? 'R 없음' : fmtR(since.total_r)}</b> (${fmtSignedUsd(since.realized_pnl)})`
    : ' · 이후 아직 청산된 거래가 없습니다') + ' · 곡선의 주황 점선이 설정이 바뀐 지점입니다.';
}

// "익절 코앞까지 갔다가 손절났다"가 실제로 얼마나 되는지 — 최고점 기록은 2026-09-09 청산부터 있다.
function renderMfe(mfe) {
  if (!mfe.losers_measured) {
    $('perf-mfe').textContent = '되돌림 통계: 최고점 기록이 있는 청산이 아직 없습니다 (기록은 2026-09-09부터 쌓입니다).';
    return;
  }
  const pct = (mfe.losers_reaching_1r / mfe.losers_measured * 100).toFixed(0);
  $('perf-mfe').innerHTML = `되돌림 통계: 손실 거래 ${mfe.losers_measured}건 중 <b>${mfe.losers_reaching_1r}건(${pct}%)</b>이
    손절 전에 +1R 이상 갔었습니다 · 손실 거래 최고점 중앙값 ${fmtR(mfe.median_loser_peak_r)}`;
}

function renderSides(bySide) {
  const sides = Object.entries(bySide);
  $('perf-side-body').innerHTML = sides.length ? sides.map(([side, s]) => `<tr>
      <td class="${side === 'long' ? 'up' : side === 'short' ? 'down' : ''}">${side === 'long' ? '롱' : side === 'short' ? '숏' : esc(side)}</td>
      <td class="r">${s.trades}</td><td class="r">${fmtPct(s.trades ? s.wins / s.trades : null)}</td>
      <td class="r ${signClass(s.total_r)}">${fmtR(s.total_r)}</td></tr>`).join('')
    : '<tr><td colspan="4" class="muted">–</td></tr>';
}

function renderSymbols(data) {
  $('perf-reasons').innerHTML = Object.entries(data.reason_counts || {}).map(
    ([reason, count]) => `<span class="chip">${esc(REASON_LABELS[reason] || reason)} <b>${count}</b></span>`).join('');
  const perSymbol = Object.entries(data.per_symbol || {}).sort((a, b) => (b[1].total_pnl || 0) - (a[1].total_pnl || 0));
  $('perf-symbol-body').innerHTML = perSymbol.length ? perSymbol.map(([symbol, s]) => `<tr>
      <td><span class="sym-cell" data-sym="${esc(symbol)}">${esc(shortSymbol(symbol))}</span></td>
      <td class="r">${s.trades}</td><td class="r">${fmtPct(s.trades ? s.wins / s.trades : null)}</td>
      <td class="r ${signClass(s.total_pnl)}">${fmtSignedUsd(s.total_pnl)}</td></tr>`).join('')
    : '<tr><td colspan="4" class="muted">–</td></tr>';
}

// 전략 버전별 성과 — 거래는 진입 시각으로 버전에 배정되고, 과거 버전 경계는 복원한 추정 시각이다.
function renderVersions(v) {
  const versions = v.versions || [];
  $('perf-version-table').style.display = versions.length ? '' : 'none';
  if (!versions.length) { $('perf-version-head').textContent = ''; return; }
  $('perf-version-head').innerHTML = `전략 버전 · 현재 <b>${esc(v.current)}</b> · 지금까지 ${v.revisions}회 개정`
    + (v.unassigned_trades ? ` · 버전 이전 거래 ${v.unassigned_trades}건` : '');
  $('perf-version-body').innerHTML = versions.slice().reverse().map(row => {
    const changed = Object.entries(row.changes || {}).filter(([, c]) => c && c.from !== null && c.from !== undefined)
      .map(([k, c]) => `${CONFIG_LABELS[k] || k} ${Array.isArray(c.from) ? '변경' : `${c.from}→${c.to}`}`).join(', ');
    const detail = row.reconstructed ? `${row.title} — ${row.detail} (경계 시각 추정)` : (changed || row.detail || row.title);
    const hasR = row.trades_with_r;
    return `<tr class="${row.label === v.current ? 'current' : ''}">
      <td><b>${esc(row.label)}</b></td><td class="muted">${fmtShortDate(row.start)}</td><td class="wrap">${esc(detail)}</td>
      <td class="r">${row.trades}</td><td class="r">${hasR ? fmtPct(row.win_rate) : '–'}</td>
      <td class="r ${hasR ? signClass(row.total_net_r) : ''}">${hasR ? fmtR(row.total_net_r) : '–'}</td>
      <td class="r">${hasR ? fmtR(row.avg_net_r) : '–'}</td></tr>`;
  }).join('');
}

async function fetchEquity() {
  const env = state.env;
  let r;
  try { r = await api('/api/equity'); } catch (e) { return; }
  if (!r.ok || env !== state.env || r.data.env !== state.env) return;
  state.equity = r.data;
  const m = r.data.month;
  const target = r.data.target_monthly_return;
  if (m) {
    setText('kpi-month', fmtSignedPct(m.return_pct, 2), signClass(m.return_pct));
    const gap = m.return_pct - target;
    $('kpi-month-sub').textContent = `목표 월 +${fmtPct(target, 2)} · ${gap >= 0 ? '달성' : '부족 ' + fmtPct(-gap, 2)} · ${m.days}일 기록`;
  } else {
    setText('kpi-month', '–', '');
    $('kpi-month-sub').textContent = `목표 월 +${fmtPct(target, 2)} · 자산 기록 ${(r.data.days || []).length}일 (2일부터 계산)`;
  }
  if (state.curveMode === 'equity') drawCurve();
}

// 누적 곡선: 순R / 실현손익 / 자산(날짜별). 0선(자산은 시작값)을 항상 포함시킨다 — 안 그러면
// 전 구간 손실인 곡선이 "우상향"처럼 보인다.
function curvePoints() {
  if (state.curveMode === 'equity') {
    const days = ((state.equity && state.equity.days) || []).filter(d => typeof d.end === 'number');
    return { values: days.map(d => d.end), times: days.map(d => d.day), base: days.length ? days[0].start || days[0].end : 0, fmt: fmtUsd, marks: [] };
  }
  const r = (state.perf && state.perf.r) || {};
  const pts = r.equity_curve || [];
  // 옛 기록만 있는 저널에서는 cumulative_net_r이 없을 수 있어 총R로 떨어진다
  const key = state.curveMode === 'r'
    ? (pts.length && pts[0].cumulative_net_r !== undefined ? 'cumulative_net_r' : 'cumulative_r')
    : 'cumulative_pnl';
  // 설정이 바뀐 지점: 경계는 "직전 거래와 다음 거래 사이"라 두 점의 중간에 긋는다.
  const marks = (r.config_changes || []).map(ch => {
    const idx = pts.findIndex(p => (p.timestamp || '') > (ch.timestamp || ''));
    if (idx === 0) return null;          // 곡선 구간보다 이전
    return idx === -1 ? pts.length - 1 : idx - 0.5;
  }).filter(v => v !== null);
  return { values: pts.map(p => p[key]), times: pts.map(p => p.timestamp), base: 0,
    fmt: state.curveMode === 'r' ? fmtR : fmtSignedUsd, marks };
}

function drawCurve() {
  const canvas = $('curve-canvas');
  const { ctx, W, H } = sizeCanvas(canvas);
  if (W < 50) return;
  const { values, base, fmt, marks } = curvePoints();
  if (values.length < 2) {
    $('curve-msg').textContent = state.curveMode === 'equity'
      ? '자산 기록이 이틀 이상 쌓이면 그려집니다' : '아직 곡선을 그릴 만큼 청산된 거래가 없습니다';
    return;
  }
  $('curve-msg').textContent = '';
  const C = { up: cssVar('--up'), down: cssVar('--down'), muted: cssVar('--muted'), line: cssVar('--line'), warn: cssVar('--warn') };
  const axisW = 76, padT = 12, padB = 12;
  const plotW = W - axisW;
  let lo = Math.min(base, ...values), hi = Math.max(base, ...values);
  const span = (hi - lo) || Math.abs(hi) * 0.01 || 1;
  lo -= span * 0.05; hi += span * 0.05;
  const x = i => 8 + (i / (values.length - 1)) * (plotW - 16);
  const y = v => padT + (hi - v) / (hi - lo) * (H - padT - padB);

  ctx.font = '11px ' + cssVar('--font');
  ctx.textBaseline = 'middle'; ctx.textAlign = 'left';
  const lastY = y(values[values.length - 1]);
  for (let i = 0; i <= 4; i++) {
    const v = lo + (hi - lo) * (i / 4);
    const yy = Math.round(y(v)) + 0.5;
    ctx.strokeStyle = C.line; ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(0, yy); ctx.lineTo(plotW, yy); ctx.stroke();
    // 마지막 값 라벨과 겹치는 눈금 글자는 생략한다
    if (Math.abs(yy - lastY) > 14) { ctx.fillStyle = C.muted; ctx.fillText(fmt(v), plotW + 6, yy); }
  }
  const by = Math.round(y(base)) + 0.5;
  ctx.strokeStyle = C.muted; ctx.setLineDash([3, 3]);
  ctx.beginPath(); ctx.moveTo(0, by); ctx.lineTo(plotW, by); ctx.stroke();
  marks.forEach(m => {
    const mx = Math.round(x(m)) + 0.5;
    ctx.strokeStyle = C.warn; ctx.globalAlpha = 0.7;
    ctx.beginPath(); ctx.moveTo(mx, padT); ctx.lineTo(mx, H - padB); ctx.stroke();
    ctx.globalAlpha = 1;
  });
  ctx.setLineDash([]);

  const last = values[values.length - 1];
  const color = last >= base ? C.up : C.down;
  ctx.beginPath();
  ctx.moveTo(x(0), by);
  values.forEach((v, i) => ctx.lineTo(x(i), y(v)));
  ctx.lineTo(x(values.length - 1), by);
  ctx.closePath();
  ctx.fillStyle = color; ctx.globalAlpha = 0.1; ctx.fill(); ctx.globalAlpha = 1;
  ctx.beginPath();
  values.forEach((v, i) => (i ? ctx.lineTo(x(i), y(v)) : ctx.moveTo(x(i), y(v))));
  ctx.strokeStyle = color; ctx.lineWidth = 1.6; ctx.stroke();
  axisLabel(ctx, fmt(last), plotW + 1, y(last), color, '#0c0e12', axisW - 1);
}

// ---------------------------------------------------------------- 봇 / 텔레그램
async function refreshBotStatus() {
  const env = state.env;
  let r;
  try { r = await api('/api/bot/status'); } catch (e) { return; }
  if (!r.ok || env !== state.env) return;
  const data = r.data;
  const dot = $('bot-status-dot'), top = $('top-bot-status');
  const hb = data.heartbeat;
  const breaker = $('breaker-line');
  if (data.running) {
    const uptime = data.started_at ? Math.max(0, Math.floor(Date.now() / 1000 - data.started_at)) : null;
    $('bot-status-text').textContent = '실행 중' + (uptime !== null ? ` · 가동 ${formatUptime(uptime)}` : '');
    $('bot-start-btn').disabled = true;
    $('bot-stop-btn').disabled = false;
    const stale = !hb || hb.age_seconds > 180;  // 30초 주기 기준 3분 넘게 조용하면 정체 의심
    dot.className = 'dot ' + (stale ? 'stale' : 'on');
    const hbLine = $('heartbeat-line');
    hbLine.className = 'muted small' + (stale ? ' stale' : '');
    hbLine.textContent = hb
      ? `마지막 사이클 ${formatAge(hb.age_seconds)} · #${hb.cycle_count}${stale ? ' · 사이클이 멈춘 것 같습니다' : ''}`
      : '사이클 기록 없음 (재시작 직후라면 잠시 후 갱신됩니다)';
    // 저널의 circuit_breaker_blocked는 막히기 시작할 때 한 줄뿐 — "지금도 막혀 있는가"는 하트비트에만 있다.
    if (hb && hb.circuit_breaker_blocked) {
      breaker.className = 'breaker-line show';
      breaker.textContent = '서킷브레이커로 신규 진입 정지 중' + (hb.breaker_reason ? ` — ${hb.breaker_reason}` : '');
    } else {
      breaker.className = 'breaker-line';
    }
    top.innerHTML = `<span class="dot ${stale ? 'stale' : 'on'}"></span><span>봇 실행 중</span>`;
  } else {
    dot.className = 'dot';
    $('bot-status-text').textContent = '중지됨';
    $('bot-start-btn').disabled = false;
    $('bot-stop-btn').disabled = true;
    $('heartbeat-line').textContent = '신규 진입과 손절/익절 감시가 멈춰 있습니다 (걸려 있는 주문은 거래소에 남아 있음)';
    $('heartbeat-line').className = 'muted small';
    breaker.className = 'breaker-line';
    top.innerHTML = '<span class="dot"></span><span>봇 중지됨</span>';
  }
}

async function startBot() {
  if (state.env === 'live' && !confirm('LIVE 감시 봇을 시작할까요? 실제 자금으로 자동 진입합니다.')) return;
  const btn = $('bot-start-btn');
  btn.disabled = true; btn.textContent = '시작 중…';
  try {
    const r = await api('/api/bot/start', { method: 'POST' });
    if (!r.ok) alert('봇 시작에 실패했습니다: ' + (r.data.message || ''));
  } catch (e) { alert('봇 시작 요청 중 네트워크 오류가 발생했습니다.'); }
  btn.textContent = '시작';
  refreshBotStatus();
}

async function stopBot() {
  if (!confirm(`감시 봇을 중지할까요? (${state.env.toUpperCase()}) 이미 걸려 있는 포지션/주문(손절·익절)은 그대로 유지됩니다.`)) return;
  const btn = $('bot-stop-btn');
  btn.disabled = true; btn.textContent = '중지 중… (최대 15초)';
  try {
    const r = await api('/api/bot/stop', { method: 'POST' });
    if (!r.ok) alert('봇 중지에 실패했습니다.');
  } catch (e) { alert('봇 중지 요청 중 네트워크 오류가 발생했습니다.'); }
  btn.textContent = '중지';
  refreshBotStatus();
}

async function refreshTelegramStatus() {
  let data;
  try {
    const res = await fetch('/api/telegram/status');
    if (res.status === 401) return;
    data = await res.json();
  } catch (e) { return; }
  const uptime = data.running && data.started_at ? Math.max(0, Math.floor(Date.now() / 1000 - data.started_at)) : null;
  $('telegram-status-dot').className = 'dot' + (data.running ? ' on' : '');
  $('telegram-status-text').textContent = data.running ? '실행 중' + (uptime !== null ? ` · 가동 ${formatUptime(uptime)}` : '') : '중지됨';
  $('telegram-start-btn').disabled = !!data.running;
  $('telegram-stop-btn').disabled = !data.running;
}

async function telegramAction(action) {
  const btn = $(`telegram-${action}-btn`);
  const label = btn.textContent;
  btn.disabled = true; btn.textContent = action === 'start' ? '시작 중…' : '중지 중…';
  try {
    const res = await fetch(`/api/telegram/${action}`, { method: 'POST' });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) alert(`텔레그램 봇 ${action === 'start' ? '시작' : '중지'}에 실패했습니다: ` + (data.message || ''));
  } catch (e) { alert('텔레그램 요청 중 네트워크 오류가 발생했습니다.'); }
  btn.textContent = label;
  refreshTelegramStatus();
}
function startTelegram() { telegramAction('start'); }
function stopTelegram() { telegramAction('stop'); }

// ---------------------------------------------------------------- 청산 / 리셋
async function closePosition(symbol) {
  if (!confirm(`${shortSymbol(symbol)} 포지션을 지금 즉시 시장가로 청산할까요? (${state.env.toUpperCase()})`)) return;
  [$('close-btn-' + shortSymbol(symbol)), $('close-card-' + shortSymbol(symbol))].forEach(btn => {
    if (btn) { btn.disabled = true; btn.textContent = '청산 중…'; }
  });
  try {
    const r = await api('/api/close/' + encodeURIComponent(symbol), { method: 'POST' });
    if (!r.ok || r.data.status === 'error') alert('청산 실패: ' + (r.data.message || '알 수 없는 오류'));
  } catch (e) { alert('청산 요청 중 네트워크 오류가 발생했습니다.'); }
  refreshStatus();
}

async function resetStreak() {
  if (!confirm(`연속 손실 카운트를 0으로 리셋할까요? (${state.env.toUpperCase()}) 서킷브레이커 임계치 자체는 그대로 유지됩니다.`)) return;
  try {
    const r = await api('/api/risk/reset-streak', { method: 'POST' });
    if (!r.ok || r.data.status === 'error') alert('리셋 실패: ' + (r.data.message || '알 수 없는 오류'));
  } catch (e) { alert('리셋 요청 중 네트워크 오류가 발생했습니다.'); }
  refreshStatus();
}

// ---------------------------------------------------------------- 브라우저 알림
let notifyEnabled = store('notifyEnabled') === 'true';
let notifyInitialized = false;
const seenEntryKeys = new Set();

function updateNotifyBtn() {
  const btn = $('notify-btn');
  const on = notifyEnabled && 'Notification' in window && Notification.permission === 'granted';
  btn.textContent = on ? '브라우저 알림 켜짐' : '브라우저 알림 켜기';
  btn.className = 'btn btn-ghost btn-block' + (on ? ' on' : '');
}

async function toggleNotify() {
  if (!('Notification' in window)) { alert('이 브라우저는 알림을 지원하지 않습니다. 휴대폰에서는 텔레그램 알림을 쓰세요.'); return; }
  if (!notifyEnabled) {
    const perm = await Notification.requestPermission();
    if (perm !== 'granted') { alert('알림 권한이 거부되었습니다.'); return; }
    notifyEnabled = true;
  } else {
    notifyEnabled = false;
  }
  store('notifyEnabled', String(notifyEnabled));
  updateNotifyBtn();
}

function collectNotifications(entries) {
  for (const e of entries) {
    const key = `${e.timestamp}|${e.symbol}|${e.event}`;
    if (seenEntryKeys.has(key)) continue;
    seenEntryKeys.add(key);
    if (notifyInitialized) notifyNewEntry(e);
  }
  notifyInitialized = true;
}

function notifyNewEntry(e) {
  if (!notifyEnabled || !('Notification' in window) || Notification.permission !== 'granted') return;
  const symbol = e.symbol ? shortSymbol(e.symbol) : '';
  if (e.event === 'entered') {
    new Notification(`${symbol} 진입`, { body: `${e.signal || ''} @ ${fmtPrice(e.entry_price)}` });
  } else if (e.event === 'closed') {
    new Notification(`${symbol} 청산 (${REASON_LABELS[e.reason] || e.reason || ''})`, { body: `실현손익 ${fmtUsd(e.realized_pnl)}` });
  }
}

// ---------------------------------------------------------------- 이벤트 연결
document.addEventListener('click', ev => {
  const symEl = ev.target.closest('[data-sym]');
  if (symEl) { selectSymbol(symEl.dataset.sym); return; }
  const tab = ev.target.closest('#dock-tabs button');
  if (tab) { setTab(tab.dataset.tab); return; }
  const m = ev.target.closest('#mnav button');
  if (m) { setMobileView(m.dataset.m); return; }
  const tfBtn = ev.target.closest('#tf-seg button');
  if (tfBtn) {
    state.tf = tfBtn.dataset.tf;
    store('auto2.chartTf', state.tf);
    document.querySelectorAll('#tf-seg button').forEach(b => b.classList.toggle('active', b === tfBtn));
    state.candles = []; state.hover = null;
    drawChart();
    loadChart();
    return;
  }
  const curveBtn = ev.target.closest('#curve-seg button');
  if (curveBtn) {
    state.curveMode = curveBtn.dataset.mode;
    document.querySelectorAll('#curve-seg button').forEach(b => b.classList.toggle('active', b === curveBtn));
    drawCurve();
    return;
  }
  const histBtn = ev.target.closest('#hist-filter button');
  if (histBtn) {
    state.histFilter = histBtn.dataset.f;
    document.querySelectorAll('#hist-filter button').forEach(b => b.classList.toggle('active', b === histBtn));
    renderEntries();
  }
});

(function wireChartPointer() {
  const canvas = $('chart-canvas');
  canvas.addEventListener('mousemove', e => chartPointer(e.clientX, e.clientY));
  canvas.addEventListener('mouseleave', () => { state.hover = null; drawChart(); });
  canvas.addEventListener('touchstart', e => { const t = e.touches[0]; chartPointer(t.clientX, t.clientY); }, { passive: true });
  canvas.addEventListener('touchmove', e => { const t = e.touches[0]; chartPointer(t.clientX, t.clientY); }, { passive: true });
  canvas.addEventListener('touchend', () => { state.hover = null; drawChart(); }, { passive: true });
  const ro = new ResizeObserver(() => { drawChart(); drawCurve(); });
  ro.observe($('chart-box'));
  ro.observe(document.querySelector('.curve-box'));
})();

// 단축키: 1~5 하단 탭(입력창이 없어서 충돌 없음). 계좌 전환은 실수 방지를 위해 단축키를 두지 않는다.
document.addEventListener('keydown', e => {
  if (e.ctrlKey || e.metaKey || e.altKey) return;
  const tabs = ['positions', 'conditions', 'history', 'performance', 'settings'];
  if (e.key >= '1' && e.key <= '5') setTab(tabs[Number(e.key) - 1]);
});

// ---------------------------------------------------------------- 시작
updateNotifyBtn();
setMobileView('home');
const savedTab = store('auto2.tab');
if (savedTab && document.querySelector(`#dock-tabs button[data-tab="${savedTab}"]`)) setTab(savedTab);
refreshAll();
refreshTelegramStatus();

// 거래소에 묻는 것(상태·시세·조건·차트)은 서버가 캐시를 공유하지만, 주기 자체도 여유 있게 둔다 —
// 바이낸스 요청 한도는 IP 단위라 두 봇과 같이 쓴다(2026-09-28 IP 차단 사고).
every(refreshStatus, 10000);
every(refreshBotStatus, 10000);     // 로컬 파일만 읽음
every(refreshTelegramStatus, 15000);
every(fetchTickers, 30000);         // 서버 캐시 30초
every(fetchPerformance, 30000);     // 저널만 읽음
every(fetchConditions, 60000);      // 서버 캐시 60초 — 신호는 마감 봉 기준이라 자주 안 바뀐다
every(loadChart, 30000);
every(fetchEquity, 60000);
