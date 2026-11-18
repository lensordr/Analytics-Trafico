'use strict';
// Timeline per tractor (Gantt): loaded road, loading/unloading, waits, empty km before each order and free gaps,
// over 3 or 7 days. Plus "Suggest tractor" for the order form, and whether drivers opened their link.

// ---------------------------------------------------------------- Timeline
let tlStart=null,tlDays=3;
const TL_FREE_MIN_MS=2*3600000;
function tlRange(){const day=tlStart||dayOf(state.now),start=dispatchMidnight(day);return {day,start,end:start+tlDays*86400000};}
function tlPos(ms,r){return Math.max(0,Math.min(100,(ms-r.start)/(r.end-r.start)*100));}
// One block: [from, to] in ms; kept only if it overlaps the visible range.
function tlBlock(r,from,to,cls,label,title,attrs=''){if(!(to>r.start&&from<r.end)||to<=from)return '';const l=tlPos(from,r),w=Math.max(0.35,tlPos(to,r)-l);
  return `<div class="tl-block ${cls}" data-l="${l.toFixed(3)}" data-w="${w.toFixed(3)}" title="${esc(title)}" ${attrs}><span>${esc(label)}</span></div>`;}
function tlTruckBlocks(t,r){const now=Date.now(),parts=state.orders.filter(o=>o.status!=='Cancelled').flatMap(o=>orderParts(o).filter(p=>p.truck_id===t.id).map(p=>({o,p}))).sort((a,b)=>Date.parse(a.p.start)-Date.parse(b.p.start));
  let html='',prevEnd=null,prevPlace=getLocation(t.location_id);
  for(const {o,p} of parts){const stops=partStops(o,p),start=Date.parse(p.start),end=Date.parse(p.end),open=`data-order-id="${o.id}"`;
    // Empty km before the order (the tractor that starts it).
    let emptyFrom=start;if(p.first===1){const ekm=orderEmptyKm(o),eh=o.route_details?.empty?.hours!=null?Number(o.route_details.empty.hours):ekm!=null?ekm/65:0;if(ekm&&eh>0){emptyFrom=start-eh*3600000;html+=tlBlock(r,emptyFrom,start,'empty',`${Math.round(ekm)} km empty`,`${o.number} · ${km(ekm)} empty to ${getLocation(stops[0].location_id)?.city||''}`,open);}}
    // Free gap before this part.
    if(prevEnd!=null&&emptyFrom-prevEnd>=TL_FREE_MIN_MS)html+=tlFree(r,t,prevEnd,emptyFrom,prevPlace);
    stops.forEach((s,i)=>{const place=getLocation(s.location_id),info=o.plan?.stops?.[s.seq-1],arrive=info?.arrival_at?Date.parse(info.arrival_at):Date.parse(s.planned_at),begin=Date.parse(s.done_at||s.planned_at),finish=s.done_at?begin+(Number(s.handling_hours)||0)*3600000:Date.parse(s.planned_end_at);
      if(i>0){const prev=stops[i-1],from=prev.done_at?Date.parse(prev.done_at)+(Number(prev.handling_hours)||0)*3600000:Date.parse(prev.planned_end_at);html+=tlBlock(r,from,Math.min(arrive,begin),'road',`${place?.city||''}`,`${o.number} · on the road to ${place?.city||''} (${km(s.leg_km)})`,open);
        if(begin>arrive+15*60000)html+=tlBlock(r,arrive,begin,'wait','🌙 closed',`${o.number} · waits at ${place?.company||''} until ${formatDate(s.planned_at)}`,open);}
      const kind=s.kind==='Loading'?'loading':s.kind==='Unloading'?'unloading':'swap',late=stopIsLate(s)&&o.status!=='Completed';
      html+=tlBlock(r,begin,Math.max(finish,begin+20*60000),`${kind} ${s.status==='Done'?'done':''} ${late?'late':''}`,`${stopIcon[s.kind]} ${place?.city||''}`,`${o.number} · ${stopKindLabel(o,s)} · ${place?.company||''}, ${place?.city||''} · ${s.status==='Done'?'confirmed '+formatDate(s.done_at):'planned '+formatDate(s.planned_at)}`,open);});
    prevEnd=end;const lastStop=stops[stops.length-1];prevPlace=getLocation(lastStop?.location_id)||prevPlace;}
  // Free after the last part (or the whole range when there is nothing).
  const freeFrom=prevEnd!=null?prevEnd:Math.max(r.start,now);if(r.end-Math.max(freeFrom,r.start)>=TL_FREE_MIN_MS)html+=tlFree(r,t,freeFrom,r.end,prevPlace,prevEnd==null);
  return html;}
function tlFree(r,t,from,to,place,unknown=false){const shown=Math.max(from,Date.now());if(to-shown<TL_FREE_MIN_MS)return '';
  const attrs=place?`data-action="nearby" data-id="${place.id}" data-truck="${t.id}" data-driver="${t.driver_id||''}" data-from="${esc(dispatchStamp(shown))}" data-context="${esc(`${t.plate} · free from ${formatDate(dispatchStamp(shown))}`)}"`:'';
  return tlBlock(r,shown,to,`free ${unknown?'unknown':''} ${place?'clickable':''}`,place?`free · ${place.city}`:'free · position?',place?`${t.plate} free from ${formatDate(dispatchStamp(shown))} near ${place.city}: click for next loads`:`${t.plate}: no known position`,attrs);}
function timelineView(){const r=tlRange(),days=[...Array(tlDays)].map((_,i)=>addDays(r.day,i)),now=Date.now();
  const trucks=state.trucks.filter(t=>t.active&&!t.archived_at&&(!filters.plate||t.plate.toLowerCase().includes(filters.plate.toLowerCase()))&&(!filters.operation||t.operation===filters.operation)).sort((a,b)=>{const own=t=>/malcom/i.test(state.drivers.find(d=>d.id===t.driver_id)?.carrier||'')?0:1;return own(a)-own(b)||a.plate.localeCompare(b.plate);});
  const legend=[['road','Loaded, on the road'],['loading','Loading'],['unloading','Unloading'],['swap','Swap / tractor change'],['wait','Waiting (closed)'],['empty','Empty km'],['free','Free: click for loads'],['late','Late']].map(([c,l])=>`<span class="tl-legend-item"><i class="tl-block ${c}"></i>${l}</span>`).join('');
  return heading('Timeline','Every tractor on one line: loads, empty km and free gaps. Click a block to open the journey, a grey gap to find loads.','new-order','New loading order')+`
  <section class="panel daily-date-panel"><div class="panel-head"><div><h2>${formatDay(r.day,true)} – ${formatDay(days[days.length-1],true)}</h2><p>${legend}</p></div><div class="actions"><button class="btn small" data-action="tl-move" data-step="-1">‹</button><input type="date" id="tl-date" value="${r.day}" aria-label="Timeline start"><button class="btn small" data-action="tl-move" data-step="1">›</button><button class="btn small primary" data-action="tl-today">Today</button><select id="tl-days" aria-label="Days shown"><option value="3" ${tlDays===3?'selected':''}>3 days</option><option value="7" ${tlDays===7?'selected':''}>7 days</option></select></div></div>
  <div class="filters"><div class="filter"><label for="filter-plate">Tractor registration</label><input id="filter-plate" data-filter="plate" data-suggest="plates" autocomplete="off" placeholder="All tractors" value="${esc(filters.plate)}"></div><div class="filter"><label for="filter-operation">Operation</label><select id="filter-operation" data-filter="operation"><option value="">All operations</option>${['International','National','Local'].map(v=>`<option ${filters.operation===v?'selected':''}>${v}</option>`).join('')}</select></div></div></section>
  <section class="panel tl-panel"><div class="tl-grid"><div class="tl-head"><div class="tl-label"></div><div class="tl-track">${days.map(d=>`<div class="tl-day ${d===dayOf(state.now)?'today':''}"><strong>${formatDay(d)}</strong><span>06</span><span>12</span><span>18</span></div>`).join('')}</div></div>
  ${trucks.map(t=>{const driver=state.drivers.find(d=>d.id===t.driver_id);return `<div class="tl-row"><div class="tl-label"><strong>${esc(t.plate)}</strong><small>${esc(driver?.name||'—')} · ${esc(t.trailer||'no trailer')}</small></div><div class="tl-track">${days.map(()=>'<div class="tl-cell"></div>').join('')}${tlTruckBlocks(t,r)}${now>r.start&&now<r.end?`<div class="tl-now" data-l="${tlPos(now,r).toFixed(3)}"></div>`:''}</div></div>`;}).join('')}</div></section>`;}
// Positions are set from code: the content security policy blocks inline style attributes.
function applyTimeline(){for(const el of document.querySelectorAll('.tl-track [data-l]')){el.style.left=el.dataset.l+'%';if(el.dataset.w)el.style.width=el.dataset.w+'%';}}

// ---------------------------------------------------------------- Suggest tractor (order form)
// Where each tractor is at the loading time: busy until the end of a part that covers it, else after its last part.
function tractorAt(t,at){const parts=state.orders.filter(o=>o.status!=='Cancelled').flatMap(o=>orderParts(o).filter(p=>p.truck_id===t.id).map(p=>({o,p})));
  const busy=parts.find(x=>Date.parse(x.p.start)<=at&&Date.parse(x.p.end)>at),before=parts.filter(x=>Date.parse(x.p.end)<=at).sort((a,b)=>Date.parse(b.p.end)-Date.parse(a.p.end))[0],ref=busy||before;
  const next=parts.filter(x=>Date.parse(x.p.start)>at).sort((a,b)=>Date.parse(a.p.start)-Date.parse(b.p.start))[0];
  const place=ref?getLocation(orderStops(ref.o).find(s=>s.seq===ref.p.last)?.location_id||ref.o.unloading_id):getLocation(t.location_id);
  return {place,free:ref?Date.parse(ref.p.end):null,next,driverId:ref?.p.driver_id||t.driver_id};}
function suggestTractors(loadingId,at,exclude){const target=getLocation(loadingId);if(!target||target.lat==null)return null;const now=Date.now();
  return state.trucks.filter(t=>t.active&&!t.archived_at&&t.id!==exclude).map(t=>{const a=tractorAt(t,at);if(!a.place||a.place.lat==null)return null;
    const air=pointKm(a.place,target),km=air*1.25,hours=km/65,ready=Math.max(a.free??now,now),arrive=ready+hours*3600000,driver=state.drivers.find(d=>d.id===a.driverId);
    return {t,driver,from:a.place,km,hours,ready,arrive,late:arrive>at+30*60000,next:a.next,own:/malcom/i.test(driver?.carrier||'')||!driver?.carrier};}).filter(Boolean)
    .sort((x,y)=>(x.late-y.late)||(x.late?x.arrive-y.arrive:x.km-y.km));}
function renderTractorSuggestions(form){const box=$('#tractor-suggest',form);if(!box)return;const e=form.elements,at=e.pickup_at.value?dispatchInputInstant(e.pickup_at.value):Date.now();
  if(!e.loading_id.value){box.innerHTML=notice('Choose the loading point first: the suggestion ranks the tractors by empty km to it.',true);return;}
  const list=suggestTractors(e.loading_id.value,at,0);if(!list){box.innerHTML=notice('The loading point has no coordinates yet. Save its position first (Data to complete).',true);return;}
  const shown=list.slice(0,6);box.innerHTML=`<div class="suggest-box"><div class="suggest-head"><strong>Closest tractors for ${esc(getLocation(e.loading_id.value)?.city||'')} · ${formatDate(dispatchStamp(at))}</strong><button type="button" class="link-button" data-action="close-suggest">Close</button></div>${shown.map(r=>`<div class="suggest-row ${r.late?'late':''}"><div><strong>${esc(r.t.plate)}</strong> ${r.own?'':badge('Subcontractor','amber')}<small>${esc(r.driver?.name||'no driver')} · trailer ${esc(r.t.trailer||'—')}</small></div><div><span class="suggest-km" data-suggest-km="${r.t.id}">≈ ${km(Math.round(r.km))} empty</span><small>from ${esc(r.from.city)} · free ${r.ready<=Date.now()+60000?'now':formatDate(dispatchStamp(r.ready))}</small></div><div><span class="${r.late?'late-text':''}">${r.late?'⚠ arrives ':'✓ can be there '}${formatDate(dispatchStamp(r.arrive))}</span>${r.next?`<small>next booking ${esc(r.next.o.number)} ${formatDate(r.next.p.start)}</small>`:''}</div><button type="button" class="btn small primary" data-action="use-tractor" data-id="${r.t.id}" data-driver="${r.driver?.id||''}">Use</button></div>`).join('')||'<p class="subtitle">No tractor with a known position.</p>'}<small class="subtitle">Ranked by empty km for tractors that arrive in time, then by arrival. Km are straight line × 1.25; the top 3 are being checked with HERE.</small></div>`;
  // Exact truck road km for the first three (HERE), when configured.
  const target=getLocation(e.loading_id.value);if(state.here_configured)for(const r of shown.slice(0,3))api('/api/road-km',{truck_id:r.t.id,from_lat:r.from.lat,from_lon:r.from.lon,to_lat:target.lat,to_lon:target.lon}).then(v=>{const el=box.querySelector(`[data-suggest-km="${r.t.id}"]`);if(el)el.textContent=`${km(v.distance_km)} empty · ${hours(v.driving_hours)}`;}).catch(()=>{});}

// ---------------------------------------------------------------- Driver link opened
// The order's link (or a tractor-change driver's link): when the driver opened it.
function linkOpen(token){return token?(state.link_opens||{})[token]||null:null;}
function linkStatusHtml(o,part){const token=part&&part.first>1?orderStops(o).find(s=>s.seq===part.first)?.driver_token:o.driver_token;if(!token)return '<span class="link-status none" title="The WhatsApp message with the link was not prepared yet">✉ not sent</span>';const open=linkOpen(token);return open?`<span class="link-status opened" title="Opened ${open.views}× · last ${formatDate(open.last_at)}">👁 opened ${formatDate(open.first_at)}</span>`:'<span class="link-status waiting" title="The link was prepared but the driver has not opened it yet">✉ sent · not opened</span>';}
