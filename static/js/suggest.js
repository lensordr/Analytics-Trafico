'use strict';
// Suggest the closest tractor (order form, client requests) and whether drivers opened their link.

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
