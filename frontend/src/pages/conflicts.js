import {store} from '../stores/workspace.js';
import {field,input,select,table,badge} from '../components/controls.js';
import {esc,date} from '../utils/format.js';
import {bindForm,request} from '../utils/forms.js';

const sideLabel={client:'来访方',opponent:'相对方'};
const refStatus={pending:'待受理',accepted:'已受理',withdrawn:'已撤回',completed:'已销案'};

function partyContrast(parties){
 return `<div class="party-contrast">${['client','opponent'].map(side=>{
  const cp=parties.find(p=>p.side===side),p=store.data.parties.find(x=>x.id===cp?.party_id);
  return `<section><small>${side==='client'?'来访方 / CLIENT':'相对方 / OPPOSING PARTY'}</small><h3>${esc(p?.name||'待补齐')}</h3><p>归一键：${esc(p?.normalized||'—')}</p></section>`;
 }).join('<span class="versus">↔</span>')}</div>`;
}

function rosterBlock(){
 const d=store.data;
 return `<h3>机构当前承接的对立方</h3><p class="hint">只统计待受理/已受理在途承接；撤回或销案后立即移除。发信前先在此核对是否已承接本卷的对立方。</p><div class="table-scroll"><table><thead><tr><th>机构</th><th>关联卷宗</th><th>承接当事人（含归一键）</th><th>角色</th><th>转介状态</th></tr></thead><tbody>${
  d.agency_roster.map(a=>a.items.length?a.items.map((it,i)=>`<tr>${i===0?`<td rowspan="${a.items.length}">${esc(a.agency_name)}</td>`:''}<td>${esc(it.intake_code)}</td><td>${esc(it.party_name)}<br><small>归一：${esc(it.normalized)}</small></td><td>${sideLabel[it.side]||it.side}</td><td>${esc(refStatus[it.status]||it.status)}</td></tr>`).join(''):`<tr><td>${esc(a.agency_name)}</td><td colspan="4" class="empty">当前无在途承接</td></tr>`).join('')
 }</tbody></table></div>`;
}

function checkRows(){
 const d=store.data;
 return d.conflict_checks.filter(v=>v.intake_id===store.selected).reverse().map(v=>{
  const agency=d.agencies.find(a=>a.id===v.agency_id);
  const hits=v.basis?.hits||[];
  const hitText=hits.length?hits.map(h=>`${esc(h.intake_code)}（${esc(h.party_name)} → 归一 ${esc(h.normalized)}）`).join('<br>'):'无命中';
  const basisText=v.blocked?hitText:`双方归一键：<br>${esc(v.basis?.client?.normalized||'—')}<br>${esc(v.basis?.opponent?.normalized||'—')}`;
  if(v.fresh){
   return [esc(agency?.name),badge(v.blocked?'不可选':'可选',v.blocked?'warning':'good'),esc(v.reason),basisText,'—',date(v.at)];
  }
  const live=v.current_blocked===true;
  return [esc(agency?.name),badge('已失效·旧核验','warning'),esc(v.reason),basisText,
    badge(live?'当前不可选':'当前可选',live?'warning':'good')+'<br><small>'+esc(v.current_reason||'')+'</small>',date(v.at)];
 });
}

export function render(){
 const d=store.data,parties=d.case_parties.filter(p=>p.intake_id===store.selected);
 return `<h2>双方向利益冲突核验</h2>
 <p class="intro">名称按 NFKC 全半角、空格和大小写归一。机构已经承接对立当事人的卷宗时必须改选；补录当事人或撤回旧转介后按最新事实重新核验，迟到的旧核验存根会标记失效，不能据此放行。</p>
 ${partyContrast(parties)}
 ${rosterBlock()}
 <div class="two-col">
 <form id="party" class="paper-form"><h3>补充或更正当事人</h3>${field('当事人角色',select('side','<option value="client">来访方</option><option value="opponent">相对方</option>'))}${field('模拟当事人名称',input('name','text','模拟许某'))}<button>保存双方关联（推进核验版本）</button></form>
 <form id="check" class="paper-form"><h3>对照候选机构</h3>${field('承接机构',select('agency_id',d.agencies.map(a=>`<option value="${a.id}">${esc(a.name)}</option>`).join('')))}<p>核验存根记录命中卷宗与归一依据；发出转介和签收时仍会按机构当时的在途承接重新检查。</p><button>核验并留存对照</button></form>
 </div>
 <h3>冲突核验存根（含命中卷宗与最终核验依据）</h3>
 ${table(['机构','核验结果','说明','命中卷宗 / 归一依据','按最新事实复算','时间'],checkRows())}`;
}
export function bind(r){
 bindForm('party',d=>request('/parties',{...d,intake_id:store.selected}),r);
 bindForm('check',d=>request('/conflict-checks',{intake_id:store.selected,agency_id:+d.agency_id}),r);
}
