import { api, escapeHtml } from '../api.js';

export async function renderSettings(el) {
  const data = await api('/api/settings');
  const items = data.items || [];
  const snapshot = data.snapshot || {};

  el.innerHTML = `
    <div class="card">
      <h2>台参数</h2>
      <p class="muted">控制质控自动扫描开关、补采最大次数、配额告警与对账口径标签等。修改会写审计。</p>
      <div class="grid">
        <div class="stat"><div class="n">${snapshot.backfill_max_attempt || '-'}</div><div class="l">补采最大次数</div></div>
        <div class="stat"><div class="n">${snapshot.qc_auto_scan || '-'}</div><div class="l">自动质控</div></div>
        <div class="stat"><div class="n">${snapshot.site_quota_warn || '-'}</div><div class="l">配额告警阈值</div></div>
        <div class="stat"><div class="n">${escapeHtml((snapshot.clinic_timezone || '-').slice(0, 12))}</div><div class="l">时区</div></div>
      </div>
    </div>
    <div class="card">
      <table>
        <thead><tr><th>键</th><th>说明</th><th>当前值</th><th>新值</th><th>操作</th></tr></thead>
        <tbody>
          ${items.map((s) => `
            <tr>
              <td><code>${escapeHtml(s.key)}</code></td>
              <td>${escapeHtml(s.label)}</td>
              <td><strong>${escapeHtml(s.value)}</strong></td>
              <td><input data-key="${escapeHtml(s.key)}" value="${escapeHtml(s.value)}" /></td>
              <td><button data-save="${escapeHtml(s.key)}">保存</button></td>
            </tr>`).join('')}
        </tbody>
      </table>
      <div id="msg" class="muted" style="margin-top:10px"></div>
    </div>
    <div class="card">
      <h2>配额闸口径</h2>
      <p class="muted">本台只展配额闸口径，不另开平行口径。落地形态：甲侧旁路凭证 + 指纹链。</p>
      <ul class="muted">
        <li>写侧：开批 / 抓取 / 追加统一过闸，<code>BEGIN IMMEDIATE</code> 串行化，整批预占不足即整笔拦截。</li>
        <li>落点：每次过闸写 <code>quota_ledger</code> 一条凭证，附 HMAC 指纹并与上一条成链，随后立即复核封存。</li>
        <li>读侧：只认本条凭证，<code>gate_key</code> 唯一拒回刷；指纹或链对不齐即驳回。</li>
        <li>徽标对账：批次计数器汇总（徽标）必须等于实时访视行（已用配额），拧花即拦。</li>
      </ul>
    </div>
    <div class="card">
      <h2>预留切口（后续题面）</h2>
      <ul class="muted">
        <li>访视行跨中心去重策略与冲突裁决面板</li>
        <li>方案换版后存量批次字段兼容扫描</li>
        <li>补采任务分片与并发上限</li>
        <li>对账报告导出与差异工单</li>
      </ul>
      <div class="detail-box">${escapeHtml(JSON.stringify(snapshot, null, 2))}</div>
    </div>`;

  const msg = el.querySelector('#msg');
  el.querySelectorAll('[data-save]').forEach((btn) => {
    btn.onclick = async () => {
      const key = btn.dataset.save;
      const input = el.querySelector(`input[data-key="${key}"]`);
      try {
        const updated = await api('/api/settings/update', {
          method: 'POST',
          body: JSON.stringify({ key, value: input.value }),
        });
        msg.className = 'muted';
        msg.textContent = `已保存 ${updated.key} = ${updated.value}`;
        await renderSettings(el);
      } catch (e) {
        msg.className = 'err';
        msg.textContent = e.message;
      }
    };
  });
}
