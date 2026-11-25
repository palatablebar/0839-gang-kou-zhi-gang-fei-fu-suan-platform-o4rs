(function () {
  var state = { plan: null, db: { shifts: [], appointments: [] } };

  function $(id) { return document.getElementById(id); }
  function esc(value) {
    return String(value === null || value === undefined ? '' : value).replace(/[&<>"']/g, function (ch) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch];
    });
  }
  async function request(url, options) {
    var headers = { 'Content-Type': 'application/json' };
    if (options && options.idempotencyKey) headers['Idempotency-Key'] = options.idempotencyKey;
    var response = await fetch(url, {
      method: (options && options.method) || 'GET',
      headers: headers,
      body: options && options.body ? JSON.stringify(options.body) : undefined
    });
    var data = await response.json().catch(function () { return {}; });
    if (!response.ok) throw new Error(data.error || data.code || ('HTTP ' + response.status));
    return data;
  }
  function notice(text, bad) {
    var node = $('notice');
    node.textContent = text || '';
    node.style.color = bad ? 'var(--danger)' : '';
  }
  function yuan(cents) {
    var value = Number(cents || 0) / 100;
    return value.toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  }
  function mergeDbRows(plan) {
    var byCid = {};
    (state.db.appointments || []).forEach(function (row) { byCid[row.container_id] = row; });
    (plan.items || []).forEach(function (item) {
      var row = byCid[item.container_id];
      if (row && (row.status === 'SENT' || row.status === 'CONFIRMED' || row.status === 'ISSUING' || row.status === 'ISSUE_FAILED')) {
        item.status = row.status;
        item.shift_id = row.shift_id;
        item.locked = true;
      }
      if (row && row.id) item.appointment_id = row.id;
    });
  }
  function renderKpis(plan) {
    var totals = plan.totals || {};
    $('kpi-scheduled').textContent = totals.scheduled_count || 0;
    $('kpi-waiting').textContent = totals.waiting_count || 0;
    $('kpi-vessel').textContent = totals.vessel_count || 0;
    $('kpi-fee').textContent = yuan(totals.new_demurrage_cents);
    $('state-status').textContent = '已排 ' + (totals.scheduled_count || 0) + ' · 等待 ' + (totals.waiting_count || 0);
  }
  function renderShifts(plan) {
    var rows = plan.shifts || [];
    $('shift-body').innerHTML = rows.length ? rows.map(function (shift) {
      var at = shift.at ? shift.at.replace('T', ' ').slice(0, 16) : (shift.date || '-');
      return '<tr data-shift="' + esc(shift.shift_id) + '">' +
        '<td><strong>' + esc(shift.shift_id) + '</strong></td>' +
        '<td>' + esc(at) + '</td>' +
        '<td class="num">' + esc(shift.capacity) + '</td>' +
        '<td class="num">' + esc(shift.locked) + '</td>' +
        '<td class="num"><strong>' + esc(shift.remaining) + '</strong></td>' +
        '<td><span class="capacity-actions">' +
          '<button type="button" data-cap="-1" aria-label="减少名额">−</button>' +
          '<span class="cap-value">' + esc(shift.capacity) + '</span>' +
          '<button type="button" data-cap="1" aria-label="增加名额">+</button>' +
        '</span></td>' +
      '</tr>';
    }).join('') : '<tr><td class="empty" colspan="6">暂无班次</td></tr>';

    Array.from(document.querySelectorAll('[data-cap]')).forEach(function (button) {
      button.addEventListener('click', function () {
        var tr = button.closest('tr');
        var sid = tr.getAttribute('data-shift');
        var shift = (plan.shifts || []).find(function (item) { return item.shift_id === sid; });
        var delta = Number(button.getAttribute('data-cap'));
        var next = Math.max(0, Number(shift.capacity) + delta);
        changeCapacity(sid, next);
      });
    });
  }
  function actionCell(item) {
    if (!item.shift_id) return '<span class="hint">等下一批</span>';
    var id = esc(item.appointment_id || ('APT-' + item.container_id));
    var status = item.status || 'PROPOSED';
    if (status === 'SENT' || status === 'CONFIRMED') {
      return '<span class="status-pill" data-status="' + esc(status) + '">已' + (status === 'SENT' ? '发车队' : '确认') + '</span>';
    }
    if (status === 'ISSUING') return '<span class="status-pill" data-status="ISSUING">签发中</span>';
    var label = status === 'ISSUE_FAILED' ? '重试签发' : '签发车队';
    return '<div class="row-actions">' +
      '<button type="button" data-issue="' + id + '">' + label + '</button>' +
      '<button type="button" data-confirm="' + id + '">仅确认</button>' +
      (status === 'ISSUE_FAILED' ? '<span class="hint">占位未释放</span>' : '') +
    '</div>';
  }
  function renderAppointments(plan) {
    var items = plan.items || [];
    $('appointment-body').innerHTML = items.length ? items.map(function (item) {
      var vessel = item.catches_vessel
        ? '<span class="vessel-yes">✔ 赶上船</span>'
        : (item.vessel_required ? '<span class="vessel-no">✕ 错过/未排</span>' : '<span class="hint">无船期约束</span>');
      var fee = Number(item.new_demurrage_cents || 0);
      return '<tr data-appt="' + esc(item.appointment_id || ('APT-' + item.container_id)) + '">' +
        '<td><strong>' + esc(item.container_id) + '</strong></td>' +
        '<td>' + esc(item.shift_id || '下一批') + '</td>' +
        '<td><span class="status-pill" data-status="' + esc(item.status || 'WAITING') + '">' + esc(item.status || 'WAITING') + '</span></td>' +
        '<td>' + vessel + '</td>' +
        '<td class="num ' + (fee < 0 ? 'fee-negative' : 'fee-positive') + '">' + yuan(fee) + '</td>' +
        '<td><small>' + esc(item.reason || '') + '</small></td>' +
        '<td>' + actionCell(item) + '</td>' +
      '</tr>';
    }).join('') : '<tr><td class="empty" colspan="7">暂无箱单，请载入示例</td></tr>';

    Array.from(document.querySelectorAll('[data-issue]')).forEach(function (button) {
      button.addEventListener('click', function () { issue(button.getAttribute('data-issue')); });
    });
    Array.from(document.querySelectorAll('[data-confirm]')).forEach(function (button) {
      button.addEventListener('click', function () { confirmOne(button.getAttribute('data-confirm')); });
    });
  }
  function render(plan, db) {
    state.plan = plan;
    if (db) state.db = db;
    mergeDbRows(plan);
    renderKpis(plan);
    renderShifts(plan);
    renderAppointments(plan);
  }
  async function loadState() {
    state.db = await request('/api/appointments');
  }
  async function loadSample() {
    notice('排程中…');
    try {
      var plan = await request('/api/appointments/plan', { method: 'POST', body: {} });
      await loadState();
      render(plan);
      notice('已按当前阶梯费率生成方案');
    } catch (error) { notice(error.message, true); }
  }
  async function replan() {
    notice('重排中…');
    try {
      var plan = await request('/api/appointments/replan', { method: 'POST', body: {} });
      await loadState();
      render(plan);
      notice('已保留硬占位并重排未确认预约');
    } catch (error) { notice(error.message, true); }
  }
  async function changeCapacity(sid, capacity) {
    try {
      var plan = await request('/api/appointments/capacity', { method: 'POST', body: { shift_id: sid, capacity: capacity } });
      await loadState();
      render(plan);
      notice(sid + ' 名额调整为 ' + capacity + '，未确认预约已重排');
    } catch (error) { notice(error.message, true); }
  }
  function keyFor(id) { return 'web-' + id + '-' + (id.startsWith('APT-') ? id.slice(4) : id); }
  async function issue(id) {
    try {
      var body = { simulate_failure: $('simulate-failure').checked };
      var row = await request('/api/appointments/' + encodeURIComponent(id) + '/issue', {
        method: 'POST', body: body, idempotencyKey: keyFor(id)
      });
      $('simulate-failure').checked = false;
      await loadSampleSilent();
      notice('预约 ' + id + ' 已签发，幂等键重试不会重复占位');
    } catch (error) {
      await loadSampleSilent();
      notice(error.message + '：可点击“重试签发”，原名额仍保留', true);
    }
  }
  async function confirmOne(id) {
    try {
      await request('/api/appointments/' + encodeURIComponent(id) + '/confirm', {
        method: 'POST', body: {}, idempotencyKey: keyFor(id)
      });
      await loadSampleSilent();
      notice('预约 ' + id + ' 已确认');
    } catch (error) { notice(error.message, true); }
  }
  async function loadSampleSilent() {
    var db = await request('/api/appointments');
    state.db = db;
    // 重新计算方案会用数据库中的硬占位；不会覆盖 SENT/CONFIRMED。
    var plan = await request('/api/appointments/replan', { method: 'POST', body: {} });
    render(plan, db);
  }
  async function fillSample() {
    var sample = await request('/api/appointments/sample');
    $('payload-json').value = JSON.stringify(sample, null, 2);
    return sample;
  }
  async function runJson() {
    var payload;
    try {
      payload = JSON.parse($('payload-json').value || '{}');
    } catch (error) {
      notice('JSON 格式错误：' + error.message, true);
      return;
    }
    try {
      var plan = await request('/api/appointments/plan', { method: 'POST', body: payload });
      await loadState();
      render(plan);
      notice('JSON 排程完成');
    } catch (error) { notice(error.message, true); }
  }
  document.addEventListener('DOMContentLoaded', function () {
    $('load-sample').addEventListener('click', async function () {
      try { await fillSample(); await runJson(); } catch (error) { notice(error.message, true); }
    });
    $('run-json').addEventListener('click', runJson);
    $('fill-sample').addEventListener('click', function () {
      fillSample().then(function () { notice('已填入样例 JSON'); }).catch(function (error) { notice(error.message, true); });
    });
    $('replan').addEventListener('click', replan);
    fillSample().catch(function () {});
    loadSample().catch(function () {});
  });
})();
