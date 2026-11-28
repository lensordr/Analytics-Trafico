'use strict';
// Daily planning: stops of the selected day.
// The day as a notebook: every tractor is a row, its trips of the day are cards of one size, one after the other
// (loading → unloading, no hours: the hours are in the details). A trip shows on its loading day and on its unloading day.
// Click a card for its actions on the right, drag it left/right to change the order, click an empty place on a row to plan
// a new load. The full lists stay below, folded.
const PT_FROM=5*60,PT_TO=21*60,PT_LANE=34,PT_SNAP=15;let plannerChip='all',plannerSel=null,ptDrag=null,ptJustDragged=false;
const madridClock=new Intl.DateTimeFormat('en-CA',{timeZone:'Europe/Madrid',year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hourCycle:'h23'});
function madridAt(value){const p=Object.fromEntries(madridClock.formatToParts(new Date(value)).map(x=>[x.type,x.value]));return {day:`${p.year}-${p.month}-${p.day}`,min:Number(p.hour)*60+Number(p.minute)};}
// Minutes on the selected day, clipped to the visible window.
function ptMinute(value){const t=madridAt(value);return t.day<selectedDay?PT_FROM:t.day>selectedDay?PT_TO:Math.min(PT_TO,Math.max(PT_FROM,t.min));}
function ptState(o,p){const stops=partStops(o,p);if(stops.every(s=>s.status==='Done'))return 'done';
  if(deliveryAlert(o)?.kind==='overdue'||stops.some(s=>s.status!=='Done'&&stopIsLate(s)))return 'late';return stops.some(s=>s.status==='Done')?'go':'plan';}
const PT_ORDER={late:0,go:1,plan:2,done:3};
function plannerRows(){const rows=new Map();
  // Every active tractor has its row, also with nothing planned yet (like a page of the notebook).
  for(const t of state.trucks)if(t.active&&!t.archived_at)rows.set(t.id,{truck:t,trips:[]});
  for(const o of filteredOrders())for(const p of orderParts(o)){if(!p.truck_id)continue;const start=madridAt(p.start).day,end=madridAt(p.end).day;if(start!==selectedDay&&end!==selectedDay)continue;
    const row=rows.get(p.truck_id)||{truck:state.trucks.find(t=>t.id===p.truck_id),trips:[]};const stops=partStops(o,p);
    row.trips.push({o,p,state:ptState(o,p),from:getLocation(stops[0]?.location_id),to:getLocation(stops[stops.length-1]?.location_id),at:madridAt(p.start).min,startsBefore:start<selectedDay,endsAfter:end>selectedDay});rows.set(p.truck_id,row);}
  return [...rows.values()].filter(r=>r.truck&&r.truck.plate.toLowerCase().includes(filters.plate.toLowerCase())).map(r=>{r.trips.sort((x,y)=>Date.parse(x.p.start)-Date.parse(y.p.start));const last=r.trips[r.trips.length-1];
    r.idle=!r.trips.length;
    r.state=r.idle?'idle':r.trips.some(t=>t.state==='late')?'late':r.trips.some(t=>t.state==='go')?'go':r.trips.every(t=>t.state==='done')?'done':'plan';
    // Free after the last trip of the day, when it is unloaded the same day and not late.
    r.free=last&&!last.endsAfter&&last.state!=='late'?last:null;return r;})
    .sort((a,b)=>(a.truck.plan_order??1e9)-(b.truck.plan_order??1e9)||a.truck.plate.localeCompare(b.truck.plate));}
const ptHHMM=m=>`${String(Math.floor(m/60)).padStart(2,'0')}:${String(m%60).padStart(2,'0')}`;
function plannerView(){const rows=plannerRows(),isFree=r=>r.free||r.idle,count=k=>rows.filter(r=>k==='free'?isFree(r):r.state===k).length,shown=rows.filter(r=>plannerChip==='all'||(plannerChip==='free'?isFree(r):r.state===plannerChip));
  const all=rows.flatMap(r=>r.trips);if(!all.some(t=>t.o.id===plannerSel))plannerSel=(all.find(t=>t.state==='late')||all.find(t=>t.state==='go')||all[0])?.o.id??null;
  const alerts=deliveryAlerts(),overdue=alerts.filter(x=>x.alert.kind==='overdue').length,today=selectedDay===dayOf(state.now);
  const chip=(k,label,n)=>`<button class="pt-chip ${k} ${plannerChip===k?'on':''}" data-action="plan-chip" data-id="${k}">${label} <b>${n}</b></button>`;
  const orders=filteredOrders(),loading=dayStops(orders,'Loading'),unloading=dayStops(orders,'Unloading'),swaps=dayStops(orders,'Trailer swap');
  const tripButton=t=>`<button class="pt-trip ${t.state} ${t.o.id===plannerSel?'sel':''} ${t.startsBefore?'cut-l':''} ${t.endsAfter?'cut-r':''}" data-action="plan-select" data-id="${t.o.id}" data-at="${t.at}" ${t.startsBefore?'data-earlier="1"':''} ${ptMovable(t.o)&&!t.startsBefore?'data-drag="1"':''} title="${esc(`${t.o.number} · ${t.from?.city||'?'} → ${t.to?.city||'?'}${t.startsBefore?' · loaded on an earlier day':''}${t.endsAfter?' · unloads on a later day':''} · double-click for details`)}">${t.startsBefore?'↓ ':''}${esc(t.from?.city||'?')} → ${esc(t.to?.city||'?')}${t.endsAfter?' ›':''}${['Planned','In transit'].includes(t.o.status)?`<span class="pt-x" data-action="plan-cancel" data-id="${t.o.id}" role="button" title="Cancel ${esc(t.o.number)}">×</span>`:''}</button>`;
  const row=r=>`<div class="pt-row ${r.state}" data-truck-row="${r.truck.id}"><div class="pt-who" data-row-grip title="Drag up or down to change the order"><strong>${esc(r.truck.plate)}</strong><small>${esc(state.drivers.find(d=>d.id===r.truck.driver_id)?.name||r.trips[0]?.o.driver_name||'')}</small></div><div class="pt-track" data-action="plan-slot" data-truck="${r.truck.id}" title="Click to plan a load for ${esc(r.truck.plate)}">${r.trips.map(tripButton).join('')}${r.free?`<button class="pt-free" data-action="reload-order" data-id="${r.free.o.id}" title="Plan the next load for ${esc(r.truck.plate)}">＋ next · ${esc(r.free.to?.city||'')}</button>`:''}</div></div>`;
  return `<div class="pt-bar"><button class="btn small" data-action="prev-day" aria-label="Previous day">‹</button><input type="date" id="planning-date" value="${selectedDay}" aria-label="Planning day"><button class="btn small" data-action="next-day" aria-label="Next day">›</button><button class="btn small ${today?'':'primary'}" data-action="today">Today</button>
  <h1 class="pt-title">${formatDay(selectedDay,true)}${today?' <small>today</small>':''}</h1><span class="pt-spacer"></span>${alerts.length?`<button class="pt-alert ${overdue?'overdue':''}" data-action="plan-alerts">${overdue?'⚠':'⏱'} ${alerts.length} to follow up</button>`:''}${todoButton()}<button class="btn primary" data-action="new-order">＋ New order</button></div>
  <div id="pt-todo" hidden>${todoPanel()}${itvPanel()}</div><div id="pt-alerts" hidden>${alertsPanel()}</div>
  <div class="pt-chips">${chip('all','All',rows.length)}${chip('late','Late',count('late'))}${chip('go','On the road',count('go'))}${chip('plan','Planned',count('plan'))}${chip('done','Done',count('done'))}${chip('free','Free',count('free'))}
  <span class="pt-spacer"></span><input id="filter-plate" class="pt-search" data-filter="plate" data-suggest="plates" autocomplete="off" placeholder="Find a tractor…" value="${esc(filters.plate)}" aria-label="Find a tractor"></div>
  <div class="pt-layout"><section class="panel pt-board"><div class="pt-scroll"><div class="pt-grid">
  ${shown.map(row).join('')||`<div class="pt-empty">${empty(rows.length?'No tractor in this filter':'No trips on this day',rows.length?'Choose another filter above.':'Add a loading order, or pick another day.','new-order','New order')}</div>`}
  </div></div>
  <div class="pt-legend"><span class="done">done</span><span class="go">on the road</span><span class="plan">planned</span><span class="late">late</span><span class="free">free · click to plan</span><span class="tip">Click an empty place on a row for a new load · drag a card left or right to change the order · double-click for details · drag a tractor up or down</span></div></section>
  <aside class="panel pt-side">${plannerSide()}</aside></div>
  <details class="panel pt-lists"><summary>Loading and unloading lists · ${loading.length} loadings · ${unloading.length} unloadings${swaps.length?` · ${swaps.length} swaps`:''} · more filters</summary>
  <div class="filters">${[['loading','Loading place'],['unloading','Unloading place']].map(([k,l])=>`<div class="filter wide"><label for="filter-${k}">${l}</label><input id="filter-${k}" data-filter="${k}" data-suggest="places" autocomplete="off" placeholder="City, company or postcode" value="${esc(filters[k])}"></div>`).join('')}<div class="filter"><label for="filter-operation">Operation</label><select id="filter-operation" data-filter="operation"><option value="">All operations</option>${['International','National','Local'].map(v=>`<option ${filters.operation===v?'selected':''}>${v}</option>`).join('')}</select></div><div class="filter"><label for="filter-status">Order status</label><select id="filter-status" data-filter="status"><option value="">All active</option>${['Planned','In transit','Completed','Cancelled'].map(v=>`<option ${filters.status===v?'selected':''}>${v}</option>`).join('')}</select></div><button class="btn small" data-action="clear-filters">Reset</button></div>
  ${dailyStopSection(loading,'Loading')}${dailyStopSection(swaps,'Trailer swap')}${dailyStopSection(unloading,'Unloading')}</details>`;}
// The selected trip: its stops and what can be done now.
function plannerSide(){const o=state.orders.find(x=>x.id===plannerSel);if(!o)return `<p class="subtitle">Click a trip on the timeline to see its stops and confirm them.</p>`;
  const next=nextStop(o),driver=state.drivers.find(d=>d.id===o.driver_id),client=o.client_id?getLocation(o.client_id):null;
  return `<div class="pt-side-head"><button class="link-button" data-action="order-detail" data-id="${o.id}">${esc(o.number)}</button>${orderStatusBadge(o)}</div>
  <h3>${esc(o.plate)} · ${esc(o.driver_name)}</h3><p class="subtitle">Trailer ${esc(trailerChain(o))}${client?` · ${esc(client.company)}`:''}</p>
  <ol class="pt-stops">${orderStops(o).map(s=>{const place=getLocation(s.location_id);return `<li class="${s.status==='Done'?'done':s.seq===next?.seq?'next':''} ${s.status!=='Done'&&stopIsLate(s)&&o.status!=='Cancelled'?'late':''}"><span>${stopIcon[s.kind]}</span><div><strong>${esc(place?.city||'?')}</strong><small>${esc(place?.company||'')}</small><small>${s.status==='Done'?`✓ ${formatDate(s.done_at||s.planned_at)}`:`${s.seq>1?'≈ ':''}${formatDate(s.planned_at)}`}</small></div></li>`;}).join('')}</ol>
  <div class="pt-actions">${next&&o.status!=='Cancelled'?`<button class="btn primary" data-action="confirm-stop" data-id="${o.id}" data-seq="${next.seq}">${confirmLabel(o,next)}</button>`:''}
  <button class="btn" data-action="order-detail" data-id="${o.id}">Change ETA · details</button>
  ${o.status!=='Cancelled'?`<button class="btn" data-action="open-whatsapp" data-id="${o.id}">💬 WhatsApp driver</button>`:''}${driver?.phone?`<a class="btn" href="${esc(telHref(driver.phone))}">☎ Call ${esc(driver.name)}</a>`:''}
  ${o.status!=='Cancelled'?`<button class="btn" data-action="reload-order" data-id="${o.id}">＋ Next load after this</button>`:''}
  ${ptMovable(o)?`<div class="pt-move"><button class="btn small" data-action="move-order" data-id="${o.id}" data-minutes="-1440">◀ 1 day earlier</button><button class="btn small" data-action="move-order" data-id="${o.id}" data-minutes="1440">1 day later ▶</button></div>`:''}</div>`;}
// Every trip but a cancelled one can be moved; a completed one moves with the driver's confirmed times.
const ptMovable=o=>o.status!=='Cancelled';
// The cards flow one after the other (no hours), so there is nothing to place.
function placeTimeline(){}
// The load a new trip at `at` follows on this tractor: its unloading is where the truck is.
function ptPrevious(truckId,at){return state.orders.filter(o=>o.truck_id===truckId&&o.status!=='Cancelled'&&Date.parse(o.pickup_at)<=at).sort((a,b)=>Date.parse(b.pickup_at)-Date.parse(a.pickup_at))[0];}
// A new load on a row goes after the tractor's last loading of the day (or at 08:00).
function planSlot(track){const t=state.trucks.find(v=>v.id===Number(track.dataset.truck));if(!t)return;
  const last=Math.max(-1,...[...track.querySelectorAll('.pt-trip[data-at]:not([data-earlier])')].map(el=>Number(el.dataset.at)));
  const at=selectedDay+'T'+ptHHMM(last<0?8*60:Math.min(20*60,last+60)),prev=ptPrevious(t.id,dispatchInputInstant(at));
  openOrder({truck_id:t.id,driver_id:prev?.driver_id||t.driver_id,trailer_id:prev?.trailer_id||t.trailer_id,trailer:prev?.trailer,loading_id:prev?.unloading_id||t.location_id||'',pickup_at:at,timing:'manual',notes:prev?'Next load after '+prev.number:''});}
// Drag a card left/right among the tractor's cards of the day: on release it takes that place in the order
// (its loading time goes between its new neighbours'; the hours themselves stay in the details).
document.addEventListener('pointerdown',event=>{const el=event.target.closest?.('.pt-trip[data-drag]');if(!el||event.button!==0||event.target.closest('.pt-x'))return;
  ptDrag={el,x:event.clientX,id:Number(el.dataset.id),at:Number(el.dataset.at)};el.setPointerCapture(event.pointerId);});
document.addEventListener('pointermove',event=>{if(!ptDrag)return;const dx=event.clientX-ptDrag.x;if(Math.abs(dx)<5&&!ptDrag.moving)return;ptDrag.moving=true;
  ptDrag.el.classList.add('dragging');ptDrag.el.style.transform=`translateX(${dx}px)`;});
function ptDropMinutes(drag){const box=drag.el.getBoundingClientRect(),mid=box.left+box.width/2;
  const others=[...drag.el.parentElement.querySelectorAll('.pt-trip[data-drag]')].filter(x=>x!==drag.el).map(x=>{const r=x.getBoundingClientRect();return {at:Number(x.dataset.at),mid:r.left+r.width/2};}).sort((a,b)=>a.mid-b.mid);
  const index=others.filter(x=>x.mid<mid).length,before=others[index-1],after=others[index];
  if((!before||before.at<=drag.at)&&(!after||after.at>=drag.at))return 0;
  let target=before&&after?Math.round((before.at+after.at)/2):before?before.at+60:after.at-60;target=Math.max(0,Math.min(23*60+55,Math.round(target/5)*5));
  if(before&&target<=before.at)target=before.at+1;if(after&&target>=after.at)target=after.at-1;return target-drag.at;}
document.addEventListener('pointerup',async()=>{const drag=ptDrag;ptDrag=null;if(!drag?.moving)return;ptJustDragged=true;setTimeout(()=>{ptJustDragged=false;},300);
  const minutes=ptDropMinutes(drag),reset=()=>{drag.el.classList.remove('dragging');drag.el.style.transform='';};
  if(!minutes){reset();return;}
  try{const result=await api('/api/order-move',{order_id:drag.id,minutes});plannerSel=drag.id;await refresh();toast(result.message);}
  catch(error){reset();toast(error.message,true);}});
// Drag a tractor (its name) up or down: the rows keep that order, for everyone.
let ptRowDrag=null;
document.addEventListener('pointerdown',event=>{const grip=event.target.closest?.('[data-row-grip]');if(!grip||event.button!==0)return;const row=grip.closest('[data-truck-row]');
  ptRowDrag={row,y:event.clientY,moved:false};row.classList.add('row-drag');grip.setPointerCapture(event.pointerId);event.preventDefault();});
document.addEventListener('pointermove',event=>{if(!ptRowDrag)return;if(Math.abs(event.clientY-ptRowDrag.y)<4&&!ptRowDrag.moved)return;ptRowDrag.moved=true;
  const rows=[...document.querySelectorAll('[data-truck-row]')].filter(r=>r!==ptRowDrag.row);
  const after=rows.find(r=>{const box=r.getBoundingClientRect();return event.clientY<box.top+box.height/2;});
  if(after)after.before(ptRowDrag.row);else rows.at(-1)?.after(ptRowDrag.row);});
document.addEventListener('pointerup',async()=>{const drag=ptRowDrag;ptRowDrag=null;if(!drag)return;drag.row.classList.remove('row-drag');if(!drag.moved)return;
  // The rows shown now (perhaps filtered) take their new order; the hidden ones keep their places around them.
  const shown=[...document.querySelectorAll('[data-truck-row]')].map(r=>Number(r.dataset.truckRow)),all=plannerRows().map(r=>r.truck.id),set=new Set(shown);let i=0;
  const ids=all.map(id=>set.has(id)?shown[i++]:id);
  try{await api('/api/truck-order',{ids});await refresh();}catch(error){toast(error.message,true);await refresh();}});
document.addEventListener('pointercancel',()=>{if(ptDrag){ptDrag.el.classList.remove('dragging');ptDrag.el.style.transform='';ptDrag=null;}});
// The click that ends a drag does not also select the card.
// Double-click a card to open the journey details.
document.addEventListener('dblclick',event=>{const el=event.target.closest?.('.pt-trip[data-id]');if(!el||event.target.closest('.pt-x'))return;event.preventDefault();orderDetail(el.dataset.id);});
document.addEventListener('click',event=>{if(ptJustDragged&&event.target.closest?.('.pt-trip')){event.stopImmediatePropagation();event.preventDefault();ptJustDragged=false;}},true);
function journeyStop(o,s,active){const point=getLocation(s.location_id),kindClass=s.kind==='Loading'?'loading-stop':['Trailer swap','Tractor change'].includes(s.kind)?'swap-stop':'unloading-stop',done=s.status==='Done';return `<div class="journey-stop ${kindClass} ${active?'active-stop':'context-stop'} ${done?'done-stop':''} ${stopIsLate(s)&&o.status!=='Cancelled'?'late-stop':''}"><div class="stop-label">${stopIcon[s.kind]} ${stopKindLabel(o,s)}${active?'<span>THIS STOP</span>':''}</div><strong>${esc(point?.city||'Unknown location')}</strong><div class="stop-company">${esc(point?.company)}</div><div class="stop-address">${esc(point?.postal_code)} · ${esc(point?.country)}</div>${s.material||s.weight_kg?`<div class="stop-material">▣ ${esc([s.material,s.weight_kg?formatKg(s.weight_kg):''].filter(Boolean).join(' · '))}</div>`:''}${s.kind==='Trailer swap'?`<div class="stop-swap">Leave ${esc(s.drop_trailer||'—')} · take ${esc(s.pick_trailer)}</div>`:''}${s.kind==='Tractor change'?`<div class="stop-swap">→ ${esc(relayText(o,s))}</div>`:''}<div class="stop-time">${done?`✓ ${s.done_at?formatDate(s.done_at):'Done'}`:(active?formatTime(s.planned_at):formatDate(s.planned_at))}${!done&&s.seq>1?'<span>estimated</span>':''}</div></div>`;}
function journeyChain(o,activeSeq){return orderStops(o).map((s,i)=>(i?`<div class="journey-connector"><span>→</span><small>${km(s.leg_km)}</small></div>`:'')+journeyStop(o,s,s.seq===activeSeq)).join('');}
function stopAction(o,s){if(o.status==='Cancelled')return '';if(s.status==='Done')return `<span class="stop-confirmed">${confirmLabel(o,s)} ${s.done_at?formatTime(s.done_at):''}</span>`;const next=nextStop(o);return next?.seq===s.seq?`<button class="btn small primary confirm-stop" data-action="confirm-stop" data-id="${o.id}" data-seq="${s.seq}">${confirmLabel(o,s)}</button>`:`<small class="muted">After stop ${next?.seq} is confirmed</small>`;}
function completedTripsOnDay(truckId,day){return state.orders.filter(order=>order.truck_id===truckId&&order.status==='Completed'&&deliveryDay(order)===day).length;}
function tripProgressBadge(order,day){const count=completedTripsOnDay(order.truck_id,day),done=count>=4,label=`${order.plate}: ${count} completed trips out of a daily target of 4 on ${formatDay(day,true)}. Counts completed orders by unloading date, across all locations.`;return `<div class="journey-progress"><span class="daily-trip-progress ${done?'target-reached':'target-pending'}" title="${esc(label)}" aria-label="${esc(label)}">${done?'✓':'◷'} ${count} / 4 trips</span></div>`;}
function journeyRow({order:o,stop:s,day}){const stops=orderStops(o),activityClass=s.kind==='Loading'?'loading-activity':['Trailer swap','Tractor change'].includes(s.kind)?'swap-activity':'unloading-activity',v=partVehicle(o,stopPart(o,s));return `<tr class="journey-row" data-order-id="${o.id}" title="Open journey details" data-stop-seq="${s.seq}" data-planning-day="${day}"><td class="vehicle-cell"><strong>${esc(v.plate)}</strong>${s.kind==='Tractor change'?`<small>→ ${esc(partVehicle(o,nextPart(o,s)||{}).plate)}</small>`:''}<small>Trailer ${esc(trailerChain(o))}</small><div class="journey-driver">${esc(v.driver_name)}</div><small>${esc(v.carrier)}</small><small>${badge(o.operation,'gray')}</small><div class="vehicle-activity"><div class="activity-label ${activityClass}">${stopIcon[s.kind]} ${stopKindLabel(o,s)} <b>${s.seq>1&&s.status!=='Done'?'≈ ':''}${formatTime(s.status==='Done'&&s.done_at?s.done_at:s.planned_at)}</b>${nextDayMark(o,s)}</div><small>Stop ${s.seq} of ${stops.length}</small></div></td><td class="journey-cell">${tripProgressBadge(o,day)}<div class="journey-stops">${journeyChain(o,s.seq)}</div></td><td class="ready-cell"><div class="ready-label">${o.status==='Cancelled'?'Cancelled order':o.status==='Completed'?'Delivered':'Planned availability'}</div><strong>${formatDate(o.status==='Completed'&&o.delivered_at?o.delivered_at:o.available_at)}</strong><small>At ${esc(getLocation(o.unloading_id)?.city)}</small>${['Planned','In transit'].includes(o.status)?'<small>Check remaining driver hours</small>':''}</td><td class="order-cell"><button class="link-button" data-action="order-detail" data-id="${o.id}">${esc(o.number)}</button><small>${orderStatusBadge(o)}</small>${stopAction(o,s)}<button class="journey-open" data-action="order-detail" data-id="${o.id}">View journey ↗</button></td></tr>`;}
