'use strict';
// ---------------------------------------------------------------- Tractor maintenance (every ~60,000 km)
// Saved per tractor: km of the next service, the last odometer reading and its day, and km per month.
// Today's km are estimated from the reading plus the monthly km since then; alerts when few km are left.
const SERVICE_WARN_KM=5000;
const thousands=n=>Math.round(n).toLocaleString('de-DE');
function truckService(t){if(t.service_due_km==null||t.odometer_km==null)return {key:'unknown',rank:3,left:Infinity,label:'Maintenance not set'};
  const today=dayOf(state.now),days=t.odometer_on?Math.max(0,(Date.parse(today+'T00:00:00Z')-Date.parse(t.odometer_on+'T00:00:00Z'))/86400000):0;
  const monthly=t.monthly_km||0,now=t.odometer_km+monthly*days/30.44,left=t.service_due_km-now;
  const daysLeft=monthly>0?Math.round(left/(monthly/30.44)):null,when=daysLeft!=null&&left>0?new Date(Date.parse(today+'T12:00:00Z')+daysLeft*86400000):null;
  const base={now,left,monthly,when,estimated:monthly>0&&days>0};
  if(left<=0)return {...base,key:'overdue',rank:0,label:`Service overdue by ${thousands(-left)} km`};
  if(left<=SERVICE_WARN_KM)return {...base,key:'soon',rank:1,label:`Service in ${thousands(left)} km`};
  return {...base,key:'ok',rank:2,label:`${thousands(left)} km to service`};}
const serviceWhen=s=>s.when?` · ≈ ${s.when.toLocaleDateString('en-GB',{day:'numeric',month:'short',year:'numeric'})}`:'';
function serviceBadge(t){const s=truckService(t);return `<span class="badge itv-badge ${s.key==='overdue'?'expired':s.key==='unknown'?'unknown':s.key}">${s.key==='overdue'?'⚠ ':s.key==='soon'?'🔧 ':''}${esc(s.label)}</span>`;}
// The lines on a tractor card: progress since the last service and the estimate of today's km.
function serviceCardHtml(t){const s=truckService(t);if(s.key==='unknown')return `<div class="service-line">${serviceBadge(t)}</div>`;
  const every=t.service_every_km||60000,done=Math.min(1,Math.max(0,1-s.left/every));
  return `<div class="service-line ${s.key}">${serviceBadge(t)}<progress class="service-bar" max="100" value="${Math.round(done*100)}" title="${Math.round(done*100)}% of ${thousands(every)} km"></progress>
  <small>Next at <strong>${thousands(t.service_due_km)} km</strong> · now ≈ ${thousands(s.now)} km${s.estimated?` (read ${thousands(t.odometer_km)} on ${fmtDate(t.odometer_on)} + ${thousands(s.monthly)}/month)`:''}${serviceWhen(s)}</small></div>`;}
const serviceDue=()=>state.trucks.filter(t=>!t.archived_at&&['overdue','soon'].includes(truckService(t).key));
function updateServiceCount(){const el=$('#nav-trucks');if(!el||!state)return;const due=serviceDue(),overdue=due.filter(t=>truckService(t).key==='overdue').length;
  el.hidden=!due.length;el.textContent=due.length;el.className=overdue?'overdue':'soon';el.title=`Maintenance: ${overdue} overdue · ${due.length-overdue} within ${thousands(SERVICE_WARN_KM)} km`;}
// Panel at the top of Tractors: the ones that need the service soon, most urgent first.
function serviceAlertsHtml(){const due=serviceDue().sort((a,b)=>truckService(a).left-truckService(b).left),unset=state.trucks.filter(t=>!t.archived_at&&truckService(t).key==='unknown').length;
  if(!due.length)return unset?`<p class="subtitle service-note">🔧 Maintenance: ${unset} ${unset===1?'tractor has':'tractors have'} no km yet. Open <strong>Edit & assign</strong> and fill in the Maintenance section.</p>`:'';
  return `<section class="panel service-alerts"><div class="panel-head"><div><h2>🔧 Maintenance due <span class="section-count">${due.length}</span></h2><p>Within ${thousands(SERVICE_WARN_KM)} km of the service, or past it. Today's km are estimated from the last reading and the km per month.</p></div></div>
  <div class="itv-list">${due.map(t=>{const s=truckService(t);return `<div class="itv-row ${s.key==='overdue'?'expired':'soon'}"><div class="itv-plate"><strong>${esc(t.plate)}</strong><small>${esc(state.drivers.find(d=>d.id===t.driver_id)?.name||'No driver')}</small></div><div class="itv-date"><strong>Next at ${thousands(t.service_due_km)} km</strong><small>now ≈ ${thousands(s.now)} km${serviceWhen(s)}</small></div>${serviceBadge(t)}<div class="actions"><button class="btn small primary" data-action="service-done" data-id="${t.id}">✓ Service done</button><button class="btn small" data-action="edit-truck" data-id="${t.id}">Update km</button></div></div>`;}).join('')}</div></section>`;}
function serviceFormHtml(item){const v=k=>item[k]??'';
  return `<div class="span-2 section-title">Maintenance · service every ${thousands(item.service_every_km||60000)} km</div>`+
  field('Next service at (km)','service_due_km',v('service_due_km'),'number','step="1" min="0" max="5000000" placeholder="e.g. 540000"','The odometer reading when the oil and filters must be changed.')+
  field('Current km (odometer)','odometer_km',v('odometer_km'),'number','step="1" min="0" max="5000000" placeholder="e.g. 512300"',item.odometer_on?`Last read ${fmtDate(item.odometer_on)}. A new value is dated today.`:'Saved with today’s date.')+
  field('Km per month','monthly_km',v('monthly_km'),'number','step="1" min="0" max="50000" placeholder="e.g. 11000"','Used to estimate today’s km and the date of the service.')+
  field('Service every (km)','service_every_km',item.service_every_km||60000,'number','step="1" min="1000" max="500000"','After ✓ Service done the next one is set this many km later.');}
function serviceDoneModal(id){const t=state.trucks.find(x=>x.id===Number(id));if(!t)return;const s=truckService(t);
  showModal('Service done · '+t.plate,`<form id="service-form" data-id="${t.id}">${errorBox}<div class="form-grid">${field('Km at the service','km',s.now!=null&&isFinite(s.now)?Math.round(s.now):'','number','required step="1" min="0" max="5000000"','Check the odometer: this becomes the new reading.')}${field('Day of the service','day',dayOf(state.now),'date','required')}</div>
  <p class="subtitle">The next service will be at this km + ${thousands(t.service_every_km||60000)} km.</p><div class="form-actions"><button type="button" class="btn" data-action="close">Cancel</button><button class="btn primary" type="submit">Save service</button></div></form>`,'TRACTORS / MAINTENANCE');}
async function saveServiceDone(form){const data=formObject(form);const result=await api('/api/truck-service',{id:Number(form.dataset.id),km:data.km,day:data.day});$('#modal').close();await refresh();toast(result.message);}
