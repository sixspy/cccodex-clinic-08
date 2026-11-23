import { api, chip, escapeHtml } from '../api.js';

function quotaBadge(s) {
  if (!s.badge_matches) {
    return `<span class="chip bad" title="徽标 ${s.quota_badge} ≠ 已用 ${s.quota_used}">徽标失衡</span>`;
  }
  if (s.quota_full) {
    return `<span class="chip bad">配额已满</span>`;
  }
  return `<span class="chip ok">余 ${s.quota_remaining}</span>`;
}

export async function renderSites(el) {
  const [data, counts, protocols, gates, matrix] = await Promise.all([
    api('/api/sites'),
    api('/api/site-counts'),
    api('/api/protocols'),
    api('/api/quota-gates'),
    api('/api/quota-matrix'),
  ]);
  const items = data.items || [];
  const activeProto = (protocols.items || []).find((p) => p.status === '生效');
  const gateSummary = gates.summary || {};
  const matrixItems = matrix.items || [];

  el.innerHTML = `
    <div class="card">
      <h2>试验中心目录</h2>
      <p class="muted">登记临床试验中心、启停状态与访视抓取配额。配额闸按「甲侧旁路凭证 + 指纹链」口径落点；停用前须先结束采集中批次。</p>
      <div class="grid">
        <div class="stat"><div class="n">${counts.total || 0}</div><div class="l">中心总数</div></div>
        <div class="stat"><div class="n">${counts['启用'] || 0}</div><div class="l">启用</div></div>
        <div class="stat"><div class="n">${counts['观察'] || 0}</div><div class="l">观察</div></div>
        <div class="stat"><div class="n">${counts['停用'] || 0}</div><div class="l">停用</div></div>
      </div>
      <div class="row">
        <input id="sname" placeholder="中心名称" />
        <input id="sregion" placeholder="区域" />
        <input id="sprincipal" placeholder="负责人" />
        <input id="squota" type="number" value="50" style="width:100px" title="配额" />
        <input id="snote" placeholder="备注" />
        <button id="create">登记中心</button>
      </div>
      <p class="muted">当前生效方案：${activeProto ? escapeHtml(activeProto.title + ' @ ' + activeProto.version) : '无'}</p>
    </div>
    <div class="card">
      <h2>配额闸摘要台</h2>
      <p class="muted">${escapeHtml(gates.caliber || '')}</p>
      <div class="grid">
        <div class="stat"><div class="n">${(gateSummary['已封存'] || {}).count || 0}</div><div class="l">已封存落点</div></div>
        <div class="stat"><div class="n">${(gateSummary['占用'] || {}).count || 0}</div><div class="l">占用中</div></div>
        <div class="stat"><div class="n">${(gateSummary['已驳回'] || {}).count || 0}</div><div class="l">已驳回</div></div>
        <div class="stat"><div class="n">${gateSummary.total || 0}</div><div class="l">落点合计</div></div>
      </div>
      <table>
        <thead>
          <tr><th>中心</th><th>配额</th><th>徽标(计数器)</th><th>已用配额</th><th>剩余</th><th>徽标对账</th><th>落点</th></tr>
        </thead>
        <tbody>
          ${matrixItems.map((m) => `
            <tr>
              <td>${escapeHtml(m.site_code)} · ${escapeHtml(m.name)}</td>
              <td>${m.quota}</td>
              <td>${m.badge}</td>
              <td>${m.used}</td>
              <td>${m.remaining}</td>
              <td>${m.badge_matches ? '<span class="chip ok">对齐</span>' : '<span class="chip bad">拧花</span>'}</td>
              <td>${m.gate_count}</td>
            </tr>`).join('') || '<tr><td colspan="7" class="muted">暂无中心</td></tr>'}
        </tbody>
      </table>
    </div>
    <div class="card">
      <div class="row">
        <select id="filterStatus">
          <option value="">全部状态</option>
          <option>启用</option>
          <option>观察</option>
          <option>停用</option>
        </select>
        <button id="refresh" class="ghost">刷新</button>
      </div>
      <table>
        <thead>
          <tr>
            <th>编码</th><th>名称</th><th>状态</th><th>配额</th><th>已用配额</th>
            <th>配额徽标</th><th>落点</th>
            <th>批次</th><th>缺口合计</th><th>区域/负责人</th><th>操作</th>
          </tr>
        </thead>
        <tbody>
          ${items.map((s) => `
            <tr data-id="${s.id}">
              <td>${escapeHtml(s.site_code)}</td>
              <td>
                <div>${escapeHtml(s.name)}</div>
                <div class="muted">${escapeHtml(s.note || '')}</div>
              </td>
              <td>${chip(s.status)}</td>
              <td>${s.quota}</td>
              <td>${s.quota_used}</td>
              <td>${quotaBadge(s)}</td>
              <td>${s.gate_count || 0}</td>
              <td>${s.batch_count}</td>
              <td>${s.gap_total}</td>
              <td>${escapeHtml(s.region || '-')} / ${escapeHtml(s.principal || '-')}</td>
              <td class="row">
                <button data-st="启用" data-id="${s.id}">启用</button>
                <button data-st="观察" data-id="${s.id}" class="warn">观察</button>
                <button data-st="停用" data-id="${s.id}" class="danger">停用</button>
                <button data-q="${s.id}">配额+10</button>
                <button data-detail="${s.id}" class="ghost">详情</button>
              </td>
            </tr>`).join('')}
        </tbody>
      </table>
      <div id="siteDetail" class="detail-box" style="margin-top:12px">选择一行查看批次明细与配额闸落点。</div>
    </div>
    <div class="card">
      <h2>方案版本</h2>
      <p class="muted">生效方案决定抓取时的必填字段与访视编码清单。同时只能有一个生效方案。</p>
      <div class="row">
        <input id="ptitle" placeholder="方案标题" />
        <input id="pver" placeholder="版本" value="1.1" style="width:90px" />
        <input id="pfields" placeholder="必填字段逗号分隔" style="min-width:260px"
          value="subject_key,visit_code,visit_date,investigator,weight_kg" />
        <input id="pvisits" placeholder="访视编码" value="V1,V2,V3,EOT" />
        <button id="pcreate">登记方案</button>
      </div>
      <table>
        <thead><tr><th>编码</th><th>标题</th><th>版本</th><th>状态</th><th>必填字段</th><th>操作</th></tr></thead>
        <tbody>
          ${(protocols.items || []).map((p) => `
            <tr>
              <td>${escapeHtml(p.protocol_code)}</td>
              <td>${escapeHtml(p.title)}</td>
              <td>${escapeHtml(p.version)}</td>
              <td>${chip(p.status)}</td>
              <td class="muted">${escapeHtml((p.required_fields_list || []).join(', '))}</td>
              <td class="row">
                <button data-pstatus="生效" data-pid="${p.id}">生效</button>
                <button data-pstatus="草稿" data-pid="${p.id}" class="ghost">草稿</button>
                <button data-pstatus="废止" data-pid="${p.id}" class="danger">废止</button>
              </td>
            </tr>`).join('')}
        </tbody>
      </table>
    </div>`;

  const errBox = (msg) => {
    const box = el.querySelector('#siteDetail');
    box.textContent = '错误：' + msg;
  };

  el.querySelector('#create').onclick = async () => {
    try {
      await api('/api/sites', {
        method: 'POST',
        body: JSON.stringify({
          name: el.querySelector('#sname').value,
          region: el.querySelector('#sregion').value,
          principal: el.querySelector('#sprincipal').value,
          quota: Number(el.querySelector('#squota').value || 50),
          note: el.querySelector('#snote').value,
        }),
      });
      await renderSites(el);
    } catch (e) { errBox(e.message); }
  };

  el.querySelector('#refresh').onclick = () => renderSites(el);
  el.querySelector('#filterStatus').onchange = async (ev) => {
    const status = ev.target.value;
    const url = status ? `/api/sites?status=${encodeURIComponent(status)}` : '/api/sites';
    const filtered = await api(url);
    const tbody = el.querySelector('tbody');
    tbody.innerHTML = (filtered.items || []).map((s) => `
      <tr data-id="${s.id}">
        <td>${escapeHtml(s.site_code)}</td>
        <td>${escapeHtml(s.name)}</td>
        <td>${chip(s.status)}</td>
        <td>${s.quota}</td>
        <td>${s.quota_used}</td>
        <td>${quotaBadge(s)}</td>
        <td>${s.gate_count || 0}</td>
        <td>${s.batch_count}</td>
        <td>${s.gap_total}</td>
        <td>${escapeHtml(s.region || '-')} / ${escapeHtml(s.principal || '-')}</td>
        <td class="muted">请点刷新回到完整操作区</td>
      </tr>`).join('');
  };

  el.querySelectorAll('[data-st]').forEach((btn) => {
    btn.onclick = async () => {
      try {
        await api(`/api/sites/${btn.dataset.id}/status`, {
          method: 'POST',
          body: JSON.stringify({ status: btn.dataset.st }),
        });
        await renderSites(el);
      } catch (e) { errBox(e.message); }
    };
  });

  el.querySelectorAll('[data-q]').forEach((btn) => {
    btn.onclick = async () => {
      const site = items.find((x) => String(x.id) === btn.dataset.q);
      try {
        await api(`/api/sites/${btn.dataset.q}/quota`, {
          method: 'POST',
          body: JSON.stringify({ quota: (site?.quota || 50) + 10 }),
        });
        await renderSites(el);
      } catch (e) { errBox(e.message); }
    };
  });

  el.querySelectorAll('[data-detail]').forEach((btn) => {
    btn.onclick = async () => {
      try {
        const detail = await api(`/api/sites/${btn.dataset.detail}`);
        const box = el.querySelector('#siteDetail');
        box.innerHTML = `
          <div class="row">
            <strong>${escapeHtml(detail.site_code)} · ${escapeHtml(detail.name)}</strong>
            ${chip(detail.status)}
            <span class="chip ${detail.badge_matches ? 'ok' : 'bad'}">
              徽标 ${detail.quota_badge} / 已用 ${detail.quota_used} / 配额 ${detail.quota}
            </span>
            <span class="chip ${detail.quota_full ? 'bad' : 'ok'}">剩余 ${detail.quota_remaining}</span>
          </div>
          <p class="muted">开放缺口 ${detail.open_gaps}。</p>
          <h3 style="font-size:14px;margin:8px 0 4px">配额闸落点</h3>
          <table>
            <thead><tr><th>凭证</th><th>动作</th><th>占用</th><th>已用→</th><th>配额</th><th>徽标</th><th>状态</th><th>时间</th></tr></thead>
            <tbody>
              ${(detail.gates || []).map((g) => `
                <tr>
                  <td>${escapeHtml(g.gate_key)}</td>
                  <td>${escapeHtml(g.action)}</td>
                  <td>${g.units}</td>
                  <td>${g.used_before} → ${g.used_after}</td>
                  <td>${g.quota_total}</td>
                  <td>${g.badge_used}</td>
                  <td>${chip(g.status)}</td>
                  <td class="muted">${escapeHtml(g.created_at || '')}</td>
                </tr>`).join('') || '<tr><td colspan="8" class="muted">尚无配额闸落点</td></tr>'}
            </tbody>
          </table>
          <h3 style="font-size:14px;margin:8px 0 4px">批次明细</h3>
          <table>
            <thead><tr><th>编码</th><th>状态</th><th>行数</th><th>缺口</th></tr></thead>
            <tbody>
              ${(detail.batches || []).map((b) => `
                <tr>
                  <td>${escapeHtml(b.batch_code)}</td>
                  <td>${chip(b.status)}</td>
                  <td>${b.item_count}</td>
                  <td>${b.gap_count}</td>
                </tr>`).join('') || '<tr><td colspan="4" class="muted">暂无批次</td></tr>'}
            </tbody>
          </table>`;
      } catch (e) { errBox(e.message); }
    };
  });

  el.querySelector('#pcreate').onclick = async () => {
    const fields = el.querySelector('#pfields').value.split(',').map((x) => x.trim()).filter(Boolean);
    const visits = el.querySelector('#pvisits').value.split(',').map((x) => x.trim()).filter(Boolean);
    try {
      await api('/api/protocols', {
        method: 'POST',
        body: JSON.stringify({
          title: el.querySelector('#ptitle').value,
          version: el.querySelector('#pver').value,
          required_fields: fields,
          visit_codes: visits,
        }),
      });
      await renderSites(el);
    } catch (e) { errBox(e.message); }
  };

  el.querySelectorAll('[data-pstatus]').forEach((btn) => {
    btn.onclick = async () => {
      try {
        await api(`/api/protocols/${btn.dataset.pid}/status`, {
          method: 'POST',
          body: JSON.stringify({ status: btn.dataset.pstatus }),
        });
        await renderSites(el);
      } catch (e) { errBox(e.message); }
    };
  });
}
