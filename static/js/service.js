'use strict';
// ---------------------------------------------------------------- Tractor maintenance (oil change every 60,000 km)
// Per tractor: km of the last change and the km read at the end of the month. Next change = last + 60,000;
// km left = next − km now. The reading before the last one gives the km per month, so the day can be estimated.
// The workshop account (role "service") sees this page and ITV only, in Romanian: it enters the km here, nothing else.
const SERVICE_WARN_KM=5000;let kmEditing=false;
const thousands=n=>Math.round(n).toLocaleString('de-DE');
const isService=()=>state?.role==='service';
// Text in English for the dispatchers, in Romanian for the workshop account.
const L=(en,ro)=>isService()?ro:en;
const dayDiff=(a,b)=>(Date.parse(b+'T00:00:00Z')-Date.parse(a+'T00:00:00Z'))/86400000;
function truckService(t){const every=t.service_every_km||60000;
  if(t.last_service_km==null||t.odometer_km==null)return {key:'unknown',rank:3,left:Infinity,every,next:t.last_service_km!=null?t.last_service_km+every:null,label:t.last_service_km==null?L('Last change not set','Fără ultimul schimb'):L('Km now not set','Fără km actuali')};
  const next=t.last_service_km+every,left=next-t.odometer_km;
  const span=t.prev_odometer_on&&t.odometer_on?dayDiff(t.prev_odometer_on,t.odometer_on):0,perDay=span>=7&&t.odometer_km>t.prev_odometer_km?(t.odometer_km-t.prev_odometer_km)/span:null;
  const when=perDay&&left>0?new Date(Date.parse(t.odometer_on+'T12:00:00Z')+left/perDay*86400000):null;
  const base={every,next,left,monthly:perDay?perDay*30.44:null,when,old:t.odometer_on&&dayDiff(t.odometer_on,dayOf(state.now))>40};
  if(left<=0)return {...base,key:'overdue',rank:0,label:L(`Overdue by ${thousands(-left)} km`,`Depășit cu ${thousands(-left)} km`)};
  if(left<=SERVICE_WARN_KM)return {...base,key:'soon',rank:1,label:L(`${thousands(left)} km left`,`Mai sunt ${thousands(left)} km`)};
  return {...base,key:'ok',rank:2,label:L(`${thousands(left)} km left`,`Mai sunt ${thousands(left)} km`)};}
const monthYear=d=>d.toLocaleDateString(L('en-GB','ro-RO'),{month:'long',year:'numeric'});
function serviceBadge(t){const s=truckService(t);return `<span class="badge itv-badge ${s.key==='overdue'?'expired':s.key}">${s.key==='overdue'?'⚠ ':s.key==='soon'?'🔧 ':''}${esc(s.label)}</span>`;}
const serviceTrucks=()=>(state.trucks||[]).filter(t=>!t.archived_at&&t.active!==0);
const serviceDue=()=>serviceTrucks().filter(t=>['overdue','soon'].includes(truckService(t).key));
function updateServiceCount(){const el=$('#nav-service');if(!el||!state)return;const due=serviceDue(),overdue=due.filter(t=>truckService(t).key==='overdue').length;
  el.hidden=!due.length;el.textContent=due.length;el.className=overdue?'overdue':'soon';el.title=L(`Oil change: ${overdue} overdue · ${due.length-overdue} within ${thousands(SERVICE_WARN_KM)} km`,`Schimb ulei: ${overdue} depășite · ${due.length-overdue} sub ${thousands(SERVICE_WARN_KM)} km`);}
// One line on a tractor card (Tractors page, dispatchers only).
function serviceCardHtml(t){const s=truckService(t);return `<div class="service-line ${s.key}">${serviceBadge(t)}${s.key!=='unknown'?`<small>Next change at <strong>${thousands(s.next)} km</strong> · km ${thousands(t.odometer_km)} on ${fmtDate(t.odometer_on)}</small>`:''}</div>`;}
const truckDriverName=t=>t.driver_name??state.drivers.find(d=>d.id===t.driver_id)?.name??'';
function maintenanceView(){const list=[...serviceTrucks()].sort((a,b)=>{const x=truckService(a),y=truckService(b);return x.rank-y.rank||x.left-y.left||a.plate.localeCompare(b.plate);});
  const count=key=>list.filter(t=>truckService(t).key===key).length;
  const chips=`<div class="service-chips"><span class="service-chip overdue"><strong>${count('overdue')}</strong> ${L('overdue','depășite')}</span><span class="service-chip soon"><strong>${count('soon')}</strong> ${L('within','sub')} ${thousands(SERVICE_WARN_KM)} km</span><span class="service-chip ok"><strong>${count('ok')}</strong> ${L('ok','în regulă')}</span>${count('unknown')?`<span class="service-chip unknown"><strong>${count('unknown')}</strong> ${L('without km','fără km')}</span>`:''}</div>`;
  const actions=kmEditing?'':`<button class="btn small" data-action="print-page">🖨 ${L('Print','Printează')}</button><button class="btn primary" data-action="km-edit">✎ ${L('Enter month-end km','Introdu km de la sfârșitul lunii')}</button>`;
  const head=`<div class="heading"><div><h1>${L('Maintenance','Mentenanță')}</h1><p class="subtitle">${L(`Oil change every ${thousands(60000)} km. Next change = last change + ${thousands(60000)} km · km left = next change − km now.`,`Schimb de ulei la fiecare ${thousands(60000)} km. Următorul schimb = ultimul schimb + ${thousands(60000)} km · km rămași = următorul schimb − km actuali.`)}</p></div><div class="actions">${actions}</div></div>`;
  if(kmEditing)return head+kmEditHtml(list);
  const rows=list.map(t=>{const s=truckService(t),done=s.key==='unknown'?0:Math.min(100,Math.max(0,Math.round((1-s.left/s.every)*100)));
    return `<tr class="${s.key}"><td><strong class="service-plate">${esc(t.plate)}</strong><small>${esc(truckDriverName(t)||'—')}</small></td>
    <td class="num">${t.last_service_km!=null?thousands(t.last_service_km):'—'}</td><td class="num"><strong>${s.next!=null?thousands(s.next):'—'}</strong></td>
    <td class="num">${t.odometer_km!=null?thousands(t.odometer_km):'—'}${t.odometer_on?`<small class="${s.old?'stale':''}">${fmtDate(t.odometer_on)}</small>`:''}</td>
    <td class="service-left"><strong>${s.key==='unknown'?'—':s.left<=0?'−'+thousands(-s.left):thousands(s.left)}</strong>${s.key!=='unknown'?`<progress class="service-bar" max="100" value="${done}"></progress>`:''}</td>
    <td>${serviceBadge(t)}${s.when?`<small>≈ ${monthYear(s.when)}${s.monthly?` · ${thousands(s.monthly)} ${L('km/month','km/lună')}`:''}</small>`:s.key==='overdue'?`<small>${L('Change as soon as possible','De schimbat cât mai repede')}</small>`:''}</td>
    <td class="clients-actions"><button class="btn small" data-action="service-done" data-id="${t.id}">✓ ${L('Change done','Schimb făcut')}</button>${isService()?'':`<button class="btn small" data-action="edit-truck" data-id="${t.id}">Edit</button>`}</td></tr>`;}).join('');
  return head+chips+`<section class="panel"><div class="schedule"><table class="clients-table service-table"><thead><tr><th>${L('Tractor','Tractor')}</th><th class="num">${L('Last change (km)','Ultimul schimb (km)')}</th><th class="num">${L('Next change (km)','Următorul schimb (km)')}</th><th class="num">${L('Km now','Km actuali')}</th><th>${L('Km left','Km rămași')}</th><th>${L('Status','Stare')}</th><th></th></tr></thead><tbody>${rows||`<tr><td colspan="7">${empty(L('No tractors','Niciun tractor'),'','','')}</td></tr>`}</tbody></table></div></section>
  <p class="subtitle key-note">${L('Km now: read at the end of the month (date under the km, in orange when older than 40 days). ≈ month: from the km done between the last two readings.','Km actuali: citiți la sfârșitul lunii (data e sub km, cu portocaliu dacă are peste 40 de zile). ≈ luna: din km făcuți între ultimele două citiri.')}</p>`;}
// The month-end list: last change and km now for every tractor, saved together with the day of the reading.
function kmEditHtml(list){return `<form id="km-form"><section class="panel padded">${errorBox}<div class="km-day">${field(L('Day of the reading','Data citirii'),'day',dayOf(state.now),'date','required',L('Usually the last day of the month.','De obicei ultima zi a lunii.'))}</div>
  <div class="schedule"><table class="clients-table service-table km-table"><thead><tr><th>${L('Tractor','Tractor')}</th><th>${L('Last change at (km)','Ultimul schimb la (km)')}</th><th>${L('Km now','Km actuali')}</th><th>${L('Next change','Următorul schimb')}</th></tr></thead><tbody>
  ${list.map(t=>`<tr data-km-row="${t.id}"><td><strong class="service-plate">${esc(t.plate)}</strong><small>${esc(truckDriverName(t)||'—')}</small></td>
    <td><input type="number" inputmode="numeric" step="1" min="0" max="5000000" name="last_${t.id}" value="${t.last_service_km??''}" aria-label="${L('Last change of','Ultimul schimb la')} ${esc(t.plate)}"></td>
    <td><input type="number" inputmode="numeric" step="1" min="0" max="5000000" name="odo_${t.id}" value="${t.odometer_km??''}" placeholder="${t.odometer_km!=null?'':'km'}" aria-label="${L('Km now of','Km actuali la')} ${esc(t.plate)}"></td>
    <td class="km-next" data-km-next="${t.id}">${t.last_service_km!=null?thousands(t.last_service_km+(t.service_every_km||60000)):'—'}</td></tr>`).join('')}</tbody></table></div>
  <div class="form-actions"><button type="button" class="btn" data-action="km-cancel">${L('Cancel','Renunță')}</button><button class="btn primary" type="submit">${L('Save km','Salvează km')}</button></div></section></form>`;}
function kmRowInput(input){const row=input.closest('[data-km-row]');if(!row)return;const t=state.trucks.find(x=>x.id===Number(row.dataset.kmRow)),last=Number(row.querySelector('[name^=last_]').value),odo=Number(row.querySelector('[name^=odo_]').value);
  const next=last?last+(t?.service_every_km||60000):null;row.querySelector('[data-km-next]').innerHTML=next?`${thousands(next)}${odo?` <small>${odo>=next?L('overdue by ','depășit cu ')+thousands(odo-next):L(thousands(next-odo)+' left','mai sunt '+thousands(next-odo))}</small>`:''}`:'—';}
async function saveKmList(form){const data=formObject(form);const items=serviceTrucks().map(t=>({id:t.id,last_service_km:data['last_'+t.id]??'',odometer_km:data['odo_'+t.id]??''}));
  const result=await api('/api/truck-km',{day:data.day,items,lang:L('en','ro')});kmEditing=false;await refresh();toast(result.message);}
function serviceFormHtml(item){const v=k=>item[k]??'';
  return `<div class="span-2 section-title">Maintenance · oil change</div>`+
  field('Last change at (km)','last_service_km',v('last_service_km'),'number','step="1" min="0" max="5000000" placeholder="e.g. 480000"','The km when the oil was changed last time.')+
  field('Km now','odometer_km',v('odometer_km'),'number','step="1" min="0" max="5000000" placeholder="e.g. 512300"',item.odometer_on?`Read ${fmtDate(item.odometer_on)}. A new value is dated today.`:'Saved with today’s date.')+
  field('Change every (km)','service_every_km',item.service_every_km||60000,'number','step="1" min="1000" max="500000"','Next change = last change + this.');}
function serviceDoneModal(id){const t=state.trucks.find(x=>x.id===Number(id));if(!t)return;
  showModal(L('Oil change done · ','Schimb de ulei făcut · ')+t.plate,`<form id="service-form" data-id="${t.id}">${errorBox}<div class="form-grid">${field(L('Km at the change','Km la schimb'),'km',t.odometer_km??'','number','required step="1" min="0" max="5000000"',L('It becomes the last change.','Devine ultimul schimb.'))}${field(L('Day of the change','Data schimbului'),'day',dayOf(state.now),'date','required')}</div>
  <p class="subtitle">${L(`The next change will be at this km + ${thousands(t.service_every_km||60000)} km.`,`Următorul schimb va fi la acești km + ${thousands(t.service_every_km||60000)} km.`)}</p><div class="form-actions"><button type="button" class="btn" data-action="close">${L('Cancel','Renunță')}</button><button class="btn primary" type="submit">${L('Save','Salvează')}</button></div></form>`,L('MAINTENANCE','MENTENANȚĂ'));}
async function saveServiceDone(form){const data=formObject(form);const result=await api('/api/truck-service',{id:Number(form.dataset.id),km:data.km,day:data.day,lang:L('en','ro')});$('#modal').close();await refresh();toast(result.message);}
// The workshop account: only ITV and Maintenance in the menu, in Romanian.
const SERVICE_VIEWS=['maintenance','itv'];
function applyRole(){const service=isService();document.body.classList.toggle('role-service',service);
  for(const el of document.querySelectorAll('.nav[data-view]'))el.hidden=service&&!SERVICE_VIEWS.includes(el.dataset.view);
  for(const el of document.querySelectorAll('.sidebar .nav-label'))el.hidden=service;
  if(!service)return;
  if(!SERVICE_VIEWS.includes(view))view='maintenance';
  document.documentElement.lang='ro';names.maintenance='Mentenanță';names.itv='ITV';
  const label=(v,text)=>{const el=$(`.nav[data-view="${v}"]`);const node=el&&[...el.childNodes].find(n=>n.nodeType===3&&n.textContent.trim());if(node)node.textContent=' '+text+' ';};
  label('maintenance','Mentenanță');label('itv','ITV');
  const logout=$('#logout');if(logout)logout.innerHTML='<span>⎋</span> Ieșire'+(state.user?' · '+esc(state.user):'');
  const crumb=$('.breadcrumb');if(crumb)crumb.textContent='Service';
  $('#today-label').textContent=new Date(state.now).toLocaleDateString('ro-RO',{weekday:'long',day:'numeric',month:'long',timeZone:'Europe/Madrid'});
  const foot=$('footer');if(foot)foot.textContent='MalcomTrans · Mentenanță și ITV';}
